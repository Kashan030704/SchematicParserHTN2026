"""Tag identities + image-space features. No metric pose, intrinsics or extrinsics."""
import math
import threading
import time
from collections import Counter


class Perception:
    def __init__(self, camera, tags):
        self.camera, self.tags = camera, tags
        self.sequence = 0

    def detect_once(self):
        import cv2
        if hasattr(self.camera, "read_sample"):
            frame, sequence, captured = self.camera.read_sample(timeout=3)
        else:
            frame = self.camera.read_cv2_image(timeout=3, strategy="newest")
            self.sequence += 1
            sequence, captured = self.sequence, time.monotonic()
        if frame is None:
            raise RuntimeError("No fresh camera frame")
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
        features = []
        if ids is not None:
            for marker, tag in zip(corners, ids.flatten()):
                if int(tag) not in self.tags:
                    continue
                points = marker.reshape(4, 2).tolist()
                u, v = (sum(p[axis] for p in points) / 4 for axis in (0, 1))
                side = sum(math.dist(points[i], points[(i + 1) % 4]) for i in range(4)) / 4
                features.append({"id": int(tag), "center_px": [u, v], "side_px": side,
                                 "corners_px": points})
        counts = Counter(self.tags[int(tag)] for tag in ids.flatten()
                         if int(tag) in self.tags) if ids is not None else Counter()
        return {"ts": time.time(), "frame": {"seq": sequence, "width": gray.shape[1],
                "height": gray.shape[0], "captured_monotonic": captured}, "tags": features, "rollup": {
            part: {"count": count, "present": True} for part, count in counts.items()
        }}


class EPCamera:
    """Decode DJI's documented TCP H.264 stream using PyAV (modern Python).

    This avoids running competing Python-SDK and plaintext control sessions.
    Only decoding is continuous; AprilTag detection occurs exclusively on request.
    """
    def __init__(self, robot, port=40921):
        self.robot, self.port = robot, port
        self.condition = threading.Condition()
        self.frame = None
        self.frame_time = 0
        self.sequence = 0
        self.error = None
        self.closed = threading.Event()
        self.thread = None

    def start_video_stream(self, display=False):
        if display:
            raise ValueError("Headless camera only")
        import av  # Pi-only optional dependency; never imported by dry-run.
        self.robot._send("stream on")
        self.thread = threading.Thread(target=self._decode, args=(av,), daemon=True)
        self.thread.start()

    def _decode(self, av):
        try:
            with av.open(f"tcp://{self.robot.host}:{self.port}", format="h264",
                         timeout=(3, 3)) as stream:
                for frame in stream.decode(video=0):
                    if self.closed.is_set():
                        break
                    image = frame.reformat(width=640, height=360).to_ndarray(format="bgr24")
                    with self.condition:
                        self.frame, self.frame_time = image, time.monotonic()
                        self.sequence += 1
                        self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()

    def read_sample(self, timeout=3):
        # Wait for a NEW frame after this call, not a pre-motion buffered image.
        started = time.monotonic()
        with self.condition:
            while self.frame_time <= started:
                if self.error or self.closed.is_set():
                    raise RuntimeError(f"Camera unavailable: {self.error or 'closed'}")
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    raise TimeoutError("Timed out waiting for a fresh EP camera frame")
                self.condition.wait(left)
            return self.frame.copy(), self.sequence, self.frame_time

    def read_cv2_image(self, timeout=3, strategy="newest"):
        return self.read_sample(timeout)[0]

    def close(self):
        self.closed.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=4)


class DryRunPerception:
    """Explicit simulation only. Never used as an ingestion substitute."""
    def __init__(self, tags, robot):
        self.tags, self.robot = tags, robot
        self.sequence = 0

    def detect_once(self):
        self.sequence += 1
        cfg = self.robot.poses["visual_approach"]
        # Deliberately simple image response, NOT a camera/physics or grip model.
        x = sum(leg["x_m"] for leg in self.robot.chassis_moves)
        y = sum(leg["y_m"] for leg in self.robot.chassis_moves)
        u = cfg["target_u_px"] + 32 - cfg["lateral_sign"] * 800 * y
        v = cfg["target_v_px"]
        side = cfg["target_side_px"] - 16 + cfg["forward_sign"] * 400 * x
        corners = [[u-side/2, v-side/2], [u+side/2, v-side/2],
                   [u+side/2, v+side/2], [u-side/2, v+side/2]]
        return {"ts": time.time(), "frame": {"seq": self.sequence,
            "width": cfg["frame_width"], "height": cfg["frame_height"],
            "captured_monotonic": time.monotonic()},
            "tags": [{"id": tag, "center_px": [u, v], "side_px": side,
                      "corners_px": corners} for tag in self.tags], "rollup": {
            part: {"count": 1, "present": True} for part in self.tags.values()
        }, "simulated": True}
