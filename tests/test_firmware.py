import shutil
import subprocess
from pathlib import Path

import pytest


def test_firmware_timers_interruption_limits_and_watchdog(tmp_path):
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("A C++ compiler is needed for the firmware harness")
    directory = Path(__file__).parent / "firmware_stubs"
    binary = tmp_path / "firmware_test"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(directory), str(directory / "test_firmware.cpp"), "-o", str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
