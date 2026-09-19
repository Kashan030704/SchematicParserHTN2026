"""Run on the Pi; no startup motion and no automatic recovery."""
import argparse
import json
import os
import signal

from config import load_poses, load_tag_map
from actuator.motion import Robot
from actuator.perception import EPCamera, Perception, DryRunPerception
from actuator.server import RobotNode, create_app


def main():
    parser = argparse.ArgumentParser(description="RoboMaster EP HTTP node")
    parser.add_argument("--poses", default="poses.yaml")
    parser.add_argument("--tags", default="tag_map.yaml")
    parser.add_argument("--ep-host", help="Confirm EP USB/RNDIS endpoint, not the Pi Wi-Fi IP")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inspect-camera", action="store_true",
                        help="Print fresh tag pixels only; no HTTP server or motion, no taught poses required")
    parser.add_argument("--at-observe", action="store_true",
                        help="Operator confirms initial chassis and arm are at calibrated observe pose")
    args = parser.parse_args()
    poses = load_poses(args.poses, hardware=not args.dry_run and not args.inspect_camera)
    tags = load_tag_map(args.tags)
    if not args.dry_run and not args.inspect_camera and not args.at_observe:
        parser.error("Physically position at observe, then explicitly pass --at-observe")
    token = os.getenv("HCP_NODE_TOKEN")
    if not args.dry_run and not args.inspect_camera and (not token or len(token) < 24):
        parser.error("Set a shared HCP_NODE_TOKEN of at least 24 characters")
    robot = Robot(poses, host=args.ep_host, dry_run=args.dry_run, read_only=args.inspect_camera)
    camera = None
    def interrupted(signum, frame):
        robot.estop()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        robot.connect()
        if args.dry_run:
            perception = DryRunPerception(tags, robot)
        else:
            camera = EPCamera(robot)
            camera.start_video_stream(display=False)
            perception = Perception(camera, tags)
        if args.inspect_camera:
            print("READ-ONLY camera inspection: no motion or HTTP endpoints. Ctrl-C to exit.", flush=True)
            while not robot.stopped.is_set():
                print(json.dumps(perception.detect_once()), flush=True)
                robot.stopped.wait(0.2)
            return
        node = RobotNode(robot, perception, tags, at_observe=args.at_observe or args.dry_run)
        create_app(node, token=token).run(host=args.bind, port=args.port, threaded=True, use_reloader=False)
    finally:
        robot.estop()
        if camera:
            camera.close()


if __name__ == "__main__":
    main()
