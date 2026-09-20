"""Approved BOM -> one frozen, sequential cup plan. No replanning or motion retry."""
import threading
import uuid

from ingestion.parse import validate_bom
from config import number


class Controller:
    def __init__(self, pi):
        self.pi = pi
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.last_stop_errors = []

    def stop(self):
        self.cancelled.set()
        self.last_stop_errors = []
        try:
            result = self.pi.call("POST", "/estop", {}, timeout=5)
            if result.get("ok") is not True:
                raise RuntimeError("Stop was not acknowledged")
        except Exception as exc:
            self.last_stop_errors = [str(exc)]
        return self.last_stop_errors

    def _check(self):
        if self.cancelled.is_set():
            raise RuntimeError("Run stopped by operator")

    def run(self, bom, *, approved=False, progress=lambda event: None):
        if approved is not True:
            raise ValueError("Human approval is required before any motion")
        bom = validate_bom(bom)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Another run is active")
        # Latched stop: a stop between HTTP approval and worker startup cannot be cleared.
        run_id = uuid.uuid4().hex
        result = {"run_id": run_id, "cups_commanded": [], "missing": [],
                  "physical_delivery_verified": False}
        try:
            self._check()
            started = self.pi.post("/begin", {"run_id": run_id, "types": list(bom)})
            self._ok(started)
            for part_type, quantity in tuple(bom.items()):
                self._check()
                detection = self.pi.get("/detect")
                self._validate_detection(detection)
                item = detection["rollup"].get(part_type, {})
                if item.get("present") is not True:
                    result["missing"].append(part_type)
                    progress({"op": "missing", "type": part_type, "quantity_needed": quantity})
                    continue
                self._check()
                progress({"op": "grasp_place", "type": part_type, "quantity_needed": quantity})
                drop = self.pi.post("/grasp_place", {"run_id": run_id, "type": part_type})
                self._ok(drop)
                if drop.get("type") != part_type or drop.get("steps") != [
                    {"op": "grasp", "ok": True}, {"op": "drive", "ok": True}, {"op": "drop", "ok": True}
                ]:
                    raise RuntimeError("Pi did not confirm the full ordered drop log")
                if "approach" in drop:
                    progress({"op": "approach_result", "type": part_type, "detail": drop["approach"]})
                self._check()
                returned = self.pi.post("/return", {"run_id": run_id, "type": part_type})
                self._ok(returned)
                if returned.get("type") != part_type or returned.get("steps") != [
                    {"op": "return", "ok": True}, {"op": "observe", "ok": True}
                ]:
                    raise RuntimeError("Pi did not confirm return/observe")
                result["cups_commanded"].append(part_type)
                progress({"op": "complete_type", "type": part_type, "quantity_needed": quantity})
            self._check()
            self._ok(self.pi.post("/end", {"run_id": run_id}))
            result["state"] = "complete_with_missing" if result["missing"] else "complete"
            return result
        except BaseException:
            self.stop()
            raise
        finally:
            self.lock.release()

    @staticmethod
    def _ok(value):
        if value.get("ok") is not True:
            raise RuntimeError("Node did not acknowledge completion")

    @staticmethod
    def _validate_detection(value):
        if not isinstance(value.get("rollup"), dict):
            raise ValueError("Malformed detection rollup")
        number(value.get("ts"), "detection timestamp", 0, 1e12)
        for part, item in value["rollup"].items():
            if not isinstance(part, str) or not isinstance(item, dict):
                raise ValueError("Malformed detection entry")
            count = item.get("count")
            if type(count) is not int or count < 0 or item.get("present") is not (count > 0):
                raise ValueError("Detection count/present disagree")
