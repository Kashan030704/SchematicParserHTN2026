"""Advisory schematic review notes derived from parsed BOM data."""
from __future__ import annotations

import re


def build_schematic_suggestions(bom):
    components = bom.get("components") if isinstance(bom, dict) else None
    if not isinstance(components, list):
        return []

    parts = [_normalize(line) for line in components if isinstance(line, dict)]
    if not parts:
        return ["No parsed component list was available for schematic suggestions."]

    suggestions = [
        "These are advisory checks based on the LLM-parsed BOM only; confirm against the actual schematic nets before changing hardware."
    ]

    low_resistors = []
    for part in parts:
        if part["kind"] != "resistor":
            continue
        ohms = _resistance_ohms(part["value"])
        if ohms is not None and ohms <= 10:
            low_resistors.append((part, ohms))
    if low_resistors:
        refs = ", ".join(_label(part) for part, _ in low_resistors)
        suggestions.append(
            f"Warning sign: {refs} are very low resistance values. If any of these sit between power and ground, they may create a short or high-current path."
        )

    zero_ohm = [part for part, ohms in low_resistors if ohms == 0]
    if zero_ohm:
        suggestions.append(
            f"{', '.join(_label(part) for part in zero_ohm)} look like 0 ohm jumpers; verify they are intended links and not accidental shorts between two nets."
        )

    ics = [part for part in parts if part["kind"] in {"ic", "mcu", "opamp"}]
    small_caps = [part for part in parts if part["kind"] == "capacitor"
                  and (cap := _capacitance_farad(part["value"])) is not None and cap <= 1e-6]
    if ics and small_caps:
        suggestions.append(
            f"{', '.join(_label(part) for part in small_caps)} may be decoupling capacitors for {', '.join(_label(part) for part in ics)}; place them close to the IC power pins."
        )
    elif ics:
        suggestions.append(
            f"{', '.join(_label(part) for part in ics)} may need local 0.1uF decoupling capacitors; none were obvious in the parsed BOM."
        )

    leds = [part for part in parts if part["kind"] == "led"]
    nonzero_resistors = [part for part in parts if part["kind"] == "resistor"
                         and (_resistance_ohms(part["value"]) or 0) > 10]
    if leds and not nonzero_resistors:
        suggestions.append(
            f"{', '.join(_label(part) for part in leds)} may need current-limiting resistors; no nonzero resistor was obvious in the parsed BOM."
        )

    bulk_caps = [part for part in parts if part["kind"] == "capacitor"
                 and (cap := _capacitance_farad(part["value"])) is not None and cap >= 1e-6]
    if bulk_caps:
        suggestions.append(
            f"{', '.join(_label(part) for part in bulk_caps)} are larger capacitors; confirm voltage rating and polarity if they are electrolytic or tantalum."
        )

    if len(suggestions) == 1:
        suggestions.append("No obvious BOM-level short-circuit warning signs were detected; net-level checks still require schematic connectivity.")
    return suggestions


def _normalize(line):
    component_id = line.get("component_id")
    kind = line.get("type", "")
    value = line.get("value", "")
    if isinstance(component_id, str) and ":" in component_id:
        kind, value = component_id.split(":", 1)
    qty = line.get("qty", line.get("quantity", 1))
    if type(qty) is not int or qty < 1:
        qty = 1
    refs = line.get("refdes")
    if not isinstance(refs, list):
        refs = [None] * qty
    clean_refs = [ref.strip() for ref in refs if isinstance(ref, str) and ref.strip()]
    return {"kind": _kind(kind, clean_refs), "value": str(value).strip(), "refs": clean_refs, "qty": qty}


def _kind(kind, refs):
    text = str(kind).strip().lower()
    if text in {"resistor", "r"} or _starts(refs, "R"):
        return "resistor"
    if text in {"capacitor", "cap", "c"} or _starts(refs, "C"):
        return "capacitor"
    if text in {"ic", "u", "chip"} or _starts(refs, "U"):
        return "ic"
    if text in {"mcu", "microcontroller"}:
        return "mcu"
    if text in {"opamp", "op-amp", "operational amplifier"}:
        return "opamp"
    if text == "led" or _starts(refs, "LED"):
        return "led"
    return text


def _starts(refs, prefix):
    return any(ref.upper().startswith(prefix) for ref in refs)


def _label(part):
    if part["refs"]:
        return ", ".join(part["refs"])
    return f"{part['kind']} {part['value']}".strip()


def _resistance_ohms(value):
    text = str(value).strip().lower().replace("\u03a9", "ohm")
    text = re.sub(r"\s+", "", text)
    if text in {"0", "0r", "0ohm", "0ohms", "jumper", "link", "wire"}:
        return 0.0
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(r|ohm|ohms|k|kohm|kohms|m|mohm|mohms)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit in {"k", "kohm", "kohms"}:
        return amount * 1000
    if unit in {"m", "mohm", "mohms"}:
        return amount * 1000000
    return amount


def _capacitance_farad(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(p|n|u|\u00b5|m)?f\s*", str(value), re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower() if match.group(2) else None
    multiplier = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "\u00b5": 1e-6, "m": 1e-3, None: 1.0}[unit]
    return amount * multiplier
