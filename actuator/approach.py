"""Bounded image-based terminal approach, below the unchanged one-shot plan.

No solvePnP, camera matrix, extrinsic transform, physical tag size, or distance
estimate. X/size and Y/horizontal-error corrections are finite chassis steps.
Vertical image error gates success; a planar chassis has no independent height DOF.
"""
import math
import threading
import time

from config import number, validate_visual_approach


class ApproachError(RuntimeError):
    pass


class VisualApproach:
    def __init__(self, robot, perception, *, clock=time.monotonic):
        self.robot, self.perception, self.clock = robot, perception, clock
        self.cfg = validate_visual_approach(robot.poses, hardware=not robot.dry_run)
        self.history = []
        self.last_report = None

    def _check(self):
        if self.robot.stopped.is_set():
            raise ApproachError("Approach stopped")

    def _sample(self, tag_id, previous=None, *, terminal=True):
        self._check()
        detection = self.perception.detect_once()
        self._check()
        cfg = self.cfg
        frame = detection.get("frame", {})
        if (frame.get("width"), frame.get("height")) != (cfg["frame_width"], cfg["frame_height"]):
            raise ApproachError("Image resolution differs from taught target")
        sequence = frame.get("seq")
        if type(sequence) is not int or sequence < 1 or (previous is not None and sequence <= previous):
            raise ApproachError("Repeated/out-of-order camera frame")
        stamp = frame.get("captured_monotonic")
        number(stamp, "frame timestamp", 0, 1e15)
        age = self.clock() - stamp
        if not 0 <= age <= cfg["max_frame_age_s"]:
            raise ApproachError("Stale camera frame")
        features = detection.get("tags")
        if not isinstance(features, list) or any(not isinstance(t, dict) for t in features):
            raise ApproachError("Malformed tag features")
        matches = [t for t in features if type(t.get("id")) is int and t["id"] == tag_id]
        if len(matches) != 1:
            raise ApproachError(f"Target tag {tag_id} lost or ambiguous ({len(matches)} detections)")
        tag = matches[0]
        center, corners = tag.get("center_px"), tag.get("corners_px")
        if not isinstance(center, list) or len(center) != 2:
            raise ApproachError("Malformed tag center")
        u = number(center[0], "tag u", 0, cfg["frame_width"])
        v = number(center[1], "tag v", 0, cfg["frame_height"])
        # A distant but identified tag can precede a configured coarse leg; the
        # minimum controllable image size applies at the terminal-servo boundary.
        side = number(tag.get("side_px"), "tag side",
                      cfg["min_side_px"] if terminal else 1, cfg["max_side_px"])
        if not isinstance(corners, list) or len(corners) != 4:
            raise ApproachError("Malformed tag corners")
        margin = cfg["edge_margin_px"]
        for point in corners:
            if not isinstance(point, list) or len(point) != 2:
                raise ApproachError("Malformed tag corner")
            number(point[0], "tag corner u", margin, cfg["frame_width"] - margin)
            number(point[1], "tag corner v", margin, cfg["frame_height"] - margin)
        # Internal feature consistency; never accept an arbitrary center/scale paired with a border-clipped tag.
        mean = [sum(p[axis] for p in corners) / 4 for axis in (0, 1)]
        edges = [math.dist(corners[i], corners[(i + 1) % 4]) for i in range(4)]
        if math.dist(mean, center) > 1 or abs(sum(edges) / 4 - side) > 1 or min(edges) <= 0:
            raise ApproachError("Inconsistent tag image geometry")
        if max(edges) / min(edges) > cfg["max_edge_ratio"]:
            raise ApproachError("Tag is too skewed for the taught apparent-size target")
        return {"seq": sequence, "u": u, "v": v, "side": side}

    def _move(self, x, y):
        self._check()
        leg = {"x_m": x, "y_m": y, "z_deg": 0}
        self.robot.chassis_move({"chassis": leg},
            speed_mps=self.cfg["speed_mps"], settle_s=self.cfg["settle_s"])
        self._check()
        self.history.append(leg)

    def align(self, tag_id, *, previous=None):
        cfg = self.cfg
        started, stable, steps, travel = self.clock(), 0, 0, 0.0
        previous_error, stalled = None, 0
        trace = []
        # Independent watchdog also interrupts a blocked camera/SDK wait. Physical stop is best effort.
        expired = threading.Event()
        def timeout():
            expired.set()
            self.robot.estop()
        timer = threading.Timer(cfg["timeout_s"], timeout)
        timer.daemon = True
        timer.start()
        try:
            while True:
                self._check()
                if expired.is_set() or self.clock() - started >= cfg["timeout_s"]:
                    raise ApproachError("Visual approach timed out")
                sample = self._sample(tag_id, previous)
                previous = sample["seq"]
                if expired.is_set() or self.clock() - started >= cfg["timeout_s"]:
                    raise ApproachError("Visual approach timed out")
                errors = {"u": sample["u"] - cfg["target_u_px"],
                          "v": sample["v"] - cfg["target_v_px"],
                          "size": cfg["target_side_px"] - sample["side"]}
                trace.append({"frame": previous, "error_px": errors})
                self.robot.emit(f"ALIGN tag={tag_id} error_px={errors}")
                if previous_error is not None:
                    axis, before = previous_error
                    improvement = before - abs(errors[axis])
                    stalled = stalled + 1 if improvement < cfg["min_improvement_px"] else 0
                    if stalled >= cfg["max_stalled_steps"]:
                        raise ApproachError("Visual error not improving; check motion signs, slip or obstruction")
                    previous_error = None
                horizontal_ok = abs(errors["u"]) <= cfg["u_tolerance_px"]
                size_ok = abs(errors["size"]) <= cfg["size_tolerance_px"]
                vertical_ok = abs(errors["v"]) <= cfg["v_tolerance_px"]
                if horizontal_ok and size_ok:
                    if not vertical_ok:
                        raise ApproachError("Vertical target mismatch: chassis cannot independently correct tag height")
                    stable += 1
                    if stable >= cfg["stable_frames"]:
                        self._check()
                        self.last_report = {"tag_id": tag_id, "aligned": True, "steps": steps,
                            "commanded_travel_m": travel, "stable_frames": stable, "trace": trace}
                        return self.last_report
                    continue
                stable = 0
                if steps >= cfg["max_steps"]:
                    raise ApproachError("Visual step budget exhausted")
                # Correct one axis at a time; no forward advance while horizontally misaligned.
                if not horizontal_ok:
                    axis, x = "u", 0.0
                    y = math.copysign(cfg["lateral_step_m"], errors["u"]) * cfg["lateral_sign"]
                else:
                    axis, y = "size", 0.0
                    x = math.copysign(cfg["forward_step_m"], errors["size"]) * cfg["forward_sign"]
                distance = math.hypot(x, y)
                if travel + distance > cfg["max_travel_m"] + 1e-9:
                    raise ApproachError("Visual travel budget exhausted")
                minimum_time = distance / cfg["speed_mps"] + cfg["settle_s"]
                if self.clock() - started + minimum_time >= cfg["timeout_s"]:
                    raise ApproachError("Insufficient time budget for another correction")
                previous_error = axis, abs(errors[axis])
                self._move(x, y)
                steps += 1
                travel += distance
        except BaseException:
            # Never retreat, search for another tag or grip after an alignment failure.
            self.robot.estop()
            raise
        finally:
            timer.cancel()
            timer.join(timeout=1)

    def pick(self, part_type, tag_id):
        """One approved pick. Undo successful approach legs before the collection route."""
        self.history, self.last_report = [], None
        if tag_id not in self.robot.poses["coarse_routes"]:
            raise ApproachError(f"No coarse route configured for tag {tag_id}")
        try:
            self.robot.observe()
            sample = self._sample(tag_id, terminal=False)
            for leg in self.robot.poses["coarse_routes"][tag_id]:
                self._check()
                # Approved coarse route is odometry-only; no pixel -> world conversion.
                self.robot.chassis_move({"chassis": leg})
                self._check()
                self.history.append(dict(leg))
            report = self.align(tag_id, previous=sample["seq"])
            self._check()
            self.robot.grasp(part_type)  # Exactly one common, scripted arm/gripper sequence.
            # All approach legs are translations at a fixed heading. Reverse their order
            # and signs so collection_wp still starts at the commanded observe station.
            # This is commanded dead-reckoning, not a measured or verified return.
            for leg in reversed(self.history):
                self._check()
                self.robot.chassis_move({"chassis": {
                    "x_m": -leg["x_m"], "y_m": -leg["y_m"], "z_deg": 0}},
                    speed_mps=self.cfg["speed_mps"], settle_s=self.cfg["settle_s"])
            return report
        except BaseException:
            self.robot.estop()
            raise
