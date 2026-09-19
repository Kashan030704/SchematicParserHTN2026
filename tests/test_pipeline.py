import copy
import json
import time
from pathlib import Path

import pytest

from config import ROOT, load_hardware
from actuator.kinematics import Kinematics
from ingestion.parse import validate_bom
from orchestrator.loop import Orchestrator, map_inventory, registry_tools
from orchestrator.simulation import DEMO_BOM, FixtureModel, simulation_config
from tests.conftest import eventually
from ui.app import create_app

INVENTORY = json.loads((ROOT / "bench_inventory.json").read_text())


def test_event_placeholders_cannot_enable_hardware():
    with pytest.raises(ValueError, match="Event configuration required"):
        load_hardware(ROOT / "config/hardware.example.json")


def test_ik_reachable_unreachable_and_limits():
    config = simulation_config()["arm"]
    ik = Kinematics(config)
    angles = ik.solve([0.2, 0, 0.03])
    assert len(angles) == 4 and all(0 <= a <= 180 for a in angles)
    with pytest.raises(ValueError, match="Unreachable"):
        ik.solve([2, 0, 0])
    with pytest.raises(ValueError, match="limits"):
        ik.solve([-0.2, 0, 0.03])


def test_bin_mapping_repeats_quantities_and_reports_missing():
    bom = copy.deepcopy(DEMO_BOM)
    bom["components"].append({"type": "resistor", "value": "1k", "quantity": 1, "refdes": ["R3"]})
    units, missing = map_inventory(bom, INVENTORY)
    assert [u["tag_id"] for u in units] == [3, 3, 5, 5, 7]
    assert missing[0]["value"] == "1k"
    inventory = copy.deepcopy(INVENTORY)
    inventory["parts"].append(inventory["parts"][0])
    with pytest.raises(ValueError, match="unambiguous"):
        map_inventory(bom, inventory)


@pytest.mark.parametrize("change", ["negative", "refs", "duplicate"])
def test_semantically_invalid_bom(change):
    bom = copy.deepcopy(DEMO_BOM)
    if change == "negative":
        bom["components"][0]["quantity"] = -1
    elif change == "refs":
        bom["components"][0]["refdes"] = ["R1"]
    else:
        bom["components"][0]["refdes"] = ["R1", "R1"]
    with pytest.raises(ValueError):
        validate_bom(bom)


def test_registry_tools_include_new_node(host):
    host.registry.register({"metadata": {"device_id": "new_sensor", "freetext_desc": "New sensor"},
                            "available_commands": {"read": {"freetext_desc": "Read measurement", "params": [{"enabled": "bool"}]}}}, object())
    tools, routes = registry_tools(host.registry.snapshot())
    assert routes["new_sensor__read"][:2] == ("new_sensor", "read")
    assert tools[0]["function"]["parameters"]["properties"]["enabled"]["type"] == "boolean"


def test_camera_saved_image_poses_and_empty_frame(host, simulation):
    from vision.camera_node import TagDetector
    import numpy as np
    snapshot = host.context_snapshot()["context/tags"]
    tags = snapshot["message"]["payload"]["tags"]
    assert [t["id"] for t in tags] == [3, 5, 7]
    assert tags[0]["x"] == pytest.approx(0.142, abs=0.001)
    assert tags[0]["y"] == pytest.approx(-0.022, abs=0.001)
    assert tags[0]["theta"] == pytest.approx(0, abs=0.01)
    detector = TagDetector(simulation.config["camera"])
    assert detector.detect(np.full((400, 640), 255, dtype=np.uint8)) == []
    with pytest.raises(ValueError, match="resolution"):
        detector.detect(np.zeros((100, 100), dtype=np.uint8))


