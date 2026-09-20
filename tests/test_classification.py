import copy
import io
import json
from pathlib import Path

import pytest
import yaml

from config import load_group_catalog, load_poses, load_tag_map
from ingestion.classification import already_grouped, group_components
from orchestrator.baseten_client import BasetenClient
from tests.test_baseten import CompletionStub, response
from tests.test_controller import make_controller
from tests.conftest import eventually
from tests.test_sch_uploads import kicad
from tests.test_schematic_review import Responses, review_result, upload
from ui.app import create_app

ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_group_catalog(ROOT / "tag_map.yaml")
NAMES = [group["name"] for group in CATALOG["groups"]]
RAW = {"R1 resistor 10k": 2, "R2 resistor 1k": 3, "C1 capacitor 100nF": 1, "J1 connector": 1}


def assignments():
    return {"assignments": [
        {"component": label, "group": group, "confidence": confidence, "reason": reason}
        for label, group, confidence, reason in [
            ("R1 resistor 10k", "resistors", "high", "A resistive component."),
            ("R2 resistor 1k", "resistors", "high", "A resistive component."),
            ("C1 capacitor 100nF", "capacitors", "high", "A capacitive component."),
            ("J1 connector", "custom printed circuit boards", "low", "No connector group; a forced board-associated storage match."),
        ]
    ]}


def app_for(model):
    controller, events = make_controller()
    app = create_app(controller, model_factory=lambda: model, part_types=NAMES, group_catalog=CATALOG)
    return app, events


def post(app, path, body):
    return app.test_client().post(path, json=body, headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})


def test_user_tag_registration_and_all_routes_match():
    assert load_tag_map(ROOT / "tag_map.yaml") == {
        0: "resistors", 2: "capacitors", 6: "integrated circuits", 7: "motors",
        1: "inductors", 5: "custom printed circuit boards", 8: "diodes/LED",
    }
    poses = load_poses(ROOT / "poses.yaml")
    assert set(poses["coarse_routes"]) == {0, 1, 2, 5, 6, 7, 8}
    assert poses["calibrated"] is False and poses["visual_approach"]["taught"] is False


def test_grouping_preserves_all_quantities_order_and_tag_zero_without_mutation():
    original = copy.deepcopy(RAW)
    value = assignments()
    value["assignments"].reverse()  # Model ordering cannot reorder the original component audit.
    result = group_components(RAW, value, CATALOG)
    assert RAW == original
    assert result["bom"] == {"resistors": 5, "capacitors": 1, "custom printed circuit boards": 1}
    rows = result["component_classification"]
    assert [row["component"] for row in rows] == list(RAW)
    assert sum(result["bom"].values()) == sum(RAW.values())
    assert rows[0]["tag_id"] == rows[1]["tag_id"] == 0
    assert rows[2]["tag_id"] == 2 and rows[3]["tag_id"] == 5
    assert rows[3]["confidence"] == "low"


@pytest.mark.parametrize("fault", ["omission", "duplicate", "invented_component", "invented_group", "quantity", "tag_id", "extra_action", "blank_reason"])
def test_model_cannot_drop_invent_recount_or_dispatch_components(fault):
    value = assignments()
    rows = value["assignments"]
    if fault == "omission": rows.pop()
    elif fault == "duplicate": rows[-1] = copy.deepcopy(rows[0])
    elif fault == "invented_component": rows[0]["component"] = "invented resistor"
    elif fault == "invented_group": rows[0]["group"] = "new eighth group"
    elif fault == "quantity": rows[0]["quantity"] = 100
    elif fault == "tag_id": rows[0]["tag_id"] = 99
    elif fault == "extra_action": value["action"] = "approve"
    else: rows[0]["reason"] = " "
    with pytest.raises(Exception):
        group_components(RAW, value, CATALOG)


