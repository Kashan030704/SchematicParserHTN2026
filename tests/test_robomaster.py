import copy
import threading
import time
from pathlib import Path

import pytest
from config import load_poses, load_tag_map
from actuator.motion import Robot, MotionError
from actuator.perception import DryRunPerception, Perception
from actuator.server import RobotNode, create_app
from tests.conftest import eventually

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def poses():
    return load_poses(ROOT / "poses.yaml")


@pytest.fixture
def node(poses):
    tags = load_tag_map(ROOT / "tag_map.yaml")
    robot = Robot(poses, dry_run=True, emit=lambda value: None)
    robot.connect()
    return RobotNode(robot, DryRunPerception(tags, robot), tags, at_observe=True)


class SocketStub:
    def __init__(self, chunks):
        self.chunks, self.writes, self.closed = iter(chunks), [], False
    def sendall(self, value):
        self.writes.append(value)
    def recv(self, size):
        item = next(self.chunks, b"")
        if isinstance(item, Exception):
            raise item
        return item
    def shutdown(self, *args): pass
    def close(self): self.closed = True


def real_robot(poses, chunks):
    poses = copy.deepcopy(poses)
    poses["calibrated"] = True
    sock = SocketStub(chunks)
    robot = Robot(poses, host="test-ep", socket_factory=lambda *a, **k: sock)
    robot._settle = lambda seconds: None
    return robot, sock


def test_split_replies_units_and_exact_commands(poses):
    robot, sock = real_robot(poses, [b"o", b"k;", b"ok;", b"ok;", b"ok;", b"ok;"])
    robot.connect()
    robot.arm_moveto({"arm": {"x": 100, "y": 25}})
    robot.gripper("close")
    robot.chassis_move({"chassis": {"x_m": 0.3, "y_m": 0, "z_deg": -90}})
    assert sock.writes == [b"command;", b"robot mode free;",
        b"robotic_arm moveto x 10 y 2.5;", b"robotic_gripper close 1;",
        b"chassis move x 0.3 y 0 z -90 vxy 0.1 vz 15;"]


@pytest.mark.parametrize("chunks", [[b"fail;"], [b""], [TimeoutError("late")], [b"ok;ok;"]])
def test_motion_fault_stops_and_never_retries(poses, chunks):
    robot, sock = real_robot(poses, chunks)
    with pytest.raises(Exception):
        robot.connect()
    assert sock.writes == [b"command;", b"quit;"]
    assert robot.stopped.is_set() and sock.closed
    with pytest.raises(MotionError):
        robot.observe()
    assert sock.writes == [b"command;", b"quit;"]


def test_hardware_rejects_placeholder_poses():
    with pytest.raises(ValueError, match="Calibrate"):
        load_poses(ROOT / "poses.yaml", hardware=True)


def test_stop_can_interrupt_write_in_same_thread(poses):
    robot, sock = real_robot(poses, [b"ok;", b"ok;"])
    robot.connect()
    completed = threading.Event()
    def interrupted_writer():
        with robot._write_lock:
            robot.estop()  # Models SIGINT arriving while send holds the write lock.
        completed.set()
    thread = threading.Thread(target=interrupted_writer, daemon=True)
    thread.start()
    assert completed.wait(1), "Signal-time stop deadlocked on the socket write lock"
    thread.join(1)
    assert sock.closed and sock.writes[-1] == b"quit;"


def test_preview_does_not_move_and_grasp_needs_approval(node):
    client = create_app(node).test_client()
    baseline = list(node.robot.commands)
    assert client.get("/detect").status_code == 200
    assert node.robot.commands == baseline
    assert client.post("/grasp_place", json={"type": "resistors", "run_id": "unknown"}).status_code == 409
    assert node.robot.commands == baseline


