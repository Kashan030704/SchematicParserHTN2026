"""Star TCP host, adapted from calhacks2025 hcp_client/main.py + hcp_executor.py.

Retains accept threads, per-client recv loops and queues; adds framing and
correlation, and removes the reference host's blocking input/LLM state machine.
"""
import argparse
import copy
import logging
import queue
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from hcp_host.envelope import FrameParser, ProtocolError, frame, message, validate_tags
from hcp_host.registry import Registry

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


class Outbox:
    """Reliable lifecycle queue plus coalesced latest context per topic."""
    def __init__(self):
        self.reliable = queue.Queue(maxsize=256)
        self.latest = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def put(self, value):
        if value["action"] == "publish":
            with self.lock:
                self.latest[value["payload"].get("topic")] = value
        else:
            self.reliable.put_nowait(value)
        self.wake.set()

    def get(self):
        try:
            return self.reliable.get_nowait()
        except queue.Empty:
            with self.lock:
                if self.latest:
                    _, result = self.latest.popitem()
                    return result
            self.wake.wait(0.05)
            self.wake.clear()
            return None


@dataclass(eq=False)
class Client:
    conn: socket.socket
    addr: tuple
    alive: threading.Event = field(default_factory=threading.Event)
    outbox: Outbox = field(default_factory=Outbox)
    device_id: str | None = None
    discovery_id: str = ""
    connected_at: float = field(default_factory=time.monotonic)


@dataclass
class PendingCommand:
    owner: Client
    future: Future
    deadline: float
    acked: bool = False


class HCPHost:
    def __init__(self, host="0.0.0.0", port=9000, command_timeout=30.0):
        self.host, self.port = host, port
        self.command_timeout = command_timeout
        self.registry = Registry()
        self.running = threading.Event()
        self.lock = threading.RLock()
        self.clients = set()
        self.pending = {}
        self.context = {}
        self.health = {}
        self.subscribers = []
        self.sock = None

    def start(self):
        if self.running.is_set():
            return self
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.host, self.port))
        except OSError:
            self.sock.close()
            raise
        self.port = self.sock.getsockname()[1]
        self.sock.listen()
        self.sock.settimeout(0.2)
        self.running.set()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._expire_loop, daemon=True).start()
        log.info("HCP listening on %s:%s", self.host, self.port)
        return self

    def _accept_loop(self):
        while self.running.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.2)
            client = Client(conn, addr)
            client.alive.set()
            with self.lock:
                self.clients.add(client)
            request = message("REQUEST_HCP_DATA", "host")
            client.discovery_id = request["id"]
            client.outbox.put(request)
            threading.Thread(target=self._write_loop, args=(client,), daemon=True).start()
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client):
        parser = FrameParser()
        try:
            while self.running.is_set() and client.alive.is_set():
                try:
                    data = client.conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                for value in parser.feed(data):
                    self._receive(client, value)
        except Exception as exc:
            log.warning("HCP connection %s: %s", client.addr, exc)
        finally:
            self._disconnect(client)

    def _write_loop(self, client):
        try:
            while self.running.is_set() and client.alive.is_set():
                value = client.outbox.get()
                if value is not None:
                    client.conn.sendall(frame(value))
        except (OSError, ValueError):
            self._disconnect(client)

    def _receive(self, client, value):
        action, payload = value["action"], value["payload"]
        if client.device_id is None:
            if action != "REQUEST_HCP_DATA" or value["id"] != client.discovery_id:
                raise ProtocolError("Discovery required before other messages")
            if payload.get("metadata", {}).get("device_id") != value["device_id"]:
                raise ProtocolError("Definition identity mismatch")
            client.device_id = self.registry.register(payload, client)
            log.info("Registered %s", client.device_id)
            # Do not replay old detections: consumers must receive a new camera frame.
            return
        if value["device_id"] != client.device_id:
            raise ProtocolError("Connection identity mismatch")
        if action in ("ack", "done"):
            with self.lock:
                pending = self.pending.get(value["id"])
                if pending is None or pending.owner is not client:
                    return
                if action == "ack":
                    pending.acked = True
                    return
                self.pending.pop(value["id"])
                if payload.get("status") == "ok":
                    pending.future.set_result(copy.deepcopy(payload))
                else:
                    pending.future.set_exception(CommandError(payload.get("error", "Node command failed")))
        elif action == "publish":
            if client.device_id != "camera" or payload.get("topic") != "context/tags":
                raise ProtocolError("Only camera may publish tag context")
            validate_tags(payload)
            self.publish_context(value, source=client)
        elif action.startswith("status/"):
            with self.lock:
                self.health[client.device_id] = {"action": action, "payload": copy.deepcopy(payload), "received_at": time.monotonic()}
        else:
            raise ProtocolError(f"Unexpected node message {action}")

    def publish_context(self, value, source=None):
        topic = value["payload"]["topic"]
        if topic == "context/tags":
            validate_tags(value["payload"])
        snapshot = {"message": copy.deepcopy(value), "received_at": time.monotonic()}
        with self.lock:
            self.context[topic] = snapshot
            subscribers = list(self.subscribers)
            clients = list(self.clients)
        for subscriber in subscribers:
            subscriber.put(value)
        if topic == "context/tags":
            for client in clients:
                if client is not source and client.device_id is not None:
                    client.outbox.put(value)

    def subscribe(self):
        outbox = Outbox()
        with self.lock:
            self.subscribers.append(outbox)
        return outbox

    def context_snapshot(self):
        with self.lock:
            return copy.deepcopy(self.context)

    def command(self, device_id, action, payload=None, timeout=None):
        payload = {} if payload is None else payload
        owner = self.registry.resolve(device_id, action, payload)
        value = message(action, device_id, payload)
        future = Future()
        future.command_id = value["id"]
        with self.lock:
            if not owner.alive.is_set():
                raise CommandError("Node disconnected")
            self.pending[value["id"]] = PendingCommand(owner, future, time.monotonic() + (self.command_timeout if timeout is None else timeout))
            try:
                owner.outbox.put(value)
            except queue.Full:
                self.pending.pop(value["id"])
                future.set_exception(CommandError("Node command queue full"))
        return future

    def _expire_loop(self):
        while self.running.is_set():
            now = time.monotonic()
            expired_owners = set()
            with self.lock:
                for key, pending in list(self.pending.items()):
                    if pending.deadline <= now:
                        del self.pending[key]
                        pending.future.set_exception(CommandError("Command timed out; physical outcome uncertain"))
                        expired_owners.add(pending.owner)
                undiscovered = [c for c in self.clients if c.device_id is None and now - c.connected_at > 5]
            for client in set(undiscovered) | expired_owners:
                self._disconnect(client)
            time.sleep(0.05)

    def _disconnect(self, client):
        with self.lock:
            if not client.alive.is_set():
                return
            client.alive.clear()
            self.clients.discard(client)
            self.registry.remove(client)
            self.health.pop(client.device_id, None)
            for key, pending in list(self.pending.items()):
                if pending.owner is client:
                    del self.pending[key]
                    pending.future.set_exception(CommandError("Node disconnected; physical outcome uncertain"))
        try:
            client.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.conn.close()
        if client.device_id == "camera":
            self.publish_context(message("publish", "camera", {"topic": "context/tags", "tags": []}, ""))

    def stop(self):
        self.running.clear()
        if self.sock:
            self.sock.close()
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            self._disconnect(client)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    host = HCPHost(args.host, args.port).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        host.stop()


if __name__ == "__main__":
    main()
