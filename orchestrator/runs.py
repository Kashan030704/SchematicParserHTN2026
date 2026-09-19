"""Shared background run lifecycle, independent of HCP and physical drivers."""
import copy
import threading
import uuid


class RunManager:
    def __init__(self, orchestrator, metadata=None):
        self.orchestrator = orchestrator
        self.metadata = copy.deepcopy(metadata or {})
        self.lock = threading.RLock()
        self.runs = {}
        self.active = None

    def submit(self, path, simulation=False):
        return self._submit(path, simulation, "pdf")

    def submit_bom(self, bom, simulation=False):
        return self._submit(copy.deepcopy(bom), simulation, "bom")

    def _submit(self, source, simulation, kind):
        with self.lock:
            if self.active is not None:
                raise RuntimeError("A run is already active")
            run_id = uuid.uuid4().hex
            self.active = run_id
            self.runs[run_id] = {**self.metadata, "id": run_id, "state": "queued", "step": "Queued",
                                 "warnings": [], "llm_schematic_suggestions": [],
                                 "delivered": [], "simulation": simulation}
            threading.Thread(target=self._run, args=(run_id, source, kind), daemon=True).start()
            return run_id

    def _run(self, run_id, source, kind):
        def update(**values):
            with self.lock:
                self.runs[run_id].update(copy.deepcopy(values))
        try:
            method = self.orchestrator.run_pdf if kind == "pdf" else self.orchestrator.run_bom
            result = method(source, update)
            update(**result, step="Finished")
        except Exception as exc:
            update(state="failed", step="Stopped", error=str(exc))
        finally:
            with self.lock:
                self.active = None

    def get(self, run_id):
        with self.lock:
            return copy.deepcopy(self.runs[run_id])
