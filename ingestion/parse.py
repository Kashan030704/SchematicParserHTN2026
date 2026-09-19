"""Thin schematic renderer and strict BOM validation. No motion or hardcoded BOM."""
import base64
import json
from pathlib import Path
from jsonschema import Draft7Validator

BOM_SCHEMA = json.loads((Path(__file__).parent / "bom_schema.json").read_text())


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


def parse_schematic(path, client, *, part_types=()):
    import pymupdf
    images = []
    with pymupdf.open(path) as document:
        if document.is_encrypted or not 1 <= len(document) <= 8:
            raise ValueError("Provide an unencrypted schematic PDF/image with 1–8 pages")
        for page in document:
            scale = min(200 / 72, 2400 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            images.append("data:image/png;base64," + base64.b64encode(pixmap.tobytes("png")).decode())
    return validate_bom(client.extract_bom(images, BOM_SCHEMA, part_types=part_types))


# Kept for callers of the existing ingestion entry point.
parse_pdf = parse_schematic


if __name__ == "__main__":
    import argparse
    from orchestrator.baseten_client import BasetenClient
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--expected")
    args = parser.parse_args()
    bom = parse_schematic(args.pdf, BasetenClient())
    print(json.dumps(bom, indent=2))
    if args.expected and bom != validate_bom(strict_json(Path(args.expected).read_text())):
        raise SystemExit("Extracted BOM differs from hand-checked ground truth")
