"""HTTP contract simulator ONLY. Arduino hardware/driver selection is pending."""
import argparse
import os
import threading

from flask import Flask, jsonify
from werkzeug.exceptions import Conflict, HTTPException
from config import number
from hcp_host.http import configure_node_app, object_body


def create_app(*, real_time=False, token=None, emit=print):
    app = Flask(__name__)
    configure_node_app(app, token=token)
    busy, stopped = threading.Lock(), threading.Event()

    @app.get("/health")
    def health():
        return jsonify(node="conveyor", simulated=True, stopped=stopped.is_set())

    @app.post("/advance")
    def advance():
        data = object_body({"seconds"})
        seconds = number(data["seconds"], "seconds", 0.1, 30)
        if stopped.is_set():
            raise Conflict("Conveyor stopped; inspect and restart")
        if not busy.acquire(blocking=False):
            raise Conflict("Conveyor busy")
        try:
            if stopped.is_set():
                raise Conflict("Conveyor stopped")
            emit(f"CONVEYOR ON {seconds:g}s (SIMULATED)")
            if real_time and stopped.wait(seconds):
                raise Conflict("Conveyor advance interrupted by stop")
            if stopped.is_set():
                raise Conflict("Conveyor stopped")
            return jsonify(ok=True, seconds=seconds, simulated=True)
        finally:
            emit("CONVEYOR OFF (SIMULATED)")
            busy.release()

    @app.post("/stop")
    def stop():
        stopped.set()
        emit("CONVEYOR STOP (SIMULATED)")
        return jsonify(ok=True, stop_requested=True, simulated=True)

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        return jsonify(error=str(exc)), 400 if isinstance(exc, ValueError) else 502

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--real-time", action="store_true", help="Wait the requested duration in simulation")
    args = parser.parse_args()
    create_app(real_time=args.real_time, token=os.getenv("HCP_NODE_TOKEN")).run(
        host=args.bind, port=args.port, threaded=True, use_reloader=False)
