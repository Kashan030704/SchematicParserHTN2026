import copy
import json
import threading
from pathlib import Path

from jsonschema import Draft7Validator

NODE_SCHEMA = json.loads((Path(__file__).parents[1] / "hcp_sdk/hcp_sdk_schema.json").read_text())
NODE_VALIDATOR = Draft7Validator(NODE_SCHEMA)
TYPE_MAP = {"int": "integer", "float": "number", "string": "string", "bool": "boolean"}


def parameters(command):
    props = {}
    for entry in command["params"]:
        name, kind = next(iter(entry.items()))
        if name in props:
            raise ValueError(f"Duplicate parameter {name}")
        props[name] = {"type": TYPE_MAP[kind]}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


class Registry:
    def __init__(self):
        self._lock = threading.RLock()
        self._nodes = {}
        self.revision = 0

    def register(self, definition, owner):
        NODE_VALIDATOR.validate(definition)
        for command in definition["available_commands"].values():
            parameters(command)
        device_id = definition["metadata"]["device_id"]
        if not device_id or device_id == "host":
            raise ValueError("Reserved/empty device id")
        with self._lock:
            if device_id in self._nodes:
                raise ValueError(f"Device {device_id} already connected")
            self._nodes[device_id] = (copy.deepcopy(definition), owner)
            self.revision += 1
        return device_id

    def remove(self, owner):
        with self._lock:
            for key in list(self._nodes):
                if self._nodes[key][1] is owner:
                    del self._nodes[key]
                    self.revision += 1

    def snapshot(self):
        with self._lock:
            return {key: copy.deepcopy(value[0]) for key, value in self._nodes.items()}

    def resolve(self, device_id, action, payload):
        with self._lock:
            definition, owner = self._nodes[device_id]
            command = definition["available_commands"][action]
            Draft7Validator(parameters(command)).validate(payload)
            return owner
