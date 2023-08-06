import enum
import logging
from collections import defaultdict
from typing import List, Optional, Union, Any
import datetime

import click
import crc

from miio.click_common import command, format_output
from miio.device import Device, DeviceStatus
from miio.exceptions import DeviceException
from miio.integrations.genericmiot.genericmiot import GenericMiot
from miio.miot_models import DeviceModel

_LOGGER = logging.getLogger(__name__)

MODEL_WY3 = "chunmi.cooker.wy3"

COOKING_FAULTS = {
    0: "No Faults",
    1: "E02",
    2: "E03",
    3: "E04",
    4: "E05",
    5: "E06",
    6: "E07",
    7: "E08",
    8: "E09",
    9: "E10",
    10: "E11",
    11: "E12",
}

class CookerException(DeviceException):
    pass

class OperationMode(enum.Enum): # Updated from the mapping
    Idle = 1
    Running = 2
    Scheduled = 3
    AutoKeepWarm = 4
    Error = 5
    Updating = 6
    Finish = 7

    Unknown = "unknown"

    @classmethod
    def _missing_(cls, _):
        return OperationMode.Unknown

class TemperatureHistory(DeviceStatus):
    def __init__(self, data: str) -> None:
        """Container of temperatures recorded every 10-15 seconds while cooking.

        Example values:

        Status waiting:
        0

        2 minutes:
        161515161c242a3031302f2eaa2f2f2e2f

        12 minutes:
        161515161c242a3031302f2eaa2f2f2e2f2e302f2e2d302f2f2e2f2f2f2f343a3f3f3d3e3c3d3c3f3d3d3d3f3d3d3d3d3e3d3e3c3f3f3d3e3d3e3e3d3f3d3c3e3d3d3e3d3f3e3d3f3e3d3c

        32 minutes:
        161515161c242a3031302f2eaa2f2f2e2f2e302f2e2d302f2f2e2f2f2f2f343a3f3f3d3e3c3d3c3f3d3d3d3f3d3d3d3d3e3d3e3c3f3f3d3e3d3e3e3d3f3d3c3e3d3d3e3d3f3e3d3f3e3d3c3f3e3d3c3f3e3d3c3f3f3d3d3e3d3d3f3f3d3d3f3f3e3d3d3d3e3e3d3daa3f3f3f3f3f414446474a4e53575e5c5c5b59585755555353545454555554555555565656575757575858585859595b5b5c5c5c5c5d5daa5d5e5f5f606061

        55 minutes:
        161515161c242a3031302f2eaa2f2f2e2f2e302f2e2d302f2f2e2f2f2f2f343a3f3f3d3e3c3d3c3f3d3d3d3f3d3d3d3d3e3d3e3c3f3f3d3e3d3e3e3d3f3d3c3e3d3d3e3d3f3e3d3f3e3d3c3f3e3d3c3f3e3d3c3f3f3d3d3e3d3d3f3f3d3d3f3f3e3d3d3d3e3e3d3daa3f3f3f3f3f414446474a4e53575e5c5c5b59585755555353545454555554555555565656575757575858585859595b5b5c5c5c5c5d5daa5d5e5f5f60606161616162626263636363646464646464646464646464646464646464646364646464646464646464646464646464646464646464646464646464aa5a59585756555554545453535352525252525151515151

        Data structure:

        Octet 1 (16): First temperature measurement in hex (22 °C)
        Octet 2 (15): Second temperature measurement in hex (21 °C)
        Octet 3 (15): Third temperature measurement in hex (21 °C)
        ...
        """
        if data == "0000":
            self.data = []
        elif not len(data) % 2:
            self.data = [int(data[i : i + 2], 16) for i in range(0, len(data), 2)]
        else:
            self.data = []

    @property
    def temperatures(self) -> List[int]:
        return self.data

    @property
    def raw(self) -> str:
        return "".join([f"{value:02x}" for value in self.data])

    def __str__(self) -> str:
        return str(self.data)

