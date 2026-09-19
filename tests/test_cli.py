"""Exercise the documented integrated entrypoint over real loopback HTTP/TCP."""
import json
import socket
import subprocess
import sys
import time
import urllib.request

from config import ROOT
from tests.conftest import eventually


def test_simulation_entrypoint_serves_and_completes_a_run(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-m", "ui.app", "--simulate", "--hcp-port", "0", "--web-port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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
        assert eventually(ready, timeout=10)["simulation"] is True
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
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stderr.close()
