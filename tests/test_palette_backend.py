import copy
import io
import json
import subprocess
import sys
import threading

import pytest

from config import ROOT
from orchestrator.fixtures import DEMO_BOM, create_demo_pdf
from tests.conftest import eventually
from ui.app import create_palette_app


def finish(client, response):
    assert response.status_code == 202, response.json
    run_id = response.json["id"]
    return eventually(lambda: (r if (r := client.get(f"/api/runs/{run_id}").json)["state"]
                               in ("complete", "failed", "incomplete") else None))


def test_fixture_pdf_through_flask_to_five_slewed_picks(tmp_path):
    app = create_palette_app(instance_path=tmp_path)
    client = app.test_client()
    assert client.get("/").status_code == 200
    status = client.get("/api/status").json
    assert status["backend"] == "palette" and status["ready"]
    assert status["nodes"] == {} and status["required_nodes"] == []
    assert status["simulation"] and status["hardware"] == "simulated"
    assert status["ingestion"] == "fixture" and status["demo_available"]
    assert app.extensions["hcp"] is None
    assert client.get("/demo.pdf").status_code == 200
    result = finish(client, client.post("/api/runs"))
    assert result["state"] == "complete", result
    assert result["bom"] == DEMO_BOM
    assert [s["slot_id"] for s in result["plan"]["steps"]] == ["slot-A", "slot-A", "slot-B", "slot-B", "slot-C"]
    assert [p["refdes"] for p in result["delivered"]] == ["R1", "R2", "C1", "C2", "U1"]
    assert all(p["simulated"] for p in result["delivered"])
    assert result["requested"] == 5 and result["motion_steps"] > 100
    assert result["physical_delivery_verified"] is False
    assert any("decoupling" in item for item in result["llm_schematic_suggestions"])
    assert len(result["events"]) <= 80
    assert [e["command"] for e in result["events"][-2:]] == ["relax", "close"]
    assert client.get("/api/status").json["active_run"] is None


def test_existing_bom_json_needs_no_ingestion_and_no_hardware(tmp_path):
    class NeverIngest:
        def extract_bom(self, *args):
            pytest.fail("JSON BOM must not call ingestion")

    # Even a reviewed hardware config cannot change this web mode's driver.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"hardware_confirmed": True}))
    app = create_palette_app(instance_path=tmp_path / "app", config_path=config_path,
                             live_ingestion=True, ingestion_model=NeverIngest())
    client = app.test_client()
    sample = json.loads((ROOT / "sample_bom.json").read_text())
    result = finish(client, client.post("/api/runs", json={"bom": sample}))
    assert result["state"] == "complete"
    assert len(result["delivered"]) == 5 and result["input_kind"] == "bom"
    assert result["simulation"] and result["hardware"] == "simulated"
    assert client.post("/api/runs", json={"bom": sample, "hardware": "real"}).status_code == 400


def test_live_pdf_uses_injected_ingestion_not_fixture(tmp_path):
    expected = {"components": [{"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U9"]}]}

    class LiveStandIn:
        def __init__(self):
            self.calls = []

        def extract_bom(self, images, schema):
            self.calls.append((images, schema))
            return copy.deepcopy(expected)

    model = LiveStandIn()
    app = create_palette_app(instance_path=tmp_path / "app", ingestion_model=model, live_ingestion=True)
    client = app.test_client()
    status = client.get("/api/status").json
    assert status["ingestion"] == "live" and not status["demo_available"]
    assert client.get("/demo.pdf").status_code == 404
    assert client.post("/api/runs").status_code == 400
    pdf = create_demo_pdf(tmp_path / "input")
    result = finish(client, client.post("/api/runs", data={"pdf": (io.BytesIO(pdf.read_bytes()), "input.pdf")}))
    assert result["state"] == "complete", result
    assert result["bom"] == expected
    assert len(result["delivered"]) == 1 and result["delivered"][0]["refdes"] == "U9"
    assert len(model.calls) == 1
    assert model.calls[0][0][0].startswith("data:image/png;base64,")


def test_live_factory_reuses_baseten_client_without_planner_model(tmp_path, monkeypatch):
    from orchestrator import baseten_client
    from orchestrator.fixtures import FixtureIngestionModel

    client = FixtureIngestionModel()
    calls = []
    monkeypatch.setattr(baseten_client, "BasetenClient", lambda: calls.append(True) or client)
    app = create_palette_app(instance_path=tmp_path, live_ingestion=True)
    assert calls == [True]
    assert app.extensions["runs"].orchestrator.ingestion_model is client


def test_missing_component_fails_whole_run_before_driver(tmp_path, monkeypatch):
    import orchestrator.palette_backend as module

    opened = []
    monkeypatch.setattr(module, "SimDriver", lambda *a, **kw: opened.append(True))
    client = create_palette_app(instance_path=tmp_path).test_client()
    bom = {"components": [{"component_id": "resistor:10k", "qty": 1},
                          {"component_id": "diode:missing", "qty": 2}]}
    result = finish(client, client.post("/api/runs", json={"bom": bom}))
    assert result["state"] == "failed" and result["delivered"] == []
    assert "diode:missing" in result["error"] and result["warnings"]
    assert not opened


