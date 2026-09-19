"""Pure BOM-to-target-angle planning; no hardware or feedback dependencies."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from palette import MissingComponent, Palette, PlaceTarget


class PlanningError(ValueError):
    pass


@dataclass(frozen=True)
class PickStep:
    component_id: str
    slot_id: str
    approach_angles: Mapping[str, float]
    grasp_angles: Mapping[str, float]
    place_target: PlaceTarget

    def to_dict(self):
        return {"component_id": self.component_id, "slot_id": self.slot_id,
                "approach_angles": dict(self.approach_angles), "grasp_angles": dict(self.grasp_angles),
                "place_target": self.place_target.to_dict()}


@dataclass(frozen=True)
class PickPlan:
    steps: tuple[PickStep, ...]

    def to_dict(self):
        return {"steps": [step.to_dict() for step in self.steps]}


def bom_to_plan(bom_json, palette: Palette) -> PickPlan:
    if isinstance(bom_json, str):
        try:
            bom_json = json.loads(bom_json)
        except json.JSONDecodeError as exc:
            raise PlanningError(f"Invalid BOM JSON: {exc}") from exc
    if not isinstance(bom_json, dict) or not isinstance(bom_json.get("components"), list):
        raise PlanningError("BOM must contain a components array")
    steps, unresolved = [], []
    for index, line in enumerate(bom_json["components"], 1):
        prefix = f"BOM line {index}"
        if not isinstance(line, dict):
            raise PlanningError(f"{prefix}: expected an object")
        component_id = line.get("component_id")
        # Adapter for the existing ingestion contract, without changing ingestion.
        if component_id is None:
            if not all(isinstance(line.get(k), str) and line[k].strip() for k in ("type", "value")):
                raise PlanningError(f"{prefix}: provide component_id, or nonempty type and value")
            component_id = f"{line['type']}:{line['value']}"
        if not isinstance(component_id, str) or not component_id.strip():
            raise PlanningError(f"{prefix}: component_id must be a nonempty string")
        qty = line.get("qty", line.get("quantity"))
        if "qty" in line and "quantity" in line:
            raise PlanningError(f"{prefix}: use qty OR quantity, not both")
        if type(qty) is not int or qty <= 0:
            raise PlanningError(f"{prefix} ({component_id}): qty/quantity must be a positive integer")
        try:
            slot = palette.lookup(component_id)
        except MissingComponent:
            unresolved.append(f"line {index}: {component_id!r} (qty {qty})")
            continue
        for _ in range(qty):
            steps.append(PickStep(component_id, slot.id, dict(slot.approach), dict(slot.grasp),
                                  PlaceTarget(dict(palette.place_target.approach), dict(palette.place_target.drop))))
    if unresolved:
        raise MissingComponent("Unresolved components; NO picks will execute: " + "; ".join(unresolved))
    return PickPlan(tuple(steps))
