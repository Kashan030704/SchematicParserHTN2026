import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from actuator.serial_controller import SerialController
from orchestrator.simulation import simulation_config


class SerialStub:
    def __init__(self):
        self.lines = []
        self.responses = queue.Queue()
        self.auto = True
        self.closed = False
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            line = data.decode().strip()
            self.lines.append(line)
        tokens = line.split(",")
        if self.auto and tokens[0] != "H":
            self.responses.put(f"DONE,{tokens[1]}\n".encode())
        return len(data)

    def readline(self, size):
        try:
            return self.responses.get(timeout=0.05)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


def test_one_serial_owner_routes_completions_by_id():
    from tests.conftest import eventually
    transport = SerialStub()
    owner = SerialController(simulation_config(), transport)
    transport.auto = False
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(owner._request, "J", 90, 60, 100, 30, 10, 0)
            second = pool.submit(owner._request, "S")
            eventually(lambda: len([line for line in transport.lines if line.startswith(("J,", "S,"))]) == 2)
            requests = {line[0]: line.split(",")[1] for line in transport.lines if line.startswith(("J,", "S,"))}
            transport.responses.put(f"DONE,{requests['S']}\n".encode())
            assert second.result(1) is None
            assert not first.done()
            transport.responses.put(f"DONE,{requests['J']}\n".encode())
            assert first.result(1) is None
    finally:
        transport.auto = True
        owner.close()
    assert transport.closed


def test_serial_timeout_aborts_and_latches_fault():
    transport = SerialStub()
    owner = SerialController(simulation_config(), transport)
    try:
        transport.auto = False
        with pytest.raises(RuntimeError, match="timeout"):
            owner._request("B", 20, timeout=0.02)
        assert any(line.startswith("X,") for line in transport.lines)
        with pytest.raises(RuntimeError, match="restart"):
            owner.move([90, 60, 100, 30])
        transport.auto = True
        owner.stop_belt()
        ids = [line.split(",")[1] for line in transport.lines if line != "H"]
        assert len(ids) == len(set(ids))
    finally:
        transport.auto = True
        owner.close()


def test_partial_serial_reply_does_not_complete_a_shorter_id():
    from tests.conftest import eventually
    transport = SerialStub()
    owner = SerialController(simulation_config(), transport)
    transport.auto = False
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(owner._request, "S")
            eventually(lambda: any(line.startswith("S,") for line in transport.lines))
            seq = next(line.split(",")[1] for line in transport.lines if line.startswith("S,"))
            transport.responses.put(f"DONE,{seq}".encode())
            eventually(lambda: transport.responses.empty())
            assert not pending.done()
            transport.responses.put(b"\n")
            assert pending.result(1) is None
    finally:
        transport.auto = True
        owner.close()
