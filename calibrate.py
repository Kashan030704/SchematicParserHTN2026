"""Interactive TUNE-and-store of SG90 target angles. Nothing is read from servos."""
from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from arm.driver import ArmDriver
from config.so100 import ConfigurationError, number
from executor import managed_arm
from palette import Palette


def calibrate(driver: ArmDriver, template: Palette, output: str | Path = "palette.yaml", *,
              prompt=input, emit=print, nudge_deg=1.0) -> Palette:
    nudge_deg = number(nudge_deg, "nudge_deg")
    if nudge_deg <= 0:
        raise ConfigurationError("nudge_deg must be positive")
    output = Path(output)
    temporary = None
    # Until HOME is tuned, the only recovery target is the confirmed startup
    # assumption, NOT the dummy template's home. No automatic template replay.
    initial_home = {j: driver.commanded_angles[j] for j in driver.config.arm_joints}
    driver.config = replace(driver.config, home_pose=initial_home)
    with managed_arm(driver):
        emit("LIVE TUNING: keep the bench clear and power cutoff ready. Values are COMMANDS, never measurements.")
        emit("For each joint: + / - nudge, +N / -N relative degrees, =N absolute target, save accepts, quit cancels.")
        data = template.to_dict()
        data["calibrated"] = True

        def tune_joint(joint, label):
            while True:
                current = driver.commanded_angles[joint]
                command = prompt(f"{label} / {joint}: commanded {current:g} deg [+/-/=N/save/quit]: ").strip().lower()
                if command in ("save", "s"):
                    return current
                if command in ("quit", "q"):
                    raise KeyboardInterrupt("Calibration cancelled")
                try:
                    if command in ("+", "-"):
                        target = current + (nudge_deg if command == "+" else -nudge_deg)
                    elif command.startswith("="):
                        target = number(float(command[1:]), joint)
                    elif command.startswith(("+", "-")) and len(command) > 1:
                        target = current + number(float(command), joint)
                    else:
                        raise ConfigurationError("Use +, -, +N, -N, =N, save or quit")
                    driver.config.validate_joint(joint, target, label)
                except ValueError as exc:
                    # Invalid jogs are loud but let the operator try a safe value.
                    emit(f"REJECTED (no move): {exc}")
                    continue
                if joint == "gripper":
                    driver.tune_gripper(target)
                else:
                    waypoint = {j: driver.commanded_angles[j] for j in driver.config.arm_joints}
                    waypoint[joint] = target
                    driver.move_to(waypoint, blocking=True)

        def tune_pose(label):
            emit(f"Tune {label} from CURRENT commanded angles; template angles are not executed.")
            for joint in driver.config.arm_joints:
                tune_joint(joint, label)
            result = {j: driver.commanded_angles[j] for j in driver.config.arm_joints}
            emit(f"Saved {label} target angles in memory: {result}")
            return result

        data["home"] = tune_pose("HOME / supported rest pose")
        driver.config = replace(driver.config, home_pose=data["home"])
        data["place_target"]["approach"] = tune_pose("PLACE APPROACH")
        data["place_target"]["drop"] = tune_pose("PLACE DROP")
        for slot in data["slots"]:
            slot["approach"] = tune_pose(f"{slot['id']} ({slot['label']}) APPROACH")
            slot["grasp"] = tune_pose(f"{slot['id']} ({slot['label']}) GRASP")
        emit("Returning to the newly tuned home before tuning jaw targets.")
        driver.home()
        data["gripper"]["open_deg"] = tune_joint("gripper", "GRIPPER OPEN")
        data["gripper"]["closed_deg"] = tune_joint("gripper", "GRIPPER CLOSED")
        palette = Palette.from_dict(data, driver.config)
        if prompt(f"Type SAVE to atomically write all targets to {output}, or anything else to cancel: ").strip() != "SAVE":
            raise KeyboardInterrupt("Calibration not saved")
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                             prefix=f".{output.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                yaml.safe_dump(palette.to_dict(), stream, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        emit(f"Saved {len(palette.slots)} tuned slots to {output}; no physical position or grip was measured.")
        return palette
