"""Print a bench-plane calibration from measured correspondences."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def calibrate(points):
    pixels = np.asarray(points["pixel_points"], dtype=float)
    world = np.asarray(points["world_points_m"], dtype=float)
    if pixels.ndim != 2 or pixels.shape[1] != 2 or len(pixels) < 4 or pixels.shape != world.shape or not np.isfinite(pixels).all() or not np.isfinite(world).all():
        raise ValueError("Supply at least four matching finite XY pixel/world points")
    size = points["image_size"]
    if len(size) != 2 or any(type(x) is not int or x <= 0 for x in size):
        raise ValueError("Supply calibration image width/height")
    if any(np.linalg.matrix_rank(np.column_stack([p, np.ones(len(p))])) < 3 for p in (pixels, world)):
        raise ValueError("Calibration points must not be collinear")
    matrix, _ = cv2.findHomography(pixels, world, method=0)
    if matrix is None or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError("Degenerate calibration")
    return {"image_size": size, "homography": matrix.tolist()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", required=True)
    args = parser.parse_args()
    print(json.dumps(calibrate(json.loads(Path(args.points).read_text())), indent=2))
