#!/usr/bin/env python3
"""
HCP SDK Generator (TCP Client Edition)
-------------------------------------
Usage:
    python hcp_sdk_gen.py --input examples/robot_arm.json --output ./out --host 127.0.0.1 --port 9000
"""

import os
import json
import argparse
from typing import Dict, Any
from pathlib import Path
import pprint
import queue

# JSON Schema validation library
try:
    from jsonschema import validate, ValidationError
except ImportError:
    raise ImportError("Please install jsonschema: pip install jsonschema")


# ---------------- JSON Schema Validation ----------------
def validate_hcp_json_schema(data: Dict[str, Any], schema_path: str) -> None:
    """Validate input JSON against a JSON Schema."""
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    try:
        validate(instance=data, schema=schema)
    except ValidationError as e:
        raise ValueError(f"JSON Schema validation error: {e.message}")


# ---------------- Code Generation ----------------
def generate_device_code(data: Dict[str, Any], host: str, port: int) -> str:
    # The original template's connect/listen/events/response queues now live in
    # hcp_sdk.runtime, shared by every generated node to avoid divergent framing.
    device_id = data["metadata"]["device_id"]
    return "\n".join([
        f'"""Auto-generated HCP TCP client: {device_id}."""',
        "from hcp_sdk.runtime import HCPClient as _HCPClient",
        "import time",
        "",
        "HCP_DEVICE_JSON = " + pprint.pformat(data, indent=2),
        f"HCP_HOST = {host!r}",
        f"HCP_PORT = {port!r}",
        "",
        "class HCPClient(_HCPClient):",
        "    def __init__(self, host=HCP_HOST, port=HCP_PORT):",
        "        super().__init__(HCP_DEVICE_JSON, host, port)",
        "",
        "if __name__ == '__main__':",
        "    # Discovery works standalone; commands fail until real handlers attach.",
        "    client = HCPClient().start()",
        "    try:",
        "        while True:",
        "            time.sleep(1)",
        "    except KeyboardInterrupt:",
        "        client.stop()",
        "",
    ])


# ---------------- CLI Entry Point ----------------
def main():
    parser = argparse.ArgumentParser(description="Generate HCP TCP Client SDK from JSON spec.")
    parser.add_argument("--input", required=True, help="Path to input JSON spec.")
    parser.add_argument("--output", default=".", help="Output directory (default: current).")
    parser.add_argument("--host", required=True, help="HCP server host to connect to.")
    parser.add_argument("--port", required=True, type=int, help="HCP server port to connect to.")
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output)
    schema_path = Path(__file__).parent / "hcp_sdk_schema.json"

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate JSON schema
    validate_hcp_json_schema(data, schema_path)

    # Generate client SDK code
    code = generate_device_code(data, args.host, args.port)
    device_id = data["metadata"]["device_id"]
    filename = f"{device_id}_hcp_support.py"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)

    print(f"[OK] Generated TCP client SDK: {out_path}")


if __name__ == "__main__":
    main()
