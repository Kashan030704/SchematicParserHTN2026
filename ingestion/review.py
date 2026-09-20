"""The advisory review contract is separate from the BOM and motion contracts."""
import copy
import math

from jsonschema import Draft7Validator


def text(limit=1600):
    return {"type": "string", "minLength": 1, "maxLength": limit}


REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "findings", "limitations"],
    "properties": {
        "summary": text(),
        "limitations": {"type": "array", "maxItems": 8, "items": text()},
        "findings": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["title", "severity", "components", "evidence", "consequence",
                             "recommendation", "estimated_damage_cost"],
                "properties": {
                    "title": text(200),
                    "severity": {"type": "string", "enum": ["info", "low", "medium", "high"]},
                    "components": {"type": "array", "maxItems": 30, "items": text(100)},
                    "evidence": text(),
                    "consequence": text(),
                    "recommendation": text(),
                    "estimated_damage_cost": {
                        "type": "object", "additionalProperties": False,
                        "required": ["currency", "low", "high", "basis"],
                        "properties": {
                            "currency": {"type": "string", "enum": ["CAD"]},
                            "low": {"type": ["number", "null"], "minimum": 0, "maximum": 1000000},
                            "high": {"type": ["number", "null"], "minimum": 0, "maximum": 1000000},
                            "basis": text(),
                        },
                    },
                },
            },
        },
    },
}


def validate_review(value):
    Draft7Validator(REVIEW_SCHEMA).validate(value)
    for finding in value["findings"]:
        cost = finding["estimated_damage_cost"]
        low, high = cost["low"], cost["high"]
        if (low is None) != (high is None):
            raise ValueError("Damage cost needs both bounds, or two nulls when unknown")
        if low is not None and (not math.isfinite(low) or not math.isfinite(high) or low > high):
            raise ValueError("Damage cost must be a finite, ordered range")
    return copy.deepcopy(value)