def test_drop_return_split_and_duplicate_rejected(node):
    client = create_app(node).test_client()
    assert client.post("/begin", json={"run_id": "r1", "types": ["resistors"]}).status_code == 200
    assert client.post("/grasp_place", json={"run_id": "r1", "type": "resistors"}).status_code == 412
    client.get("/detect")
    result = client.post("/grasp_place", json={"run_id": "r1", "type": "resistors"})
    assert result.status_code == 200
    assert result.json["steps"][-1] == {"op": "drop", "ok": True}
    assert node.state == "dropped"
    assert result.json["approach"]["tag_id"] == 0
    assert result.json["approach"]["aligned"] is True
    assert result.json["approach"]["steps"] > 0
    # Visual approach + retrace precede the preset collection leg.
    assert node.robot.chassis_moves[-1] == node.robot.poses["collection_wp"]["chassis"]
    assert client.get("/detect").status_code == 412
    assert client.post("/return", json={"run_id": "r1", "type": "resistors"}).status_code == 200
    assert node.state == "observe"
    client.get("/detect")
    assert client.post("/grasp_place", json={"run_id": "r1", "type": "resistors"}).status_code == 409
    assert client.post("/end", json={"run_id": "r1"}).status_code == 200
    assert client.post("/begin", json={"run_id": "r1", "types": ["resistors"]}).status_code == 409


def test_detection_cannot_run_mid_motion_stop_bypasses_motion_lock(node):
    entered, release = threading.Event(), threading.Event()
    def slow(part):
        entered.set()
        release.wait(2)
    node.robot.grasp = slow
    node.begin("r", ["resistors"])
    node.detect()
    result = []
    thread = threading.Thread(target=lambda: result.append(node.grasp_place("r", "resistors")))
    thread.start()
    assert entered.wait(1)
    commands_before_stop = list(node.robot.commands)
    client = create_app(node).test_client()
    assert client.get("/detect").status_code == 409
    assert client.post("/estop", json={}).status_code == 200
    release.set()
    thread.join(2)
    assert result[0][1] == 502
    assert node.robot.commands == commands_before_stop + ["quit;"]
    assert client.get("/detect").status_code == 409


def test_stale_and_unknown_type_rejected(node):
    node.begin("r", ["resistors"])
    node.detect()
    node.detected_at = time.monotonic() - 11
    client = create_app(node).test_client()
    assert client.post("/grasp_place", json={"run_id": "r", "type": "resistors"}).status_code == 412
    assert client.post("/grasp_place", json={"run_id": "r", "type": "wrong"}).status_code == 400


def test_auth(node):
    client = create_app(node, token="a" * 32).test_client()
    assert client.get("/detect").status_code == 401
    assert client.get("/detect", headers={"Authorization": "Bearer " + "a" * 32}).status_code == 200


def test_image_apriltags_identification_and_pixel_features_only():
    import cv2
    import numpy as np
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    canvas = np.full((300, 650), 255, dtype=np.uint8)
    for x, tag in [(30, 0), (240, 0), (450, 8)]:
        canvas[70:230, x:x+160] = cv2.aruco.generateImageMarker(dictionary, tag, 160)
    class Camera:
        def read_cv2_image(self, **kwargs):
            assert kwargs["strategy"] == "newest"
            return canvas
    result = Perception(Camera(), {0: "resistors", 2: "capacitors", 8: "diodes/LED"}).detect_once()
    assert result["rollup"] == {"resistors": {"count": 2, "present": True},
                                 "diodes/LED": {"count": 1, "present": True}}
    assert set(result) == {"ts", "rollup", "frame", "tags"}
    assert result["frame"]["width"] == 650
    assert len([t for t in result["tags"] if t["id"] == 0]) == 2
    led = next(t for t in result["tags"] if t["id"] == 8)
    assert led["center_px"] == pytest.approx([529.5, 149.5], abs=1)
    assert led["side_px"] == pytest.approx(159, abs=1)
    assert set(led) == {"id", "center_px", "side_px", "corners_px"}  # No world pose.


def test_empty_camera_is_error():
    class Camera:
        def read_cv2_image(self, **kwargs): return None
    with pytest.raises(RuntimeError, match="fresh"):
        Perception(Camera(), {0: "resistors"}).detect_once()


def test_camera_inspection_cannot_move_or_serve_motion(poses):
    robot = Robot(poses, dry_run=True, read_only=True, emit=lambda text: None)
    robot.connect()
    with pytest.raises(MotionError, match="read-only"):
        robot.grasp("resistors")
    with pytest.raises(MotionError, match="read-only"):
        robot.to_collection()
    with pytest.raises(ValueError, match="inspection"):
        RobotNode(robot, DryRunPerception({0: "resistors"}, robot), {0: "resistors"})
    assert robot.commands == ["command;", "robot mode free;"]
