"""Stage Arduino's matching sketch directory; compile for a confirmed board."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fqbn", required=True, help="Confirmed board FQBN")
    args = parser.parse_args()
    cli = shutil.which("arduino-cli")
    if not cli:
        raise SystemExit("Install arduino-cli and the confirmed board core first")
    firmware = Path(__file__).resolve().parents[1] / "firmware"
    if not (firmware / "pins.h").is_file():
        raise SystemExit("Fill firmware/pins.h from pins.example.h before compiling")
    with tempfile.TemporaryDirectory(prefix="schematic-firmware-") as directory:
        sketch = Path(directory) / "servo_controller"
        sketch.mkdir()
        for name in ("servo_controller.ino", "pins.h"):
            shutil.copyfile(firmware / name, sketch / name)
        subprocess.run([cli, "compile", "--fqbn", args.fqbn, str(sketch)], check=True)


if __name__ == "__main__":
    main()
