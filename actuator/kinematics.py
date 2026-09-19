"""Analytic IK: base yaw + shoulder pitch + elbow pitch; no wrist."""
import math


def finite_vector(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length or any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(f"{name} must contain {length} finite numbers")
    return value


class Kinematics:
    def __init__(self, config):
        self.config = config
        self.links = finite_vector(config["links_m"], 2, "links_m")
        self.base = finite_vector(config["base_world_m"], 3, "base_world_m")
        self.offsets = finite_vector(config["servo_offsets_deg"], 3, "servo_offsets_deg")
        self.directions = finite_vector(config["servo_directions"], 3, "servo_directions")
        self.yaw = finite_vector([config["base_yaw_rad"]], 1, "base_yaw_rad")[0]
        self.elbow_sign = config["elbow_sign"]
        if min(self.links) <= 0 or self.elbow_sign not in (-1, 1) or any(x not in (-1, 1) for x in self.directions):
            raise ValueError("Invalid links, servo directions, or elbow branch")
        self.limits = config["servo_limits_deg"]
        if len(self.limits) != 4:
            raise ValueError("Four servo limits required")
        for pair in self.limits:
            lo, hi = finite_vector(pair, 2, "servo limit")
            if not 0 <= lo < hi <= 180:
                raise ValueError("Servo limits must lie within 0..180 degrees")
        self.check_angles(config["home_deg"])
        for name in ("gripper_open_deg", "gripper_closed_deg"):
            angle = finite_vector([config[name]], 1, name)[0]
            if not self.limits[3][0] <= angle <= self.limits[3][1]:
                raise ValueError(f"{name} exceeds limits")
        if config["home_deg"][3] != config["gripper_open_deg"]:
            raise ValueError("Home pose must use the open gripper position")
        for name in ("move_ms", "settle_ms"):
            if type(config[name]) is not int or not 0 <= config[name] <= 30000:
                raise ValueError(f"Invalid {name}")
        if config["move_ms"] == 0:
            raise ValueError("Movement duration must be positive")
        finite_vector([config["clearance_z_m"]], 1, "clearance_z_m")
        if "belt" not in config["zones"] or not config["bins"]:
            raise ValueError("Configure belt zone and pickup bins")
        for pose in config["zones"].values():
            finite_vector(pose, 3, "zone")
            if pose[2] >= config["clearance_z_m"]:
                raise ValueError("Clearance height must exceed zone height")
            self.solve(pose)
            self.solve([pose[0], pose[1], config["clearance_z_m"]])
        for bin_config in config["bins"].values():
            finite_vector(bin_config["offset_m"], 2, "bin offset")
            height = finite_vector([bin_config["pickup_z_m"]], 1, "pickup height")[0]
            if height >= config["clearance_z_m"]:
                raise ValueError("Clearance height must exceed pickup height")

    def check_angles(self, angles):
        finite_vector(angles, 4, "joint angles")
        if any(not lo <= angle <= hi for angle, (lo, hi) in zip(angles, self.limits)):
            raise ValueError("Joint target exceeds calibrated servo limits")
        return list(angles)

    def solve(self, world, gripper=None):
        x, y, z = finite_vector(world, 3, "world target")
        dx, dy, dz = x - self.base[0], y - self.base[1], z - self.base[2]
        radius = math.hypot(dx, dy)
        l1, l2 = self.links
        if radius < 1e-8:
            raise ValueError("Target lies on base yaw singularity")
        cosine = (radius * radius + dz * dz - l1 * l1 - l2 * l2) / (2 * l1 * l2)
        if cosine < -1 - 1e-9 or cosine > 1 + 1e-9:
            raise ValueError("Unreachable world target")
        elbow = self.elbow_sign * math.acos(max(-1, min(1, cosine)))
        shoulder = math.atan2(dz, radius) - math.atan2(l2 * math.sin(elbow), l1 + l2 * math.cos(elbow))
        yaw = (math.atan2(dy, dx) - self.yaw + math.pi) % (2 * math.pi) - math.pi
        angles = [offset + direction * math.degrees(angle) for offset, direction, angle in zip(self.offsets, self.directions, (yaw, shoulder, elbow))]
        angles.append(self.config["gripper_open_deg"] if gripper is None else gripper)
        return self.check_angles(angles)
