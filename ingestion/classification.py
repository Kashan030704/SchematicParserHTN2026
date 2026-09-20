"""Complete component-to-group assignments; quantities and tag IDs stay local."""
from jsonschema import Draft7Validator

from ingestion.parse import validate_bom


def classification_schema(bom, catalog):
    return {
        "type": "object", "additionalProperties": False, "required": ["assignments"],
        "properties": {"assignments": {
            "type": "array", "minItems": len(bom), "maxItems": len(bom),
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["component", "group", "confidence", "reason"],
                "properties": {
                    "component": {"type": "string", "enum": list(bom)},
                    "group": {"type": "string", "enum": [g["name"] for g in catalog["groups"]]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1200},
                },
            },
        }},
    }


def group_components(bom, value, catalog):
    """Reject omissions, duplicates, and invented groups before summing the original counts."""
    bom = validate_bom(bom)
    Draft7Validator(classification_schema(bom, catalog)).validate(value)
    assignments = value["assignments"]
    by_component = {row["component"]: row for row in assignments}
    if len(by_component) != len(assignments) or set(by_component) != set(bom):
        raise ValueError("Classification must assign every original component exactly once")
    groups = {group["name"]: group for group in catalog["groups"]}
    grouped, rows = {}, []
    for component, quantity in bom.items():
        row = by_component[component]
        if not row["reason"].strip():
            raise ValueError("Classification needs a reason for each assignment")
        group = row["group"]
        grouped[group] = grouped.get(group, 0) + quantity
        rows.append({**row, "quantity": quantity, "tag_id": groups[group]["tag_id"]})
    return {"bom": validate_bom(grouped), "component_classification": rows}


def already_grouped(bom, catalog):
    """Explicit group JSON remains usable without an API key or another model call."""
    if not set(bom) <= {group["name"] for group in catalog["groups"]}:
        return None
    return group_components(bom, {"assignments": [
        {"component": name, "group": name, "confidence": "high",
         "reason": "The input already names a registered group."} for name in bom
    ]}, catalog)
