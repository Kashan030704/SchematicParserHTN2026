"""Actual loopback HCP transport with simulated/fake-I2C arm; no physical hardware."""
import io
import json
import queue
import threading
import time
from dataclasses import replace

import pytest

from arm.hcp_node import PaletteArmNode
from arm.palette_contract import palette_id
from arm.pca9685_driver import PCA9685Driver
from arm.sim_driver import SimDriver
from config import ROOT
from config.so100 import ArmConfig
from hcp_host.server import CommandError
from hcp_sdk.loader import load_client
from orchestrator.fixtures import create_demo_pdf
from palette import load_palette
from tests.conftest import eventually
from tests.test_palette_backend import finish
from tests.test_pca9685_driver import FakeBackend
from ui.app import create_remote_palette_app


def start_node(host, tmp_path, *, hardware=False, lease_s=5, sim_factory=None):
    config = ArmConfig(hardware_confirmed=hardware)
    palette = replace(load_palette(ROOT / "palette.yaml", config), calibrated=hardware)
    client = load_client("palette_arm", "127.0.0.1", host.port, tmp_path / "out")
    node = PaletteArmNode(client, palette, config, hardware=hardware, lease_s=lease_s, sim_factory=sim_factory).start()
    eventually(lambda: "palette_arm" in host.registry.snapshot())
    return node


def command(host, action, **payload):
    return host.command("palette_arm", action, payload).result(timeout=3)["result"]


def begin(host, node, run_id="run-1"):
    return command(host, "begin_run", run_id=run_id, palette_id=node.palette_id, hardware=node.hardware)


def test_flask_bom_through_generated_client_five_picks_and_second_session(host, tmp_path):
    node = start_node(host, tmp_path)
    try:
        client = create_remote_palette_app(host, instance_path=tmp_path / "app").test_client()
        status = eventually(lambda: (s if (s := client.get("/api/status").json)["ready"] else None))
        assert status["backend"] == "palette-hcp" and status["simulation"]
        assert status["required_nodes"] == ["palette_arm"]
        assert set(status["nodes"]) == {"palette_arm"}
        sample = json.loads((ROOT / "sample_bom.json").read_text())
        result = finish(client, client.post("/api/runs", json={"bom": sample}))
        assert result["state"] == "complete", result
        assert len(result["delivered"]) == 5 and result["motion_steps"] > 100
        assert [p["slot_id"] for p in result["delivered"]] == ["slot-A", "slot-A", "slot-B", "slot-B", "slot-C"]
        assert result["physical_delivery_verified"] is False
        assert node.driver is None and node.state == "ready"
        # New session creates a fresh software driver, never resumes a relaxed one.
        result = finish(client, client.post("/api/runs", json={"bom": {"components": [
            {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]}]}}))
        assert result["state"] == "complete" and len(result["delivered"]) == 1
    finally:
        node.stop()


def test_fixture_pdf_over_tcp_stays_simulated(host, tmp_path):
    node = start_node(host, tmp_path)
    try:
        client = create_remote_palette_app(host, instance_path=tmp_path / "app").test_client()
        result = finish(client, client.post("/api/runs"))
        assert result["state"] == "complete", result
        assert result["input_kind"] == "pdf" and len(result["delivered"]) == 5
        assert result["simulation"] and result["ingestion"] == "fixture"
    finally:
        node.stop()


def test_unknown_component_fails_before_network_session(host, tmp_path):
    app = create_remote_palette_app(host, instance_path=tmp_path)
    backend = app.extensions["runs"].orchestrator
    backend._command = lambda *a, **kw: pytest.fail("No command should be sent before full BOM validation")
    result = finish(app.test_client(), app.test_client().post("/api/runs", json={"bom": {"components": [
        {"component_id": "resistor:10k", "qty": 1}, {"component_id": "diode:unknown", "qty": 1}]}}))
    assert result["state"] == "failed" and "diode:unknown" in result["error"]
    assert result["delivered"] == []


def test_palette_and_mode_mismatch_do_not_move(host, tmp_path):
    node = start_node(host, tmp_path)
    try:
        with pytest.raises(CommandError, match="mismatch"):
            command(host, "begin_run", run_id="r", palette_id="wrong", hardware=False)
        with pytest.raises(CommandError, match="mismatch"):
            command(host, "begin_run", run_id="r", palette_id=node.palette_id, hardware=True)
        assert node.driver is None
        app = create_remote_palette_app(host, instance_path=tmp_path / "app")
        backend = app.extensions["runs"].orchestrator
        backend.palette_id = "mismatched-backend-palette"
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            backend.run_bom({"components": [{"component_id": "resistor:10k", "qty": 1}]})
        assert node.driver is None
    finally:
        node.stop()


