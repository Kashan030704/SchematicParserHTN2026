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

from config import load_yaml, load_tag_map, load_group_catalog
from ingestion.parse import validate_bom, strict_json, SCHEMATIC_EXTENSIONS, SCHEMATIC_FORMATS
from ingestion.classification import already_grouped, group_components
from orchestrator.ingest import ingest
from orchestrator.schematic_advice import build_schematic_suggestions
from orchestrator.controller import Controller
from orchestrator.nodes import NodeClient
from orchestrator.baseten_client import BasetenClient
from orchestrator.commands import (BasetenCommandClient, DemoCommandClient, capabilities,
                                   is_stop, validate_intent, validate_text)


def serialize_proposal(run):
    """Pair quantities with full IDs in the configured map's supported tag36h11 family."""
    value = copy.deepcopy(run)
    tags = value.get("group_tags", {})
    value["tagged_bom"] = {
        name: {"quantity": quantity, "tag_id": f"tag36h11[{tags[name]}]" if name in tags else None}
        for name, quantity in value["bom"].items()
    }
    return value


def create_app(controller, *, model_factory=BasetenClient, part_types=(),
               command_mode="baseten", command_model_factory=BasetenCommandClient,
               group_catalog=None):
    if command_mode not in {"baseten", "demo"}:
        raise ValueError("command_mode must be baseten or demo (no automatic fallback)")
    app = Flask(__name__, static_folder="static")
    # Preserve the human-reviewed BOM order through proposal -> approval -> plan.
    app.json.sort_keys = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["UI_TOKEN"] = secrets.token_urlsafe(32)
    runs, lock = {}, threading.Lock()
    command_lock = threading.Lock()
    known = set(part_types)
    group_catalog = copy.deepcopy(group_catalog)
    groups = group_catalog["groups"] if group_catalog else []
    if groups and {group["name"] for group in groups} != known:
        raise ValueError("Classification groups must match the configured robot types")
    group_tags = {group["name"]: group["tag_id"] for group in groups}

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
        with lock:
            active = next((key for key, run in runs.items() if run["state"] == "running"), None)
        return jsonify(architecture="RoboMaster HTTP", types=sorted(known), groups=groups,
                       stopped=controller.cancelled.is_set(), active_run=active,
                       accepts_bom=True, command_mode=command_mode, physical_delivery_verified=False)

    @app.get("/nodes")
    def nodes():
        # Health is read-only. Keep it separate from frequent local progress polls.
        result = {}
        for name, node in (("robomaster", controller.pi),):
            try:
                result[name] = {"connected": True, **node.call("GET", "/health", timeout=2)}
            except Exception as exc:
                result[name] = {"connected": False, "error": str(exc)}
        return jsonify(nodes=result)

    def preview():
        value = controller.pi.get("/detect")
        controller._validate_detection(value)
        return value

    @app.get("/detect")
    def detect():
        return jsonify(preview())

    def ensure_idle(*, require_armed=False):
        # Called under lock when mutating shared run records.
        if any(r["state"] == "running" for r in runs.values()):
            raise Conflict("A run is active; commands cannot modify or interleave with it")
        if require_armed and controller.cancelled.is_set():
            raise Conflict("Stopped session: inspect hardware and restart all processes")

    def save_run(run):
        with lock:
            ensure_idle()
            if len(runs) >= 50:
                oldest = next((key for key, item in runs.items() if item["state"] != "running"), None)
                if oldest:
                    del runs[oldest]
            runs[run["id"]] = run
            return serialize_proposal(run)

    def make_proposal(bom, source, component_details=()):
        bom = validate_bom(bom)
        try:
            detection, detection_error = preview(), None
        except Exception as exc:
            detection, detection_error = None, str(exc)
        return {"id": uuid.uuid4().hex, "kind": "fulfillment", "state": "awaiting_confirmation",
                "bom": bom, "source": source, "detection": detection, "detection_error": detection_error,
                "events": [], "component_details": list(component_details),
                "group_tags": group_tags,
                "llm_schematic_suggestions": build_schematic_suggestions(
                    {"components": component_details} if component_details else bom),
                "suggestions_kind": "rule_based_advisory",
                "warnings": ["No configured cup/tag mapping for: " + key for key in bom if key not in known],
                "physical_delivery_verified": False}

    @app.post("/proposals")
    def propose():
        with lock:
            ensure_idle()
        component_details = []
        parsed = {}
        model = None
        if request.is_json:
            data = strict_json(request.get_data(as_text=True))
            if not isinstance(data, dict) or set(data) != {"bom"}:
                raise BadRequest("Manual proposal requires only a bom object")
            bom, source = validate_bom(data["bom"]), "manual"
        else:
            upload = request.files.get("schematic")
            if not upload:
                raise BadRequest("Upload a " + SCHEMATIC_FORMATS + " schematic")
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in SCHEMATIC_EXTENSIONS:
                raise BadRequest("Supported schematic formats: " + SCHEMATIC_FORMATS)
            with tempfile.TemporaryDirectory(prefix="hcp-ingest-") as folder:
                path = Path(folder) / ("schematic" + suffix)
                upload.save(path)
                # SCH extraction stays local; the optional grouping step below uses the model.
                model = None if suffix == ".sch" else model_factory()
                parsed = ingest(path, model, part_types=() if groups else sorted(known), details=True, review=True)
                bom, component_details, source = parsed["bom"], parsed["component_details"], parsed["source"]
        raw_bom = dict(bom)
        classification, classification_error, classification_method = None, None, None
        if groups:
            try:
                classification = already_grouped(raw_bom, group_catalog)
                classification_method = "explicit_groups"
                if classification is None:
                    model = model or model_factory()
                    value = model.classify_components(dict(raw_bom), group_catalog,
                                                      component_details=component_details)
                    classification = group_components(raw_bom, value, group_catalog)
                    classification_method = "llm"
                bom = classification["bom"]
            except Exception as exc:
                # Preserve extraction for manual correction; never drop entries or fabricate a mapping.
                classification_error = str(exc)[:1200]
        run = make_proposal(bom, source, component_details)
        if groups:
            run["raw_bom"] = raw_bom
            run["llm_schematic_suggestions"] = build_schematic_suggestions(
                {"components": component_details} if component_details else raw_bom)
            if classification is not None and classification_error is None:
                run.update(component_classification=classification["component_classification"],
                           classification_bom=dict(bom), classification_method=classification_method,
                           classification_stale=False)
                run["warnings"].extend(
                    f"Check grouping: {row['component']} → {row['group']} ({row['confidence']} confidence): {row['reason']}"
                    for row in classification["component_classification"] if row["confidence"] != "high")
            else:
                run["classification_error"] = classification_error
        if parsed.get("schematic_review") is not None:
            run.update(schematic_review=parsed["schematic_review"],
                       schematic_review_model=parsed["schematic_review_model"],
                       schematic_review_bom=raw_bom, schematic_review_grouped_bom=dict(bom),
                       schematic_review_stale=False)
        if "schematic_review_error" in parsed:
            run["schematic_review_error"] = parsed["schematic_review_error"]
        return jsonify(save_run(run)), 201

    @app.get("/commands/capabilities")
    def command_capabilities():
        return jsonify(mode=command_mode, capabilities=capabilities(), types=sorted(known),
                       speech="browser_optional", motion_requires_click_approval=True)

    @app.post("/commands")
    def command():
        data = strict_json(request.get_data(as_text=True))
        if not isinstance(data, dict) or set(data) not in ({"text"}, {"text", "proposal_id"}):
            raise BadRequest("Command requires text and optional proposal_id; never raw action JSON")
        text = validate_text(data["text"])
        # Stop does not wait for the LLM, a command lock, or a still-valid proposal ID.
        if is_stop(text):
            errors = controller.stop()
            return jsonify(action="stop", interpreter="local_stop", stopped=True,
                           message="RoboMaster stop requested. Inspect hardware and restart locally.",
                           errors=errors, physical_stop_verified=False), 502 if errors else 200
        identifier = data.get("proposal_id")
        if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 100):
            raise BadRequest("Invalid proposal_id")
        if not command_lock.acquire(blocking=False):
            raise Conflict("Another command is being interpreted; stop remains available")
        try:
            with lock:
                if identifier is not None and identifier not in runs:
                    raise BadRequest("Unknown proposal")
                selected = copy.deepcopy(runs.get(identifier))
            pending_bom = selected["bom"] if selected and selected["state"] == "awaiting_confirmation" and selected["kind"] == "fulfillment" else None
            model = DemoCommandClient() if command_mode == "demo" else command_model_factory()
            value = validate_intent(model.interpret(text, part_types=known, current_bom=pending_bom), known)
            action = value["action"]
            result = {"action": action, "interpreter": command_mode, "intent": value,
                      "physical_delivery_verified": False}
            if action == "stop":
                errors = controller.stop()
                return jsonify(**result, stopped=True, message="RoboMaster stop requested; inspect and restart locally.",
                               errors=errors, physical_stop_verified=False), 502 if errors else 200
            if action == "clarify":
                return jsonify(**result, message=value["question"])
            if action == "status":
                with lock:
                    active = next((r for r in runs.values() if r["state"] == "running"), None)
                    latest = next(reversed(runs.values()), None)
                    selected_run = active or runs.get(identifier) or latest
                    report = serialize_proposal(selected_run) if selected_run else None
                return jsonify(**result, stopped=controller.cancelled.is_set(), run=report,
                               message="Stopped: inspect and restart locally." if controller.cancelled.is_set() else
                               ("Run state: " + report["state"] if report else "Idle. No run has been created."))
            if action == "detect":
                return jsonify(**result, detection=preview(), message="Fresh tagged-cup detection; no motion requested.")
            with lock:
                ensure_idle(require_armed=True)
                if action == "current_bom":
                    current = runs.get(identifier)
                    if not current or current["kind"] != "fulfillment" or current["state"] != "awaiting_confirmation":
                        raise Conflict("Select a pending BOM first. Completed or running requests cannot be replayed.")
                    return jsonify(**result, proposal=serialize_proposal(current),
                                   message="Review the selected BOM and click Approve. A spoken request is not approval.")
            # Only fetch remains after strict intent validation and read-only/stop handling.
            run = make_proposal({part["type"]: part["quantity"] for part in value["parts"]}, "command")
            run.update(request_text=text, interpreted_intent=value, interpreter=command_mode)
            with lock:
                ensure_idle(require_armed=True)
            return jsonify(**result, proposal=save_run(run),
                           message="Proposal only. Check the interpreted action and click Approve before motion."), 201
        finally:
            command_lock.release()

    def execute(proposal_id, run):
        def progress(event):
            with lock:
                runs[proposal_id]["events"].append(event)
        try:
            result = controller.run(run["bom"], approved=True, progress=progress)
            with lock:
                runs[proposal_id].update(result)
        except BaseException as exc:
            with lock:
                runs[proposal_id].update(state="halted", error=str(exc),
                                         stop_errors=controller.last_stop_errors)

    @app.post("/proposals/<proposal_id>/approve")
    def approve(proposal_id):
        data = strict_json(request.get_data(as_text=True))
        if not isinstance(data, dict) or data.get("approved") is not True:
            raise BadRequest("Approval requires approved: true")
        with lock:
            if proposal_id not in runs:
                raise BadRequest("Unknown proposal")
            if controller.cancelled.is_set():
                raise Conflict("Stopped session: inspect hardware and restart all processes")
            if runs[proposal_id]["state"] != "awaiting_confirmation":
                raise Conflict("Proposal already used; approval cannot replay a run")
            if any(r["state"] == "running" for r in runs.values()):
                raise Conflict("A run is active")
            run = runs[proposal_id]
            if set(data) != {"bom", "approved"}:
                raise BadRequest("BOM approval requires edited bom and approved: true")
            bom = validate_bom(data["bom"])
            missing = sorted(set(bom) - known)
            if missing:
                raise BadRequest("No cup/tag mapping for: " + ", ".join(missing))
            advice_input = {"components": run["component_details"]} if bom == run["bom"] and run["component_details"] else bom
            if "raw_bom" in run:
                advice_input = {"components": run["component_details"]} if run["component_details"] else run["raw_bom"]
            run.update(bom=dict(bom), llm_schematic_suggestions=build_schematic_suggestions(advice_input))
            if "classification_bom" in run:
                run["classification_stale"] = bom != run["classification_bom"]
            if run.get("schematic_review") is not None:
                run["schematic_review_stale"] = bom != run["schematic_review_grouped_bom"]
            run.update(state="running", warnings=[])
            threading.Thread(target=execute, args=(proposal_id, copy.deepcopy(run)), daemon=True).start()
        return jsonify(id=proposal_id, state="running"), 202

    @app.get("/proposals/<proposal_id>")
    def get_run(proposal_id):
        with lock:
            if proposal_id not in runs:
                return jsonify(error="Unknown proposal"), 404
            return jsonify(serialize_proposal(runs[proposal_id]))

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
    parser = argparse.ArgumentParser(description="Schematic to Fetch confirmation UI and controller")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--web-port", type=int, default=5002)
    parser.add_argument("--command-mode", choices=("baseten", "demo"),
                        help="demo uses a small offline grammar, not an LLM; never selected implicitly")
    args = parser.parse_args()
    config_path = Path(args.config)
    config = load_yaml(config_path)
    tags = load_tag_map(config_path.parent / config.get("tag_map", "tag_map.yaml"))
    group_catalog = load_group_catalog(config_path.parent / config.get("tag_map", "tag_map.yaml"))
    token = os.getenv(config.get("node_token_env", "HCP_NODE_TOKEN"))
    controller = Controller(NodeClient(config["pi"]["base_url"], token=token))
    baseten = config.get("baseten", {})
    def model():
        return BasetenClient(vision_model=baseten.get("model"), base_url=baseten.get("url"),
                             api_key_env=baseten.get("api_key_env", "BASETEN_API_KEY"))
    def command_model():
        return BasetenCommandClient(command_model=baseten.get("command_model"), base_url=baseten.get("url"),
                                    api_key_env=baseten.get("api_key_env", "BASETEN_API_KEY"))
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        create_app(controller, model_factory=model, part_types=tags.values(),
                   group_catalog=group_catalog,
                   command_model_factory=command_model,
                   command_mode=args.command_mode or config.get("command_mode", "baseten")).run(
            host="127.0.0.1", port=args.web_port, threaded=True, use_reloader=False)
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
