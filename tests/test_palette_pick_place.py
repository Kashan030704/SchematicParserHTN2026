import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import cli
from arm.driver import ArmDriver, ServoError
from arm.sim_driver import SimDriver
from calibrate import calibrate
from config.so100 import ArmConfig, ChannelConfig, ConfigurationError, load_config
from executor import run_plan
from palette import MissingComponent, Palette, PaletteError, load_palette
from planner import PlanningError, bom_to_plan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def palette():
    return load_palette(ROOT / "palette.yaml")


@pytest.fixture
def plan(palette):
    return bom_to_plan(json.loads((ROOT / "sample_bom.json").read_text()), palette)


def quiet_driver(palette, **kwargs):
    return SimDriver(ArmConfig().with_palette(palette), emit=lambda _: None, **kwargs)


def test_sample_config_and_order(palette, plan):
    config = load_config(ROOT / "config/so100.example.json")
    assert not config.hardware_confirmed
    assert len(config.arm_joints) == 6
    assert config.channels["gripper"].channel == 6
    assert [s.slot_id for s in plan.steps] == ["slot-A", "slot-A", "slot-B", "slot-B", "slot-C"]
    assert palette.lookup("ic:NE555").id == "slot-C"
    palette.validate_all_angles_within_limits(config)
    assert not hasattr(ArmDriver, "read_positions")
    assert not hasattr(quiet_driver(palette), "read_positions")


def test_existing_ingestion_adapter(palette):
    plan = bom_to_plan({"components": [{"type": "resistor", "value": "10k", "quantity": 2,
                                       "refdes": ["R1", "R2"]}]}, palette)
    assert [s.component_id for s in plan.steps] == ["resistor:10k", "resistor:10k"]


def test_missing_components_are_all_reported(palette):
    with pytest.raises(MissingComponent, match="NO picks will execute") as caught:
        bom_to_plan({"components": [{"component_id": c, "qty": 1}
                                   for c in ("missing-1", "resistor:10k", "missing-2")]}, palette)
    assert "missing-1" in str(caught.value) and "missing-2" in str(caught.value)


@pytest.mark.parametrize("qty", [0, -1, True, 1.5, "2", None])
def test_invalid_quantities(palette, qty):
    with pytest.raises(PlanningError, match="positive integer"):
        bom_to_plan({"components": [{"component_id": "resistor:10k", "qty": qty}]}, palette)


@pytest.mark.parametrize("angle", [float("nan"), float("inf"), True, "90", 999])
def test_invalid_angles(palette, angle):
    data = palette.to_dict()
    data["slots"][0]["grasp"]["base"] = angle
    with pytest.raises(ConfigurationError):
        Palette.from_dict(data)


def test_duplicate_components_slots_and_yaml_keys(palette, tmp_path):
    data = palette.to_dict()
    data["slots"][1]["components"] = data["slots"][0]["components"]
    with pytest.raises(PaletteError, match="more than one mapping"):
        Palette.from_dict(data)
    data = palette.to_dict()
    data["slots"][1]["id"] = data["slots"][0]["id"]
    with pytest.raises(PaletteError, match="Duplicate slot"):
        Palette.from_dict(data)
    path = tmp_path / "duplicate.yaml"
    path.write_text("home: {}\nhome: {}\n")
    with pytest.raises(PaletteError, match="Duplicate YAML"):
        load_palette(path)


def test_gripper_cannot_be_in_waypoint_and_old_encoder_format_rejected(palette):
    data = palette.to_dict()
    data["home"]["gripper"] = 60
    with pytest.raises(ConfigurationError):
        Palette.from_dict(data)
    data = palette.to_dict()
    data["gripper"] = {"open": 210, "closed": 160}
    with pytest.raises(PaletteError, match="open_deg"):
        Palette.from_dict(data)


def test_palette_uses_its_configured_limits(palette):
    config = ArmConfig()
    channels = {**config.channels, "base": replace(config.channels["base"], min_angle=0)}
    config = replace(config, channels=channels)
    data = palette.to_dict()
    data["home"]["base"] = 10
    loaded = Palette.from_dict(data, config)
    loaded.validate_all_angles_within_limits()
    loaded.home["base"] = 181
    with pytest.raises(ConfigurationError):
        loaded.validate_all_angles_within_limits()


