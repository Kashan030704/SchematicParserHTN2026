"""Runtime extracted from the reference generated HCPClient template.

The reference connect/listen/events/response-queue structure is retained here
so every generated client uses exactly the same framing and lifecycle patch.
"""
import copy
import logging
import queue
import random
import socket
import threading
import time

from hcp_host.envelope import FrameParser, ProtocolError, frame, message, validate_tags
from hcp_host.registry import parameters
from hcp_host.server import Outbox
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)


class HCPClient:
    def __init__(self, definition, host, port):
        self.definition = definition
        self.device_id = definition["metadata"]["device_id"]
        self.host, self.port = host, port
        self.sock = None
        self.running = threading.Event()
        self.connected = threading.Event()
        self.events = queue.Queue(maxsize=64)
        self.handlers = {}
        self.urgent_handlers = {}
        self.on_disconnect = None
        self._responses = Outbox()
        self._generation = 0
        self._lock = threading.RLock()
        self._context = {}
        self._seen = {}

    def register_handler(self, action, handler, urgent=False):
        if action not in self.definition["available_commands"]:
            raise ValueError(f"Undeclared handler {action}")
        (self.urgent_handlers if urgent else self.handlers)[action] = handler

    def start(self):
        if not self.running.is_set():
            self.running.set()
            threading.Thread(target=self._connect_loop, daemon=True).start()
            threading.Thread(target=self._worker, daemon=True).start()
        return self

    def send_response(self, action, body, command_id, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if not self.connected.is_set():
                return
            self._responses.put(message(action, self.device_id, body, command_id))

    def publish(self, topic, **payload):
        self.send_response("publish", {"topic": topic, **payload}, "")

    def status(self, state, **payload):
        self.send_response("status/health", {"state": state, **payload}, "")

    def tags(self, max_age=1.0):
        with self._lock:
            snapshot = self._context.get("context/tags")
            if not self.connected.is_set() or not snapshot or time.monotonic() - snapshot[0] > max_age:
                raise ValueError("No fresh camera context")
            return copy.deepcopy(snapshot[1]["tags"])

    def _connect_loop(self):
        backoff = 0.25
        while self.running.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((self.host, self.port))
                sock.settimeout(0.2)
                with self._lock:
                    self.sock = sock
                    self._generation += 1
                    generation = self._generation
                    self._responses = Outbox()
                    self._seen = {}
                    self._context = {}
                    outbox = self._responses
                threading.Thread(target=self._write_loop, args=(sock, outbox, generation), daemon=True).start()
                self._listen_loop(sock, generation)
                backoff = 0.25
            except (OSError, ValueError, queue.Full) as exc:
                log.debug("%s connection: %s", self.device_id, exc)
            finally:
                with self._lock:
                    was_connected = self.connected.is_set()
                    self.connected.clear()
                    self._generation += 1
                    self._context = {}
                if sock:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
                if was_connected and self.on_disconnect:
                    try:
                        self.on_disconnect()
                    except Exception:
                        log.exception("Disconnect handler failed")
            deadline = time.monotonic() + backoff + random.uniform(0, backoff / 4)
            while self.running.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            backoff = min(backoff * 2, 5)

    def _listen_loop(self, sock, generation):
        parser = FrameParser()
        while self.running.is_set() and generation == self._generation:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                return
            for value in parser.feed(data):
                action = value["action"]
                if action == "REQUEST_HCP_DATA":
                    if value["device_id"] != "host":
                        raise ProtocolError("Invalid discovery sender")
                    # Discovery must be queued before concurrent camera/status output.
                    with self._lock:
                        self._responses.put(message(action, self.device_id, self.definition, value["id"]))
                        self.connected.set()
                elif action == "publish":
                    if value["device_id"] != "camera":
                        raise ProtocolError("Invalid context source")
                    validate_tags(value["payload"])
                    with self._lock:
                        self._context["context/tags"] = (time.monotonic(), value["payload"])
                else:
                    if value["device_id"] != self.device_id or not self.connected.is_set():
                        raise ProtocolError("Invalid command target")
                    self._enqueue(value, generation)

    def _enqueue(self, value, generation):
        command_id = value["id"]
        self.send_response("ack", {}, command_id, generation)
        with self._lock:
            if command_id in self._seen:
                result = self._seen[command_id]
                if result is not None:
                    self.send_response("done", result, command_id, generation)
                return
            self._seen[command_id] = None
        try:
            command = self.definition["available_commands"][value["action"]]
            Draft7Validator(parameters(command)).validate(value["payload"])
            if value["action"] in self.urgent_handlers:
                # Urgent handlers must only signal cancellation/queue a serial stop.
                threading.Thread(target=self._execute, args=(value, generation, self.urgent_handlers[value["action"]]), daemon=True).start()
            else:
                if value["action"] not in self.handlers:
                    raise ValueError("No hardware handler attached")
                self.events.put_nowait((value, generation))
        except Exception as exc:
            self._finish(command_id, {"status": "error", "error": str(exc)}, generation)

    def _worker(self):
        while self.running.is_set():
            try:
                value, generation = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if generation == self._generation and self.connected.is_set():
                self._execute(value, generation, self.handlers[value["action"]])

    def _execute(self, value, generation, handler):
        try:
            result = handler(**value["payload"])
            payload = {"status": "ok"}
            if result is not None:
                payload["result"] = result
        except Exception as exc:
            payload = {"status": "error", "error": str(exc)}
        self._finish(value["id"], payload, generation)

    def _finish(self, command_id, payload, generation):
        with self._lock:
            if generation == self._generation:
                self._seen[command_id] = payload
                # Finite session cache; host never resends commands automatically.
                if len(self._seen) > 4096:
                    for key in list(self._seen):
                        if self._seen[key] is not None and key != command_id:
                            del self._seen[key]
                            break
                self.send_response("done", payload, command_id, generation)

    def _write_loop(self, sock, outbox, generation):
        try:
            while self.running.is_set() and generation == self._generation:
                value = outbox.get()
                if value is not None:
                    sock.sendall(frame(value))
        except (OSError, ValueError):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self):
        self.running.clear()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