def test_node_checks_component_slot_and_monotonic_units(host, tmp_path):
    node = start_node(host, tmp_path)
    try:
        begin(host, node)
        with pytest.raises(CommandError, match="does not belong"):
            command(host, "pick_place", run_id="run-1", unit=1, component_id="resistor:10k", slot_id="slot-C")
        assert node.driver is None and node.motion_steps == 0
        begin(host, node, "run-2")
        command(host, "pick_place", run_id="run-2", unit=1, component_id="resistor:10k", slot_id="slot-A")
        prior_steps = node.motion_steps
        with pytest.raises(CommandError, match="Expected unit 2"):
            command(host, "pick_place", run_id="run-2", unit=1, component_id="resistor:10k", slot_id="slot-A")
        assert node.motion_steps == prior_steps and node.driver is None
    finally:
        node.stop()


def test_done_waits_for_motion_while_heartbeat_works(host, tmp_path):
    started, release = threading.Event(), threading.Event()

    def pause(_):
        started.set()
        release.wait(2)

    node = start_node(host, tmp_path, sim_factory=lambda: SimDriver(ArmConfig(), emit=None, sleep=pause))
    try:
        begin(host, node)
        future = host.command("palette_arm", "pick_place", {"run_id": "run-1", "unit": 1,
            "component_id": "resistor:10k", "slot_id": "slot-A"})
        assert started.wait(1)
        eventually(lambda: host.pending[future.command_id].acked)
        assert not future.done()  # ACK is not completion.
        assert command(host, "heartbeat", run_id="run-1")["state"] == "running"
        release.set()
        assert future.result(timeout=3)["status"] == "ok"
        command(host, "end_run", run_id="run-1")
    finally:
        release.set()
        node.stop()


def test_heartbeat_lease_expiry_relaxes_without_replay(host, tmp_path):
    node = start_node(host, tmp_path, lease_s=0.2)
    try:
        begin(host, node)
        driver = node.driver
        eventually(lambda: node.driver is None)
        assert driver._relaxed and node.motion_steps == 0
        assert "expired" in node.error
        with pytest.raises(CommandError, match="No matching"):
            command(host, "pick_place", run_id="run-1", unit=1, component_id="resistor:10k", slot_id="slot-A")
        with pytest.raises(CommandError, match="fresh"):
            begin(host, node)
    finally:
        node.stop()


def test_status_backpressure_cannot_kill_watchdog(host, tmp_path):
    node = start_node(host, tmp_path, lease_s=0.2)
    try:
        def full(**kwargs):
            raise queue.Full

        node.client.status = full
        begin(host, node)
        driver = node.driver
        eventually(lambda: node.driver is None)
        assert driver._relaxed and "expired" in node.error
    finally:
        node.stop()


def test_urgent_abort_cancels_an_active_slew(host, tmp_path):
    # Wall-clock simulation exercises cancellation while pick handler is occupied.
    node = start_node(host, tmp_path, sim_factory=lambda: SimDriver(ArmConfig(), emit=None,
                                                                  sleep=lambda _: time.sleep(0.01)))
    try:
        begin(host, node)
        driver = node.driver
        future = host.command("palette_arm", "pick_place", {"run_id": "run-1", "unit": 1,
            "component_id": "resistor:10k", "slot_id": "slot-A"})
        eventually(lambda: node.motion_steps > 0)
        assert not future.done()
        command(host, "abort_run", run_id="run-1")
        with pytest.raises(CommandError, match="cancelled|relaxed|matching"):
            future.result(timeout=2)
        assert driver._relaxed and node.driver is None
        steps = node.motion_steps
        time.sleep(0.05)
        assert node.motion_steps == steps
    finally:
        node.stop()


def test_heartbeat_keeps_slow_run_alive(host, tmp_path):
    node = start_node(host, tmp_path, lease_s=0.3,
                      sim_factory=lambda: SimDriver(ArmConfig(), emit=None, sleep=lambda _: time.sleep(0.001)))
    try:
        app = create_remote_palette_app(host, instance_path=tmp_path / "app")
        result = finish(app.test_client(), app.test_client().post("/api/runs", json={"bom": {
            "components": [{"component_id": "resistor:10k", "qty": 3}]}}))
        assert result["state"] == "complete", result
        assert len(result["delivered"]) == 3
    finally:
        node.stop()


def arm_fake_hardware(node):
    bus = FakeBackend()
    driver = PCA9685Driver(node.config, startup_confirmed=True, backend=bus, sleep=lambda _: None)
    node.arm_local(driver)
    return driver, bus


