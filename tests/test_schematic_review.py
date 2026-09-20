"""Vision evidence and cost advice must remain independent of BOM approval/motion."""
import copy
import io
import json
import threading

import pymupdf
import pytest

from ingestion.review import REVIEW_SCHEMA, validate_review
from orchestrator.baseten_client import BasetenClient
from tests.conftest import eventually
from tests.test_baseten import CompletionStub, response
from tests.test_controller import make_controller
from ui.app import create_app


def review_result():
    return {
        "summary": "Check the apparent supply-to-ground connection before powering.",
        "limitations": ["The junction marker is unclear; verify connectivity in the CAD file."],
        "findings": [{
            "title": "Possible supply-to-ground short", "severity": "high", "components": ["R3"],
            "evidence": "Page 1 shows R3 near the supply and ground labels; the junction is unclear.",
            "consequence": "If connected across the rails, the link could overheat and damage nearby parts.",
            "recommendation": "Verify the net connections and measure resistance with power disconnected.",
            "estimated_damage_cost": {
                "currency": "CAD", "low": 2, "high": 10,
                "basis": "Illustrative test estimate for replacing one small passive component; board damage unknown.",
            },
        }],
    }


class Responses(CompletionStub):
    def __init__(self, *replies):
        super().__init__(None)
        self.replies = iter(replies)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def setup(*replies):
    stub = Responses(*replies)
    model = BasetenClient(stub, "test-vision")
    controller, events = make_controller()
    app = create_app(controller, model_factory=lambda: model, part_types=["R3", "C1"])
    return app, stub, events


def upload(app):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 40), "R3 0R; VCC; GND")
        data = doc.tobytes()
    return app.test_client().post("/proposals", data={
        "schematic": (io.BytesIO(data), "circuit.pdf")},
        headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})


def test_vision_review_uses_original_images_and_cannot_change_or_approve_bom():
    review = review_result()
    app, stub, events = setup(response('{"R3":1}'), response(json.dumps(review)))
    result = upload(app)
    assert result.status_code == 201, result.json
    run = result.json
    assert run["bom"] == run["schematic_review_bom"] == {"R3": 1}
    assert run["state"] == "awaiting_confirmation"
    assert run["schematic_review"] == review
    assert run["schematic_review_model"] == "test-vision"
    assert not run["schematic_review_stale"]
    assert run["llm_schematic_suggestions"]  # Local checks are preserved separately.
    assert [event[1] for event in events] == ["/detect"]
    assert len(stub.requests) == 2
    extraction, advice = stub.requests
    assert advice["messages"][1]["content"][1:] == extraction["messages"][1]["content"][1:]
    assert advice["response_format"]["json_schema"]["schema"] == REVIEW_SCHEMA
    assert advice["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("reply", [
    RuntimeError("429 rate limited"), RuntimeError("request timed out"),
    response('{"summary":', "length"), response('{"summary":"incomplete schema"}'),
    response('{"summary":"one","summary":"duplicate"}'),
    response(json.dumps({**review_result(), "bom": {"C1": 100}, "action": "approve"})),
])
def test_failed_review_preserves_bom_without_retry_or_motion(reply):
    app, stub, events = setup(response('{"R3":1}'), reply)
    result = upload(app)
    assert result.status_code == 201
    assert result.json["bom"] == {"R3": 1}
    assert result.json["schematic_review_error"]
    assert "schematic_review" not in result.json
    assert result.json["state"] == "awaiting_confirmation"
    assert result.json["llm_schematic_suggestions"]
    assert len(stub.requests) == 2
    assert [event[1] for event in events] == ["/detect"]


def test_invalid_extraction_never_calls_review_or_nodes():
    app, stub, events = setup(response('{"R3":0}'))
    assert upload(app).status_code == 400
    assert len(stub.requests) == 1
    assert not events


@pytest.mark.parametrize("low,high", [(10, 2), (None, 10), (1, None), (-1, 2),
                                     (True, 2), (1, float("inf")), (float("nan"), 2)])
def test_invalid_costs_are_rejected(low, high):
    review = review_result()
    review["findings"][0]["estimated_damage_cost"].update(low=low, high=high)
    with pytest.raises(Exception):
        validate_review(review)


def test_unknown_cost_and_no_supported_findings_are_valid():
    review = review_result()
    review["findings"][0]["estimated_damage_cost"].update(
        low=None, high=None, basis="Part numbers and board repair scope are unknown.")
    original = copy.deepcopy(review)
    assert validate_review(review) == original
    assert review == original
    review["findings"] = []
    assert validate_review(review)["findings"] == []


def test_editing_bom_preserves_source_review_and_marks_it_stale_without_another_call():
    app, stub, _ = setup(response('{"R3":1}'), response(json.dumps(review_result())))
    run = upload(app).json
    client = app.test_client()
    assert client.post(f"/proposals/{run['id']}/approve", json={"approved": True, "bom": {"C1": 2}},
        headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]}).status_code == 202
    result = eventually(lambda: (r if (r := client.get(f"/proposals/{run['id']}").json)["state"] != "running" else None))
    assert result["state"] == "complete"
    assert result["bom"] == {"C1": 2}
    assert result["schematic_review_bom"] == {"R3": 1}
    assert result["schematic_review"] == run["schematic_review"]
    assert result["schematic_review_stale"]
    assert len(stub.requests) == 2


def test_stop_does_not_wait_for_advisory_model_and_late_review_cannot_rearm():
    entered, release = threading.Event(), threading.Event()

    class Model:
        def extract_bom(self, *args, **kwargs):
            return {"R3": 1}

        def review_schematic(self, *args):
            entered.set()
            assert release.wait(3)
            return review_result()

    controller, events = make_controller()
    app = create_app(controller, model_factory=Model, part_types=["R3"])
    results = []
    worker = threading.Thread(target=lambda: results.append(upload(app)))
    worker.start()
    try:
        assert entered.wait(2)
        token = {"X-HCP-UI-Token": app.config["UI_TOKEN"]}
        assert app.test_client().post("/stop", headers=token).status_code == 200
        assert controller.cancelled.is_set()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    run = results[0].json
    assert results[0].status_code == 201
    assert app.test_client().post(f"/proposals/{run['id']}/approve",
        json={"approved": True, "bom": run["bom"]}, headers=token).status_code == 409
    assert all(event[1] in {"/detect", "/estop"} for event in events)
