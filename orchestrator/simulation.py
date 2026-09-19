"""Explicit software-only fixtures. Never imported to provide hardware defaults."""
import json
import threading
import time
import uuid

from actuator.arm_node import ActuatorRuntime
from hcp_sdk.loader import load_client
from vision.camera_node import CameraNode
from orchestrator.fixtures import DEMO_BOM, FixtureIngestionModel, create_demo_pdf


def simulation_config():
    return {
        "serial": {"port": "SIMULATED", "baud": 115200},
        "arm": {"links_m": [0.2, 0.2], "base_world_m": [0, 0, 0], "base_yaw_rad": 0,
                "elbow_sign": 1, "servo_offsets_deg": [90, 90, 0], "servo_directions": [1, 1, 1],
                "servo_limits_deg": [[0, 180]] * 4, "home_deg": [90, 60, 100, 30],
                "gripper_open_deg": 30, "gripper_closed_deg": 80, "move_ms": 2, "settle_ms": 0,
                "clearance_z_m": 0.1,
                "bins": {str(i): {"offset_m": [0, 0], "pickup_z_m": 0.02} for i in (3, 5, 7)},
                "zones": {"belt": [0.22, -0.05, 0.03], "staging": [0.24, 0.03, 0.03]}},
        "belt": {"neutral_us": 1500, "forward_us": 1700, "delivery_duration_s": 0.03, "max_duration_s": 10},
        "camera": {"source": 0, "homography": [[0.0002, 0, 0.12], [0, 0.0002, -0.06], [0, 0, 1]],
                   "image_size": [640, 400], "publish_hz": 20, "max_pose_age_s": 1},
    }


class SimulatedController:
    def __init__(self):
        self.commands = []
        self.cancelled = threading.Event()
        self.fault = False
        self.lock = threading.Lock()

    def move(self, angles):
        if self.fault:
            raise RuntimeError("Simulated hardware session interrupted")
        with self.lock:
            self.commands.append(("J", list(angles), time.monotonic()))
        time.sleep(0.002)

    def advance(self, duration_s):
        if self.fault:
            raise RuntimeError("Simulated hardware session interrupted")
        self.cancelled.clear()
        with self.lock:
            self.commands.append(("B", duration_s, time.monotonic()))
        if self.cancelled.wait(duration_s):
            raise RuntimeError("Simulated belt interrupted")

    def stop_belt(self):
        self.cancelled.set()
        with self.lock:
            self.commands.append(("S", None, time.monotonic()))

    def abort(self):
        self.fault = True
        self.stop_belt()

    def close(self):
        self.abort()


class FixtureModel(FixtureIngestionModel):
    def plan(self, messages, tools):
        context = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))
        if not context["belt_stopped"]:
            name, args = "conveyor__stop", {}
        elif not context["initialized"]:
            name, args = "arm__home", {}
        elif context["holding"]:
            name, args = "arm__place", {"zone": "belt"}
        elif context["on_belt"]:
            name, args = "conveyor__advance", {"duration_s": context["delivery_duration_s"]}
        else:
            name, args = "arm__pick", {"tag_id": context["remaining"][0]["tag_id"]}
        assert name in {t["function"]["name"] for t in tools}
        return {"role": "assistant", "tool_calls": [{"id": uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def create_fixtures(directory):
    import cv2
    import numpy as np
    directory.mkdir(parents=True, exist_ok=True)
    pdf = create_demo_pdf(directory)
    image = np.full((400, 640), 255, dtype=np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    for tag_id, x in ((3, 60), (5, 260), (7, 460)):
        image[140:240, x:x+100] = cv2.aruco.generateImageMarker(dictionary, tag_id, 100)
    image_path = directory / "tags.png"
    cv2.imwrite(str(image_path), image)
    return pdf, image_path


class Simulation:
    def __init__(self, host, directory):
        self.config = simulation_config()
        self.pdf, image = create_fixtures(directory)
        self.controller = SimulatedController()
        self.actuator = ActuatorRuntime(self.config, "127.0.0.1", host.port, directory / "out", self.controller).start()
        client = load_client("camera", "127.0.0.1", host.port, directory / "out")
        self.camera = CameraNode(client, self.config["camera"], image).start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tags = host.context_snapshot().get("context/tags", {}).get("message", {}).get("payload", {}).get("tags", [])
            if set(host.registry.snapshot()) == {"arm", "conveyor", "camera"} and len(tags) == 3:
                return
            time.sleep(0.02)
        self.stop()
        raise RuntimeError("Simulation nodes failed to become ready")

    def stop(self):
        self.camera.stop()
        self.actuator.stop()
