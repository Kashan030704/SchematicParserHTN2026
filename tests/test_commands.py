import json
import threading

import pytest

from orchestrator.commands import (BasetenCommandClient, DemoCommandClient, intent,
                                   is_stop, validate_intent)
from tests.test_baseten import CompletionStub, response
from tests.test_controller import make_controller
from tests.conftest import eventually
from ui.app import create_app

TYPES = ["R3", "C1", "LED_RX"]


def setup_app(*, model=None, mode="demo", fail=None):
    controller, events = make_controller(fail=fail)
    original = controller.pi.get
    controller.pi.get = lambda path: ({"state": "observe", "run_id": None} if path == "/health" else original(path))
    app = create_app(controller, part_types=TYPES, command_mode=mode,
                     command_model_factory=lambda: model)
    return app, controller, events


def post(app, path, data):
    return app.test_client().post(path, json=data, headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})


def command(app, text, **kwargs):
    return post(app, "/commands", {"text": text, **kwargs})


def finish(app, identifier):
    return eventually(lambda: (r if (r := app.test_client().get(f"/proposals/{identifier}").json)["state"] != "running" else None))


def test_baseten_command_is_one_strict_call_with_capabilities_and_no_vision_slug(monkeypatch):
    monkeypatch.delenv("BASETEN_VISION_MODEL", raising=False)
    stub = CompletionStub(response(json.dumps(intent("fetch", parts=[{"type": "LED_RX", "quantity": 3}]))))
    model = BasetenCommandClient(client=stub, command_model="confirmed-command-model")
    result = model.interpret("fetch three LED_RX parts", part_types=TYPES, current_bom={"R3": 2})
    assert result["action"] == "fetch"
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["model"] == "confirmed-command-model"
    assert request["response_format"]["json_schema"]["name"] == "hcp_command"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert "current_bom" in request["messages"][0]["content"]
    assert json.loads(request["messages"][1]["content"])["selected_pending_bom"] == {"R3": 2}


@pytest.mark.parametrize("content", [
    '{"action":"status","action":"stop","parts":[],"seconds":null,"question":""}',
    '{"action":"advance","parts":[],"seconds":NaN,"question":""}',
    json.dumps(intent("raw_drive")),
    json.dumps(intent("fetch", parts=[{"type": "WRONG", "quantity": 1}])),
])
def test_malformed_model_results_rejected(content):
    model = BasetenCommandClient(client=CompletionStub(response(content)), command_model="test")
    with pytest.raises(Exception):
        model.interpret("fetch R3", part_types=TYPES)


@pytest.mark.parametrize("value", [
    intent("fetch"), intent("fetch", parts=[{"type": "R3", "quantity": True}]),
    intent("fetch", parts=[{"type": "R3", "quantity": 1.0}]),
    intent("fetch", parts=[{"type": "R3", "quantity": 1}] * 2),
    {**intent("status"), "seconds": 2}, intent("stop", parts=[{"type": "R3", "quantity": 1}]),
    intent("advance"), {**intent("advance"), "seconds": 2},
    intent("clarify"), {**intent("status"), "approved": True},
])
def test_typed_intent_rejects_unsafe_or_irrelevant_parameters(value):
    with pytest.raises(Exception):
        validate_intent(value, TYPES)


@pytest.mark.parametrize("text", ["stop", "STOP!", "stop robot", "emergency stop."])
def test_stop_shortcut_bypasses_model_and_stale_proposal(text):
    class FailIfCalled:
        def interpret(self, *args, **kwargs):
            raise AssertionError("Stop must not reach a model")
    app, controller, events = setup_app(mode="baseten", model=FailIfCalled())
    result = command(app, text, proposal_id="stale-page")
    assert result.status_code == 200
    assert result.json["interpreter"] == "local_stop"
    assert controller.cancelled.is_set()
    assert {event[1] for event in events} == {"/estop"}


@pytest.mark.parametrize("text", ["don't stop", "what happens if I say stop?", "fetch stop resistors"])
def test_stop_is_not_a_substring_match(text):
    assert not is_stop(text)


