"""EP plaintext motion: one command/reply at a time; never retry uncertain motion."""
import math
import socket
import threading

from config import number


class MotionError(RuntimeError):
    pass


class Robot:
    def __init__(self, poses, *, host=None, port=40923, dry_run=False,
                 timeout=30, socket_factory=socket.create_connection, emit=print, read_only=False):
        if not dry_run and (not host or (not read_only and poses.get("calibrated") is not True)):
            raise ValueError("Hardware needs an explicit EP RNDIS address and calibrated poses")
        self.read_only = read_only
        self.poses, self.host, self.port = poses, host, port
        self.dry_run, self.timeout = dry_run, timeout
        self.socket_factory, self.emit = socket_factory, emit
        self.socket = None
        self.buffer = b""
        self.commands = []
        self.chassis_moves = []  # Successful commanded legs, NOT measured odometry.
        self._lock = threading.Lock()
        # A signal handler may call estop in the same thread during sendall.
        self._write_lock = threading.RLock()
        self.stopped = threading.Event()

    def connect(self):
        if self.stopped.is_set():
            raise MotionError("Stopped session cannot reconnect; restart after inspection")
        if not self.dry_run:
            self.socket = self.socket_factory((self.host, self.port), timeout=self.timeout)
        try:
            self._send("command")
            self._send("robot mode free")
        except BaseException:
            self.estop()
            raise

    def _send(self, cmd):
        cmd = cmd.rstrip(";")
        if ";" in cmd or "\n" in cmd or "\r" in cmd:
            raise ValueError("Exactly one SDK command is allowed")
        if self.read_only and cmd not in ("command", "robot mode free", "stream on", "stream off"):
            raise MotionError("Camera inspection is read-only: motion commands are disabled")
        with self._lock:
            if self.stopped.is_set():
                raise MotionError("Robot is stopped; no further commands allowed")
            self.commands.append(cmd + ";")
            if self.dry_run:
                self.emit(cmd + ";")
                return
            try:
                with self._write_lock:
                    if self.stopped.is_set():
                        raise MotionError("Robot stopped")
                    self.socket.sendall((cmd + ";").encode("ascii"))
                while b";" not in self.buffer:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        raise MotionError("EP disconnected; command outcome unknown")
                    self.buffer += chunk
                    if len(self.buffer) > 8192:
                        raise MotionError("Oversized EP response")
                reply, self.buffer = self.buffer.split(b";", 1)
                if reply.strip() != b"ok":
                    raise MotionError(f"EP rejected {cmd!r}: {reply.decode('ascii', errors='replace')}")
                if self.buffer.strip():
                    raise MotionError("Unexpected extra EP reply; command correlation uncertain")
                if self.stopped.is_set():
                    raise MotionError("Stopped while command was in flight")
            except BaseException:
                self.estop()
                raise

    def _settle(self, seconds):
        if self.stopped.is_set() or (not self.dry_run and self.stopped.wait(seconds)):
            raise MotionError("Stopped during motion dwell")

    def arm_moveto(self, pose):
        x, y = pose["arm"]["x"], pose["arm"]["y"]
        number(x, "arm x mm", -500, 500)
        number(y, "arm y mm", -500, 500)
        # DJI plaintext docs specify CENTIMETERS, unlike Python SDK millimeters.
        self._send(f"robotic_arm moveto x {x / 10:g} y {y / 10:g}")
        self._settle(self.poses["arm_settle_s"])

    def gripper(self, state):
        if state not in ("open", "close"):
            raise ValueError("Gripper state must be open or close")
        self._send(f"robotic_gripper {state} {self.poses['gripper_level']}")
        self._settle(self.poses["gripper_settle_s"])

    def chassis_move(self, waypoint, *, speed_mps=None, settle_s=None):
        wp = waypoint["chassis"]
        x = number(wp["x_m"], "chassis x m", -5, 5)
        y = number(wp["y_m"], "chassis y m", -5, 5)
        z = number(wp["z_deg"], "chassis z deg", -1800, 1800)
        speed = self.poses["chassis_speed_mps"] if speed_mps is None else speed_mps
        number(speed, "chassis speed", 0.01, self.poses["chassis_speed_mps"])
        rotation = self.poses["chassis_speed_dps"]
        dwell = self.poses["chassis_settle_s"] if settle_s is None else settle_s
        number(dwell, "chassis settle seconds", 0.05, 120)
        self._send(f"chassis move x {x:g} y {y:g} z {z:g} vxy {speed:g} vz {rotation:g}")
        # Conservative open-loop dwell. Visual corrections observe BETWEEN moves.
        self._settle(math.hypot(x, y) / speed + abs(z) / rotation + dwell)
        self.chassis_moves.append(dict(wp))

    def observe(self):
        self.arm_moveto(self.poses["observe_pose"])

    def grasp(self, part_type):
        pose = self.poses["grab_pose"]
        self.gripper("open")
        self.arm_moveto(pose)
        self.gripper("close")
        self.observe()  # Calibrate this as a carry-safe lift, with the jaws still closed.

    def to_collection(self):
        self.chassis_move(self.poses["collection_wp"])

    def drop(self):
        self.arm_moveto(self.poses["drop_pose"])
        self.gripper("open")
        self.observe()  # Clear the collection point before acknowledging drop.

    def to_home(self):
        self.chassis_move(self.poses["home_wp"])

    def estop(self):
        """Best effort quit + disconnect, independent of the normal command lock."""
        self.stopped.set()
        if self.dry_run:
            if not self.commands or self.commands[-1] != "quit;":
                self.commands.append("quit;")
                self.emit("quit;")
            return
        sock = self.socket
        if sock:
            try:
                # Do not wait behind a long-running motion reply.
                with self._write_lock:
                    sock.sendall(b"quit;")
            except OSError:
                pass
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
                self.socket = None
