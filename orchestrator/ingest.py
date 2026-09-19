"""Ingestion seam; reuses the existing PDF/image pipeline."""
from ingestion.parse import parse_schematic, validate_bom


def ingest(path, client=None, part_types=(), *, details=False):
    return parse_schematic(path, client, part_types=part_types, details=details)
