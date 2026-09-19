"""Shared palette identity and conservative open-loop command time allowance."""
import hashlib
import json
import math


def palette_id(palette):
    data = palette.to_dict()
    # The node alone enforces local tuning/arming. Identity binds every waypoint,
    # gripper angle and component-to-slot mapping, independent of the local flag.
    data.pop("calibrated", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def command_timeout(config):
    steps = max(math.ceil((c.max_angle - c.min_angle) / config.step_cap(j))
                for j, c in config.channels.items())
    seconds = max(30, math.ceil(12 * (steps * config.step_delay_ms / 1000
                                    + max(c.settle_ms for c in config.channels.values()) / 1000) + 10))
    if seconds > 3600:
        raise ValueError("Configured slew is too slow: worst-case command allowance exceeds one hour")
    return seconds