@pytest.mark.parametrize("change", [
    {"move_step_deg": 0}, {"step_delay_ms": 0}, {"step_delay_ms": 10}, {"pwm_frequency_hz": 100},
    {"i2c_address": 256}, {"i2c_bus": True}, {"hardware_confirmed": "yes"},
])
def test_bad_config_rejected(change):
    with pytest.raises(ConfigurationError):
        ArmConfig(**change)


def test_duplicate_channels_rejected():
    config = ArmConfig()
    with pytest.raises(ConfigurationError, match="unique"):
        replace(config, channels={**config.channels, "gripper": ChannelConfig(channel=0)})


def test_ordered_execution_with_slew_and_settle(palette, plan):
    waits = []
    driver = quiet_driver(palette, sleep=waits.append)
    verified = []

    def verify(driver, step):
        assert driver.commanded_angles["gripper"] == palette.gripper.open_deg
        assert {j: driver.commanded_angles[j] for j in driver.config.arm_joints} == step.grasp_angles
        verified.append(step.slot_id)

    run_plan(plan, driver, verify=verify)
    assert verified == [s.slot_id for s in plan.steps]
    commands = driver.commands
    moves = [c["target_angles"] for c in commands if c["command"] == "move_to"]
    assert moves[:10] == [
        dict(palette.home), {"gripper": palette.gripper.open_deg},
        dict(plan.steps[0].approach_angles), dict(plan.steps[0].grasp_angles),
        {"gripper": palette.gripper.closed_deg}, dict(plan.steps[0].approach_angles),
        dict(palette.place_target.approach), dict(palette.place_target.drop),
        {"gripper": palette.gripper.open_deg}, dict(palette.home)]
    previous = dict(driver.config.startup_angles)
    for c in commands:
        if c["command"] == "step":
            driver.config.validate_slew(previous, c["commanded_angles"])
            previous = c["commanded_angles"]
    assert sum(c.get("state") == "closed" for c in commands) == 5
    settles = [c for c in commands if c["command"] == "settle"]
    assert len(settles) == len(moves)
    assert all(c["milliseconds"] > 0 for c in settles)
    assert waits == [c["seconds"] for c in commands if c["command"] == "delay"]
    assert commands[-2]["command"] == "relax" and commands[-1]["command"] == "close"


def test_joint_specific_cap_applies_to_all_frame_axes(palette):
    config = ArmConfig().with_palette(palette)
    config = replace(config, move_step_deg=2, channels={
        **config.channels, "base": replace(config.channels["base"], max_step_deg=0.25)})
    driver = SimDriver(config, emit=lambda _: None)
    target = {**config.home_pose, "base": 93, "elbow": 96}
    driver.move_to(target)
    frames = [c["commanded_angles"] for c in driver.commands if c["command"] == "step"]
    assert len(frames) == 12
    previous = dict(config.startup_angles)
    for frame in frames:
        assert set(frame) == set(config.channels)
        config.validate_slew(previous, frame)
        previous = frame
    assert driver.commanded_angles["base"] == 93


def test_gripper_preserved_through_home_and_command_copy_is_not_mutable(palette):
    driver = quiet_driver(palette)
    driver.set_gripper("closed")
    driver.home()
    assert driver.commanded_angles["gripper"] == palette.gripper.closed_deg
    copy = driver.commanded_angles
    copy["base"] = 999
    assert driver.commanded_angles["base"] != 999


def test_bad_later_step_fails_before_picking(palette, plan):
    driver = quiet_driver(palette)
    plan.steps[-1].grasp_angles["base"] = 999
    with pytest.raises(ConfigurationError):
        run_plan(plan, driver)
    assert not any(c["command"] == "set_gripper" for c in driver.commands)
    assert driver.commands[-2]["command"] == "relax"


def test_mutation_after_preflight_cannot_bypass_limits(palette, plan):
    driver = quiet_driver(palette)

    def verify(driver, step):
        step.approach_angles["base"] = 999

    with pytest.raises(ConfigurationError):
        run_plan(plan, driver, verify=verify)
    assert all(c.get("commanded_angles", {}).get("base") != 999 for c in driver.commands)


@pytest.mark.parametrize("failure", [RuntimeError("external sensor failed"), KeyboardInterrupt(), SystemExit(1)])
def test_failure_interrupt_cleanup(palette, plan, failure):
    driver = quiet_driver(palette)

    def verify(driver, step):
        raise failure

    with pytest.raises(type(failure)):
        run_plan(plan, driver, verify=verify)
    assert driver.commands[-2]["command"] == "relax"
    assert not any(c.get("state") == "closed" for c in driver.commands)