def test_fetch_requires_approval_and_preserves_exact_sequence():
    app, _, events = setup_app()
    result = command(app, "fetch 2 R3 and C1")
    assert result.status_code == 201
    proposal = result.json["proposal"]
    assert proposal["bom"] == {"R3": 2, "C1": 1}
    assert proposal["interpreter"] == "demo"
    assert [e[1] for e in events] == ["/detect"]
    endpoint = f"/proposals/{proposal['id']}/approve"
    assert post(app, endpoint, {"approved": True, "bom": proposal["bom"]}).status_code == 202
    completed = finish(app, proposal["id"])
    assert completed["cups_commanded"] == ["R3", "C1"]
    assert [e[1] for e in events] == ["/detect", "/begin"] + ["/detect", "/grasp_place", "/return"] * 2 + ["/end"]
    assert post(app, endpoint, {"approved": True, "bom": proposal["bom"]}).status_code == 409


def test_current_bom_shows_pending_proposal_but_never_approves_or_replays():
    app, _, events = setup_app()
    identifier = post(app, "/proposals", {"bom": {"LED_RX": 3}}).json["id"]
    result = command(app, "run this BOM", proposal_id=identifier)
    assert result.json["proposal"]["id"] == identifier
    assert result.json["proposal"]["state"] == "awaiting_confirmation"
    assert [e[1] for e in events] == ["/detect"]
    post(app, f"/proposals/{identifier}/approve", {"approved": True, "bom": {"LED_RX": 3}})
    finish(app, identifier)
    assert command(app, "run this BOM", proposal_id=identifier).status_code == 409




def test_demo_ambiguous_compound_unknown_and_unsupported_requests_do_not_dispatch():
    app, _, events = setup_app()
    for text in ["fetch R3 and WRONG", "drive forward", "fetch R3 then run belt", "resume", "approve", "run belt for 2 seconds"]:
        result = command(app, text)
        assert result.status_code == 200
        assert result.json["action"] == "clarify"
    assert events == []


def test_removed_action_cannot_be_advertised_or_dispatched_by_a_model():
    model = BasetenCommandClient(client=CompletionStub(response(json.dumps({
        "action": "advance", "parts": [], "question": "", "seconds": 2}))), command_model="test")
    app, _, events = setup_app(mode="baseten", model=model)
    names = {c["name"] for c in app.test_client().get("/commands/capabilities").json["capabilities"]}
    assert names == {"fetch", "current_bom", "detect", "status", "stop", "clarify"}
    assert command(app, "run belt for 2 seconds").status_code == 400
    assert not events


def test_status_and_detect_are_read_only():
    app, _, events = setup_app()
    assert command(app, "status").json["run"] is None
    assert command(app, "what cups can you see?").json["detection"]["rollup"]["R3"]["present"]
    assert [e[1] for e in events] == ["/detect"]


def test_baseten_failure_has_no_demo_fallback_and_does_not_break_uploads():
    model = BasetenCommandClient(client=CompletionStub(RuntimeError("429 rate limited")), command_model="test")
    app, _, events = setup_app(mode="baseten", model=model)
    assert command(app, "fetch R3").status_code == 502
    assert events == []
    assert len(model.transport.client.requests) == 1
    assert post(app, "/proposals", {"bom": {"R3": 1}}).status_code == 201


def test_command_model_is_lazy_and_missing_config_is_clear(monkeypatch):
    monkeypatch.delenv("BASETEN_COMMAND_MODEL", raising=False)
    controller, events = make_controller()
    app = create_app(controller, part_types=TYPES)
    assert app.test_client().get("/").status_code == 200
    result = command(app, "fetch R3")
    assert result.status_code == 400
    assert "BASETEN_COMMAND_MODEL" in result.json["error"]
    assert events == []


def test_commands_require_csrf_and_reject_raw_action_input():
    app, _, events = setup_app()
    assert app.test_client().post("/commands", json={"text": "stop"}).status_code == 403
    assert post(app, "/commands", {"text": "fetch R3", "approved": True}).status_code == 400
    assert post(app, "/commands", {"action": "advance", "seconds": 2}).status_code == 400
    for text in (None, "", " " * 3, "x" * 2001, ["stop"]):
        assert command(app, text).status_code == 400
    assert events == []


