"""Open-loop simulation: identical ramp math, logged delays, zero hardware imports."""
from __future__ import annotations

import json

from arm.driver import SlewDriver
from config.so100 import ArmConfig


class SimDriver(SlewDriver):
    def __init__(self, config: ArmConfig | None = None, emit=print, *, sleep=None,
                 event_sink=None, record_commands=True):
        self.commands = []
        self.emit = emit
        self.event_sink = event_sink
        self.record_commands = record_commands
        # Fast by default, but every requested delay is logged and testable.
        super().__init__(config or ArmConfig(), sleep=sleep or (lambda seconds: None), event_sink=event_sink)

    def _event(self, command, **data):
        event = {"command": command, **data}
        if self.record_commands:
            self.commands.append(event)
        if self.emit is not None:
            self.emit("SIM " + json.dumps(event, sort_keys=True))
        if self.event_sink is not None:
            self.event_sink(event)

    def _pause(self, seconds):
        self._event("delay", seconds=seconds)
        super()._pause(seconds)

    def _write_channel(self, joint, angle):
        # No transport: _write_frame tracks only commanded target estimates.
        pass

    def _disable_channel(self, joint):
        self._event("pwm_off", joint=joint, channel=self.config.channels[joint].channel)

    def close(self):
        self._event("close")
