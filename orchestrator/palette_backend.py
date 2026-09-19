"""Existing ingestion -> fixed palette -> SimDriver. No hardware-mode switch."""
import copy
from collections import deque
import threading

from arm.sim_driver import SimDriver
from executor import run_plan
from ingestion.parse import parse_pdf
from orchestrator.schematic_advice import build_schematic_suggestions
from palette import MissingComponent
from planner import bom_to_plan


class PaletteBackend:
    def __init__(self, palette, config, ingestion_model, *, live_ingestion=False, demo_pdf=None):
        palette.validate_all_angles_within_limits(config)
        self.palette = palette
        self.config = config.with_palette(palette)
        self.ingestion_model = ingestion_model
        self.ingestion_mode = "live" if live_ingestion else "fixture"
        self.demo_pdf = demo_pdf
        self.run_lock = threading.Lock()
        self.metadata = {"backend": "palette", "hardware": "simulated", "ingestion": self.ingestion_mode,
                         "physical_delivery_verified": False}

    def run_pdf(self, path, update=lambda **kw: None):
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("A run is already active")
        try:
            update(state="parsing", step="Reading schematic", input_kind="pdf", **self.metadata)
            return self._run_bom(parse_pdf(path, self.ingestion_model), update)
        finally:
            self.run_lock.release()

    def run_bom(self, bom, update=lambda **kw: None):
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("A run is already active")
        try:
            update(input_kind="bom", **self.metadata)
            return self._run_bom(bom, update)
        finally:
            self.run_lock.release()

    def _prepare_bom(self, bom, update):
        suggestions = build_schematic_suggestions(bom)
        update(state="planning", step="Resolving fixed palette slots", bom=bom,
               llm_schematic_suggestions=suggestions)
        self.palette.validate_all_angles_within_limits(self.config)
        # Bound untrusted HTTP/model quantities BEFORE planner expansion.
        components = bom.get("components") if isinstance(bom, dict) else None
        if isinstance(components, list):
            count = 0
            for line in components:
                quantity = line.get("qty", line.get("quantity")) if isinstance(line, dict) else None
                if type(quantity) is int and quantity > 0:
                    count += quantity
            if count > 200:
                raise ValueError("Camera-free runs are limited to 200 picks per run")
        try:
            plan = bom_to_plan(bom, self.palette)
        except MissingComponent as exc:
            update(warnings=[str(exc)])
            raise
        # Preserve refdes when the existing ingestion schema supplies them.
        references = []
        for line in bom["components"]:
            qty = line.get("qty", line.get("quantity"))
            refs = line.get("refdes", [None] * qty)
            if not isinstance(refs, list) or len(refs) != qty or any(
                    r is not None and (not isinstance(r, str) or not r.strip()) for r in refs):
                raise ValueError("BOM refdes must be a list matching the component quantity")
            references.extend(refs)
        present_refs = [r for r in references if r is not None]
        if len(present_refs) != len(set(present_refs)):
            raise ValueError("BOM refdes must be unique")
        return plan, references, suggestions

    def _run_bom(self, bom, update):
        plan, references, suggestions = self._prepare_bom(bom, update)
        completed, events = [], deque(maxlen=80)
        motion_steps = 0
        update(state="running", step="Starting camera-free simulation", plan=plan.to_dict(),
               requested=len(plan.steps), available=len(plan.steps), delivered=[], warnings=[],
               llm_schematic_suggestions=suggestions, events=[], motion_steps=0,
               commanded_angles=dict(self.config.startup_angles))

        def event_sink(event):
            nonlocal motion_steps
            if event["command"] == "step":
                motion_steps += 1
                if motion_steps % 25:
                    return
            elif event["command"] == "delay":
                return  # Do not flood HTTP snapshots with every timing tick.
            else:
                events.append(copy.deepcopy(event))
            values = {"events": list(events), "motion_steps": motion_steps}
            if "commanded_angles" in event:
                values["commanded_angles"] = event["commanded_angles"]
            update(**values)

        def progress(phase, index, step):
            if phase == "starting":
                update(step=f"Simulating {index + 1}/{len(plan.steps)}: {step.component_id} from {step.slot_id}",
                       current_pick=index + 1)
            else:
                completed.append({"component_id": step.component_id, "slot_id": step.slot_id,
                                  "refdes": references[index], "unit": index + 1, "simulated": True})
                update(delivered=completed)

        # Hard-wired software driver: neither config nor an HTTP parameter can
        # enable PCA9685, serial, camera, conveyor, or physical HCP nodes here.
        driver = SimDriver(self.config, emit=None, event_sink=event_sink, record_commands=False)
        run_plan(plan, driver, on_progress=progress)
        return {"state": "complete", "delivered": completed, "warnings": [],
                "llm_schematic_suggestions": suggestions,
                "motion_steps": motion_steps, "events": list(events),
                "commanded_angles": driver.commanded_angles, "physical_delivery_verified": False}
