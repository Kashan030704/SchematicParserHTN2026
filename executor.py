"""Sequential camera-free pick/place, with a driver-independent safety policy."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable

from arm.driver import ArmDriver, ServoError
from planner import PickPlan, PickStep

LOG = logging.getLogger(__name__)


def grasp_verify(driver: ArmDriver, step: PickStep):
    """No-op, NOT grip confirmation. Future sensor hook runs BEFORE closing.

    An implementation should raise (or return False) to prevent the close/lift.
    """


@contextmanager
def managed_arm(driver: ArmDriver):
    """Best-effort home, then ALWAYS relax and close, including Ctrl-C.

    Preserve the original failure if recovery also fails. No software can
    promise physical arrival or successful PWM disable on a failed I2C bus.
    """
    primary = None
    try:
        yield driver
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = []
        for label, action in (("home", driver.home), ("relax", driver.relax), ("close", driver.close)):
            try:
                if label == "home":
                    driver.config.validate_waypoint(driver.config.home_pose, "cleanup.home")
                action()
            except BaseException as exc:
                failures.append(f"{label}: {exc}")
        if failures:
            message = "Arm cleanup failed: " + "; ".join(failures)
            if primary is not None:
                LOG.error(message)
            else:
                raise ServoError(message)


def validate_step(step: PickStep, driver: ArmDriver):
    for point in (step.approach_angles, step.grasp_angles, step.place_target.approach, step.place_target.drop):
        driver.config.validate_waypoint(point, step.slot_id)
    driver.config.validate_waypoint(driver.config.home_pose, "home")
    driver.config.validate_joint("gripper", driver.config.gripper_open_deg)
    driver.config.validate_joint("gripper", driver.config.gripper_closed_deg)


def execute_pick(step: PickStep, driver: ArmDriver, *, verify=None):
    """One cycle within an already owned session; the caller owns final cleanup."""
    validate_step(step, driver)

    def move(point):
        driver.move_to(driver.config.validate_waypoint(point), blocking=True)

    driver.home()
    driver.set_gripper("open")
    move(step.approach_angles)
    move(step.grasp_angles)
    if (verify or grasp_verify)(driver, step) is False:
        raise ServoError(f"Grasp verification rejected {step.component_id} in {step.slot_id}")
    driver.set_gripper("closed")
    move(step.approach_angles)
    move(step.place_target.approach)
    move(step.place_target.drop)
    driver.set_gripper("open")
    driver.home()


def run_plan(plan: PickPlan, driver: ArmDriver, *,
             verify: Callable[[ArmDriver, PickStep], None] | None = None,
             on_progress: Callable[[str, int, PickStep], None] | None = None):
    with managed_arm(driver):
        for step in plan.steps:
            validate_step(step, driver)
        for index, step in enumerate(plan.steps):
            if on_progress is not None:
                on_progress("starting", index, step)
            LOG.info("Picking %s from %s", step.component_id, step.slot_id)
            execute_pick(step, driver, verify=verify)
            if on_progress is not None:
                on_progress("completed", index, step)
