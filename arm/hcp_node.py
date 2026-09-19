"""Camera-free HCP node. Wire commands name slots, never untrusted joint angles."""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
import time

from arm.palette_contract import command_timeout, palette_id
from arm.sim_driver import SimDriver
from config import ROOT
from config.so100 import load_config
from executor import execute_pick
from hcp_sdk.loader import load_client
from palette import load_palette
from planner import PickStep

LOG = logging.getLogger(__name__)


class PaletteArmNode:
    def __init__(self, client, palette, config, *, hardware=False, lease_s=5.0, sim_factory=None):
        palette.validate_all_angles_within_limits(config)
        self.client, self.palette = client, palette
        self.config = config.with_palette(palette)
        self.hardware = hardware
        if hardware:
            self.config.require_hardware()
            if not palette.calibrated:
                raise ValueError("Tune a local calibrated palette before enabling hardware")
        if not 0.2 <= lease_s <= 30:
            raise ValueError("lease_s must be between 0.2 and 30 seconds")
        self.lease_s = lease_s
        self.palette_id = palette_id(palette)
        self.timeout = command_timeout(self.config)
        self.sim_factory = sim_factory or (lambda: SimDriver(self.config, emit=None, record_commands=False))
        self.lock, self.stop_lock = threading.RLock(), threading.Lock()
        self.stopped = threading.Event()
        self.driver = None
        self.state = "disarmed" if hardware else "ready"
        self.active_run = self.last_run = None
        self.deadline = None
        self.next_unit, self.motion_steps = 1, 0
        self.angles = dict(self.config.startup_angles)
        self.error = None
        for action in ("get_state", "begin_run", "pick_place", "heartbeat", "end_run", "abort_run"):
            client.register_handler(action, getattr(self, action), urgent=action in ("get_state", "heartbeat", "abort_run"))
        client.on_disconnect = lambda: self._release("TCP disconnected")

    def get_state(self):
        with self.lock:
            return {"state": self.state, "ready": self.state == "ready", "hardware": self.hardware,
                    "palette_id": self.palette_id, "active_run": self.active_run,
                    "commanded_angles": dict(self.angles), "motion_steps": self.motion_steps,
                    "command_timeout_s": self.timeout, "lease_s": self.lease_s,
                    "error": self.error, "physical_delivery_verified": False}

    def arm_local(self, driver):
        """Only the operator CLI calls this; there is deliberately no wire equivalent."""
        with self.lock:
            if (not self.hardware or self.state != "disarmed" or self.stopped.is_set()
                    or not self.client.connected.is_set()):
                raise RuntimeError("Local arming requires a connected, disarmed physical node")
            if driver.config != self.config:
                raise ValueError("Driver configuration does not match the node")
            self.driver = driver
            driver.event_sink = self._event
            self.angles = dict(driver.commanded_angles)
            self.state, self.error = "ready", None
            self.deadline = time.monotonic() + 30  # Unused local arming expires too.

    def _event(self, event):
        with self.lock:
            if event["command"] == "step":
                self.motion_steps += 1
            if "commanded_angles" in event:
                self.angles = dict(event["commanded_angles"])

    def begin_run(self, run_id, palette_id, hardware):
        with self.lock:
            if not isinstance(run_id, str) or not run_id.strip() or run_id == self.last_run:
                raise ValueError("A fresh nonempty run_id is required")
            if palette_id != self.palette_id or hardware is not self.hardware:
                raise ValueError("Palette fingerprint or hardware mode mismatch")
            if self.state != "ready" or self.stopped.is_set() or not self.client.connected.is_set():
                raise RuntimeError("Node is not ready; physical runs require local arming")
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise RuntimeError("Local arming expired; wait for disarm, then arm locally again")
            if not self.hardware:
                self.driver = self.sim_factory()
                self.driver.event_sink = self._event
            self.active_run, self.state = run_id, "running"
            self.next_unit, self.motion_steps, self.error = 1, 0, None
            self.deadline = time.monotonic() + self.lease_s
            return self.get_state()

    def _require_run(self, run_id):
        if self.state != "running" or self.active_run != run_id or self.driver is None:
            raise RuntimeError("No matching active run")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("Heartbeat lease expired")
        return self.driver

    def heartbeat(self, run_id):
        with self.lock:
            # A heartbeat can race successful end_run; it must never re-arm.
            if self.active_run is None and run_id == self.last_run:
                return self.get_state()
            self._require_run(run_id)
            self.deadline = time.monotonic() + self.lease_s
            return self.get_state()

    def pick_place(self, run_id, unit, component_id, slot_id):
        with self.lock:
            driver = self._require_run(run_id)
        try:
            with self.lock:
                if type(unit) is not int or unit != self.next_unit:
                    raise ValueError(f"Expected unit {self.next_unit}; no repeated/skipped units")
                slot = self.palette.lookup(component_id)
                if slot.id != slot_id:
                    raise ValueError("Component does not belong to the requested slot")
            step = PickStep(component_id, slot.id, slot.approach, slot.grasp, self.palette.place_target)
            execute_pick(step, driver)
            with self.lock:
                if self._require_run(run_id) is not driver:
                    raise RuntimeError("Run cancelled during pick")
                self.next_unit += 1
                return {**self.get_state(), "unit": unit, "component_id": component_id, "slot_id": slot_id}
        except BaseException:
            self._release("Pick failed; inspect arm before retrying", expected=driver)
            raise

    def end_run(self, run_id):
        with self.lock:
            driver = self._require_run(run_id)
        try:
            driver.home()
        finally:
            self._release(None, expected=driver)
        result = self.get_state()
        if result["state"] == "fault":
            raise RuntimeError(result["error"])
        return result

    def abort_run(self, run_id):
        with self.lock:
            if self.active_run != run_id:
                raise RuntimeError("Abort refused: no matching active run")
            driver = self.driver
        self._release("Run aborted", expected=driver)
        return self.get_state()

    def _release(self, reason, *, expected=None):
        # No autonomous homing on emergency/link loss. Disable PWM, without any
        # further ramp. A supported arm and physical power cutoff are mandatory.
        with self.stop_lock:
            with self.lock:
                driver = self.driver
                if driver is None or (expected is not None and driver is not expected):
                    return
                self.state = "stopping"
                self.driver = None
                self.last_run, self.active_run = self.active_run, None
                self.deadline = None
            errors = []
            for action in (driver.relax, driver.close):
                try:
                    action()
                except Exception as exc:
                    errors.append(str(exc))
            with self.lock:
                self.angles = dict(driver.commanded_angles)
                self.error = "; ".join(errors) if errors else reason
                self.state = "fault" if errors else ("disarmed" if self.hardware else "ready")
            if errors:
                LOG.error("PWM disable/close failed; use physical cutoff: %s", self.error)

    def start(self):
        self.client.start()
        threading.Thread(target=self._monitor, name="palette-arm-watchdog", daemon=True).start()
        return self

    def _monitor(self):
        next_status = 0
        while not self.stopped.wait(0.05):
            with self.lock:
                expired = self.deadline is not None and time.monotonic() >= self.deadline
                driver = self.driver
            if expired:
                self._release("Heartbeat/local arming expired", expected=driver)
            if time.monotonic() >= next_status:
                next_status = time.monotonic() + 0.5
                try:
                    self.client.status(**self.get_state())
                except queue.Full:
                    # Telemetry backpressure must NEVER kill the lease watchdog.
                    pass

    def stop(self):
        self.stopped.set()
        self._release("Node stopped")
        self.client.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--palette", default=str(ROOT / "palette.yaml"))
    parser.add_argument("--config")
    parser.add_argument("--hardware", action="store_true", help="Physical PCA9685; requires local ARM and START confirmation")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    palette = load_palette(args.palette, config)
    client = load_client("palette_arm", args.host, args.port, ROOT / "out")
    node = PaletteArmNode(client, palette, config, hardware=args.hardware)
    def terminate(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        node.start()
        if not args.hardware:
            print("Simulated palette_arm node; NO I2C or physical motion. Ctrl-C to stop.", flush=True)
            while not node.stopped.wait(0.5):
                pass
        else:
            from cli import _hardware_driver
            while True:
                print("Physical node is disarmed between runs. Arming expires after 30 seconds unused.")
                command = input("Type ARM to confirm a new physical session, or QUIT: ").strip()
                if command == "QUIT":
                    break
                if command != "ARM":
                    continue
                if node.get_state()["state"] != "disarmed" or not client.connected.is_set():
                    print("Wait until connected and disarmed. A fault requires inspection/restart.")
                    continue
                driver = _hardware_driver(node.config)
                try:
                    node.arm_local(driver)
                except BaseException:
                    try:
                        driver.relax()
                    finally:
                        driver.close()
                    raise
                print("Armed. Submit one run within 30 seconds; keep the arm supported and cutoff ready.")
                while node.get_state()["state"] in ("ready", "running", "stopping"):
                    time.sleep(0.1)
    except (KeyboardInterrupt, EOFError):
        print("Stopping; attempting PWM disable. Check/support the arm.")
    finally:
        node.stop()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
