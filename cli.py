"""Standalone camera-free, open-loop SO-100-DUPE pick/place CLI."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import replace
from pathlib import Path

from arm.driver import ServoError
from arm.sim_driver import SimDriver
from calibrate import calibrate
from config.so100 import ConfigurationError, load_config
from executor import managed_arm, run_plan
from palette import load_palette
from planner import bom_to_plan


def _hardware_driver(config):
    # Unreachable during --dry-run; no hardware dependency is imported then.
    from arm.pca9685_driver import PCA9685Driver
    config.require_hardware()
    print("WARNING: SG90 has NO position feedback. The first PWM command cannot be slewed from an unknown pose.")
    print("Confirmed STARTUP ANGLE ASSUMPTION:", json.dumps(dict(config.startup_angles), sort_keys=True))
    print("Use your established safe startup fixture/procedure. Do not force SG90 gears by hand.")
    print("Clear/support the arm; home and relax on exit can move/release it. Keep a physical cutoff ready.")
    if input("Type START only if the physical startup pose matches this assumption: ").strip() != "START":
        raise ConfigurationError("Startup pose not confirmed; no I2C hardware opened")
    return PCA9685Driver(config, startup_confirmed=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Camera-free SG90/PCA9685 fixed-palette pick/place (no feedback)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "calibrate", "home"):
        command = commands.add_parser(name)
        command.add_argument("--config", help="Reviewed PWM JSON config; required for hardware")
        command.add_argument("--palette", default="palette.yaml", help="Palette YAML (default: palette.yaml)")
        if name == "run":
            command.add_argument("--bom", required=True, help="Existing BOM JSON or sample_bom.json")
            command.add_argument("--dry-run", action="store_true", help="Print plan/slewed moves; never open I2C")
        elif name == "calibrate":
            command.add_argument("--template", default="palette.yaml", help="Slot identities if output does not exist")
    return parser


def _execute(args):
    config = load_config(args.config)
    if args.command == "calibrate":
        config.require_hardware()
        # Scaffold targets are not moved to. Accept their 0..180 degree shape, but
        # use actual reviewed limits for every live jog and the final saved palette.
        template_config = replace(config, channels={
            j: replace(c, min_angle=0, max_angle=180) for j, c in config.channels.items()})
        template_path = args.palette if Path(args.palette).exists() else args.template
        template = load_palette(template_path, template_config)
        calibrate(_hardware_driver(config), template, args.palette)
        return
    palette = load_palette(args.palette, config)
    palette.validate_all_angles_within_limits(config)
    config = config.with_palette(palette)
    plan = None
    if args.command == "run":
        # Complete planning BEFORE opening a driver or asking to enable PWM.
        plan = bom_to_plan(json.loads(Path(args.bom).read_text()), palette)
        print(f"Ordered pick plan ({len(plan.steps)} picks):", flush=True)
        print(json.dumps(plan.to_dict(), indent=2), flush=True)
        if args.dry_run:
            print("DRY RUN: slewed target estimates only; NO I2C, camera, network, ingestion or feedback.", flush=True)
            run_plan(plan, SimDriver(config))
            print(f"Simulated {len(plan.steps)} pick/place cycles. Physical arrival/grip is not measured.")
            return
    config.require_hardware()
    if not palette.calibrated:
        raise ConfigurationError("Palette is simulation-only/untuned. Run calibrate before real motion.")
    driver = _hardware_driver(config)
    if args.command == "home":
        with managed_arm(driver):
            pass  # home -> relax -> close
    else:
        run_plan(plan, driver)
        print(f"Sent {len(plan.steps)} open-loop pick/place cycles; verify tray contents manually.")


def main(argv=None):
    args = build_parser().parse_args(argv)

    def terminate(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        _execute(args)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. Home/relax cleanup attempted where applicable; check/support the arm.", file=sys.stderr)
        return 130
    except (ValueError, OSError, ServoError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
