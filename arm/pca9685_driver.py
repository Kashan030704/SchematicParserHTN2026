"""PCA9685 PWM implementation using the existing smbus2 I2C transport.

Register sequence: https://www.nxp.com/docs/en/data-sheet/PCA9685.pdf
No servo sensors or positional reads exist anywhere in this module.
"""
from __future__ import annotations

import importlib
import logging
import time
from typing import Protocol

from arm.driver import ServoError, SlewDriver
from config.so100 import ArmConfig, ConfigurationError

LOG = logging.getLogger(__name__)


class PWMBackend(Protocol):
    """Small adapter boundary; an Adafruit implementation can replace SMBusPWM."""
    def set_pulse(self, channel: int, pulse_us: float): ...
    def disable(self, channel: int): ...
    def close(self): ...


class SMBusPWM:
    def __init__(self, config: ArmConfig, *, bus_factory=None, sleep=time.sleep):
        self.address = config.i2c_address
        self.clock = config.reference_clock_hz
        self.prescale = round(self.clock / (4096 * config.pwm_frequency_hz)) - 1
        self.bus = None
        self.channels = [c.channel for c in config.channels.values()]
        try:
            if bus_factory is None:
                bus_factory = importlib.import_module("smbus2").SMBus
            self.bus = bus_factory(config.i2c_bus)
            # Full-off every owned output BEFORE changing frequency. No servo PWM
            # is enabled by construction. Own the PCA board exclusively.
            for channel in self.channels:
                self.disable(channel)
            self.bus.write_byte_data(self.address, 0x00, 0x10)  # MODE1 sleep
            self.bus.write_byte_data(self.address, 0xFE, self.prescale)
            self.bus.write_byte_data(self.address, 0x01, 0x04)  # MODE2 totem-pole, update on STOP
            self.bus.write_byte_data(self.address, 0x00, 0x20)  # wake + auto-increment
            sleep(0.005)  # oscillator startup
            self.bus.write_byte_data(self.address, 0x00, 0xA0)  # restart + auto-increment
        except BaseException as exc:
            if self.bus is not None:
                for channel in self.channels:
                    try:
                        self.disable(channel)
                    except BaseException as cleanup:
                        LOG.error("PWM initialization cleanup failed on channel %s: %s", channel, cleanup)
                try:
                    self.close()
                except Exception as cleanup:
                    LOG.error("I2C close failed: %s", cleanup)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ServoError(f"PCA9685 initialization failed on I2C bus {config.i2c_bus}, "
                             f"address {self.address:#x}: {exc}; install requirements-so100.txt on the Pi") from exc

    def set_pulse(self, channel, pulse_us):
        # Use the rounded prescaler's ACTUAL frequency, not the requested 50 Hz.
        ticks = round(pulse_us * self.clock / (self.prescale + 1) / 1_000_000)
        if not 1 <= ticks < 4096:
            raise ServoError(f"Invalid PWM pulse {pulse_us} us")
        # Stagger channel pulse phases to avoid all rising edges coinciding.
        on = channel * 4096 // 16
        off = (on + ticks) % 4096
        try:
            self.bus.write_i2c_block_data(self.address, 0x06 + 4 * channel,
                                         [on & 255, on >> 8, off & 255, off >> 8])
        except Exception as exc:
            raise ServoError(f"I2C PWM write failed at {self.address:#x}, channel {channel}: {exc}") from exc

    def disable(self, channel):
        try:
            # Single OFF_H register write also works before auto-increment setup.
            self.bus.write_byte_data(self.address, 0x09 + 4 * channel, 0x10)
        except Exception as exc:
            raise ServoError(f"I2C full-off failed at {self.address:#x}, channel {channel}: {exc}") from exc

    def close(self):
        self.bus.close()


class PCA9685Driver(SlewDriver):
    def __init__(self, config: ArmConfig, *, startup_confirmed=False, backend: PWMBackend | None = None, sleep=None):
        config.require_hardware()
        if startup_confirmed is not True:
            raise ConfigurationError("Initial physical pose is unknown. Confirm the configured startup_angles "
                                     "before enabling PWM; they are NOT sensor readings.")
        super().__init__(config, sleep=sleep)
        self.backend = backend if backend is not None else SMBusPWM(config)

    def _write_channel(self, joint, angle):
        channel = self.config.channels[joint]
        try:
            self.backend.set_pulse(channel.channel, channel.pulse_us(angle))
        except Exception as exc:
            raise ServoError(f"{joint} PWM/I2C error: {exc}") from exc

    def _disable_channel(self, joint):
        self.backend.disable(self.config.channels[joint].channel)

    def close(self):
        self.backend.close()
