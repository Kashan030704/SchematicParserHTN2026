"""OpenCV AprilTag detector; derived from reference vision/tag_detections.py."""
import argparse
import math
import os
import threading
import time

import cv2
import numpy as np

from config import ROOT, load_hardware
from hcp_sdk.loader import load_client


class TagDetector:
    def __init__(self, config):
        self.homography = np.asarray(config["homography"], dtype=float)
        if self.homography.shape != (3, 3) or not np.isfinite(self.homography).all() or abs(np.linalg.det(self.homography)) < 1e-12:
            raise ValueError("A finite invertible pixel-to-bench homography is required")
        self.image_size = tuple(config["image_size"])
        if len(self.image_size) != 2 or any(type(x) is not int or x <= 0 for x in self.image_size):
            raise ValueError("Provide calibration image width and height")
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(dictionary, params)

    def detect(self, image):
        if (image.shape[1], image.shape[0]) != self.image_size:
            raise ValueError("Camera resolution differs from calibration")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        corners, ids, _ = self.detector.detectMarkers(gray)
        tags = []
        if ids is None:
            return tags
        for points, tag_id in zip(corners, ids.flatten()):
            points = points.reshape(4, 2).astype(np.float64)
            center = points.mean(axis=0)
            # Tag +x axis follows the upper edge; transform heading too.
            edge_point = center + points[1] - points[0]
            samples = np.array([[center, edge_point]], dtype=np.float64)
            world = cv2.perspectiveTransform(samples, self.homography)[0]
            if not np.isfinite(world).all():
                continue
            vector = world[1] - world[0]
            tags.append({"id": int(tag_id), "x": float(world[0, 0]), "y": float(world[0, 1]), "theta": math.atan2(vector[1], vector[0])})
        return sorted(tags, key=lambda tag: tag["id"])


class CameraNode:
    def __init__(self, client, config, image_path=None):
        self.client = client
        self.detector = TagDetector(config)
        self.source = config["source"]
        self.image_path = image_path
        self.hz = config.get("publish_hz", 10.0)
        if type(self.hz) not in (int, float) or not math.isfinite(self.hz) or not 0 < self.hz <= 60:
            raise ValueError("Camera publish_hz must be in (0, 60]")
        self.latest = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        client.register_handler("get_tags", self.get_tags)

    def get_tags(self):
        with self.lock:
            return {"tags": [dict(t) for t in self.latest]}

    def start(self):
        self.client.start()
        threading.Thread(target=self._capture, daemon=True).start()
        return self

    def _capture(self):
        cap = None
        image = None
        try:
            if self.image_path:
                image = cv2.imread(str(self.image_path))
                if image is None:
                    raise ValueError("Cannot read saved camera image")
            else:
                cap = cv2.VideoCapture(self.source)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.detector.image_size[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.detector.image_size[1])
                if not cap.isOpened():
                    raise ValueError("Cannot open camera source")
            while not self.stopped.is_set():
                start = time.monotonic()
                if cap is not None:
                    ok, image = cap.read()
                    if not ok:
                        raise ValueError("Camera capture failed")
                tags = self.detector.detect(image)
                with self.lock:
                    self.latest = tags
                self.client.publish("context/tags", tags=tags)
                self.client.status("ready", visible_tags=len(tags))
                self.stopped.wait(max(0, 1 / self.hz - (time.monotonic() - start)))
        except Exception as exc:
            with self.lock:
                self.latest = []
            self.client.publish("context/tags", tags=[])
            self.client.status("error", error=str(exc))
        finally:
            if cap is not None:
                cap.release()

    def stop(self):
        self.stopped.set()
        self.client.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.getenv("HARDWARE_CONFIG", str(ROOT / "config/hardware.json")))
    parser.add_argument("--host", default=os.getenv("HCP_HOST"), required=not os.getenv("HCP_HOST"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HCP_PORT", "9000")))
    parser.add_argument("--image", help="Saved image at the calibrated resolution")
    parser.add_argument("--output", default=str(ROOT / "out"))
    args = parser.parse_args()
    config = load_hardware(args.config)
    client = load_client("camera", args.host, args.port, args.output)
    node = CameraNode(client, config["camera"], args.image).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()


if __name__ == "__main__":
    main()
