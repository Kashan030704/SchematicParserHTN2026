"""Visual executor tests: convergence, no blind motion, no plan-level replanning."""
import copy
import threading
import time
from pathlib import Path

import pytest

from actuator.approach import VisualApproach, ApproachError
from actuator.motion import Robot
from actuator.perception import DryRunPerception
from actuator.server import RobotNode, create_app
from config import load_poses, validate_visual_approach

ROOT = Path(__file__).resolve().parents[1]


def robot():
    return Robot(load_poses(ROOT / "poses.yaml"), dry_run=True, emit=lambda text: None)


def observation(seq, *, tag=0, u=320, v=180, side=80, age=0):
    return {"frame": {"seq": seq, "width": 640, "height": 360,
                     "captured_monotonic": time.monotonic() - age},
            "tags": [{"id": tag, "center_px": [u, v], "side_px": side,
                      "corners_px": [[u-side/2, v-side/2], [u+side/2, v-side/2],
                                     [u+side/2, v+side/2], [u-side/2, v+side/2]]}]}


class Frames:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.calls = 0
    def detect_once(self):
        self.calls += 1
        return next(self.frames)


def test_converges_on_requested_tag_and_only_grips_after_stable_frames():
    r = robot()
    perception = DryRunPerception({0: "R3", 2: "LED_RX"}, r)
    executor = VisualApproach(r, perception)
    report = executor.pick("LED_RX", 2)
    assert report["tag_id"] == 2 and report["aligned"]
    assert report["stable_frames"] == 3
    assert report["steps"] == 6
    close = r.commands.index("robotic_gripper close 1;")
    assert sum(c.startswith("chassis move") for c in r.commands[:close]) == 6
    assert sum(c.startswith("chassis move") for c in r.commands[close:]) == 6
    assert sum(m["x_m"] for m in r.chassis_moves) == pytest.approx(0)
    assert sum(m["y_m"] for m in r.chassis_moves) == pytest.approx(0)


def test_consecutive_frames_and_debounce_reset():
    r = robot()
    frames = Frames([observation(1), observation(2), observation(3, u=336),
                     observation(4), observation(5), observation(6)])
    report = VisualApproach(r, frames).align(0)
    assert report["steps"] == 1 and frames.calls == 6


@pytest.mark.parametrize("kind", ["lost", "wrong_id", "duplicate", "stale", "future", "repeat",
                                  "resolution", "too_small", "too_close", "edge", "nan",
                                  "vertical", "geometry", "skew"])
def test_invalid_visual_input_stops_without_grip(kind):
    r = robot()
    bad = observation(2)
    if kind == "lost": bad["tags"] = []
    if kind == "wrong_id": bad["tags"][0]["id"] = 2
    if kind == "duplicate": bad["tags"] *= 2
    if kind == "stale": bad = observation(2, age=3)
    if kind == "future": bad = observation(2, age=-5)
    if kind == "repeat": bad["frame"]["seq"] = 1
    if kind == "resolution": bad["frame"]["width"] = 1280
    if kind == "too_small": bad = observation(2, side=5)
    if kind == "too_close": bad = observation(2, side=140)
    if kind == "edge": bad = observation(2, u=30)
    if kind == "nan": bad["tags"][0]["center_px"][0] = float("nan")
    if kind == "vertical": bad = observation(2, v=240)
    if kind == "geometry": bad["tags"][0]["side_px"] = 100
    if kind == "skew":
        bad["tags"][0]["corners_px"] = [[270, 165], [370, 165], [370, 195], [270, 195]]
        bad["tags"][0]["side_px"] = 65
    with pytest.raises((ApproachError, ValueError)):
        VisualApproach(r, Frames([observation(1), bad])).align(0)
    assert r.stopped.is_set()
    assert r.commands == ["quit;"]


def test_no_improvement_halts_without_grip_or_blind_retreat():
    r = robot()
    frames = Frames([observation(i, u=380) for i in range(1, 10)])
    with pytest.raises(ApproachError, match="not improving"):
        VisualApproach(r, frames).align(0)
    assert len(r.chassis_moves) == 3
    assert all(m["y_m"] > 0 for m in r.chassis_moves)
    assert not any("gripper" in command for command in r.commands)


@pytest.mark.parametrize("budget", ["steps", "travel"])
def test_budget_is_enforced_before_extra_motion(budget):
    r = robot()
    cfg = r.poses["visual_approach"]
    cfg["max_steps"] = 1 if budget == "steps" else 40
    cfg["max_travel_m"] = 0.01 if budget == "travel" else 0.3
    with pytest.raises(ApproachError, match="budget"):
        VisualApproach(r, Frames([observation(1, u=380), observation(2, u=372)])).align(0)
    assert len(r.chassis_moves) == 1