def test_pdf_to_five_deliveries_through_flask(host, simulation, tmp_path):
    app = create_app(host, FixtureModel(), simulation.config, instance_path=tmp_path / "instance", simulation=simulation)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/status").json["simulation"] is True
    response = client.post("/api/runs", data={"pdf": (simulation.pdf.open("rb"), "demo.pdf")})
    assert response.status_code == 202
    assert client.post("/api/runs").status_code == 409
    run_id = response.json["id"]
    result = eventually(lambda: (r if (r := client.get(f"/api/runs/{run_id}").json)["state"] in ("complete", "incomplete", "failed") else None), timeout=15)
    assert result["state"] == "complete", result
    assert [p["refdes"] for p in result["delivered"]] == ["R1", "R2", "C1", "C2", "U1"]
    commands = simulation.controller.commands
    assert len([c for c in commands if c[0] == "B"]) == 5
    previous_belt_index = 0
    for index, command in enumerate(commands):
        if command[0] == "B":
            moves = [c for c in commands[previous_belt_index:index] if c[0] == "J"]
            assert len(moves) >= 8  # pick and place each finish their four waypoints
            assert moves[-1][1][3] == simulation.config["arm"]["gripper_open_deg"]
            previous_belt_index = index


def test_missing_parts_deliver_available_and_finish_incomplete(host, simulation):
    bom = copy.deepcopy(DEMO_BOM)
    bom["components"].append({"type": "diode", "value": "1N4148", "quantity": 1, "refdes": ["D1"]})
    loop = Orchestrator(host, FixtureModel(), INVENTORY, simulation.config["belt"]["delivery_duration_s"])
    result = loop.run_bom(bom)
    assert result["state"] == "incomplete"
    assert len(result["delivered"]) == 5
    assert "1N4148" in result["warnings"][0]


def test_wrong_part_and_early_belt_calls_never_execute(host, simulation):
    class InvalidModel(FixtureModel):
        def plan(self, messages, tools):
            call = super().plan(messages, tools)
            call["tool_calls"][0]["function"] = {"name": "conveyor__advance", "arguments": '{"duration_s":0.03}'}
            return call
    loop = Orchestrator(host, InvalidModel(), INVENTORY, 0.03)
    with pytest.raises(RuntimeError, match="invalid-tool-call"):
        loop.run_bom(DEMO_BOM)
    assert not any(c[0] in ("J", "B") for c in simulation.controller.commands)


def test_arm_rejects_unseen_tag_without_serial_write(host, simulation):
    before = len(simulation.controller.commands)
    with pytest.raises(RuntimeError, match="not visible"):
        host.command("arm", "pick", {"tag_id": 99}).result(3)
    assert len(simulation.controller.commands) == before


def test_conveyor_stop_interrupts_advance(host, simulation):
    started = time.monotonic()
    advancing = host.command("conveyor", "advance", {"duration_s": 2.0}, timeout=3)
    eventually(lambda: any(c[0] == "B" for c in simulation.controller.commands))
    assert host.command("conveyor", "stop").result(2)["status"] == "ok"
    with pytest.raises(RuntimeError, match="interrupted"):
        advancing.result(2)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("invalid_function", [
    {"name": "arm__pick", "arguments": '{"tag_id":99}'},
    {"name": "arm__pick", "arguments": '{"tag_id":true}'},
    {"name": "invented__pick", "arguments": '{"tag_id":3}'},
])
def test_wrong_part_bad_type_and_unknown_tool_rejected(host, simulation, invalid_function):
    class InvalidPick(FixtureModel):
        def plan(self, messages, tools):
            call = super().plan(messages, tools)
            function = call["tool_calls"][0]["function"]
            if function["name"] == "arm__pick":
                call["tool_calls"][0]["function"] = invalid_function
            return call
    with pytest.raises(RuntimeError, match="invalid-tool-call"):
        Orchestrator(host, InvalidPick(), INVENTORY, 0.03).run_bom(DEMO_BOM)
    assert [c[0] for c in simulation.controller.commands].count("J") == 1  # home only
    assert not any(c[0] == "B" for c in simulation.controller.commands)


def test_calibration_roundtrip_and_collinear_rejection():
    from vision.calibrate import calibrate
    import cv2
    import numpy as np
    points = {"image_size": [640, 400], "pixel_points": [[0, 0], [640, 0], [640, 400], [0, 400]],
              "world_points_m": [[0.1, -0.1], [0.42, -0.1], [0.42, 0.1], [0.1, 0.1]]}
    calibration = calibrate(points)
    result = cv2.perspectiveTransform(np.array([[[320.0, 200.0]]]), np.asarray(calibration["homography"]))
    assert result[0, 0] == pytest.approx([0.26, 0], abs=1e-6)
    points["pixel_points"] = [[0, 0], [1, 0], [2, 0], [3, 0]]
    with pytest.raises(ValueError, match="collinear"):
        calibrate(points)
