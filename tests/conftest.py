import time

import pytest

from hcp_host.server import HCPHost


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


@pytest.fixture
def simulation(host, tmp_path):
    from orchestrator.simulation import Simulation
    runtime = Simulation(host, tmp_path / "simulation")
    yield runtime
    runtime.stop()
