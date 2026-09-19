"""One serial owner, correlated replies, and a separate heartbeat/read path."""
import threading
import time
from concurrent.futures import Future


class SerialController:
    def __init__(self, config, transport=None, boot_delay=2.0):
        if transport is None:
            import serial
            transport = serial.Serial(config["serial"]["port"], config["serial"]["baud"], timeout=0.1, write_timeout=1, exclusive=True)
            time.sleep(boot_delay)
        self.transport = transport
        self.config = config
        self.lock = threading.RLock()
        self.pending = {}
        self.sequence = 0
        self.running = threading.Event()
        self.running.set()
        self.fault = None
        threading.Thread(target=self._read, daemon=True).start()
        belt = config["belt"]
        try:
            self._request("C", belt["neutral_us"], belt["forward_us"], round(belt["max_duration_s"] * 1000), timeout=3)
        except Exception:
            self.close()
            raise
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _write(self, line):
        with self.lock:
            encoded = (line + "\n").encode("ascii")
            if self.transport.write(encoded) != len(encoded):
                raise RuntimeError("Incomplete serial write")

    def _request(self, action, *values, timeout=5):
        with self.lock:
            if self.fault and action not in ("S", "X"):
                raise RuntimeError(self.fault)
            self.sequence += 1
            seq = str(self.sequence)
            future = Future()
            self.pending[seq] = future
            try:
                self._write(",".join([action, seq, *(str(value) for value in values)]))
            except Exception:
                self.pending.pop(seq, None)
                raise
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            self.fault = "Serial completion timeout; restart actuator after inspecting hardware"
            with self.lock:
                self.sequence += 1
                self._write(f"X,{self.sequence}")
            raise RuntimeError(self.fault) from exc
        finally:
            with self.lock:
                self.pending.pop(seq, None)

    def _read(self):
        buffer = bytearray()
        try:
            while self.running.is_set():
                chunk = self.transport.readline(256)
                if not chunk:
                    continue
                # pyserial.readline may time out halfway through an ID or line.
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    if len(line) > 255:
                        raise RuntimeError("Oversized serial reply")
                    fields = line.decode("ascii", errors="strict").strip().split(",")
                    if len(fields) < 2 or fields[0] not in ("DONE", "ERR"):
                        continue
                    with self.lock:
                        future = self.pending.get(fields[1])
                        if future and not future.done():
                            if fields[0] == "DONE":
                                future.set_result(None)
                            else:
                                future.set_exception(RuntimeError(",".join(fields[2:]) or "Firmware error"))
                if len(buffer) > 255:
                    raise RuntimeError("Unterminated serial reply")
        except Exception as exc:
            self.fault = f"Serial link failed: {exc}"
            with self.lock:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError(self.fault))

    def _heartbeat(self):
        while self.running.is_set():
            try:
                self._write("H")
            except Exception as exc:
                self.fault = f"Heartbeat failed: {exc}"
                return
            time.sleep(0.25)

    def move(self, angles):
        arm = self.config["arm"]
        return self._request("J", *(f"{angle:.3f}" for angle in angles), arm["move_ms"], arm["settle_ms"], timeout=(arm["move_ms"] + arm["settle_ms"]) / 1000 + 2)

    def advance(self, duration_s):
        return self._request("B", round(duration_s * 1000), timeout=duration_s + 2)

    def stop_belt(self):
        return self._request("S", timeout=2)

    def abort(self):
        self.fault = "Hardware session interrupted; inspect hardware and restart actuator"
        return self._request("X", timeout=2)

    def close(self):
        if self.running.is_set():
            try:
                self.abort()
            except Exception:
                pass
        self.running.clear()
        self.transport.close()
