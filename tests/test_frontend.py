"""Run dependency-free JS workflow regressions when Node is available on the dev host."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional on the Pi")
def test_browser_workflow():
    result = subprocess.run(
        [shutil.which("node"), "--test", str(Path(__file__).with_name("frontend.test.cjs"))],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
