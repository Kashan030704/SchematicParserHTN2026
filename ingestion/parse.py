"""Thin schematic renderer and strict BOM validation. No motion or hardcoded BOM."""
import base64
import json
from pathlib import Path
from jsonschema import Draft7Validator

BOM_SCHEMA = json.loads((Path(__file__).parent / "bom_schema.json").read_text())
SCHEMATIC_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".sch"}
SCHEMATIC_FORMATS = "PDF, JPEG, PNG, BMP, TIFF, or SCH (legacy KiCad / EAGLE XML)"


def validate_bom(bom):
    Draft7Validator(BOM_SCHEMA).validate(bom)
    # JSON Schema considers 1.0 an integer; the wire contract requires JSON integers.
    for part_type, quantity in bom.items():
        if not part_type.strip() or type(quantity) is not int:
            raise ValueError("BOM must map nonempty part types to positive integer quantities")
    return dict(bom)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def component_records_to_bom(records, part_types=()):
    """Keep every SCH component; resolve only exact known labels, never guess cup identity."""
    known, bom = set(part_types), {}
    for part in records["components"]:
        canonical = f"{part['type']}:{part['value']}"
        refs = part["refdes"]
        # Prefer an exact type:value cup. If every ref is explicitly a cup label,
        # preserve those instead. Otherwise expose an unmatched canonical label
        # for the human to map; do not silently drop or partly map a grouped line.
        if canonical not in known and all(ref in known for ref in refs):
            for ref in refs:
                bom[ref] = bom.get(ref, 0) + 1
        else:
            bom[canonical] = bom.get(canonical, 0) + part["quantity"]
    return validate_bom(bom)


def parse_schematic(path, client=None, *, part_types=(), details=False, review=False):
    if review and not details:
        raise ValueError("Schematic review requires details=True")
    suffix = Path(path).suffix.lower()
    if suffix not in SCHEMATIC_EXTENSIONS:
        raise ValueError("Provide a " + SCHEMATIC_FORMATS + " schematic")
    if suffix == ".sch":
        from ingestion.sch import parse_sch
        records = parse_sch(path)
        bom = component_records_to_bom(records, part_types)
        return {"bom": bom, "component_details": records["components"], "source": "sch"} if details else bom
    if client is None:
        raise ValueError("PDF/image ingestion requires a configured Baseten client")
    import pymupdf
    images = []
    with pymupdf.open(path) as document:
        if document.is_pdf != (suffix == ".pdf"):
            raise ValueError("Schematic file contents do not match the file extension")
        if document.is_encrypted or not 1 <= len(document) <= 8:
            raise ValueError("Provide an unencrypted schematic PDF/image with 1–8 pages")
        for page in document:
            scale = min(200 / 72, 2400 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            images.append("data:image/png;base64," + base64.b64encode(pixmap.tobytes("png")).decode())
    bom = validate_bom(client.extract_bom(images, BOM_SCHEMA, part_types=part_types))
    if not details:
        return bom
    result = {"bom": bom, "component_details": [], "source": "baseten"}
    if review:
        from ingestion.review import validate_review
        try:
            # Reuse the rendered pages. A failed advisory call must not discard a valid BOM.
            result["schematic_review"] = validate_review(client.review_schematic(images, dict(bom)))
            result["schematic_review_model"] = getattr(client, "vision_model", None)
        except Exception as exc:
            result["schematic_review_error"] = str(exc)[:1200]
    return result


# Kept for callers of the existing ingestion entry point.
parse_pdf = parse_schematic


if __name__ == "__main__":
    import argparse
    from orchestrator.baseten_client import BasetenClient
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--expected")
    args = parser.parse_args()
    client = None if Path(args.pdf).suffix.lower() == ".sch" else BasetenClient()
    bom = parse_schematic(args.pdf, client)
    print(json.dumps(bom, indent=2))
    if args.expected and bom != validate_bom(strict_json(Path(args.expected).read_text())):
        raise SystemExit("Extracted BOM differs from hand-checked ground truth")
