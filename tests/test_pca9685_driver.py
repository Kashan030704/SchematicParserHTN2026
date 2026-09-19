import threading
from dataclasses import replace

import pytest

from arm.driver import ServoError
from arm.pca9685_driver import PCA9685Driver, SMBusPWM
from config.so100 import ArmConfig, ChannelConfig, ConfigurationError


class FakeBackend:
    def __init__(self):
        self.pulses = []
        self.disabled = []
        self.closed = False
        self.fail_channel = None
        self.fail_disable = None

    def set_pulse(self, channel, pulse_us):
        if channel == self.fail_channel:
            raise OSError("I2C NACK")
        self.pulses.append((channel, pulse_us))

    def disable(self, channel):
        self.disabled.append(channel)
        if channel == self.fail_disable:
            raise OSError("off NACK")

    def close(self):
        self.closed = True


def driver_fixture(**kwargs):
    config = ArmConfig(hardware_confirmed=True)
    backend = FakeBackend()
    return PCA9685Driver(config, startup_confirmed=True, backend=backend,
                         sleep=kwargs.pop("sleep", lambda _: None), **kwargs), backend


def test_angle_pulse_mapping_and_inversion():
    channel = ChannelConfig(channel=0, min_angle=0, max_angle=180, min_pulse_us=500, max_pulse_us=2400)
    assert channel.pulse_us(0) == 500
    assert channel.pulse_us(90) == 1450
    assert channel.pulse_us(180) == 2400
    inverted = replace(channel, invert=True)
    assert inverted.pulse_us(0) == 2400
    assert inverted.pulse_us(180) == 500
    # Tightening mechanical limits does NOT remap the calibrated pulse endpoints.
    assert replace(channel, min_angle=20, max_angle=160).pulse_us(90) == 1450


def test_all_seven_channels_written_per_slewed_step_with_settle():
    waits = []
    driver, backend = driver_fixture(sleep=waits.append)
    assert not backend.pulses  # Never energize by construction.
    driver.move_to({**driver.config.home_pose, "base": 93, "shoulder": 91})
    assert len(backend.pulses) == 7 * 4  # One initial assumed pose + three ramp frames.
    for index in range(0, len(backend.pulses), 7):
        assert [c for c, _ in backend.pulses[index:index + 7]] == list(range(7))
    assert waits == [0.04] * 4 + [0.25]
    assert driver.commanded_angles["base"] == 93
    assert not hasattr(driver, "read_positions")


def test_gripper_is_slewed_and_settled_too():
    waits = []
    driver, backend = driver_fixture(sleep=waits.append)
    driver.set_gripper("closed")
    assert len(backend.pulses) == 7 * 41
    assert waits[-1] == driver.config.channels["gripper"].settle_ms / 1000
    assert driver.commanded_angles["gripper"] == 100


def test_bad_angle_has_no_pwm_side_effects():
    driver, backend = driver_fixture()
    with pytest.raises(ConfigurationError, match="outside limits"):
        driver.move_to({**driver.config.home_pose, "joint_6": 181})
    assert not backend.pulses


def test_i2c_error_is_loud_and_uncertain_state_cannot_resume():
    driver, backend = driver_fixture()
    driver.home()
    backend.fail_channel = 2
    with pytest.raises(ServoError, match="elbow.*I2C"):
        driver.move_to({**driver.config.home_pose, "base": 92, "elbow": 92})
    assert driver.commanded_angles["base"] == 91  # Already sent first channel.
    assert driver.commanded_angles["elbow"] == 90  # Failed channel not falsely updated.
    with pytest.raises(ServoError, match="uncertain"):
        driver.home()
    driver.relax()
    assert backend.disabled == list(range(7))


def test_relax_attempts_every_channel_even_when_one_fails():
    driver, backend = driver_fixture()
    backend.fail_disable = 1
    with pytest.raises(ServoError, match="shoulder"):
        driver.relax()
    assert backend.disabled == list(range(7))
    with pytest.raises(ServoError, match="relaxed"):
        driver.home()


def test_hardware_and_startup_gates_before_i2c():
    with pytest.raises(ConfigurationError, match="Hardware disabled"):
        PCA9685Driver(ArmConfig())
    with pytest.raises(ConfigurationError, match="unknown"):
        PCA9685Driver(ArmConfig(hardware_confirmed=True))
    with pytest.raises(ConfigurationError, match="unknown"):
        PCA9685Driver(ArmConfig(hardware_confirmed=True), startup_confirmed="no")


def test_nonblocking_move_future_and_overlap_rejection():
    entered, proceed = threading.Event(), threading.Event()

    def wait(seconds):
        entered.set()
        assert proceed.wait(2)

    driver, backend = driver_fixture(sleep=wait)
    future = driver.move_to({**driver.config.home_pose, "base": 92}, blocking=False)
    assert entered.wait(2)
    with pytest.raises(ServoError, match="busy"):
        driver.home()
    proceed.set()
    assert future.result(timeout=2) is None
    assert driver.commanded_angles["base"] == 92
    driver.relax()


def test_relax_stops_background_pwm_before_returning():
    driver, backend = driver_fixture(sleep=None)  # Real interruptible delays, fake I2C.
    future = driver.move_to({**driver.config.home_pose, "base": 160}, blocking=False)
    driver.relax()
    count = len(backend.pulses)
    with pytest.raises(ServoError, match="cancelled"):
        future.result(timeout=2)
    assert len(backend.pulses) == count
    assert backend.disabled == list(range(7))


class FakeBus:
    def __init__(self):
        self.bytes = []
        self.blocks = []
        self.closed = False
        self.fail = False

    def write_byte_data(self, address, register, value):
        if self.fail:
            raise OSError("NACK")
        self.bytes.append((address, register, value))

    def write_i2c_block_data(self, address, register, values):
        if self.fail:
            raise OSError("NACK")
        self.blocks.append((address, register, values))

    def close(self):
        self.closed = True


def test_raw_backend_frequency_pulse_registers_and_full_off():
    bus = FakeBus()
    waits = []
    backend = SMBusPWM(ArmConfig(), bus_factory=lambda bus_id: bus, sleep=waits.append)
    assert bus.bytes[:7] == [(0x40, 0x09 + 4 * i, 0x10) for i in range(7)]
    assert (0x40, 0xFE, 121) in bus.bytes  # ~50 Hz at 25 MHz.
    assert waits == [0.005]
    backend.set_pulse(6, 1500)
    address, register, data = bus.blocks[-1]
    assert address == 0x40 and register == 0x06 + 4 * 6
    on, off = data[0] | data[1] << 8, data[2] | data[3] << 8
    assert on == 6 * 4096 // 16
    assert (off - on) % 4096 == round(1500 * 25_000_000 / 122 / 1_000_000)
    backend.disable(6)
    assert bus.bytes[-1] == (0x40, 0x09 + 4 * 6, 0x10)
    backend.close()
    assert bus.closed


def test_raw_i2c_errors_report_channel_and_address():
    bus = FakeBus()
    backend = SMBusPWM(ArmConfig(), bus_factory=lambda _: bus, sleep=lambda _: None)
    bus.fail = True
    with pytest.raises(ServoError, match="0x40, channel 3"):
        backend.set_pulse(3, 1500)


def test_initialization_failure_closes_bus():
    bus = FakeBus()
    bus.fail = True
    with pytest.raises(ServoError, match="initialization failed"):
        SMBusPWM(ArmConfig(), bus_factory=lambda _: bus, sleep=lambda _: None)
    assert bus.closed