def test_classifier_makes_one_constrained_model_call_on_original_component_data():
    stub = CompletionStub(response(json.dumps(assignments())))
    client = BasetenClient(stub, "test-vision")
    assert client.classify_components(RAW, CATALOG) == assignments()
    assert len(stub.requests) == 1
    call = stub.requests[0]
    schema = call["response_format"]["json_schema"]
    assert schema["name"] == "component_groups" and schema["strict"] is True
    properties = schema["schema"]["properties"]["assignments"]["items"]["properties"]
    assert properties["group"]["enum"] == NAMES
    assert properties["component"]["enum"] == list(RAW)
    assert json.loads(call["messages"][1]["content"])["bom"] == RAW
    assert "closest available group" in call["messages"][0]["content"]


def test_configured_fallback_is_sent_to_model():
    catalog = {**CATALOG, "policy": "fallback", "fallback_group": "custom printed circuit boards"}
    stub = CompletionStub(response(json.dumps(assignments())))
    BasetenClient(stub, "test").classify_components(RAW, catalog)
    assert "fallback group 'custom printed circuit boards'" in stub.requests[0]["messages"][0]["content"]


def test_pdf_preserves_review_and_source_bom_while_robot_proposal_uses_only_groups():
    review = review_result()
    stub = Responses(response(json.dumps(RAW)), response(json.dumps(review)), response(json.dumps(assignments())))
    app, events = app_for(BasetenClient(stub, "test-vision"))
    result = upload(app)
    assert result.status_code == 201, result.json
    run = result.json
    assert run["raw_bom"] == RAW
    assert run["bom"] == {"resistors": 5, "capacitors": 1, "custom printed circuit boards": 1}
    assert run["schematic_review"] == review
    assert run["schematic_review_bom"] == RAW
    assert run["schematic_review_grouped_bom"] == run["bom"]
    assert run["classification_bom"] == run["bom"]
    assert not run["schematic_review_stale"] and not run["classification_stale"]
    assert run["classification_method"] == "llm"
    assert run["state"] == "awaiting_confirmation"
    assert run["group_tags"]["resistors"] == 0
    assert run["tagged_bom"] == {
        "resistors": {"quantity": 5, "tag_id": "tag36h11[0]"},
        "capacitors": {"quantity": 1, "tag_id": "tag36h11[2]"},
        "custom printed circuit boards": {"quantity": 1, "tag_id": "tag36h11[5]"},
    }
    assert app.test_client().get(f"/proposals/{run['id']}").json["tagged_bom"] == run["tagged_bom"]
    assert any("J1 connector" in warning for warning in run["warnings"])
    assert [event[1] for event in events] == ["/detect"]
    assert len(stub.requests) == 3


def test_raw_manual_bom_is_classified_and_registered_group_bom_stays_offline():
    stub = CompletionStub(response(json.dumps(assignments())))
    app, events = app_for(BasetenClient(stub, "test-vision"))
    result = post(app, "/proposals", {"bom": RAW})
    assert result.status_code == 201
    assert result.json["bom"]["resistors"] == 5
    assert len(stub.requests) == 1
    result = post(app, "/proposals", {"bom": {"diodes/LED": 3, "motors": 1}})
    assert result.status_code == 201
    assert result.json["classification_method"] == "explicit_groups"
    assert result.json["bom"] == {"diodes/LED": 3, "motors": 1}
    assert len(stub.requests) == 1
    assert [event[1] for event in events] == ["/detect", "/detect"]
    assert already_grouped(RAW, CATALOG) is None


def test_sch_extraction_is_local_and_classifier_receives_original_values_and_refs():
    calls = []

    class Model:
        def extract_bom(self, *args, **kwargs):
            pytest.fail("SCH extraction must stay local")

        def review_schematic(self, *args, **kwargs):
            pytest.fail("No wiring images are available for this SCH upload")

        def classify_components(self, bom, catalog, *, component_details):
            calls.append((bom, component_details))
            return {"assignments": [{"component": label, "group": "resistors", "confidence": "high", "reason": "Resistor value."} for label in bom]}

    app, _ = app_for(Model())
    result = app.test_client().post("/proposals", data={
        "schematic": (io.BytesIO(kicad(("R1", "10k"), ("R2", "1k")).encode()), "parts.sch")},
        headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})
    assert result.status_code == 201
    assert result.json["bom"] == {"resistors": 2}
    assert result.json["raw_bom"] == {"resistor:10k": 1, "resistor:1k": 1}
    assert calls[0][1][0]["refdes"] == ["R1"]


