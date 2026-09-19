"""BOM planner -> existing HCP TCP client/node -> locally owned ArmDriver."""
import math
import threading
import time
import uuid

from arm.palette_contract import palette_id
from orchestrator.palette_backend import PaletteBackend


class RemotePaletteBackend(PaletteBackend):
    device_id = "palette_arm"

    def __init__(self, host, palette, config, ingestion_model, *, allow_hardware=False, **kwargs):
        super().__init__(palette, config, ingestion_model, **kwargs)
        self.host = host
        self.allow_hardware = allow_hardware
        self.palette_id = palette_id(palette)
        self.metadata.update(backend="palette-hcp", hardware="physical" if allow_hardware else "simulated",
                             transport="hcp-tcp")
        if allow_hardware:
            self.demo_pdf = None  # Never send canned PDF fixture parts to real motors.

    def status(self):
        nodes = self.host.registry.snapshot()
        with self.host.lock:
            health = dict(self.host.health)
        snapshot = health.get(self.device_id, {})
        state = snapshot.get("payload", {})
        ready = (self.device_id in nodes and state.get("ready") is True
                 and state.get("hardware") is self.allow_hardware
                 and state.get("palette_id") == self.palette_id
                 and time.monotonic() - snapshot.get("received_at", 0) < 2)
        return {"nodes": nodes, "health": health, "ready": ready, "required_nodes": [self.device_id]}

    def run_pdf(self, path, update=lambda **kw: None):
        if self.allow_hardware and self.ingestion_mode != "live":
            raise ValueError("Physical runs require a reviewed JSON BOM or --live-ingestion; fixture PDFs are refused")
        return super().run_pdf(path, update)

    def _command(self, action, payload=None, *, timeout=3):
        reply = self.host.command(self.device_id, action, payload or {}, timeout=timeout).result(timeout=timeout + 1)
        result = reply.get("result")
        if reply.get("status") != "ok" or not isinstance(result, dict):
            raise RuntimeError(f"Malformed {action} completion from palette_arm")
        return result

    def _validate_state(self, state):
        if state.get("hardware") is not self.allow_hardware:
            raise ValueError("Node hardware mode mismatch; real motors require backend --allow-hardware AND local arming")
        if state.get("palette_id") != self.palette_id:
            raise ValueError("Palette fingerprint mismatch: backend and arm node must use the same palette")

    def _run_bom(self, bom, update):
        plan, references = self._prepare_bom(bom, update)
        # Resolve EVERY part before contacting the arm or beginning a session.
        state = self._command("get_state")
        self._validate_state(state)
        if state.get("ready") is not True:
            raise RuntimeError("palette_arm is not ready; check node connection and local ARM/START confirmation")
        timeout, lease = state.get("command_timeout_s"), state.get("lease_s")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 3600
                or type(lease) not in (int, float) or not math.isfinite(lease) or not 0.2 <= lease <= 30):
            raise ValueError("Invalid node command timeout/heartbeat lease")
        run_id = uuid.uuid4().hex
        completed = []
        stop = threading.Event()
        failures = []
        heartbeat_thread = None
        begun = False
        update(state="running", step="Beginning HCP palette session", plan=plan.to_dict(),
               requested=len(plan.steps), available=len(plan.steps), delivered=[], warnings=[],
               session_id=run_id, events=[], motion_steps=0, commanded_angles=state.get("commanded_angles", {}))

        def heartbeat():
            while not stop.wait(min(0.5, lease / 3)):
                try:
                    progress = self._command("heartbeat", {"run_id": run_id})
                    if not stop.is_set():
                        update(commanded_angles=progress.get("commanded_angles", {}),
                               motion_steps=progress.get("motion_steps", 0))
                except Exception as exc:
                    if not stop.is_set():
                        failures.append(exc)
                    return

        try:
            # Mark before sending: even a lost begin reply needs best-effort abort.
            begun = True
            self._command("begin_run", {"run_id": run_id, "palette_id": self.palette_id,
                                         "hardware": self.allow_hardware})
            heartbeat_thread = threading.Thread(target=heartbeat, name="palette-run-heartbeat", daemon=True)
            heartbeat_thread.start()
            for index, step in enumerate(plan.steps):
                if failures:
                    raise RuntimeError(f"HCP heartbeat failed: {failures[0]}")
                update(step=f"Commanding {index + 1}/{len(plan.steps)}: {step.component_id} from {step.slot_id}",
                       current_pick=index + 1)
                result = self._command("pick_place", {"run_id": run_id, "unit": index + 1,
                    "component_id": step.component_id, "slot_id": step.slot_id}, timeout=timeout)
                if failures:
                    raise RuntimeError(f"HCP heartbeat failed: {failures[0]}")
                self._validate_state(result)
                if (result.get("unit") != index + 1 or result.get("component_id") != step.component_id
                        or result.get("slot_id") != step.slot_id):
                    raise RuntimeError("Pick completion does not match the requested unit/component/slot")
                completed.append({"component_id": step.component_id, "slot_id": step.slot_id,
                                  "unit": index + 1, "refdes": references[index],
                                  "simulated": not self.allow_hardware, "physical_delivery_verified": False})
                update(delivered=list(completed), commanded_angles=result.get("commanded_angles", {}),
                       motion_steps=result.get("motion_steps", 0))
            final = self._command("end_run", {"run_id": run_id}, timeout=timeout)
            begun = False
            return {"state": "complete", "delivered": completed, "warnings": [],
                    "commanded_angles": final.get("commanded_angles", {}),
                    "motion_steps": final.get("motion_steps", 0), "physical_delivery_verified": False}
        finally:
            stop.set()
            if begun:
                try:
                    self._command("abort_run", {"run_id": run_id})
                except Exception:
                    # Never retry a movement. Link loss/lease expiry disables PWM
                    # locally; a dead Pi/I2C bus still requires physical cutoff.
                    pass
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=4)
