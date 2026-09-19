import time

import pytest

from hcp_host.server import HCPHost


def load_client(device_id, host, port, output_dir):
    """Generic codegen regression fixtures, NOT deployable actuator definitions."""
    from hcp_sdk.hcp_sdk_gen import generate_device_code
    commands = {
        "home": {"freetext_desc": "Test no-argument command", "params": []},
        "pick": {"freetext_desc": "Test integer command", "params": [{"tag_id": "int"}]},
    }
    definition = {"metadata": {"device_id": device_id, "freetext_desc": "TEST ONLY"},
                  "available_commands": commands}
    namespace = {"__name__": "test_generated_client"}
    exec(compile(generate_device_code(definition, host, port), "<test-generated>", "exec"), namespace)
    return namespace["HCPClient"]()


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Condition did not become true")


@pytest.fixture
def host():
    server = HCPHost("127.0.0.1", 0, command_timeout=2).start()
    yield server
    server.stop()
