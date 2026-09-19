import base64
import json
from pathlib import Path

from jsonschema import Draft7Validator

BOM_SCHEMA = json.loads((Path(__file__).parent / "bom_schema.json").read_text())
SCHEMATIC_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".sch"}


def validate_bom(bom):
    Draft7Validator(BOM_SCHEMA).validate(bom)
    refs = set()
    for part in bom["components"]:
        if type(part["quantity"]) is not int or part["quantity"] <= 0:
            raise ValueError("BOM quantities must be positive integers")
        if not part["type"] or not part["value"]:
            raise ValueError("BOM type and value must be nonempty")
        if len(part["refdes"]) != part["quantity"]:
            raise ValueError("BOM quantity must match its reference designators")
        for ref in part["refdes"]:
            if not ref or ref in refs:
                raise ValueError("BOM reference designators must be nonempty and unique")
            refs.add(ref)
    return bom


def parse_schematic(path, client):
    import pymupdf
    suffix = Path(path).suffix.lower()
    if suffix not in SCHEMATIC_EXTENSIONS:
        raise ValueError("Provide a PDF, JPEG, PNG, BMP, TIFF, or SCH schematic")
    if suffix == ".sch":
        from ingestion.sch import parse_sch
        return validate_bom(parse_sch(path))
    images = []
    with pymupdf.open(path) as document:
        if document.is_pdf != (suffix == ".pdf"):
            raise ValueError("Schematic file contents do not match the file extension")
        if document.is_encrypted or not 1 <= len(document) <= 8:
            raise ValueError("Provide an unencrypted schematic with 1–8 pages")
        for page in document:
            # Bound raster dimensions while preserving circuit label legibility.
            scale = min(200 / 72, 2400 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            images.append("data:image/png;base64," + encoded)
    return validate_bom(client.extract_bom(images, BOM_SCHEMA))


def parse_pdf(path, client):
    """Compatibility entry point for existing PDF callers."""
    return parse_schematic(path, client)


if __name__ == "__main__":
    import argparse
    from orchestrator.baseten_client import BasetenClient
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--expected", help="Hand-checked BOM JSON; mismatch exits nonzero")
    args = parser.parse_args()
    bom = parse_pdf(args.pdf, BasetenClient())
    print(json.dumps(bom, indent=2))
    if args.expected:
        expected = validate_bom(json.loads(Path(args.expected).read_text()))
        def canonical(value):
            return sorted((p["type"], p["value"], p["quantity"], tuple(sorted(p["refdes"]))) for p in value["components"])
        if canonical(bom) != canonical(expected):
            raise SystemExit("Extracted BOM differs from hand-checked ground truth")
