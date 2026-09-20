import json
import socket
import threading
import time

import pytest

from hcp_host.envelope import FrameParser, ProtocolError, frame, message
from tests.conftest import eventually, load_client


def test_framing_concatenated_split_remainder_and_utf8():
    first = message("pick", "arm", {"tag_id": 3}, "c17")
    second = message("done", "arm", {"status": "ok", "text": "résistance"}, "c17")
    parser = FrameParser()
    assert parser.feed(frame(first) + frame(second) + b'{"act') == [first, second]
    assert parser.buffer == b'{"act'
    parser = FrameParser()
    encoded = frame(second)
    cut = encoded.index("é".encode()) + 1
    assert parser.feed(encoded[:cut]) == []
    assert parser.feed(encoded[cut:]) == [second]
    assert parser.buffer == b""


@pytest.mark.parametrize("data", [b"{bad}\n", b"{}\n", b"\n", b'\xff\n', b'{"action":"done","id":"x","device_id":"arm","payload":{"x":NaN}}\n'])
def test_malformed_frames_rejected(data):
    with pytest.raises(ProtocolError):
        FrameParser().feed(data)


def test_oversized_unterminated():
    with pytest.raises(ProtocolError):
        FrameParser(max_bytes=8).feed(b"x" * 9)


def test_generated_clients_discover_late_nodes_and_do_not_fake_success(host, tmp_path):
    arm = load_client("arm", "127.0.0.1", host.port, tmp_path).start()
    sensor = load_client("sensor", "127.0.0.1", host.port, tmp_path).start()
    try:
        eventually(lambda: set(host.registry.snapshot()) == {"arm", "sensor"})
        with pytest.raises(RuntimeError, match="No hardware handler"):
            host.command("arm", "home").result(3)
        camera = load_client("camera", "127.0.0.1", host.port, tmp_path).start()
        try:
            eventually(lambda: len(host.registry.snapshot()) == 3)
        finally:
            camera.stop()
    finally:
        arm.stop()
        sensor.stop()


def test_ack_is_not_completion_and_context_flows_during_handler(host, tmp_path):
    release = threading.Event()
    arm = load_client("arm", "127.0.0.1", host.port, tmp_path)
    arm.register_handler("pick", lambda tag_id: release.wait(2))
    arm.start()
    camera = load_client("camera", "127.0.0.1", host.port, tmp_path).start()
    try:
        eventually(lambda: len(host.registry.snapshot()) == 2)
        future = host.command("arm", "pick", {"tag_id": 3})
        eventually(lambda: host.pending[future.command_id].acked)
        assert not future.done()
        camera.publish("context/tags", tags=[{"id": 3, "x": 0.2, "y": 0, "theta": 0}])
        eventually(lambda: host.context_snapshot().get("context/tags"))
        eventually(lambda: arm._context.get("context/tags"))
        assert not future.done()
        release.set()
        assert future.result(3)["status"] == "ok"
    finally:
        release.set()
        arm.stop()
        camera.stop()


def test_disconnect_fails_pending_and_reconnects_without_replay(host, tmp_path):
    release = threading.Event()
    calls = []
    arm = load_client("arm", "127.0.0.1", host.port, tmp_path)
    arm.register_handler("pick", lambda tag_id: (calls.append(tag_id), release.wait(2)))
    arm.start()
    try:
        eventually(lambda: "arm" in host.registry.snapshot())
        future = host.command("arm", "pick", {"tag_id": 3})
        eventually(lambda: calls)
        old_owner = host.registry.resolve("arm", "home", {})
        host._disconnect(old_owner)
        with pytest.raises(RuntimeError, match="disconnected"):
            future.result(1)
        release.set()
        eventually(lambda: "arm" in host.registry.snapshot() and host.registry.resolve("arm", "home", {}) is not old_owner)
        assert calls == [3]
    finally:
        release.set()
        arm.stop()


def test_timeout_is_failure(host, tmp_path):
    release = threading.Event()
    arm = load_client("arm", "127.0.0.1", host.port, tmp_path)
    arm.register_handler("home", lambda: release.wait(1))
    arm.start()
    try:
        eventually(lambda: "arm" in host.registry.snapshot())
        with pytest.raises(RuntimeError, match="timed out"):
            host.command("arm", "home", timeout=0.1).result(2)
    finally:
        release.set()
        arm.stop()


def test_duplicate_node_and_wrong_completion_id(host, tmp_path):
    arm = load_client("arm", "127.0.0.1", host.port, tmp_path)
    gate = threading.Event()
    arm.register_handler("home", lambda: gate.wait(2))
    arm.start()
    duplicate = None
    try:
        eventually(lambda: "arm" in host.registry.snapshot())
        owner = host.registry.resolve("arm", "home", {})
        duplicate = load_client("arm", "127.0.0.1", host.port, tmp_path).start()
        future = host.command("arm", "home")
        eventually(lambda: future.command_id in host.pending and host.pending[future.command_id].acked)
        arm.send_response("done", {"status": "ok"}, "wrong-id")
        time.sleep(0.05)
        assert not future.done()
        assert host.registry.resolve("arm", "home", {}) is owner
        gate.set()
        assert future.result(3)["status"] == "ok"
    finally:
        gate.set()
        arm.stop()
        if duplicate:
            duplicate.stop()
