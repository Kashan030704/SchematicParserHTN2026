"""Mac confirmation gate. Ingestion and preview can NEVER dispatch motion."""
import argparse
import copy
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import tempfile
import threading
import uuid

from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, Conflict

from config import load_yaml, load_tag_map
from ingestion.parse import validate_bom, strict_json
from orchestrator.ingest import ingest
from orchestrator.controller import Controller
from orchestrator.nodes import NodeClient
from orchestrator.baseten_client import BasetenClient


def create_app(controller, *, model_factory=BasetenClient, part_types=()):
    app = Flask(__name__, static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["UI_TOKEN"] = secrets.token_urlsafe(32)
    runs, lock = {}, threading.Lock()
    known = set(part_types)

    @app.before_request
    def csrf():
        if request.method == "POST" and not hmac.compare_digest(
                request.headers.get("X-HCP-UI-Token", ""), app.config["UI_TOKEN"]):
            return jsonify(error="Missing UI approval token; reload this page"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def index():
        text = (Path(app.static_folder) / "index.html").read_text()
        return text.replace("{{UI_TOKEN}}", app.config["UI_TOKEN"])

    @app.get("/status")
    def status():
        return jsonify(architecture="RoboMaster + conveyor HTTP", types=sorted(known),
                       stopped=controller.cancelled.is_set(), physical_delivery_verified=False)

    def preview():
        value = controller.pi.get("/detect")
        controller._validate_detection(value)
        return value

    @app.get("/detect")
    def detect():
        return jsonify(preview())

    @app.post("/proposals")
    def propose():
        with lock:
            if any(r["state"] == "running" for r in runs.values()):
                raise Conflict("A run is active")
        if request.is_json:
            data = strict_json(request.get_data(as_text=True))
            if not isinstance(data, dict) or set(data) != {"bom"}:
                raise BadRequest("Manual proposal requires only a bom object")
            bom, source = validate_bom(data["bom"]), "manual"
        else:
            upload = request.files.get("schematic")
            if not upload:
                raise BadRequest("Upload a schematic PDF, PNG or JPEG")
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in (".pdf", ".png", ".jpg", ".jpeg"):
                raise BadRequest("Supported schematic formats: PDF, PNG, JPEG")
            with tempfile.TemporaryDirectory(prefix="hcp-ingest-") as folder:
                path = Path(folder) / ("schematic" + suffix)
                upload.save(path)
                bom = ingest(path, model_factory(), part_types=sorted(known))
            source = "baseten"
        try:
            detection, detection_error = preview(), None
        except Exception as exc:
            detection, detection_error = None, str(exc)
        proposal_id = uuid.uuid4().hex
        run = {"id": proposal_id, "state": "awaiting_confirmation", "bom": bom, "source": source,
               "detection": detection, "detection_error": detection_error, "events": [],
               "physical_delivery_verified": False}
        with lock:
            # Bound in-memory history; never evict a running record.
            if len(runs) >= 50:
                oldest = next((key for key, item in runs.items() if item["state"] != "running"), None)
                if oldest:
                    del runs[oldest]
            runs[proposal_id] = run
        return jsonify(copy.deepcopy(run)), 201

    def execute(proposal_id, bom):
        def progress(event):
            with lock:
                runs[proposal_id]["events"].append(event)
        try:
            result = controller.run(bom, approved=True, progress=progress)
            with lock:
                runs[proposal_id].update(result)
        except BaseException as exc:
            with lock:
                runs[proposal_id].update(state="halted", error=str(exc),
                                         stop_errors=controller.last_stop_errors)

    @app.post("/proposals/<proposal_id>/approve")
    def approve(proposal_id):
        data = strict_json(request.get_data(as_text=True))
        if not isinstance(data, dict) or set(data) != {"bom", "approved"} or data["approved"] is not True:
            raise BadRequest("Approval requires edited bom and approved: true")
        bom = validate_bom(data["bom"])
        missing = sorted(set(bom) - known)
        if missing:
            raise BadRequest("No tag/preset mapping for: " + ", ".join(missing))
        with lock:
            if proposal_id not in runs:
                raise BadRequest("Unknown proposal")
            if controller.cancelled.is_set():
                raise Conflict("Stopped session: inspect hardware and restart all processes")
            if runs[proposal_id]["state"] != "awaiting_confirmation":
                raise Conflict("Proposal already used; approval cannot replay a run")
            if any(r["state"] == "running" for r in runs.values()):
                raise Conflict("A run is active")
            runs[proposal_id].update(state="running", bom=dict(bom))
            threading.Thread(target=execute, args=(proposal_id, dict(bom)), daemon=True).start()
        return jsonify(id=proposal_id, state="running"), 202

    @app.get("/proposals/<proposal_id>")
    def get_run(proposal_id):
        with lock:
            if proposal_id not in runs:
                return jsonify(error="Unknown proposal"), 404
            return jsonify(copy.deepcopy(runs[proposal_id]))

    @app.post("/stop")
    def stop():
        errors = controller.stop()
        return jsonify(ok=not errors, errors=errors, physical_stop_verified=False), 502 if errors else 200

    @app.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException
        from jsonschema import ValidationError
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, (ValueError, ValidationError)):
            return jsonify(error=str(exc)), 400
        return jsonify(error=str(exc)), 502

    return app


def main():
    parser = argparse.ArgumentParser(description="Mac HCP confirm gate and one-shot controller")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--web-port", type=int, default=5002)
    args = parser.parse_args()
    config_path = Path(args.config)
    config = load_yaml(config_path)
    tags = load_tag_map(config_path.parent / config.get("tag_map", "tag_map.yaml"))
    token = os.getenv(config.get("node_token_env", "HCP_NODE_TOKEN"))
    controller = Controller(NodeClient(config["pi"]["base_url"], token=token),
                            NodeClient(config["arduino"]["base_url"], token=token),
                            advance_seconds=config["arduino"].get("advance_seconds", 3))
    baseten = config.get("baseten", {})
    def model():
        return BasetenClient(vision_model=baseten.get("model"), base_url=baseten.get("url"),
                             api_key_env=baseten.get("api_key_env", "BASETEN_API_KEY"))
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        create_app(controller, model_factory=model, part_types=tags.values()).run(
            host="127.0.0.1", port=args.web_port, threaded=True, use_reloader=False)
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
