import argparse
import atexit
import json
import logging
import os
from pathlib import Path
import uuid

from flask import Flask, jsonify, request, send_from_directory

from config import ROOT, load_hardware
from ingestion.parse import SCHEMATIC_EXTENSIONS
from orchestrator.runs import RunManager


def create_app(host=None, model=None, config=None, inventory=None, instance_path=None,
               simulation=None, ingestion_model=None, palette_backend=None):
    app = Flask(__name__, instance_path=str(Path(instance_path or ROOT / "instance").resolve()))
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if palette_backend is None:
        from orchestrator.loop import Orchestrator
        inventory = inventory if inventory is not None else json.loads((ROOT / "bench_inventory.json").read_text())
        orchestrator = Orchestrator(host, model, inventory, config["belt"]["delivery_duration_s"], config["camera"]["max_pose_age_s"], ingestion_model=ingestion_model)
        ingestion_mode = "live" if simulation is None or (ingestion_model is not None and ingestion_model is not model) else "fixture"
        metadata = {"backend": "hcp", "ingestion": ingestion_mode}
        demo_pdf = simulation.pdf if simulation is not None else None
    else:
        orchestrator = palette_backend
        metadata = palette_backend.metadata
        demo_pdf = palette_backend.demo_pdf
    simulated = metadata.get("hardware") == "simulated" if palette_backend is not None else simulation is not None
    manager = RunManager(orchestrator, metadata)
    app.extensions["runs"] = manager
    app.extensions["hcp"] = host

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/status")
    def status():
        with manager.lock:
            active_run = manager.active
        if palette_backend is not None:
            node_status = (palette_backend.status() if hasattr(palette_backend, "status") else
                           {"nodes": {}, "health": {}, "ready": True, "required_nodes": []})
            return jsonify(**metadata, **node_status, simulation=simulated,
                           demo_available=demo_pdf is not None, accepts_bom=True, active_run=active_run)
        with host.lock:
            health = dict(host.health)
        nodes = host.registry.snapshot()
        required = ["arm", "conveyor", "camera"]
        return jsonify(**metadata, nodes=nodes, health=health, simulation=simulated, active_run=active_run,
                       ready=all(n in nodes for n in required), required_nodes=required,
                       demo_available=demo_pdf is not None, accepts_bom=False)

    @app.get("/demo.pdf")
    def demo():
        if demo_pdf is None:
            return jsonify(error="Demo PDF is available only in simulation"), 404
        return send_from_directory(str(demo_pdf.parent), demo_pdf.name)

    @app.post("/api/runs")
    def run():
        if request.is_json:
            if palette_backend is None:
                return jsonify(error="JSON BOM submissions require camera-free mode"), 400
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or set(body) != {"bom"} or not isinstance(body["bom"], dict):
                return jsonify(error='Send JSON {"bom": {"components": [...]}}'), 400
            try:
                run_id = manager.submit_bom(body["bom"], simulation=simulated)
            except RuntimeError as exc:
                return jsonify(error=str(exc)), 409
            return jsonify(id=run_id, state="queued"), 202
        upload = request.files.get("pdf")
        if upload is None:
            if demo_pdf is None:
                return jsonify(error="Upload a schematic PDF, image, or SCH file"), 400
            path = demo_pdf
        else:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in SCHEMATIC_EXTENSIONS:
                return jsonify(error="Upload a PDF, JPEG, PNG, BMP, TIFF, or SCH file"), 400
            path = Path(app.instance_path) / f"{uuid.uuid4().hex}{suffix}"
            upload.save(path)
        try:
            run_id = manager.submit(path, simulation=simulated)
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
        return jsonify(error="Request exceeds the 16 MiB upload limit"), 413

    return app


def create_palette_app(*, palette_path=None, config_path=None, live_ingestion=False,
                       ingestion_model=None, instance_path=None):
    """Always SimDriver: no public factory/HTTP option can enable real motors."""
    from config.so100 import load_config
    from orchestrator.fixtures import FixtureIngestionModel, create_demo_pdf
    from orchestrator.palette_backend import PaletteBackend
    from palette import load_palette

    config = load_config(config_path)
    palette = load_palette(palette_path or ROOT / "palette.yaml", config)
    directory = Path(instance_path or ROOT / "instance/palette-simulation").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if ingestion_model is None:
        if live_ingestion:
            from orchestrator.baseten_client import BasetenClient
            ingestion_model = BasetenClient()
        else:
            ingestion_model = FixtureIngestionModel()
    demo_pdf = None if live_ingestion else create_demo_pdf(directory / "demo")
    backend = PaletteBackend(palette, config, ingestion_model, live_ingestion=live_ingestion, demo_pdf=demo_pdf)
    return create_app(instance_path=directory, palette_backend=backend)


