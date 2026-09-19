import io
import threading
from pathlib import Path

import pymupdf
import pytest
from ui.app import create_app
from tests.test_controller import make_controller
from tests.conftest import eventually


@pytest.fixture
def app():
    controller, events = make_controller()
    app = create_app(controller, part_types=["R3", "C1", "LED_RX"])
    app.testing = True
    app.events = events
    return app


def headers(app):
    return {"X-HCP-UI-Token": app.config["UI_TOKEN"]}


def proposal(app, bom=None):
    return app.test_client().post("/proposals", json={"bom": bom or {"R3": 2}},
                                  headers=headers(app))


def test_propose_and_preview_never_move(app):
    response = proposal(app)
    assert response.status_code == 201
    assert response.json["state"] == "awaiting_confirmation"
    assert [e[1] for e in app.events] == ["/detect"]
    assert app.test_client().get("/").status_code == 200


def test_explicit_edited_approval_and_replay_rejected(app):
    client = app.test_client()
    identifier = proposal(app).json["id"]
    url = f"/proposals/{identifier}/approve"
    assert client.post(url, json={"bom": {"R3": 1}}, headers=headers(app)).status_code == 400
    assert [e[1] for e in app.events] == ["/detect"]
    response = client.post(url, json={"approved": True, "bom": {"C1": 5}}, headers=headers(app))
    assert response.status_code == 202
    finished = eventually(lambda: (r if (r := client.get(f"/proposals/{identifier}").json)["state"] != "running" else None))
    assert finished["state"] == "complete"
    assert finished["cups_commanded"] == ["C1"]
    assert len([e for e in app.events if e[1] == "/grasp_place"]) == 1
    assert client.post(url, json={"approved": True, "bom": {"R3": 1}}, headers=headers(app)).status_code == 409


def test_unknown_type_and_duplicate_json_blocked(app):
    client = app.test_client()
    identifier = proposal(app).json["id"]
    response = client.post(f"/proposals/{identifier}/approve",
                           json={"approved": True, "bom": {"WRONG": 1}}, headers=headers(app))
    assert response.status_code == 400
    response = client.post("/proposals", data='{"bom":{"R3":1,"R3":2}}',
                           content_type="application/json", headers=headers(app))
    assert response.status_code == 400
    assert "/begin" not in [e[1] for e in app.events]


def test_csrf_token_required(app):
    assert app.test_client().post("/proposals", json={"bom": {"R3": 1}}).status_code == 403
    assert app.test_client().post("/stop").status_code == 403


def test_upload_invokes_ingestion_not_stub_and_awaits_confirmation():
    calls = []
    class Model:
        def extract_bom(self, images, schema, *, part_types):
            calls.append((images, schema, part_types))
            return {"LED_RX": 3}
    controller, events = make_controller()
    app = create_app(controller, model_factory=Model, part_types=["LED_RX"])
    doc = pymupdf.open()
    doc.new_page().insert_text((30, 30), "LED_RX")
    data = doc.tobytes()
    doc.close()
    response = app.test_client().post("/proposals",
        data={"schematic": (io.BytesIO(data), "circuit.pdf")}, headers=headers(app))
    assert response.status_code == 201
    assert response.json["source"] == "baseten"
    assert response.json["state"] == "awaiting_confirmation"
    assert response.json["bom"] == {"LED_RX": 3}
    assert len(calls) == 1
    assert [e[1] for e in events] == ["/detect"]


def test_stopped_gate_cannot_rearm(app):
    identifier = proposal(app).json["id"]
    client = app.test_client()
    assert client.post("/stop", headers=headers(app)).status_code == 200
    assert client.post(f"/proposals/{identifier}/approve",
        json={"approved": True, "bom": {"R3": 1}}, headers=headers(app)).status_code == 409


def test_health_and_status_are_read_only(app):
    client = app.test_client()
    assert client.get("/status").json["active_run"] is None
    result = client.get("/nodes").json
    assert set(result["nodes"]) == {"robomaster", "conveyor"}
    assert all(node["connected"] for node in result["nodes"].values())
    assert [event[:2] for event in app.events] == [("GET", "/health")] * 2


def test_health_failure_is_visible_without_dispatching_stop():
    controller, events = make_controller(fail="/health")
    client = create_app(controller).test_client()
    nodes = client.get("/nodes").json["nodes"]
    assert all(not node["connected"] and "injected" in node["error"] for node in nodes.values())
    assert [event[:2] for event in events] == [("GET", "/health")] * 2


def test_proposal_preserves_human_order_and_edits_refresh_advice(app):
    client = app.test_client()
    response = proposal(app, {"LED_RX": 3, "R3": 2, "C1": 1})
    assert list(response.json["bom"]) == ["LED_RX", "R3", "C1"]
    assert any("LED_RX" in note for note in response.json["llm_schematic_suggestions"])
    identifier = response.json["id"]
    assert client.post(f"/proposals/{identifier}/approve",
        json={"approved": True, "bom": {"R3": 1}}, headers=headers(app)).status_code == 202
    result = eventually(lambda: (r if (r := client.get(f"/proposals/{identifier}").json)["state"] != "running" else None))
    assert not any("LED_RX" in note for note in result["llm_schematic_suggestions"])