def test_schematic_suggestions_warn_about_low_resistance_short_risk():
    from orchestrator.schematic_advice import build_schematic_suggestions

    suggestions = build_schematic_suggestions({"components": [
        {"type": "resistor", "value": "0R", "quantity": 1, "refdes": ["R1"]},
        {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]},
    ]})
    assert suggestions[0].startswith("These are advisory checks")
    assert any("short" in suggestion and "R1" in suggestion for suggestion in suggestions)
    assert any("0.1uF" in suggestion and "U1" in suggestion for suggestion in suggestions)


@pytest.mark.parametrize("bom", [
    {"components": [{"component_id": "resistor:10k", "qty": -1}]},
    {"components": [{"component_id": "resistor:10k", "qty": 10**20}]},
    {"components": [{"component_id": "resistor:10k", "qty": 2, "refdes": ["R1"]}]},
    {"components": [{"component_id": "resistor:10k", "qty": 2, "refdes": ["R1", "R1"]}]},
])
def test_malformed_or_unbounded_bom_never_executes(tmp_path, bom):
    client = create_palette_app(instance_path=tmp_path).test_client()
    result = finish(client, client.post("/api/runs", json={"bom": bom}))
    assert result["state"] == "failed" and result["delivered"] == []
    assert not result.get("events")


def test_concurrent_run_rejected_until_ingestion_finishes(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowModel:
        def extract_bom(self, *args):
            entered.set()
            assert release.wait(3)
            return DEMO_BOM

    client = create_palette_app(instance_path=tmp_path, ingestion_model=SlowModel()).test_client()
    response = client.post("/api/runs")
    try:
        assert entered.wait(2)
        assert client.post("/api/runs", json={"bom": DEMO_BOM}).status_code == 409
    finally:
        release.set()
    assert finish(client, response)["state"] == "complete"


def test_executor_failure_is_not_reported_as_a_placement_and_can_retry_new_run(tmp_path, monkeypatch):
    import orchestrator.palette_backend as module
    from arm.driver import ServoError
    from arm.sim_driver import SimDriver

    class FailingSim(SimDriver):
        def set_gripper(self, state):
            if state == "closed":
                raise ServoError("injected simulated grip-command failure")
            super().set_gripper(state)

    client = create_palette_app(instance_path=tmp_path).test_client()
    monkeypatch.setattr(module, "SimDriver", FailingSim)
    failed = finish(client, client.post("/api/runs"))
    assert failed["state"] == "failed" and failed["delivered"] == []
    assert "injected" in failed["error"]
    assert [e["command"] for e in failed["events"][-2:]] == ["relax", "close"]
    monkeypatch.setattr(module, "SimDriver", SimDriver)
    recovered = finish(client, client.post("/api/runs"))
    assert recovered["state"] == "complete" and len(recovered["delivered"]) == 5
    assert recovered["id"] != failed["id"]


def test_bad_upload_and_schema_errors_surface(tmp_path):
    client = create_palette_app(instance_path=tmp_path).test_client()
    assert client.post("/api/runs", data={"pdf": (io.BytesIO(b"text"), "a.txt")}).status_code == 400
    result = finish(client, client.post("/api/runs", data={"pdf": (io.BytesIO(b"not a pdf"), "a.pdf")}))
    assert result["state"] == "failed" and result["error"]
    assert client.post("/api/runs", json={"wrong": "shape"}).status_code == 400
    assert client.get("/api/runs/unknown").status_code == 404
    assert client.post("/api/runs", data=b"a" * (16 * 1024 * 1024 + 1),
                       content_type="application/json").status_code == 413


def test_factory_and_run_never_import_hardware_hcp_camera_or_ik(tmp_path):
    script = '''
import builtins, sys, time
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'smbus2', 'board', 'busio', 'adafruit_pca9685', 'adafruit_servokit',
                             'scservo_sdk', 'serial', 'vision', 'cv2', 'actuator', 'hcp_host', 'hcp_sdk'}:
        raise AssertionError('Forbidden backend import: ' + name)
    if name == 'arm.pca9685_driver':
        raise AssertionError('Real driver imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from ui.app import create_palette_app
app = create_palette_app(instance_path=sys.argv[1])
client = app.test_client()
reply = client.post('/api/runs')
assert reply.status_code == 202
for attempt in range(200):
    run = client.get('/api/runs/' + reply.json['id']).json
    if run['state'] in ('complete', 'failed'):
        assert run['state'] == 'complete', run
        assert len(run['delivered']) == 5
        print('No hardware/HCP/camera imports; five simulated placements complete')
        break
    time.sleep(0.01)
else:
    raise AssertionError('Run timed out')
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "five simulated placements complete" in result.stdout
