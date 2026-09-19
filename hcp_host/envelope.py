"""Frozen NDJSON wire contract. Parse bytes only after a complete line arrives."""
import json
import math
import uuid

MAX_FRAME_BYTES = 1024 * 1024


class ProtocolError(ValueError):
    pass


def _invalid_constant(value):
    raise ProtocolError(f"Non-finite JSON number: {value}")


def validate_message(value):
    if not isinstance(value, dict) or set(value) != {"action", "id", "device_id", "payload"}:
        raise ProtocolError("Envelope requires action, id, device_id, payload only")
    for key in ("action", "id", "device_id"):
        if not isinstance(value[key], str):
            raise ProtocolError(f"{key} must be a string")
    if not value["action"] or not value["device_id"] or not isinstance(value["payload"], dict):
        raise ProtocolError("Empty action/device_id or non-object payload")
    if not value["id"] and value["action"] != "publish" and not value["action"].startswith("status/"):
        raise ProtocolError("Commands and discovery require a nonempty id")
    return value


def message(action, device_id, payload=None, command_id=None):
    return validate_message({"action": action, "id": uuid.uuid4().hex if command_id is None else command_id,
                             "device_id": device_id, "payload": {} if payload is None else payload})


def frame(value):
    validate_message(value)
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("Frame too large")
    return data + b"\n"


class FrameParser:
    def __init__(self, max_bytes=MAX_FRAME_BYTES):
        self.buffer = bytearray()
        self.max_bytes = max_bytes

    def feed(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer[:] = rest
            if not line or len(line) > self.max_bytes:
                raise ProtocolError("Empty or oversized frame")
            try:
                result.append(validate_message(json.loads(line.decode("utf-8"), parse_constant=_invalid_constant)))
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError(str(exc)) from exc
        if len(self.buffer) > self.max_bytes:
            raise ProtocolError("Unterminated oversized frame")
        return result


def validate_tags(payload):
    tags = payload.get("tags")
    if payload.get("topic") != "context/tags" or not isinstance(tags, list):
        raise ProtocolError("Expected context/tags with a tags array")
    seen = set()
    for tag in tags:
        if not isinstance(tag, dict) or set(tag) != {"id", "x", "y", "theta"}:
            raise ProtocolError("Invalid tag shape")
        if type(tag["id"]) is not int or tag["id"] < 0 or tag["id"] in seen:
            raise ProtocolError("Invalid or duplicate tag id")
        seen.add(tag["id"])
        if any(type(tag[k]) not in (int, float) or not math.isfinite(tag[k]) for k in ("x", "y", "theta")):
            raise ProtocolError("Tag coordinates must be finite numbers")
    return tags
