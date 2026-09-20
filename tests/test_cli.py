"""Offline CLI plus the real RoboMaster HTTP boundary; no physical devices."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from werkzeug.serving import make_server

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
    assert result.stdout.count('"op": "complete_type"') == 3
    assert '"state": "complete"' in result.stdout


def test_unknown_component_errors_before_connect(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"MISSING":1}')
    result = subprocess.run([sys.executable, "-m", "main", "run", "--bom", str(path), "--dry-run"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "No cup mapping for: MISSING" in result.stdout
    assert "command;" not in result.stdout


def test_actual_http_robot_node_complete():
    tags = load_tag_map(ROOT / "tag_map.yaml")
    robot = Robot(load_poses(ROOT / "poses.yaml"), dry_run=True, emit=lambda text: None)
    robot.connect()
    node = RobotNode(robot, DryRunPerception(tags, robot), tags, at_observe=True)
    server = make_server("127.0.0.1", 0, robot_app(node), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        controller = Controller(NodeClient(f"http://127.0.0.1:{server.server_port}"))
        result = controller.run(dict.fromkeys(tags.values(), 1), approved=True)
        assert result["cups_commanded"] == list(tags.values())
        assert node.state == "observe"
        assert controller.stop() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


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
        node.post("/grasp_place", {"run_id": "r1", "type": "R3"})
    assert opener.calls == 1
