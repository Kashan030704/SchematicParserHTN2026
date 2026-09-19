"""Extract component records from legacy KiCad and EAGLE XML schematics."""
import re
import shlex
from pathlib import Path
from xml.etree import ElementTree

UNSUPPORTED = "Unsupported .sch format. Use legacy KiCad or EAGLE XML, or export the schematic as PDF/PNG."
TYPES = {"R": "resistor", "C": "capacitor", "L": "inductor", "D": "diode",
         "Q": "transistor", "U": "ic", "IC": "ic", "J": "connector",
         "P": "connector", "SW": "switch", "F": "fuse", "Y": "crystal"}


def _kicad(text):
    lines = [line.strip() for line in text.splitlines()]
    if "$Sheet" in lines or any(line.startswith("AR ") for line in lines):
        raise ValueError("Hierarchical KiCad schematics require an exported PDF or a flattened schematic.")
    if not lines or lines[-1] != "$EndSCHEMATC":
        raise ValueError("Incomplete KiCad schematic")
    fields = None
    for line in lines:
        if line == "$Comp":
            if fields is not None:
                raise ValueError("Malformed KiCad component block")
            fields = {}
        elif line == "$EndComp":
            if fields is None or "0" not in fields or "1" not in fields:
                raise ValueError("KiCad component is missing its reference or value")
            yield fields["0"], fields["1"]
            fields = None
        elif fields is not None and line.startswith("F "):
            tokens = shlex.split(line)
            if len(tokens) < 3:
                raise ValueError("Malformed KiCad component field")
            fields[tokens[1]] = tokens[2]
    if fields is not None:
        raise ValueError("Incomplete KiCad component block")


def _eagle(text):
    # Standard EAGLE files reference eagle.dtd; do not resolve it or custom entities.
    if "<!ENTITY" in text.upper():
        raise ValueError("XML entity declarations are not supported")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("Malformed EAGLE XML schematic") from exc
    schematic = root.find("./drawing/schematic")
    if root.tag != "eagle" or schematic is None:
        raise ValueError(UNSUPPORTED)
    if schematic.findall("./modules/module") or schematic.findall(".//moduleinst"):
        raise ValueError("EAGLE modules require an exported PDF or a flattened schematic.")
    for part in schematic.findall("./parts/part"):
        yield part.get("name", ""), part.get("value") or part.get("deviceset", "")


def parse_sch(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(UNSUPPORTED) from exc
    if text.startswith("EESchema Schematic File Version"):
        records = _kicad(text)
        allow_units = True
    elif text.startswith("<"):
        records = _eagle(text)
        allow_units = False
    else:
        raise ValueError(UNSUPPORTED)
    refs = {}
    grouped = {}
    for ref, value in records:
        if ref.startswith("#"):
            continue  # KiCad power symbols are not physical components.
        if not ref or "?" in ref or not value or value == "~":
            raise ValueError("Every schematic component needs an annotated reference and value")
        prefix = re.match(r"[A-Za-z]+", ref)
        kind = TYPES.get(prefix[0].upper(), "component") if prefix else "component"
        key = (kind, value)
        if ref in refs:
            if not allow_units or refs[ref] != key:
                raise ValueError(f"Conflicting or duplicate component reference: {ref}")
            continue  # Multiple KiCad units belong to one physical package.
        refs[ref] = key
        grouped.setdefault(key, []).append(ref)
    if not grouped:
        raise ValueError("No physical components found in schematic")
    return {"components": [dict(type=kind, value=value, quantity=len(refs), refdes=refs)
                           for (kind, value), refs in grouped.items()]}
