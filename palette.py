"""Validated fixed-palette contract. No camera, transforms, IK or hardware imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from config.so100 import ArmConfig, ConfigurationError


class PaletteError(ValueError):
    pass


class MissingComponent(PaletteError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently replacing a waypoint."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise PaletteError("YAML mapping keys must be strings")
        if key in result:
            raise PaletteError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _keys(value, required, label, optional=()):
    if not isinstance(value, dict):
        raise PaletteError(f"{label}: expected a mapping")
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        raise PaletteError(f"{label}: missing keys {sorted(missing)}; unknown keys {sorted(extra)}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise PaletteError(f"{label}: expected a nonempty string")
    return value


@dataclass(frozen=True)
class Gripper:
    open_deg: float
    closed_deg: float


@dataclass(frozen=True)
class PlaceTarget:
    approach: Mapping[str, float]
    drop: Mapping[str, float]

    def to_dict(self):
        return {"approach": dict(self.approach), "drop": dict(self.drop)}


@dataclass(frozen=True)
class Slot:
    id: str
    label: str
    components: tuple[str, ...]
    approach: Mapping[str, float]
    grasp: Mapping[str, float]

    def to_dict(self):
        return {"id": self.id, "label": self.label, "components": list(self.components),
                "approach": dict(self.approach), "grasp": dict(self.grasp)}


@dataclass(frozen=True)
class Palette:
    home: Mapping[str, float]
    gripper: Gripper
    place_target: PlaceTarget
    slots: tuple[Slot, ...]
    calibrated: bool = False
    _config: ArmConfig = field(default_factory=ArmConfig, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data, config: ArmConfig | None = None):
        config = config or ArmConfig()
        _keys(data, ("home", "gripper", "place_target", "slots"), "palette", ("calibrated",))
        _keys(data["gripper"], ("open_deg", "closed_deg"), "gripper")
        _keys(data["place_target"], ("approach", "drop"), "place_target")
        calibrated = data.get("calibrated", False)
        if type(calibrated) is not bool:
            raise PaletteError("calibrated must be a boolean")
        if not isinstance(data["slots"], list) or not data["slots"]:
            raise PaletteError("slots must be a nonempty list")
        slots, ids, components = [], set(), set()
        for i, row in enumerate(data["slots"]):
            label = f"slots[{i}]"
            _keys(row, ("id", "label", "components", "approach", "grasp"), label)
            slot_id = _text(row["id"], f"{label}.id")
            if slot_id in ids:
                raise PaletteError(f"Duplicate slot id: {slot_id}")
            ids.add(slot_id)
            _text(row["label"], f"{label}.label")
            if not isinstance(row["components"], list) or not row["components"]:
                raise PaletteError(f"{label}.components must be a nonempty list")
            for component in row["components"]:
                _text(component, f"{label}.components")
                if component in components:
                    raise PaletteError(f"Component {component!r} has more than one mapping")
                components.add(component)
            slots.append(Slot(slot_id, row["label"], tuple(row["components"]),
                              config.validate_waypoint(row["approach"], f"{label}.approach"),
                              config.validate_waypoint(row["grasp"], f"{label}.grasp")))
        opened = config.validate_joint("gripper", data["gripper"]["open_deg"])
        closed = config.validate_joint("gripper", data["gripper"]["closed_deg"])
        if opened == closed:
            raise PaletteError("gripper.open_deg and gripper.closed_deg must differ")
        return cls(config.validate_waypoint(data["home"], "home"), Gripper(opened, closed),
                   PlaceTarget(config.validate_waypoint(data["place_target"]["approach"], "place_target.approach"),
                               config.validate_waypoint(data["place_target"]["drop"], "place_target.drop")),
                   tuple(slots), calibrated, config)

    def lookup(self, component_id: str) -> Slot:
        for slot in self.slots:
            if component_id in slot.components:
                return slot
        raise MissingComponent(f"No palette slot for component {component_id!r}")

    def validate_all_angles_within_limits(self, config: ArmConfig | None = None):
        # Re-validate even if a caller modified a nested waypoint after loading.
        Palette.from_dict(self.to_dict(), config or self._config)

    def to_dict(self):
        return {"calibrated": self.calibrated, "home": dict(self.home),
                "gripper": {"open_deg": self.gripper.open_deg, "closed_deg": self.gripper.closed_deg},
                "place_target": self.place_target.to_dict(), "slots": [s.to_dict() for s in self.slots]}


def load_palette(path: str | Path = "palette.yaml", config: ArmConfig | None = None) -> Palette:
    try:
        data = yaml.load(Path(path).read_text(), Loader=_UniqueKeyLoader)
        return Palette.from_dict(data, config)
    except (yaml.YAMLError, ConfigurationError) as exc:
        raise PaletteError(f"{path}: {exc}") from exc
