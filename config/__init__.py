"""Validated event configuration. No guessed coordinates or embedded credentials."""
import math
from pathlib import Path
import yaml


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(path):
    value = yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML object")
    return value


def number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    return value


def load_tag_map(path):
    data = load_yaml(path)
    tags = data.get("tags")
    if data.get("family") != "tag36h11" or not isinstance(tags, dict) or not tags:
        raise ValueError("Require tag36h11 and a nonempty tags mapping")
    for tag, part in tags.items():
        if type(tag) is not int or not 0 <= tag < 587 or not isinstance(part, str) or not part.strip():
            raise ValueError("Tags require integer IDs 0..586 and nonempty part-type labels")
    if len(set(tags.values())) != len(tags):
        raise ValueError("Use one AprilTag ID per part type")
    return tags


def load_group_catalog(path):
    """Optional classification metadata; old exact-label tag maps still work."""
    data = load_yaml(path)
    if "groups" not in data:
        return None
    tags = load_tag_map(path)
    groups = data["groups"]
    if not isinstance(groups, dict) or set(groups) != set(tags.values()):
        raise ValueError("Group definitions must exactly match the registered tag labels")
    catalog = []
    for tag_id, name in tags.items():
        entry = groups[name]
        if (not isinstance(entry, dict) or set(entry) != {"description"}
                or not isinstance(entry["description"], str) or not 1 <= len(entry["description"].strip()) <= 2000):
            raise ValueError(f"Group {name} requires a nonempty description")
        catalog.append({"name": name, "tag_id": tag_id, "description": entry["description"].strip()})
    settings = data.get("classification", {"policy": "closest"})
    if not isinstance(settings, dict) or settings.get("policy") not in {"closest", "fallback"}:
        raise ValueError("Classification policy must be closest or fallback")
    if set(settings) - {"policy", "fallback_group"}:
        raise ValueError("Unknown classification setting")
    fallback = settings.get("fallback_group")
    if settings["policy"] == "fallback" and fallback not in groups:
        raise ValueError("fallback_group must be a registered group")
    if settings["policy"] == "closest" and fallback is not None:
        raise ValueError("Closest-group classification does not use a fallback_group")
    return {"groups": catalog, **settings}


def load_poses(path, *, hardware=False):
    data = load_yaml(path)
    if data.get("arm_units") != "mm":
        raise ValueError("poses.arm_units must explicitly be mm")
    if hardware and data.get("calibrated") is not True:
        raise ValueError("Calibrate poses and set calibrated: true before enabling hardware")
    def arm(pose, name):
        if not isinstance(pose, dict) or set(pose) != {"arm"} or set(pose["arm"]) != {"x", "y"}:
            raise ValueError(f"{name} requires arm: {{x, y}} in mm")
        for axis, value in pose["arm"].items():
            # Broad input guard, NOT a certified workspace; local calibration is mandatory.
            number(value, f"{name}.{axis}", -500, 500)
    for name in ("observe_pose", "grab_pose", "drop_pose"):
        arm(data.get(name), name)
    if "grab_poses" in data:
        raise ValueError("Retired grab_poses: use one scripted grab_pose after visual alignment")
    for name in ("collection_wp", "home_wp"):
        wp = data.get(name, {}).get("chassis", {})
        if set(wp) != {"x_m", "y_m", "z_deg"}:
            raise ValueError(f"{name} requires chassis: {{x_m, y_m, z_deg}}")
        for axis in ("x_m", "y_m"):
            number(wp[axis], f"{name}.{axis}", -5, 5)
        number(wp["z_deg"], f"{name}.z_deg", -1800, 1800)
    level = data.get("gripper_level")
    if type(level) is not int or not 1 <= level <= 4:
        raise ValueError("gripper_level must be an integer 1..4")
    for name in ("arm_settle_s", "gripper_settle_s", "chassis_settle_s"):
        number(data.get(name), name, 0.1, 120)
    number(data.get("chassis_speed_mps"), "chassis_speed_mps", 0.01, 0.5)
    number(data.get("chassis_speed_dps"), "chassis_speed_dps", 1, 45)
    validate_visual_approach(data, hardware=hardware)
    return data


def validate_visual_approach(data, *, hardware=False):
    """Image-space setpoints/control bounds; no camera matrices or metric tag poses."""
    cfg = data.get("visual_approach")
    if not isinstance(cfg, dict):
        raise ValueError("visual_approach configuration is required")
    if hardware and cfg.get("taught") is not True:
        raise ValueError("Teach/check visual target and control directions; set visual_approach.taught: true")
    def integer(name, low, high):
        value = cfg.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"visual_approach.{name} must be an integer {low}..{high}")
        return value
    width, height = integer("frame_width", 160, 1920), integer("frame_height", 120, 1080)
    for name, maximum in (("target_u_px", width), ("target_v_px", height)):
        number(cfg.get(name), name, 1, maximum - 1)
    for name in ("u_tolerance_px", "v_tolerance_px", "size_tolerance_px"):
        number(cfg.get(name), name, 1, 40)
    target = number(cfg.get("target_side_px"), "target_side_px", 10, min(width, height) / 2)
    number(cfg.get("min_side_px"), "min_side_px", 4, target - cfg["size_tolerance_px"])
    number(cfg.get("max_side_px"), "max_side_px", target + cfg["size_tolerance_px"], min(width, height))
    number(cfg.get("edge_margin_px"), "edge_margin_px", 1, 40)
    number(cfg.get("max_edge_ratio"), "max_edge_ratio", 1.05, 2)
    for axis in ("forward_sign", "lateral_sign"):
        if type(cfg.get(axis)) is not int or cfg[axis] not in (-1, 1):
            raise ValueError(f"{axis} must be +1 or -1; check direction on hardware")
    for name in ("forward_step_m", "lateral_step_m"):
        number(cfg.get(name), name, 0.001, 0.02)
    number(cfg.get("speed_mps"), "speed_mps", 0.01, min(0.1, data["chassis_speed_mps"]))
    number(cfg.get("settle_s"), "settle_s", 0.05, 2)
    number(cfg.get("max_travel_m"), "max_travel_m", 0.01, 0.5)
    number(cfg.get("max_frame_age_s"), "max_frame_age_s", 0.05, 1)
    number(cfg.get("timeout_s"), "timeout_s", 1, 120)
    number(cfg.get("min_improvement_px"), "min_improvement_px", 0.1, 5)
    integer("max_steps", 1, 60)
    integer("stable_frames", 2, 10)
    integer("max_stalled_steps", 1, 10)
    routes = data.get("coarse_routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError("coarse_routes must map tag IDs to lists of relative chassis legs")
    for tag_id, legs in routes.items():
        if type(tag_id) is not int or not 0 <= tag_id < 587 or not isinstance(legs, list) or len(legs) > 4:
            raise ValueError("Invalid coarse route tag ID or legs (maximum 4)")
        travel = 0
        for leg in legs:
            if not isinstance(leg, dict) or set(leg) != {"x_m", "y_m", "z_deg"}:
                raise ValueError("Each coarse leg requires x_m, y_m, z_deg")
            number(leg["x_m"], "coarse x_m", -1, 1)
            number(leg["y_m"], "coarse y_m", -1, 1)
            if type(leg["z_deg"]) not in (int, float) or leg["z_deg"] != 0:
                raise ValueError("Coarse approach keeps heading fixed: z_deg must be 0")
            travel += math.hypot(leg["x_m"], leg["y_m"])
        if travel > 1:
            raise ValueError("Coarse approach is limited to 1 meter of commanded travel per tag")
    return cfg
