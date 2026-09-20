"""Hardware-free contract smoke test; physical execution requires the browser gate."""
import argparse
import json
from pathlib import Path

from config import load_poses, load_tag_map
from ingestion.parse import strict_json, validate_bom


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--bom", required=True)
    run.add_argument("--dry-run", action="store_true", required=True)
    run.add_argument("--poses", default="poses.yaml")
    run.add_argument("--tags", default="tag_map.yaml")
    args = parser.parse_args(argv)
    try:
        bom = validate_bom(strict_json(Path(args.bom).read_text()))
        tags, poses = load_tag_map(args.tags), load_poses(args.poses)
        missing = sorted(set(bom) - set(tags.values()))
        if missing:
            raise ValueError("No cup mapping for: " + ", ".join(missing))
        print("DRY RUN ONLY — one cup per type, no socket/camera/hardware or Baseten call.")
        print("Ordered plan:", json.dumps([{"type": part, "quantity_needed": qty,
              "ops": ["detect", "grasp", "drive", "drop", "return", "observe"]}
             for part, qty in bom.items()], indent=2))
        from actuator.motion import Robot
        from actuator.perception import DryRunPerception
        from actuator.server import RobotNode, create_app as robot_app
        from orchestrator.controller import Controller
        class InProcessClient:
            def __init__(self, app):
                self.client = app.test_client()
            def call(self, method, path, payload=None, **kwargs):
                response = self.client.open(path, method=method, json=payload)
                value = response.get_json()
                if response.status_code >= 400:
                    raise RuntimeError(str(value))
                return value
            def get(self, path):
                return self.call("GET", path)
            def post(self, path, payload):
                return self.call("POST", path, payload)
        robot = Robot(poses, dry_run=True)
        robot.connect()
        try:
            node = RobotNode(robot, DryRunPerception(tags, robot), tags, at_observe=True)
            controller = Controller(InProcessClient(robot_app(node)))
            result = controller.run(bom, approved=True, progress=lambda e: print(json.dumps(e)))
            print(json.dumps(result, indent=2))
        finally:
            robot.estop()
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2
