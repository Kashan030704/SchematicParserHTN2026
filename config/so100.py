"""Open-loop SO-100-DUPE / SG90 settings, separate from legacy Arduino config."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

# Six arm axes on channels 0..5 PLUS gripper on 6. joint_6 is intentionally
# generic; the config may rename any arm axis once the mechanics are confirmed.
ARM_JOINTS = ("base", "shoulder", "elbow", "wrist_flex", "wrist_roll", "joint_6")
JOINTS = (*ARM_JOINTS, "gripper")


class ConfigurationError(ValueError):
    pass


def number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigurationError(f"{label}: expected a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class ChannelConfig:
    channel: int
    # Pulse endpoints correspond to 0/180 degrees, before optional inversion.
    min_pulse_us: float = 1000
    max_pulse_us: float = 2000
    min_angle: float = 20
    max_angle: float = 160
    invert: bool = False
    max_step_deg: float = 1
    settle_ms: float = 250

    def __post_init__(self):
        if type(self.channel) is not int or not 0 <= self.channel <= 15:
            raise ConfigurationError("PCA9685 channel must be an integer in 0..15")
        for key in ("min_pulse_us", "max_pulse_us", "min_angle", "max_angle", "max_step_deg", "settle_ms"):
            object.__setattr__(self, key, number(getattr(self, key), key))
        if not 100 <= self.min_pulse_us < self.max_pulse_us <= 3000:
            raise ConfigurationError("Require 100 <= min_pulse_us < max_pulse_us <= 3000; verify servo endpoints")
        if not 0 <= self.min_angle < self.max_angle <= 180:
            raise ConfigurationError("Require 0 <= min_angle < max_angle <= 180")
        if not 0 < self.max_step_deg <= 180 or self.settle_ms <= 0:
            raise ConfigurationError("max_step_deg must be in (0, 180]; settle_ms must be positive")
        if type(self.invert) is not bool:
            raise ConfigurationError("invert must be boolean")

    def pulse_us(self, angle):
        angle = number(angle, "angle")
        if not self.min_angle <= angle <= self.max_angle:
            raise ConfigurationError(f"Angle {angle:g} outside limits [{self.min_angle:g}, {self.max_angle:g}]")
        fraction = (180 - angle if self.invert else angle) / 180
        return self.min_pulse_us + fraction * (self.max_pulse_us - self.min_pulse_us)


@dataclass(frozen=True)
class ArmConfig:
    i2c_bus: int = 1
    i2c_address: int = 0x40
    pwm_frequency_hz: float = 50
    reference_clock_hz: int = 25_000_000
    channels: Mapping[str, ChannelConfig] = field(default_factory=lambda: {
        j: ChannelConfig(channel=i) for i, j in enumerate(JOINTS)})
    move_step_deg: float = 1
    step_delay_ms: float = 40
    gripper_open_deg: float = 60
    gripper_closed_deg: float = 100
    home_pose: Mapping[str, float] = field(default_factory=lambda: dict.fromkeys(ARM_JOINTS, 90))
    # An OPERATOR-SUPPLIED ASSUMPTION, never measured or automatically persisted.
    startup_angles: Mapping[str, float] = field(default_factory=lambda: {
        **dict.fromkeys(ARM_JOINTS, 90), "gripper": 60})
    hardware_confirmed: bool = False

    def __post_init__(self):
        if type(self.i2c_bus) is not int or self.i2c_bus < 0:
            raise ConfigurationError("i2c_bus must be a nonnegative integer")
        if type(self.i2c_address) is not int or not 0x03 <= self.i2c_address <= 0x77:
            raise ConfigurationError("i2c_address must be an integer in 0x03..0x77")
        if number(self.pwm_frequency_hz, "pwm_frequency_hz") != 50:
            raise ConfigurationError("This SG90 configuration requires 50 Hz PWM")
        if type(self.reference_clock_hz) is not int or not 20_000_000 <= self.reference_clock_hz <= 30_000_000:
            raise ConfigurationError("reference_clock_hz must describe the internal oscillator (20..30 MHz)")
        if not isinstance(self.channels, Mapping) or len(self.channels) != 7 or "gripper" not in self.channels:
            raise ConfigurationError("channels must define six arm joints plus gripper (seven outputs)")
        channels = {}
        for joint, value in self.channels.items():
            if not isinstance(joint, str) or not joint.strip():
                raise ConfigurationError("Joint names must be nonempty strings")
            if isinstance(value, Mapping):
                try:
                    value = ChannelConfig(**value)
                except TypeError as exc:
                    raise ConfigurationError(f"channels.{joint}: {exc}") from exc
            if not isinstance(value, ChannelConfig):
                raise ConfigurationError(f"channels.{joint}: expected channel settings")
            channels[joint] = value
        if len({c.channel for c in channels.values()}) != len(channels):
            raise ConfigurationError("PCA9685 channel assignments must be unique")
        object.__setattr__(self, "channels", MappingProxyType(channels))
        if not 0 < number(self.move_step_deg, "move_step_deg") <= 180:
            raise ConfigurationError("move_step_deg must be in (0, 180]")
        if number(self.step_delay_ms, "step_delay_ms") < 1000 / self.pwm_frequency_hz:
            raise ConfigurationError("step_delay_ms must be at least one PWM period (20 ms at 50 Hz)")
        if type(self.hardware_confirmed) is not bool:
            raise ConfigurationError("hardware_confirmed must be boolean")
        object.__setattr__(self, "home_pose", MappingProxyType(self.validate_waypoint(self.home_pose, "home_pose")))
        object.__setattr__(self, "startup_angles", MappingProxyType(self.validate_angles(self.startup_angles, "startup_angles")))
        self.validate_joint("gripper", self.gripper_open_deg)
        self.validate_joint("gripper", self.gripper_closed_deg)
        if self.gripper_open_deg == self.gripper_closed_deg:
            raise ConfigurationError("gripper open and closed angles must differ")

    @property
    def arm_joints(self):
        return tuple(j for j in self.channels if j != "gripper")

    def validate_joint(self, joint, value, label="target") -> float:
        if joint not in self.channels:
            raise ConfigurationError(f"{label}: unknown joint {joint!r}")
        angle = number(value, f"{label}.{joint}")
        channel = self.channels[joint]
        if not channel.min_angle <= angle <= channel.max_angle:
            raise ConfigurationError(f"{label}.{joint}: {angle:g} outside limits "
                                     f"[{channel.min_angle:g}, {channel.max_angle:g}] degrees")
        return angle

    def validate_angles(self, angles, label="angles", *, arm_only=False):
        names = self.arm_joints if arm_only else tuple(self.channels)
        if not isinstance(angles, Mapping) or set(angles) != set(names):
            raise ConfigurationError(f"{label}: require exactly {names}; gripper has separate open_deg/closed_deg targets")
        return {j: self.validate_joint(j, angles[j], label) for j in names}

    def validate_waypoint(self, waypoint, label="waypoint") -> dict[str, float]:
        return self.validate_angles(waypoint, label, arm_only=True)

    def step_cap(self, joint):
        return min(self.move_step_deg, self.channels[joint].max_step_deg)

    def validate_slew(self, previous, following):
        self.validate_angles(following)
        for joint in self.channels:
            if abs(following[joint] - previous[joint]) > self.step_cap(joint) + 1e-9:
                raise ConfigurationError(f"{joint}: commanded step exceeds slew cap {self.step_cap(joint):g} deg")

    def with_palette(self, palette):
        return replace(self, home_pose=palette.home, gripper_open_deg=palette.gripper.open_deg,
                       gripper_closed_deg=palette.gripper.closed_deg)

    def require_hardware(self):
        if not self.hardware_confirmed:
            raise ConfigurationError("Hardware disabled: review I2C, channels, pulses, limits and startup_angles; "
                                     "then set hardware_confirmed=true in your local SO-100-DUPE config")


def load_config(path: str | Path | None = None) -> ArmConfig:
    if path is None:
        return ArmConfig()
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ConfigurationError("SO-100-DUPE config must be a JSON object")
    try:
        return ArmConfig(**data)
    except TypeError as exc:
        raise ConfigurationError(f"Invalid PWM config (old Feetech settings are not compatible): {exc}") from exc