def test_physical_mode_requires_local_arm_and_disarms_after_each_run(host, tmp_path):
    node = start_node(host, tmp_path, hardware=True)
    try:
        with pytest.raises(CommandError, match="local arming"):
            begin(host, node)
        driver, bus = arm_fake_hardware(node)
        app = create_remote_palette_app(host, allow_hardware=True, instance_path=tmp_path / "app")
        result = finish(app.test_client(), app.test_client().post("/api/runs", json={"bom": {
            "components": [{"component_id": "resistor:10k", "qty": 1}]}}))
        assert result["state"] == "complete", result
        assert not result["simulation"] and result["hardware"] == "physical"
        assert not result["delivered"][0]["simulated"]
        assert result["physical_delivery_verified"] is False
        assert node.state == "disarmed" and driver._relaxed and bus.closed
        assert bus.disabled == list(range(7)) and bus.pulses
        with pytest.raises(CommandError, match="local arming"):
            begin(host, node, "another-run")
    finally:
        node.stop()


def test_physical_node_rejected_without_backend_opt_in(host, tmp_path):
    node = start_node(host, tmp_path, hardware=True)
    try:
        _, bus = arm_fake_hardware(node)
        client = create_remote_palette_app(host, instance_path=tmp_path / "app").test_client()
        result = finish(client, client.post("/api/runs", json={"bom": {
            "components": [{"component_id": "resistor:10k", "qty": 1}]}}))
        assert result["state"] == "failed" and "hardware mode mismatch" in result["error"]
        assert not bus.pulses
        assert not client.get("/api/status").json["ready"]
    finally:
        node.stop()


def test_disconnect_disarms_physical_node_and_reconnect_never_rearms(host, tmp_path):
    node = start_node(host, tmp_path, hardware=True)
    try:
        driver, bus = arm_fake_hardware(node)
        begin(host, node)
        owner = host.registry.resolve("palette_arm", "get_state", {})
        host._disconnect(owner)
        eventually(lambda: node.state == "disarmed")
        eventually(lambda: "palette_arm" in host.registry.snapshot())
        assert driver._relaxed and bus.closed and not bus.pulses
        assert command(host, "get_state")["state"] == "disarmed"
        with pytest.raises(CommandError, match="local arming"):
            begin(host, node, "reconnected-run")
    finally:
        node.stop()


def test_i2c_failure_is_loud_with_disable_attempted(host, tmp_path):
    node = start_node(host, tmp_path, hardware=True)
    try:
        _, bus = arm_fake_hardware(node)
        bus.fail_channel = 2
        begin(host, node)
        with pytest.raises(CommandError, match="I2C"):
            command(host, "pick_place", run_id="run-1", unit=1, component_id="resistor:10k", slot_id="slot-A")
        assert bus.disabled == list(range(7)) and bus.closed and node.state == "disarmed"
    finally:
        node.stop()


def test_disable_failure_latches_fault_and_blocks_rearming(host, tmp_path):
    node = start_node(host, tmp_path, hardware=True)
    try:
        _, bus = arm_fake_hardware(node)
        bus.fail_disable = 1
        begin(host, node)
        with pytest.raises(CommandError, match="disable"):
            command(host, "end_run", run_id="run-1")
        assert node.state == "fault" and bus.closed and bus.disabled == list(range(7))
        replacement = SimDriver(node.config, emit=None)
        with pytest.raises(RuntimeError, match="disarmed"):
            node.arm_local(replacement)
    finally:
        node.stop()


def test_physical_mode_refuses_fixture_pdf_before_any_node_command(host, tmp_path):
    app = create_remote_palette_app(host, allow_hardware=True, instance_path=tmp_path / "app")
    client = app.test_client()
    assert not client.get("/api/status").json["demo_available"]
    assert client.post("/api/runs").status_code == 400
    pdf = create_demo_pdf(tmp_path / "pdf")
    result = finish(client, client.post("/api/runs", data={"pdf": (io.BytesIO(pdf.read_bytes()), "fixture.pdf")}))
    assert result["state"] == "failed" and "fixture PDFs are refused" in result["error"]
    assert result["delivered"] == []


def test_palette_identity_changes_with_any_waypoint_not_calibrated_flag():
    original = load_palette(ROOT / "palette.yaml", ArmConfig())
    assert palette_id(original) == palette_id(replace(original, calibrated=True))
    changed = replace(original, home={**original.home, "base": original.home["base"] + 1})
    assert palette_id(original) != palette_id(changed)
