"""Arm handler and shared arm/conveyor process entrypoint."""
import argparse
import logging
import math
import os
import threading
import time

from actuator.kinematics import Kinematics
from config import ROOT, load_hardware
from hcp_sdk.loader import load_client


class ArmNode:
    def __init__(self, client, controller, config):
        self.client, self.controller = client, controller
        self.config = config["arm"]
        self.max_pose_age = config["camera"]["max_pose_age_s"]
        self.ik = Kinematics(self.config)
        self.holding = False
        self.fault = False
        client.register_handler("pick", self.pick)
        client.register_handler("place", self.place)
        client.register_handler("home", self.home)
        client.on_disconnect = self.abort

    def abort(self):
        self.fault = True
        self.controller.abort()

    def _move_all(self, poses):
        if self.fault:
            raise RuntimeError("Arm session interrupted; inspect hardware and restart")
        try:
            for angles in poses:
                if self.fault or not self.client.connected.is_set():
                    raise RuntimeError("Arm disconnected during movement")
                self.controller.move(angles)
        except Exception:
            self.abort()
            raise

    def pick(self, tag_id):
        if self.holding:
            raise ValueError("Arm already holds a part")
        tags = self.client.tags(self.max_pose_age)
        tag = next((t for t in tags if t["id"] == tag_id), None)
        if tag is None:
            raise ValueError(f"Tag {tag_id} is not visible")
        bin_config = self.config["bins"].get(str(tag_id))
        if bin_config is None:
            raise ValueError(f"No pickup calibration for tag {tag_id}")
        ox, oy = bin_config["offset_m"]
        # Offset is expressed in tag-local axes; the gripper has no wrist.
        c, s = math.cos(tag["theta"]), math.sin(tag["theta"])
        x, y = tag["x"] + c * ox - s * oy, tag["y"] + s * ox + c * oy
        high, low = [x, y, self.config["clearance_z_m"]], [x, y, bin_config["pickup_z_m"]]
        opened, closed = self.config["gripper_open_deg"], self.config["gripper_closed_deg"]
        # Validate the entire waypoint sequence before sending any joint target.
        poses = [self.ik.solve(high, opened), self.ik.solve(low, opened), self.ik.solve(low, closed), self.ik.solve(high, closed)]
        self.client.status("picking", tag_id=tag_id)
        self._move_all(poses)
        self.holding = True
        self.client.status("holding", tag_id=tag_id)
        return {"tag_id": tag_id, "grip_feedback": "unmeasured"}

    def place(self, zone):
        if not self.holding:
            raise ValueError("Arm has no commanded pickup to place")
        if zone not in self.config["zones"]:
            raise ValueError(f"Unknown zone {zone}")
        low = self.config["zones"][zone]
        high = [low[0], low[1], self.config["clearance_z_m"]]
        opened, closed = self.config["gripper_open_deg"], self.config["gripper_closed_deg"]
        poses = [self.ik.solve(high, closed), self.ik.solve(low, closed), self.ik.solve(low, opened), self.ik.solve(high, opened)]
        self.client.status("placing", zone=zone)
        self._move_all(poses)
        self.holding = False
        self.client.status("ready")
        return {"zone": zone, "retreated": True}

    def home(self):
        if self.holding:
            raise ValueError("Cannot home while holding a part")
        self._move_all([self.ik.check_angles(self.config["home_deg"])])
        self.client.status("ready")


class ActuatorRuntime:
    def __init__(self, config, host, port, output_dir, controller=None):
        from actuator.conveyor_node import ConveyorNode
        from actuator.serial_controller import SerialController
        # Check kinematics before even opening the serial port.
        Kinematics(config["arm"])
        self.controller = controller or SerialController(config)
        self.arm_client = load_client("arm", host, port, output_dir)
        self.belt_client = load_client("conveyor", host, port, output_dir)
        self.arm = ArmNode(self.arm_client, self.controller, config)
        self.belt = ConveyorNode(self.belt_client, self.controller, config)

    def start(self):
        self.arm_client.start()
        self.belt_client.start()
        return self

    def stop(self):
        self.arm_client.stop()
        self.belt_client.stop()
        self.controller.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.getenv("HARDWARE_CONFIG", str(ROOT / "config/hardware.json")))
    parser.add_argument("--host", default=os.getenv("HCP_HOST"), required=not os.getenv("HCP_HOST"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HCP_PORT", "9000")))
    parser.add_argument("--output", default=str(ROOT / "out"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    runtime = ActuatorRuntime(load_hardware(args.config), args.host, args.port, args.output).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    main()
