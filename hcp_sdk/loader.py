"""Generate and import the reference SDK's node-specific client artifacts."""
import importlib.util
import json
from pathlib import Path

from hcp_sdk.hcp_sdk_gen import generate_device_code, validate_hcp_json_schema

SDK = Path(__file__).resolve().parent


def load_client(device_id, host, port, output_dir):
    if device_id not in ("arm", "conveyor", "camera"):
        raise ValueError("Unknown built-in node")
    definition = json.loads((SDK / "nodes" / f"{device_id}.json").read_text())
    validate_hcp_json_schema(definition, SDK / "hcp_sdk_schema.json")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{device_id}_hcp_support.py"
    path.write_text(generate_device_code(definition, host, port), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"generated_{device_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Explicit overrides avoid stale Python bytecode if regenerated rapidly.
    return module.HCPClient(host, port)