def test_home_failure_still_relaxes_and_preserves_error(palette, plan, monkeypatch, caplog):
    driver = quiet_driver(palette)

    def failed_home():
        raise ServoError("I2C unavailable")

    monkeypatch.setattr(driver, "home", failed_home)
    with pytest.raises(ServoError, match="I2C unavailable"):
        run_plan(plan, driver)
    assert driver.commands[-2]["command"] == "relax"
    assert "cleanup failed" in caplog.text


def test_false_verify_stops_before_close(palette, plan):
    driver = quiet_driver(palette)
    with pytest.raises(ServoError, match="verification rejected"):
        run_plan(plan, driver, verify=lambda *_: False)
    assert not any(c.get("state") == "closed" for c in driver.commands)


def test_calibration_tunes_and_saves_without_feedback(palette, tmp_path):
    driver = quiet_driver(palette)
    responses = []
    # Nine full poses, six joints each. Move every joint by 1 degree for each pose.
    for _ in range(9 * 6):
        responses.extend(["+", "save"])
    responses.extend(["=55", "save", "=105", "save", "SAVE"])
    commands = iter(responses)
    output = tmp_path / "palette.yaml"
    result = calibrate(driver, palette, output, prompt=lambda _: next(commands), emit=lambda _: None)
    assert result.calibrated
    assert result.home["base"] == 91
    assert result.place_target.drop["base"] == 93
    assert result.slots[-1].grasp["base"] == 99
    assert result.gripper.open_deg == 55 and result.gripper.closed_deg == 105
    assert load_palette(output).to_dict() == result.to_dict()
    assert driver.commands[-2]["command"] == "relax"
    previous = dict(driver.config.startup_angles)
    for event in driver.commands:
        if event["command"] == "step":
            driver.config.validate_slew(previous, event["commanded_angles"])
            previous = event["commanded_angles"]


def test_invalid_jog_loudly_rejected_and_cancel_preserves_file(palette, tmp_path):
    driver = quiet_driver(palette)
    commands = iter(["=999", "=nan", "+", "quit"])
    messages = []
    path = tmp_path / "palette.yaml"
    path.write_text("original palette")
    with pytest.raises(KeyboardInterrupt):
        calibrate(driver, palette, path, prompt=lambda _: next(commands), emit=messages.append)
    assert path.read_text() == "original palette"
    assert sum("REJECTED" in m for m in messages) == 2
    assert driver.commands[-2]["command"] == "relax"
    assert all(c.get("commanded_angles", {}).get("base") != 999 for c in driver.commands)


def test_dry_run_forbids_i2c_network_vision_and_feedback_sdk_imports():
    script = '''
import builtins, runpy, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'smbus2', 'board', 'busio', 'adafruit_pca9685', 'adafruit_servokit',
                             'scservo_sdk', 'serial', 'socket', 'vision', 'cv2', 'ingestion', 'openai'}:
        raise AssertionError('Forbidden dry-run import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['main', 'run', '--bom', 'sample_bom.json', '--dry-run']
runpy.run_module('main', run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "Ordered pick plan (5 picks)" in result.stdout
    assert result.stdout.count('"state": "closed"') == 5
    assert '"command": "step"' in result.stdout
    assert "Simulated 5 pick/place cycles" in result.stdout


def test_missing_cli_opens_no_hardware(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli, "_hardware_driver", lambda _: pytest.fail("hardware opened"))
    path = tmp_path / "bom.json"
    path.write_text(json.dumps({"components": [{"component_id": "unknown:part", "qty": 1}]}))
    assert cli.main(["run", "--bom", str(path)]) == 2
    assert "unknown:part" in capsys.readouterr().err


def test_dummy_palette_cannot_run_hardware(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli, "_hardware_driver", lambda _: pytest.fail("hardware opened"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hardware_confirmed": True}))
    assert cli.main(["home", "--config", str(path)]) == 2
    assert "simulation-only" in capsys.readouterr().err


def test_declined_startup_confirmation_never_opens_i2c(monkeypatch):
    import arm.pca9685_driver as hardware

    monkeypatch.setattr(hardware, "SMBusPWM", lambda _: pytest.fail("I2C opened"))
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(ConfigurationError, match="not confirmed"):
        cli._hardware_driver(ArmConfig(hardware_confirmed=True))
