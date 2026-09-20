import copy

from orchestrator.schematic_advice import build_schematic_suggestions
from tests.test_controller import make_controller
from ui.app import create_app


def test_incoming_low_resistance_warning_preserved():
    suggestions = build_schematic_suggestions({"components": [
        {"type": "resistor", "value": "0R", "quantity": 1, "refdes": ["R1"]},
        {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]},
    ]})
    assert suggestions[0].startswith("These are advisory checks")
    assert any("short" in suggestion and "R1" in suggestion for suggestion in suggestions)
    assert any("0.1uF" in suggestion and "U1" in suggestion for suggestion in suggestions)


def test_flat_bom_advice_does_not_mutate_requirements():
    bom = {"resistor:0R": 1, "ic:NE555": 1, "capacitor:0.1uF": 1}
    original = copy.deepcopy(bom)
    notes = build_schematic_suggestions(bom)
    assert bom == original
    assert any("short" in note for note in notes)
    assert any("decoupling" in note for note in notes)
    assert "not a separate LLM review" in notes[0]


def test_label_only_bom_does_not_invent_values():
    notes = build_schematic_suggestions({"R3": 2, "C1": 1, "LED_RX": 3})
    assert any("lack component values" in note for note in notes)
    assert not any("very low resistance" in note for note in notes)


def test_suggestions_are_visible_before_any_motion():
    controller, events = make_controller()
    app = create_app(controller, part_types=["R3"])
    response = app.test_client().post("/proposals", json={"bom": {"R3": 2}},
        headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})
    assert response.status_code == 201
    assert response.json["suggestions_kind"] == "rule_based_advisory"
    assert response.json["llm_schematic_suggestions"]
    assert [e[1] for e in events] == ["/detect"]


def test_incoming_frontend_features_keep_approval_and_stop_controls():
    controller, _ = make_controller()
    app = create_app(controller, part_types=["R3"])
    page = app.test_client().get("/").get_data(as_text=True)
    for element in ("llm-suggestions", "requested-parts", "run-form", "bom-json",
                    "approve", "confirm", "stop", "detection", "nodes"):
        assert f'id="{element}"' in page
    assert ".sch" in page and "TIFF" in page and "no motion" in page
    assert "{{UI_TOKEN}}" not in page
    assert "SG90" not in page
    assert "<title>Schematic to Fetch</title>" in page
    assert "<h1>Schematic to Fetch</h1>" in page
    assert "Hardware Context Protocol" not in page and "Ask HCP" not in page
