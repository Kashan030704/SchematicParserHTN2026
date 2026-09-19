"""Exercise the documented integrated entrypoint over real loopback HTTP/TCP."""
import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from config import ROOT
from tests.conftest import eventually


@pytest.mark.parametrize("mode", ["--simulate", "--camera-free", "--palette-hcp"])
def test_simulation_entrypoint_serves_and_completes_a_run(tmp_path, mode):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        hcp_port = reservation.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-m", "ui.app", mode, "--hcp-port", str(hcp_port), "--web-port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    node_process = None
    base = f"http://127.0.0.1:{port}"

    def ready():
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=1) as reply:
                return json.load(reply)
        except OSError:
            return None

    try:
        status = eventually(ready, timeout=10)
        assert status["simulation"] is True
        if mode == "--camera-free":
            assert status["backend"] == "palette" and status["required_nodes"] == []
            assert status["nodes"] == {} and status["ready"]
        elif mode == "--palette-hcp":
            assert status["backend"] == "palette-hcp" and status["required_nodes"] == ["palette_arm"]
            assert not status["ready"]
            node_process = subprocess.Popen([sys.executable, "-m", "arm.hcp_node", "--port", str(hcp_port)],
                                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            eventually(lambda: ready().get("ready"), timeout=10)
        with urllib.request.urlopen(urllib.request.Request(base + "/api/runs", method="POST", data=b""), timeout=2) as reply:
            run_id = json.load(reply)["id"]

        def finished():
            with urllib.request.urlopen(base + f"/api/runs/{run_id}", timeout=2) as reply:
                result = json.load(reply)
                return result if result["state"] in ("complete", "failed", "incomplete") else None

        result = eventually(finished, timeout=15)
        assert result["state"] == "complete", result
        assert len(result["delivered"]) == 5
    finally:
        if node_process is not None:
            node_process.terminate()
            try:
                node_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                node_process.kill()
                node_process.wait(timeout=2)
            node_process.stderr.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stderr.close()
