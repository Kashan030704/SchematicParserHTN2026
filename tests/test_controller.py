import pytest
from orchestrator.controller import Controller


class NodeStub:
    def __init__(self, events, *, fail=None, absent=()):
        self.events, self.fail, self.absent = events, fail, absent
    def call(self, method, path, payload=None, **kwargs):
        self.events.append((method, path, payload))
        if path == self.fail:
            raise RuntimeError("injected failure")
        if path == "/detect":
            return {"ts": 1, "rollup": {part: {"count": 1, "present": True}
                for part in ("R3", "C1", "LED_RX") if part not in self.absent}}
        if path == "/grasp_place":
            return {"ok": True, "type": payload["type"],
                    "steps": [{"op": op, "ok": True} for op in ("grasp", "drive", "drop")]}
        if path == "/return":
            return {"ok": True, "type": payload["type"],
                    "steps": [{"op": op, "ok": True} for op in ("return", "observe")]}
        return {"ok": True}
    def get(self, path): return self.call("GET", path)
    def post(self, path, payload): return self.call("POST", path, payload)


def make_controller(fail=None, absent=()):
    events = []
    pi, belt = NodeStub(events, fail=fail, absent=absent), NodeStub(events, fail=fail)
    return Controller(pi, belt), events


def test_one_cup_per_type_ordered_drop_belt_then_return():
    controller, events = make_controller()
    result = controller.run({"R3": 2, "C1": 1, "LED_RX": 3}, approved=True)
    paths = [event[1] for event in events]
    assert paths == ["/begin"] + ["/detect", "/grasp_place", "/advance", "/return"] * 3 + ["/end"]
    assert result["cups_commanded"] == ["R3", "C1", "LED_RX"]
    assert result["physical_delivery_verified"] is False


def test_no_approval_no_network():
    controller, events = make_controller()
    with pytest.raises(ValueError, match="approval"):
        controller.run({"R3": 1})
    assert events == []


def test_absent_type_flagged_skipped_not_replanned():
    controller, events = make_controller(absent=["R3"])
    result = controller.run({"R3": 2, "C1": 1}, approved=True)
    assert result["state"] == "complete_with_missing"
    assert result["missing"] == ["R3"]
    assert result["cups_commanded"] == ["C1"]
    assert len([e for e in events if e[1] == "/advance"]) == 1


@pytest.mark.parametrize("path", ["/begin", "/detect", "/grasp_place", "/advance", "/return", "/end"])
def test_any_step_failure_stops_both_no_retry(path):
    controller, events = make_controller(fail=path)
    with pytest.raises(RuntimeError, match="injected"):
        controller.run({"R3": 1, "C1": 1}, approved=True)
    paths = [e[1] for e in events]
    assert paths.count(path) == 1
    assert set(paths[-2:]) == {"/estop", "/stop"}
    if path == "/grasp_place":
        assert "/advance" not in paths
    if path == "/advance":
        assert "/return" not in paths


def test_malformed_drop_never_advances_belt():
    controller, events = make_controller()
    original = controller.pi.post
    def post(path, body):
        value = original(path, body)
        if path == "/grasp_place":
            value["steps"] = [{"op": "drop", "ok": True}]
        return value
    controller.pi.post = post
    with pytest.raises(RuntimeError, match="ordered drop"):
        controller.run({"R3": 1}, approved=True)
    assert "/advance" not in [e[1] for e in events]


def test_stop_is_latched_even_before_worker_starts():
    controller, events = make_controller()
    controller.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        controller.run({"R3": 1}, approved=True)
    assert "/begin" not in [e[1] for e in events]