def test_tag_loss_after_moving_does_not_grip():
    r = robot()
    frames = Frames([observation(1, u=380), observation(2, tag=9)])
    with pytest.raises(ApproachError, match="lost"):
        VisualApproach(r, frames).align(0)
    assert len(r.chassis_moves) == 1
    assert r.commands[-1] == "quit;"


def test_too_large_but_within_safety_guard_reverses_forward_step():
    r = robot()
    frames = Frames([observation(1, side=92), observation(2), observation(3), observation(4)])
    VisualApproach(r, frames).align(0)
    assert r.chassis_moves == [{"x_m": -0.01, "y_m": 0.0, "z_deg": 0}]


def test_coarse_route_and_corrections_retraced_before_belt():
    r = robot()
    r.poses["coarse_routes"][0] = [{"x_m": 0.02, "y_m": 0.01, "z_deg": 0}]
    approach = VisualApproach(r, DryRunPerception({0: "R3"}, r))
    approach.pick("R3", 0)
    assert r.chassis_moves[0] == {"x_m": 0.02, "y_m": 0.01, "z_deg": 0}
    assert r.chassis_moves[-1] == {"x_m": -0.02, "y_m": -0.01, "z_deg": 0}
    assert sum(m["x_m"] for m in r.chassis_moves) == pytest.approx(0)
    assert sum(m["y_m"] for m in r.chassis_moves) == pytest.approx(0)


def test_hardware_requires_teaching_not_only_arm_calibration():
    poses = load_poses(ROOT / "poses.yaml")
    poses["calibrated"] = True
    with pytest.raises(ValueError, match="Teach"):
        validate_visual_approach(poses, hardware=True)


def test_coarse_leg_can_bring_distant_tag_into_servo_range():
    r = robot()
    r.poses["coarse_routes"][0] = [{"x_m": 0.1, "y_m": 0, "z_deg": 0}]
    frames = Frames([observation(1, side=8), observation(2), observation(3), observation(4)])
    result = VisualApproach(r, frames).pick("R3", 0)
    assert result["aligned"] and result["steps"] == 0
    assert r.chassis_moves == [{"x_m": 0.1, "y_m": 0, "z_deg": 0},
                                {"x_m": -0.1, "y_m": 0, "z_deg": 0}]


@pytest.mark.parametrize("change", [
    {"forward_sign": 0}, {"lateral_sign": True}, {"max_steps": 0},
    {"stable_frames": 1}, {"target_side_px": float("nan")}, {"speed_mps": 1},
    {"max_travel_m": 2}, {"timeout_s": 1000}
])
def test_bad_visual_config_rejected(change):
    r = robot()
    r.poses["visual_approach"].update(change)
    with pytest.raises(ValueError):
        VisualApproach(r, Frames([]))


def test_coarse_yaw_disallowed_no_mistaken_inverse_rotation():
    r = robot()
    r.poses["coarse_routes"][0] = [{"x_m": 0, "y_m": 0, "z_deg": 90}]
    with pytest.raises(ValueError, match="heading"):
        VisualApproach(r, Frames([]))


def test_timeout_during_camera_wait_interrupts_and_no_grip():
    r = robot()
    r.poses["visual_approach"]["timeout_s"] = 1
    class BlockedCamera:
        def detect_once(self):
            assert r.stopped.wait(2)
            return observation(1)
    with pytest.raises(ApproachError):
        VisualApproach(r, BlockedCamera()).align(0)
    assert r.commands == ["quit;"]


def test_api_approach_failure_never_drives_to_belt_or_grips():
    r = robot()
    perception = DryRunPerception({0: "R3"}, r)
    node = RobotNode(r, perception, {0: "R3"}, at_observe=True)
    node.begin("r1", ["R3"])
    node.detect()
    node.approach.perception = Frames([observation(1, tag=9)])
    client = create_app(node).test_client()
    result = client.post("/grasp_place", json={"run_id": "r1", "type": "R3"})
    assert result.status_code == 502
    assert result.json["steps"] == [{"op": "grasp", "ok": False,
        "error": "Target tag 0 lost or ambiguous (0 detections)"}]
    assert not r.chassis_moves
    assert not any("gripper" in command for command in r.commands)


def test_urgent_stop_during_alignment_prevents_grip_and_retrace():
    r = robot()
    entered, release = threading.Event(), threading.Event()
    class Camera:
        def detect_once(self):
            entered.set()
            release.wait(2)
            return observation(1)
    outcome = []
    def worker():
        try: VisualApproach(r, Camera()).align(0)
        except ApproachError as exc: outcome.append(str(exc))
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    r.estop()
    release.set()
    thread.join(1)
    assert outcome == ["Approach stopped"]
    assert r.commands == ["quit;"]