def test_busy_run_rejects_new_actions_and_stop_is_latched():
    app, controller, events = setup_app()
    entered, release = threading.Event(), threading.Event()
    original = controller.pi.post
    def paused(path, data):
        if path == "/begin":
            entered.set()
            assert release.wait(5)
        return original(path, data)
    controller.pi.post = paused
    proposal = command(app, "fetch R3").json["proposal"]
    try:
        assert post(app, f"/proposals/{proposal['id']}/approve", {"approved": True, "bom": proposal["bom"]}).status_code == 202
        assert entered.wait(2)
        assert command(app, "run this BOM", proposal_id=proposal["id"]).status_code == 409
        assert command(app, "fetch C1").status_code == 409
        assert command(app, "status").json["run"]["state"] == "running"
        assert command(app, "stop").status_code == 200
    finally:
        release.set()
    assert finish(app, proposal["id"])["state"] == "halted"
    assert command(app, "fetch C1").status_code == 409
    assert "/grasp_place" not in [e[1] for e in events]


def test_stop_during_slow_llm_does_not_wait_and_prevents_late_action():
    entered, release = threading.Event(), threading.Event()
    class SlowModel:
        def interpret(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return intent("fetch", parts=[{"type": "R3", "quantity": 1}])
    app, _, events = setup_app(mode="baseten", model=SlowModel())
    results = []
    worker = threading.Thread(target=lambda: results.append(command(app, "fetch R3")))
    worker.start()
    try:
        assert entered.wait(2)
        assert command(app, "another request").status_code == 409
        assert command(app, "stop").status_code == 200
    finally:
        release.set()
        worker.join(5)
    assert results[0].status_code == 409
    assert {e[1] for e in events} == {"/estop"}






def test_demo_never_picks_arbitrarily_between_case_colliding_cup_labels():
    model = DemoCommandClient()
    result = model.interpret("fetch LED", part_types={"led", "Led"})
    assert result["action"] == "clarify"
    assert model.interpret("fetch Led", part_types={"led", "Led"})["parts"][0]["type"] == "Led"


@pytest.mark.parametrize("mode", ["demo", "baseten"])
def test_command_approval_to_real_http_simulated_robot(mode):
    from pathlib import Path
    from werkzeug.serving import make_server
    from actuator.motion import Robot
    from actuator.perception import DryRunPerception
    from actuator.server import RobotNode, create_app as robot_app
    from config import load_poses, load_tag_map
    from orchestrator.controller import Controller
    from orchestrator.nodes import NodeClient

    root = Path(__file__).resolve().parents[1]
    tags = load_tag_map(root / "tag_map.yaml")
    robot = Robot(load_poses(root / "poses.yaml"), dry_run=True, emit=lambda text: None)
    robot.connect()
    baseline = list(robot.commands)
    node = RobotNode(robot, DryRunPerception(tags, robot), tags, at_observe=True)
    token = "test-node-token-0123456789012345"
    server = make_server("127.0.0.1", 0, robot_app(node, token=token), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    controller = Controller(NodeClient(f"http://127.0.0.1:{server.server_port}", token=token))
    stub = CompletionStub(response(json.dumps(intent("fetch", parts=[{"type": "resistors", "quantity": 2}]))))
    model = BasetenCommandClient(client=stub, command_model="mocked-command-model")
    app = create_app(controller, part_types=tags.values(), command_mode=mode, command_model_factory=lambda: model)
    try:
        proposed = command(app, "fetch 2 resistors").json["proposal"]
        assert robot.commands == baseline
        post(app, f"/proposals/{proposed['id']}/approve", {"approved": True, "bom": proposed["bom"]})
        result = finish(app, proposed["id"])
        assert result["state"] == "complete", result
        assert result["cups_commanded"] == ["resistors"]
        assert node.state == "observe" and node.run_id is None
        assert result["events"][1]["detail"]["aligned"] is True
        if mode == "baseten":
            assert len(stub.requests) == 1  # No paid/provider calls in automated tests.
    finally:
        controller.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