def create_remote_palette_app(host, *, palette_path=None, config_path=None, live_ingestion=False,
                              allow_hardware=False, ingestion_model=None, instance_path=None):
    """Explicit opt-in HCP transport; physical mode also requires arming on the Pi."""
    from config.so100 import load_config
    from orchestrator.fixtures import FixtureIngestionModel, create_demo_pdf
    from orchestrator.remote_palette import RemotePaletteBackend
    from palette import load_palette

    config = load_config(config_path)
    palette = load_palette(palette_path or ROOT / "palette.yaml", config)
    directory = Path(instance_path or ROOT / "instance/palette-hcp").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if ingestion_model is None:
        if live_ingestion:
            from orchestrator.baseten_client import BasetenClient
            ingestion_model = BasetenClient()
        else:
            ingestion_model = FixtureIngestionModel()
    demo_pdf = None if live_ingestion or allow_hardware else create_demo_pdf(directory / "demo")
    backend = RemotePaletteBackend(host, palette, config, ingestion_model, allow_hardware=allow_hardware,
                                   live_ingestion=live_ingestion, demo_pdf=demo_pdf)
    return create_app(host=host, instance_path=directory, palette_backend=backend)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--simulate", action="store_true", help="Legacy HCP camera/arm/conveyor simulation")
    modes.add_argument("--camera-free", action="store_true", help="Fixed palette + SimDriver, no camera/conveyor/HCP/I2C")
    modes.add_argument("--palette-hcp", action="store_true", help="Fixed palette over HCP TCP; launch arm.hcp_node separately")
    parser.add_argument("--allow-hardware", action="store_true", help="Only with --palette-hcp: permit a locally armed physical node")
    parser.add_argument("--live-ingestion", action="store_true", help="Use existing Baseten PDF→BOM instead of fixture ingestion")
    parser.add_argument("--palette", default=str(ROOT / "palette.yaml"), help="Camera-free palette YAML")
    parser.add_argument("--arm-config", help="Camera-free PWM limits config (does not itself enable hardware)")
    parser.add_argument("--config", default=os.getenv("HARDWARE_CONFIG", str(ROOT / "config/hardware.json")))
    parser.add_argument("--hcp-bind", help="HCP listener address; palette/simulation defaults to loopback")
    parser.add_argument("--hcp-port", type=int, default=int(os.getenv("HCP_PORT", "9000")))
    parser.add_argument("--web-bind", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=5000)
    args = parser.parse_args()
    if args.allow_hardware and not args.palette_hcp:
        parser.error("--allow-hardware requires --palette-hcp; --camera-free always stays simulated")
    if args.hcp_bind is None:
        args.hcp_bind = "127.0.0.1" if args.palette_hcp or args.simulate else "0.0.0.0"
    logging.basicConfig(level=logging.INFO)
    if args.camera_free:
        app = create_palette_app(palette_path=args.palette, config_path=args.arm_config,
                                 live_ingestion=args.live_ingestion)
        app.run(host=args.web_bind, port=args.web_port, threaded=True, debug=False, use_reloader=False)
        return
    if args.palette_hcp:
        from hcp_host.server import HCPHost
        host = HCPHost(args.hcp_bind, args.hcp_port).start()
        try:
            app = create_remote_palette_app(host, palette_path=args.palette, config_path=args.arm_config,
                                            live_ingestion=args.live_ingestion, allow_hardware=args.allow_hardware)
            app.run(host=args.web_bind, port=args.web_port, threaded=True, debug=False, use_reloader=False)
        finally:
            host.stop()
        return
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
    from hcp_host.server import HCPHost
    host = HCPHost(args.hcp_bind, args.hcp_port, command_timeout=timeout).start()
    atexit.register(host.stop)
    if args.simulate:
        simulation = Simulation(host, ROOT / "instance/simulation")
        atexit.register(simulation.stop)
    app = create_app(host, model, config, simulation=simulation, ingestion_model=ingestion_model)
    app.run(host=args.web_bind, port=args.web_port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
