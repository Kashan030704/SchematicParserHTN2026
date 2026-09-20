"""Pi HTTP node for approved cup collection and return to observe."""
import threading
import time
from contextlib import contextmanager

from flask import Flask, jsonify
from werkzeug.exceptions import Conflict, BadRequest, PreconditionFailed

from hcp_host.http import configure_node_app, object_body
from actuator.approach import VisualApproach


class RobotNode:
    def __init__(self, robot, perception, tags, *, at_observe=False):
        if robot.read_only:
            raise ValueError("A camera-inspection session cannot serve motion endpoints")
        self.robot, self.perception, self.tags = robot, perception, tags
        self.approach = VisualApproach(robot, perception)
        self.tag_ids = {part: tag for tag, part in tags.items()}
        self.lock = threading.Lock()
        self.state = "observe" if at_observe else "unpositioned"
        self.run_id = None
        self.allowed = set()
        self.attempted = set()
        self.used_runs = set()
        self.current_type = None
        self.last_detection = None
        self.detected_at = 0

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise Conflict("Node busy; motion/detection must not overlap")
        try:
            if self.robot.stopped.is_set():
                raise Conflict("Node stopped; inspect and restart locally")
            yield
        finally:
            self.lock.release()

    def check_run(self, run_id):
        if not isinstance(run_id, str) or run_id != self.run_id:
            raise Conflict("No matching approved run")

    def begin(self, run_id, types):
        with self.exclusive():
            if not isinstance(run_id, str) or not 1 <= len(run_id) <= 100:
                raise BadRequest("Invalid run_id")
            if not isinstance(types, list) or not types or any(t not in self.tags.values() for t in types):
                raise BadRequest("All types must have a configured AprilTag")
            if len(set(types)) != len(types):
                raise BadRequest("One cup per type; duplicate types not allowed")
            if self.run_id or run_id in self.used_runs or self.state != "observe":
                raise Conflict("Run already used, busy, or robot not at observe station")
            if len(self.used_runs) >= 1000:
                raise Conflict("Session capacity reached; inspect and restart")
            if any(self.tag_ids[t] not in self.robot.poses["coarse_routes"] for t in types):
                raise BadRequest("Every approved tag needs a coarse_routes entry (may be an empty list)")
            self.run_id, self.allowed = run_id, set(types)
            self.attempted = set()
            self.used_runs.add(run_id)
            self.last_detection = None
            return {"ok": True, "run_id": run_id}

    def detect(self):
        with self.exclusive():
            if self.state != "observe":
                raise PreconditionFailed("Detection requires the observe station; no auto-motion in GET")
            result = self.perception.detect_once()
            self.last_detection, self.detected_at = result, time.monotonic()
            return result

    def grasp_place(self, run_id, part_type):
        with self.exclusive():
            self.check_run(run_id)
            if not isinstance(part_type, str) or part_type not in self.allowed:
                raise BadRequest("Type is not in the approved run")
            if self.state != "observe" or part_type in self.attempted:
                raise Conflict("Wrong phase or cup already attempted; never replay motion")
            seen = (self.last_detection or {}).get("rollup", {}).get(part_type, {})
            if time.monotonic() - self.detected_at > 10 or seen.get("present") is not True:
                raise PreconditionFailed("Require a fresh detection of this cup")
            self.attempted.add(part_type)
            self.current_type, self.state = part_type, "moving"
            steps = []
            for op, action in (("grasp", lambda: self.approach.pick(part_type, self.tag_ids[part_type])),
                               ("drive", self.robot.to_collection), ("drop", self.robot.drop)):
                try:
                    action()
                    if self.robot.stopped.is_set():
                        raise RuntimeError("Robot stopped")
                    steps.append({"op": op, "ok": True})
                except Exception as exc:
                    self.estop()
                    steps.append({"op": op, "ok": False, "error": str(exc)})
                    return {"type": part_type, "steps": steps, "ok": False}, 502
            self.state = "dropped"
            return {"type": part_type, "steps": steps, "ok": True,
                    "approach": self.approach.last_report,
                    "physical_delivery_verified": False}, 200

    def return_home(self, run_id, part_type):
        with self.exclusive():
            self.check_run(run_id)
            if self.state != "dropped" or part_type != self.current_type:
                raise Conflict("Return only after this cup's drop")
            self.state = "returning"
            try:
                self.robot.to_home()
                self.robot.observe()
                if self.robot.stopped.is_set():
                    raise RuntimeError("Robot stopped")
            except Exception:
                self.estop()
                raise
            self.state, self.current_type = "observe", None
            self.last_detection = None
            return {"ok": True, "type": part_type,
                    "steps": [{"op": "return", "ok": True}, {"op": "observe", "ok": True}]}

    def end(self, run_id):
        with self.exclusive():
            self.check_run(run_id)
            if self.state != "observe":
                raise Conflict("Cannot end away from observe station")
            self.run_id = None
            self.allowed = set()
            return {"ok": True}

    def estop(self):
        self.state = "stopped"
        self.robot.estop()
        return {"ok": True, "stop_requested": True, "physical_stop_verified": False}


def create_app(node, *, token=None):
    app = Flask(__name__)
    configure_node_app(app, token=token, hardware=not node.robot.dry_run)

    @app.get("/health")
    def health():
        return jsonify(node="robomaster", state=node.state, simulated=node.robot.dry_run,
                       run_id=node.run_id, types=sorted(node.tags.values()))

    @app.get("/detect")
    def detect():
        return jsonify(node.detect())

    @app.post("/begin")
    def begin():
        data = object_body({"run_id", "types"})
        return jsonify(node.begin(**data))

    @app.post("/grasp_place")
    def grasp_place():
        data = object_body({"run_id", "type"})
        result, code = node.grasp_place(data["run_id"], data["type"])
        return jsonify(result), code

    @app.post("/return")
    def return_home():
        data = object_body({"run_id", "type"})
        return jsonify(node.return_home(data["run_id"], data["type"]))

    @app.post("/end")
    def end():
        data = object_body({"run_id"})
        return jsonify(node.end(**data))

    @app.post("/estop")
    def estop():
        return jsonify(node.estop())

    @app.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        node.estop()
        return jsonify(error=str(exc)), 502

    return app
