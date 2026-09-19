"""Offline CLI plus the real two-node HTTP boundary; no physical devices."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from werkzeug.serving import make_server

from actuator.conveyor_node import create_app as belt_app
from actuator.motion import Robot
from actuator.perception import DryRunPerception
from actuator.server import RobotNode, create_app as robot_app
from config import load_poses, load_tag_map
from orchestrator.controller import Controller
from orchestrator.nodes import NodeClient, NodeError

ROOT = Path(__file__).resolve().parents[1]


def test_dry_run_command():
    result = subprocess.run([sys.executable, "-m", "main", "run", "--bom", "sample_bom.json",
                             "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "one cup per type" in result.stdout
    assert result.stdout.count("CONVEYOR ON") == 3
    assert '"state": "complete"' in result.stdout


def test_unknown_component_errors_before_connect(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"MISSING":1}')
    result = subprocess.run([sys.executable, "-m", "main", "run", "--bom", str(path), "--dry-run"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "No cup mapping for: MISSING" in result.stdout
    assert "command;" not in result.stdout


def test_actual_http_two_nodes_complete():
    tags = load_tag_map(ROOT / "tag_map.yaml")
    robot = Robot(load_poses(ROOT / "poses.yaml"), dry_run=True, emit=lambda text: None)
    robot.connect()
    node = RobotNode(robot, DryRunPerception(tags, robot), tags, at_observe=True)
    servers = [make_server("127.0.0.1", 0, robot_app(node), threaded=True),
               make_server("127.0.0.1", 0, belt_app(emit=lambda text: None), threaded=True)]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads: thread.start()
    try:
        controller = Controller(*(NodeClient(f"http://127.0.0.1:{s.server_port}") for s in servers))
        result = controller.run({"R3": 2, "C1": 1, "LED_RX": 3}, approved=True)
        assert result["cups_commanded"] == list(tags.values())
        assert node.state == "observe"
        assert controller.stop() == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads: thread.join(2)


def test_no_redirect_no_retry_on_http_failure():
    import urllib.error
    class Opener:
        calls = 0
        def open(self, *args, **kwargs):
            self.calls += 1
            raise urllib.error.URLError("connection gone")
    opener = Opener()
    node = NodeClient("http://127.0.0.1:1234", opener=opener)
    with pytest.raises(NodeError, match="NOT retried"):
        node.post("/advance", {"seconds": 3})
    assert opener.calls == 1


def test_conveyor_stop_interrupts_advance():
    started = threading.Event()
    app = belt_app(real_time=True, emit=lambda text: started.set() if " ON " in text else None)
    results = []
    thread = threading.Thread(target=lambda: results.append(
        app.test_client().post("/advance", json={"seconds": 10})))
    thread.start()
    assert started.wait(1)
    assert app.test_client().post("/stop", json={}).status_code == 200
    thread.join(1)
    assert not thread.is_alive()
    assert results[0].status_code == 409
    assert app.test_client().post("/advance", json={"seconds": 1}).status_code == 409


@pytest.mark.parametrize("seconds", [0, -1, 31, True, "3", float("nan")])
def test_invalid_belt_duration_cannot_start(seconds):
    events = []
    response = belt_app(emit=events.append).test_client().post("/advance", json={"seconds": seconds})
    assert response.status_code == 400
    assert events == []
