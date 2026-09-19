"""Event configuration is required explicitly; sample values never enable motion."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_hardware(path):
    value = json.loads(Path(path).read_text())
    missing = []

    def walk(item, location):
        if item is None or (isinstance(item, str) and (not item or "<CONFIRM" in item)):
            missing.append(location)
        elif isinstance(item, dict):
            for key, child in item.items():
                walk(child, f"{location}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{location}[{index}]")

    walk(value, "hardware")
    if missing:
        raise ValueError("Event configuration required: " + ", ".join(missing))
    for key in ("serial", "arm", "belt", "camera"):
        if not isinstance(value.get(key), dict):
            raise ValueError(f"Missing configuration section: {key}")
    if not isinstance(value["serial"].get("port"), str) or type(value["serial"].get("baud")) is not int or value["serial"]["baud"] <= 0:
        raise ValueError("Provide the measured serial port and firmware baud")
    belt = value["belt"]
    for name in ("neutral_us", "forward_us"):
        if type(belt.get(name)) is not int or not 500 <= belt[name] <= 2500:
            raise ValueError(f"Invalid belt {name}")
    if belt["neutral_us"] == belt["forward_us"]:
        raise ValueError("Belt neutral and forward must differ")
    for name in ("delivery_duration_s", "max_duration_s"):
        if type(belt.get(name)) not in (int, float) or not math.isfinite(belt[name]) or not 0 < belt[name] <= 60:
            raise ValueError(f"Invalid belt {name}")
    if belt["delivery_duration_s"] > belt["max_duration_s"]:
        raise ValueError("Delivery duration exceeds maximum")
    return value
