"""Hardware-independent commanded-angle interface. Deliberately NO feedback API."""
from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future
from typing import Literal, Mapping

from config.so100 import ArmConfig, ConfigurationError

GripperState = Literal["open", "closed"]


class ServoError(RuntimeError):
    """PWM/I2C failure, interrupted motion or invalid driver state."""


class ArmDriver(ABC):
    """Targets are degrees, not observed positions.

    Blocking moves return after the commanded ramp and settle time, NOT measured
    arrival. Nonblocking moves return a Future; overlapping moves are rejected.
    All implementations must enforce config angle/slew/delay/settle constraints.
    home preserves the jaw command; relax disables outputs and ends the session.
    """

    def __init__(self, config: ArmConfig):
        self.config = config

    @property
    @abstractmethod
    def commanded_angles(self) -> Mapping[str, float]:
        """Copy of last successfully SENT targets; initially a startup assumption."""

    @abstractmethod
    def move_to(self, waypoint: Mapping[str, float], *, blocking=True):
        pass

    @abstractmethod
    def set_gripper(self, state: GripperState):
        pass

    @abstractmethod
    def home(self):
        pass

    @abstractmethod
    def relax(self):
        pass

    def close(self):
        """Release transport resources after relax."""

    def tune_gripper(self, angle_deg):
        """Optional calibration extension, still slew-limited; not used by executor."""
        raise NotImplementedError("This driver does not support gripper tuning")


class SlewDriver(ArmDriver):
    """Shared ramp policy for real PWM and simulation; transport lives in subclasses."""

    def __init__(self, config: ArmConfig, *, sleep=None, event_sink=None):
        super().__init__(config)
        self.event_sink = event_sink
        self._commanded = dict(config.startup_angles)
        self._motion_lock = threading.Lock()
        self._cancel = threading.Event()
        self._sleep = sleep or self._cancel.wait
        self._energized = False
        self._relaxed = False
        self._faulted = False

    @property
    def commanded_angles(self):
        return dict(self._commanded)

    def _event(self, command, **data):
        if self.event_sink is not None:
            self.event_sink({"command": command, **data})

    @abstractmethod
    def _write_channel(self, joint, angle):
        pass

    @abstractmethod
    def _disable_channel(self, joint):
        pass

    def _pause(self, seconds):
        if self._cancel.is_set():
            raise ServoError("Motion cancelled by relax")
        self._sleep(seconds)
        if self._cancel.is_set():
            raise ServoError("Motion cancelled by relax")

    def _write_frame(self, angles):
        self.config.validate_slew(self._commanded, angles)
        # All seven channels are written on every step, including unchanged axes.
        for joint in sorted(self.config.channels, key=lambda j: self.config.channels[j].channel):
            if self._cancel.is_set():
                raise ServoError("Motion cancelled by relax")
            try:
                self._write_channel(joint, angles[joint])
            except BaseException:
                # An I2C error might occur AFTER a PWM register was accepted.
                # Do not attempt a recovery ramp from an uncertain command state.
                self._faulted = True
                raise
            self._commanded[joint] = angles[joint]

    def _ramp(self, targets):
        start = self.commanded_angles
        end = {**start, **targets}
        self.config.validate_angles(end)
        self._event("move_to", target_angles=dict(targets))
        if not self._energized:
            self._event("startup_assumption", commanded_angles=start, measured=False)
            self._write_frame(start)
            self._energized = True
            self._pause(self.config.step_delay_ms / 1000)
        count = max(1, math.ceil(max(abs(end[j] - start[j]) / self.config.step_cap(j)
                                     for j in self.config.channels)))
        for index in range(1, count + 1):
            angles = {j: (end[j] if index == count else start[j] + (end[j] - start[j]) * index / count)
                      for j in end}
            self._write_frame(angles)
            self._event("step", index=index, total=count, commanded_angles=self.commanded_angles)
            self._pause(self.config.step_delay_ms / 1000)
        settle_ms = max(self.config.channels[j].settle_ms for j in targets)
        self._event("settle", milliseconds=settle_ms)
        self._pause(settle_ms / 1000)
        self._event("move_complete", commanded_angles=self.commanded_angles, measured=False)

    def _move(self, targets, *, blocking=True):
        if type(blocking) is not bool:
            raise ConfigurationError("blocking must be boolean")
        for joint, angle in targets.items():
            self.config.validate_joint(joint, angle)
        if not self._motion_lock.acquire(blocking=False):
            raise ServoError("Arm is busy; wait for the previous move before commanding another")
        try:
            if self._relaxed:
                raise ServoError("Driver is relaxed; start a new session and confirm startup pose again")
            if self._faulted:
                raise ServoError("PWM command state uncertain after I2C failure; recovery motion refused; relax required")
        except BaseException:
            self._motion_lock.release()
            raise

        def perform():
            try:
                self._ramp(targets)
            finally:
                self._motion_lock.release()

        if blocking:
            perform()
            return None
        future = Future()

        def worker():
            if not future.set_running_or_notify_cancel():
                self._motion_lock.release()
                return
            try:
                perform()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(None)

        try:
            threading.Thread(target=worker, name="arm-pwm-ramp", daemon=True).start()
        except BaseException:
            self._motion_lock.release()
            raise
        return future

    def move_to(self, waypoint, *, blocking=True):
        return self._move(self.config.validate_waypoint(waypoint), blocking=blocking)

    def set_gripper(self, state):
        if state not in ("open", "closed"):
            raise ConfigurationError(f"Unknown gripper state {state!r}")
        target = self.config.gripper_open_deg if state == "open" else self.config.gripper_closed_deg
        self._event("set_gripper", state=state, target_deg=target)
        self._move({"gripper": target})

    def tune_gripper(self, angle_deg):
        self._move({"gripper": self.config.validate_joint("gripper", angle_deg)})

    def home(self):
        self._event("home")
        return self.move_to(self.config.home_pose)

    def relax(self):
        # Cancel and join any active ramp before disabling outputs: no worker can
        # write PWM again after relax returns. Stopping Python writes alone is NOT off.
        self._cancel.set()
        failures = []
        with self._motion_lock:
            self._relaxed = True
            for joint in self.config.channels:
                try:
                    self._disable_channel(joint)
                except BaseException as exc:
                    failures.append(f"{joint}: {exc}")
            self._event("relax")
        if failures:
            raise ServoError("Could not disable all PWM outputs: " + "; ".join(failures))