class Wy3CookerProfile:
    """This class can be used to modify and validate an existing cooking profile."""

    def __init__(self, profile_hex: str, duration: Optional[int] = None, schedule: Optional[int] = None, auto_keep_warm: Optional[bool] = None, taste: Optional[int] = None) -> None:
        if len(profile_hex) < 5:
            raise CookerException("Invalid profile")
        else:
            self.checksum = bytearray.fromhex(profile_hex)[-2:]
            self.profile_bytes = bytearray.fromhex(profile_hex)[:-2]

            if not self.is_valid():
                raise CookerException("Profile checksum error")

            if duration is not None:
                self.set_duration(duration)
            if schedule is not None and schedule > self.get_duration() and schedule <= 1440:
                self.set_schedule_duration(schedule)
            if auto_keep_warm is True:
                self.set_auto_keep_warm(auto_keep_warm)
            if taste is not None:
                self.set_taste(taste)

    # Generic other stuff
    def get_device_type(self):
        return self.profile_bytes[0]

    def get_small_type(self):
        return self.profile_bytes[1]

    def get_index(self):
        return self.profile_bytes[2]

    def set_index(self, value):
        self.profile_bytes[2] = value

    def get_menu_id(self):
        cID = ""
        for i in range(3, 7):
            iid = hex(self.profile_bytes[i])[2:]
            if len(iid) < 2:
                iid = "0" + iid
            cID += iid
        return int(cID, 16)

    def get_cook_time_max(self):
        return self.profile_bytes[10] * 60 + self.profile_bytes[11]

    def get_cook_time_min(self):
        return self.profile_bytes[12] * 60 + self.profile_bytes[13]

    # Check capabilities
    def is_can_schedule(self):
        return (self.profile_bytes[7] & 0x40) != 0

    def is_can_set_auto_keep_warm(self):
        return (self.profile_bytes[7] & 0x20) != 0

    def is_can_set_duration(self):
        return self.get_cook_time_max() > self.get_cook_time_min()

    def is_can_choose_rice(self):
        return self.get_menu_id() in [2, 1]

    def is_can_config_taste(self):
        return self.get_menu_id() == 1

    # Cooking duration
    def get_duration(self):
        """Get the duration in minutes."""
        return (self.profile_bytes[8] * 60) + self.profile_bytes[9]

    def set_duration(self, minutes):
        """Set the duration in minutes if the profile allows it."""
        if not self.is_can_set_duration():
            return

        max_minutes = self.get_cook_time_max()
        min_minutes = self.get_cook_time_min()

        minutes = max(min_minutes, min(max_minutes, minutes))

        self.profile_bytes[8] = (minutes // 60) & 0xFF
        self.profile_bytes[9] = minutes % 60

    # Schedule cooking
    def get_schedule_enabled(self):
        return (self.profile_bytes[14] & 0x80) == 0x80

    def set_schedule_enabled(self, enabled):
        if enabled:
            self.profile_bytes[14] |= 0x80
        else:
            self.profile_bytes[14] &= 0x7F

    def get_schedule_duration(self):
        return (self.profile_bytes[14] & 0x7F) * 60 + (self.profile_bytes[15] & 0x7F)

    def set_schedule_duration(self, duration):
        """Set the schedule time (delay before cooking) in minutes."""

        if not self.is_can_schedule():
            return

        self.profile_bytes[14] = (self.profile_bytes[14] & 0x80) | ((duration // 60) & 0xFF)
        self.profile_bytes[15] = (duration % 60 | self.profile_bytes[15] & 0x80) & 0xFF

        self.set_schedule_enabled(True)

    # Auto keep warm
    def get_auto_keep_warm(self):
        return (self.profile_bytes[15] & 0x80) == 0x80

    def set_auto_keep_warm(self, enabled):

        if not self.is_can_set_auto_keep_warm():
            return

        if enabled is True:
            self.profile_bytes[15] |= 0x80
        else:
            self.profile_bytes[15] &= 0x7F

    # Rice type
    def get_rice_id(self):
        riceId = (self.profile_bytes[17] << 8) + self.profile_bytes[18]
        return riceId

    def set_rice_id(self, riceId):
        """Does not seem to have an effect"""

        if not self.is_can_choose_rice():
            return

        if riceId > int("ffff", 16):
            riceId = 0
        self.profile_bytes[17] = riceId >> 8
        self.profile_bytes[18] = riceId & 0xff

    # Taste
    def get_taste(self):
        return self.profile_bytes[19]

    def set_taste(self, tasteIndex):
        """0 -> Soft, 1 -> Moderate, 2 -> Rigid"""

        if not self.is_can_config_taste():
            return

        if not tasteIndex in {0, 1, 2}:
            raise CookerException(f"Taste must be in [0, 1, 2] from soft to hard. {tasteIndex} was provided.")

        self.profile_bytes[19] = tasteIndex

    # Checksum
    def calc_checksum(self):

        calculator = crc.Calculator(crc.Crc16.CCITT)
        _crc = calculator.checksum(self.profile_bytes)
        checksum = bytearray(2)
        checksum[0] = (_crc >> 8) & 0xFF
        checksum[1] = _crc & 0xFF

        config = crc.Configuration(
            width=16,
            polynomial=0x1021,
            init_value=0x0000,
            final_xor_value=0x0000,
            reverse_input=False,
            reverse_output=False,
        )
        verifier = crc.Calculator(config)

        assert verifier.checksum(self.profile_bytes + checksum) == 0

        return checksum

    def update_checksum(self):
        self.checksum = self.calc_checksum()

    def is_valid(self):
        return self.checksum == self.calc_checksum()

    def get_profile_hex(self):
        self.update_checksum()
        return (self.profile_bytes + self.checksum).hex()

    def get_recipe_attributes(self):
        """Return a dict of attributes that show the current state of the profile."""
        return {
            "menu_id": self.get_menu_id(),
            "is_can_schedule": self.is_can_schedule(),
            "is_can_set_auto_keep_warm": self.is_can_set_auto_keep_warm(),
            "is_can_set_duration": self.is_can_set_duration(),
            "is_can_choose_rice": self.is_can_choose_rice(),
            "is_can_config_taste": self.is_can_config_taste(),
            "cook_time_min": self.get_cook_time_min(),
            "cook_time_max": self.get_cook_time_max(),
            "duration": self.get_duration(),
            "schedule_enabled": self.get_schedule_enabled(),
            "schedule_duration": self.get_schedule_duration(),
            "auto_keep_warm": self.get_auto_keep_warm(),
            "rice_id": self.get_rice_id(),
            "taste": self.get_taste(),
        }

class CookerStatus(DeviceStatus):
    def __init__(self, data) -> None:
        self.data = data

    @property
    def mode(self) -> OperationMode:
        """Current operation mode."""
        return OperationMode(self.data["status"])

    @property
    def fault(self) -> str:
        """Current fault code."""
        return COOKING_FAULTS[self.data["fault"]]

    @property
    def menu(self) -> str:
        """Selected menu id."""
        try:
            return int(self.data["menu-id"])
        except KeyError:
            return "Unknown menu"

    @property
    def remaining(self) -> int:
        """Remaining minutes of the cooking process."""
        return int(int(self.data["left-time"]) / 60)

    @property
    def cooking_delayed(self) -> Optional[int]:
        """Wait n minutes before cooking / scheduled cooking."""
        delay = int(int(self.data["pre-left-time"]) / 60)

        if delay >= 0:
            return delay

        return None

    @property
    def duration(self) -> int:
        """Duration of the cooking process."""
        return int(self.data["cook-total-time"])

    @property
    def keep_warm(self) -> bool:
        """Keep warm after cooking?"""
        return self.data["auto-keepwarm-flag"] == 1

    @property
    def keep_warm_duration(self) -> int:
        """How long to keep warm after cooking?"""
        return self.data["keepwarm-time"]

    @property
    def settings(self) -> None:
        """Settings of the cooker."""
        return None

    @property
    def hardware_version(self) -> None:
        """Hardware version."""
        return None

    @property
    def firmware_version(self) -> None:
        """Firmware version."""
        return None

    @property
    def taste(self) -> int:
        """Taste id."""
        return self.data["taste"]

    @property
    def rice(self) -> int:
        """Rice id."""
        return self.data["rice-type"]

    @property
    def temperature(self) -> Optional[int]:
        """Temperature."""
        return self.data["temperature"]

class GenericMiotLocal(GenericMiot):

    def initialize_model(self, device_model: DeviceModel):
        """Initialize the miot model and create descriptions."""
        if self._miot_model is not None:
            return

        self._miot_model = device_model
        self._create_descriptors()

class CookerWY3(GenericMiotLocal):
    """Main class representing the wy3 cooker."""

    _supported_models = [MODEL_WY3]

    @command(
        default_output=format_output(
            "",
            "Mode: {result.mode}\n"
            "Fault: {result.fault}\n"
            "Menu: {result.menu}\n"
            "Remaining: {result.remaining}\n"
            "Cooking delayed: {result.cooking_delayed}\n"
            "Duration: {result.duration}\n"
            "Keep warm: {result.keep_warm}\n"
            "Rice: {result.rice}\n"
            "Taste: {result.taste}\n"
        )
    )
    def status(self) -> CookerStatus:
        """Retrieve properties."""

        properties_with_values = {x['did']:x['value'] for x in self.get_properties_for_mapping() if x['code'] == 0}

        temperature_history = self.get_temperature_history().temperatures

        last_temperature = temperature_history[-1] if len(temperature_history) > 0 else None

        properties_with_values["temperature"] = last_temperature

        return CookerStatus(defaultdict(lambda: None, properties_with_values))

    @command(
        click.argument("profile", type=str, required=True),
        click.option("--duration", type=Any, required=False),
        click.option("--schedule", type=Any, required=False),
        click.option("--auto_keep_warm", type=bool, required=False),
        click.option("--taste", type=int, required=False),
        default_output=format_output("Cooking profile started"),
    )
    def start(self, profile: str, duration: Optional[Union[int, datetime.timedelta]] = None, schedule: Optional[Union[int, datetime.datetime]] = None, auto_keep_warm: Optional[bool] = None, taste: Optional[int] = None):
        """Start cooking a profile."""

        if duration is not None and isinstance(duration, datetime.timedelta):
            duration = int(duration.total_seconds() / 60)

        if schedule is not None and isinstance(schedule, datetime.datetime):
            # Calculate time in minutes from now until the scheduled time
            # Make sure the time is passed with a timezone from the frontend
            # e.g. {{ states('input_datetime.rice_cooker_schedule_time') | as_datetime | as_local }}

            schedule = max(int((schedule - datetime.datetime.utcnow().astimezone(datetime.timezone.utc)).total_seconds() / 60),0)
            schedule = schedule if schedule > 0 else None

        self._start(profile, duration, schedule, auto_keep_warm, taste)

    @command(
        click.argument("profile", type=str, required=True),
        click.option("--duration", type=int, required=False),
        click.option("--schedule", type=int, required=False),
        click.option("--auto_keep_warm", type=bool, required=False),
        click.option("--taste", type=int, required=False),
        default_output=format_output("Cooking profile started"),
    )
    def _start(self, profile: str, duration: Optional[int] = None, schedule: Optional[int] = None, auto_keep_warm: Optional[bool] = None, taste: Optional[int] = None):
        """Start cooking a profile."""
        cookerProfile = Wy3CookerProfile(profile, duration, schedule, auto_keep_warm, taste)

        # PIID 10 is the cooking-data property
        response = self.call_action("custom:cooking-start", [{'piid': 10, 'value': cookerProfile.get_profile_hex()}])

        if response.get('code') != 0:
            raise CookerException("Failed to start cooking")

    @command(default_output=format_output("Cooking stopped"))
    def stop(self):
        """Stop cooking."""
        response = self.call_action("cooker:cancel-cooking")

        if response.get('code') != 0:
            _LOGGER.warning("Failed to stop cooking")

    @command(default_output=format_output("", "Temperature history: {result}\n"))
    def get_temperature_history(self) -> TemperatureHistory:
        """Retrieves a temperature history.

        The temperature is only available while cooking. Approx. six data points per
        minute.
        """
        response = self.call_action("custom:get-temp-history")

        if response.get('code') != 0:
            raise CookerException("Failed to get temperature history")

        temperature_history = TemperatureHistory(response.get('out')[0].get('value'))

        return temperature_history
