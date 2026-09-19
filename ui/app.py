import argparse
import atexit
import json
import logging
import os
from pathlib import Path
import uuid

from flask import Flask, jsonify, request, send_from_directory

from config import ROOT, load_hardware
from hcp_host.server import HCPHost
from orchestrator.loop import Orchestrator, RunManager


def create_app(host, model, config, inventory=None, instance_path=None, simulation=None, ingestion_model=None):
    app = Flask(__name__, instance_path=str(Path(instance_path or ROOT / "instance").resolve()))
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    inventory = inventory if inventory is not None else json.loads((ROOT / "bench_inventory.json").read_text())
    orchestrator = Orchestrator(host, model, inventory, config["belt"]["delivery_duration_s"], config["camera"]["max_pose_age_s"], ingestion_model=ingestion_model)
    manager = RunManager(orchestrator)
    app.extensions["runs"] = manager
    app.extensions["hcp"] = host

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/status")
    def status():
        with host.lock:
            health = dict(host.health)
        return jsonify(nodes=host.registry.snapshot(), health=health, simulation=simulation is not None, active_run=manager.active)

    @app.get("/demo.pdf")
    def demo():
        if simulation is None:
            return jsonify(error="Demo PDF is available only in simulation"), 404
        return send_from_directory(str(simulation.pdf.parent), simulation.pdf.name)

    @app.post("/api/runs")
    def run():
        upload = request.files.get("pdf")
        if upload is None:
            if simulation is None:
                return jsonify(error="Upload a schematic PDF"), 400
            path = simulation.pdf
        else:
            if not upload.filename or not upload.filename.lower().endswith(".pdf"):
                return jsonify(error="Upload a .pdf file"), 400
            path = Path(app.instance_path) / f"{uuid.uuid4().hex}.pdf"
            upload.save(path)
        try:
            run_id = manager.submit(path, simulation=simulation is not None)
        except RuntimeError as exc:
            if upload is not None:
                path.unlink(missing_ok=True)
            return jsonify(error=str(exc)), 409
        return jsonify(id=run_id, state="queued"), 202

    @app.get("/api/runs/<run_id>")
    def run_status(run_id):
        try:
            return jsonify(manager.get(run_id))
        except KeyError:
            return jsonify(error="Unknown run"), 404

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="PDF exceeds the 16 MiB upload limit"), 413

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true", help="Use simulated camera, arm, and conveyor")
    parser.add_argument("--live-ingestion", action="store_true", help="In --simulate mode, use Baseten for PDF→BOM while keeping simulated hardware")
    parser.add_argument("--config", default=os.getenv("HARDWARE_CONFIG", str(ROOT / "config/hardware.json")))
    parser.add_argument("--hcp-bind", default="0.0.0.0")
    parser.add_argument("--hcp-port", type=int, default=int(os.getenv("HCP_PORT", "9000")))
    parser.add_argument("--web-bind", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=5000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    simulation = None
    if args.simulate:
        from orchestrator.simulation import FixtureModel, Simulation, simulation_config
        model, config = FixtureModel(), simulation_config()
        ingestion_model = model
        if args.live_ingestion:
            from orchestrator.baseten_client import BasetenClient
            ingestion_model = BasetenClient()
        if args.hcp_bind == "0.0.0.0":
            args.hcp_bind = "127.0.0.1"
    else:
        from actuator.kinematics import Kinematics
        from orchestrator.baseten_client import BasetenClient
        from vision.camera_node import TagDetector
        config = load_hardware(args.config)
        Kinematics(config["arm"])
        TagDetector(config["camera"])
        model = BasetenClient()
        ingestion_model = model
    timeout = max(30, 4 * (config["arm"]["move_ms"] + config["arm"]["settle_ms"]) / 1000 + 10, config["belt"]["max_duration_s"] + 5)
    host = HCPHost(args.hcp_bind, args.hcp_port, command_timeout=timeout).start()
    atexit.register(host.stop)
    if args.simulate:
        simulation = Simulation(host, ROOT / "instance/simulation")
        atexit.register(simulation.stop)
    app = create_app(host, model, config, simulation=simulation, ingestion_model=ingestion_model)
    app.run(host=args.web_bind, port=args.web_port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
