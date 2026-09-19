import math
import threading


class ConveyorNode:
    def __init__(self, client, controller, config):
        self.client, self.controller = client, controller
        self.maximum = config["belt"]["max_duration_s"]
        self.cancelled = threading.Event()
        self.fault = False
        client.register_handler("advance", self.advance)
        client.register_handler("stop", self.stop, urgent=True)
        client.on_disconnect = self.abort

    def advance(self, duration_s):
        if self.fault:
            raise RuntimeError("Conveyor session interrupted; inspect and restart actuator")
        if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 < duration_s <= self.maximum:
            raise ValueError("Duration exceeds calibrated conveyor bounds")
        self.cancelled.clear()
        self.client.status("running", duration_s=duration_s)
        try:
            self.controller.advance(duration_s)
            if self.cancelled.is_set():
                raise RuntimeError("Advance interrupted")
        finally:
            self.controller.stop_belt()
            self.client.status("stopped")

    def stop(self):
        self.cancelled.set()
        self.controller.stop_belt()
        self.client.status("stopped")

    def abort(self):
        self.fault = True
        self.cancelled.set()
        self.controller.abort()