@pytest.mark.parametrize("reply", [RuntimeError("429 rate limited"), response('{"assignments":[]}')])
def test_classification_failure_preserves_extraction_and_cannot_approve_unknown_labels(reply):
    stub = CompletionStub(reply)
    app, events = app_for(BasetenClient(stub, "test"))
    result = post(app, "/proposals", {"bom": RAW})
    assert result.status_code == 201
    run = result.json
    assert run["raw_bom"] == run["bom"] == RAW
    assert run["classification_error"]
    assert run["tagged_bom"] == {name: {"quantity": qty, "tag_id": None} for name, qty in RAW.items()}
    assert "component_classification" not in run
    assert post(app, f"/proposals/{run['id']}/approve", {"approved": True, "bom": RAW}).status_code == 400
    assert len(stub.requests) == 1  # No automatic retries or silent classifier fallback.
    assert [event[1] for event in events] == ["/detect"]


def test_grouped_input_does_not_need_a_configured_model():
    controller, _ = make_controller()
    def no_model():
        pytest.fail("Explicit group JSON must work offline")
    app = create_app(controller, model_factory=no_model, part_types=NAMES, group_catalog=CATALOG)
    assert post(app, "/proposals", {"bom": {"resistors": 1}}).status_code == 201
    assert app.test_client().get("/status").json["groups"] == CATALOG["groups"]


def test_tagged_bom_tracks_approved_edits_and_command_proposal_responses():
    controller, _ = make_controller()
    app = create_app(controller, part_types=NAMES, group_catalog=CATALOG, command_mode="demo")
    run = post(app, "/commands", {"text": "fetch 3 resistors"}).json["proposal"]
    original = {"resistors": {"quantity": 3, "tag_id": "tag36h11[0]"}}
    assert run["tagged_bom"] == original
    selected = post(app, "/commands", {"text": "run this BOM", "proposal_id": run["id"]})
    assert selected.json["proposal"]["tagged_bom"] == original
    path = f"/proposals/{run['id']}"
    edited = {"capacitors": 2, "diodes/LED": 1}
    assert post(app, path + "/approve", {"approved": True, "bom": edited}).status_code == 202
    result = eventually(lambda: (r if (r := app.test_client().get(path).json)["state"] != "running" else None))
    expected = {"capacitors": {"quantity": 2, "tag_id": "tag36h11[2]"}, "diodes/LED": {"quantity": 1, "tag_id": "tag36h11[8]"}}
    assert result["bom"] == edited
    assert result["tagged_bom"] == expected
    assert list(result["tagged_bom"]) == list(edited)
    status = post(app, "/commands", {"text": "status", "proposal_id": run["id"]})
    assert status.json["run"]["tagged_bom"] == expected


@pytest.mark.parametrize("fault", ["missing_group", "extra_group", "empty_definition", "bad_policy", "bad_fallback"])
def test_bad_group_configuration_rejected_at_startup(tmp_path, fault):
    data = yaml.safe_load((ROOT / "tag_map.yaml").read_text())
    if fault == "missing_group": data["groups"].pop("motors")
    elif fault == "extra_group": data["groups"]["extra"] = {"description": "extra"}
    elif fault == "empty_definition": data["groups"]["motors"]["description"] = " "
    elif fault == "bad_policy": data["classification"]["policy"] = "guess_anything"
    else: data["classification"] = {"policy": "fallback", "fallback_group": "extra"}
    path = tmp_path / "tags.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_group_catalog(path)
