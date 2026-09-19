# Schematic to Fetch

## Autonomous schematic-to-bench assembly

Schematic to Fetch turns a circuit schematic into a physical parts run. Upload a PDF, let the vision model produce a validated bill of materials, and the Pi coordinates an AprilTag camera, a four-degree-of-freedom arm, and a timed conveyor to collect the mapped components.

![Schematic to Fetch simulation interface with arm, conveyor, and camera status and schematic PDF upload](docs/images/schematic-to-fetch-simulation.png)

One Flask button starts PDF → BOM → arm pick/place → belt delivery. Python runs on the Pi; Arduino firmware owns PWM. Every HCP node is a TCP client. Host, orchestrator, ingestion, and UI share one process. Arm and conveyor share one serial connection while registering as separate nodes.

The five-part simulation verifies software behavior. Physical delivery and live Baseten accuracy require event hardware, models, calibration, and a hand-checked schematic. Timed servo completion does not measure whether a part was gripped.

## Run the simulation

Use Python 3.11+ (verified on Python 3.13). OpenCV contrib includes AprilTag detection; do not also install a conflicting opencv-python package.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m ui.app --simulate
```

Open <http://127.0.0.1:5000> and click **Run** without uploading a PDF. The fixture requests two 10k resistors, two 0.1uF capacitors, and one NE555. It uses real PDF rendering, OpenCV detection of a generated saved tag image, generated clients, TCP framing/discovery, orchestration guards, IK, and command lifecycle handling. Model responses and motors are fixtures. Uploaded PDFs in simulation also use the fixture BOM; the UI explicitly labels simulation.

Demo assets live in ignored `instance/simulation/`. Simulation never opens an Arduino, real camera, or Baseten connection.

To exercise the real PDF→BOM call while retaining simulated arm/conveyor behavior, export the Baseten key and start the hybrid backend:

```sh
export BASETEN_API_KEY='<your-key>'
export BASETEN_VISION_MODEL='moonshotai/Kimi-K2.6'
python -m ui.app --simulate --live-ingestion --web-port 5001
```

Upload a PDF at <http://127.0.0.1:5001>. Baseten supplies the BOM; only the downstream hardware actions are simulated. The key is read from the process environment and is never written to the repository.

## Event configuration

Copy `config/hardware.example.json` to ignored `config/hardware.json` and fill every null / `<CONFIRM>`. Unfilled templates cannot enable motion. `.env.example` documents variables; `.env` is not auto-loaded. Keep API keys in the environment, never in committed files.

```sh
export BASETEN_API_KEY='<CONFIRM>'
# Recommended current vision/tool-capable slug; verify availability in your catalog.
export BASETEN_VISION_MODEL='moonshotai/Kimi-K2.6'
export BASETEN_TOOL_MODEL='moonshotai/Kimi-K2.6'
export HCP_HOST='<CONFIRM_PI_IP>'
export HCP_PORT=9000
export HARDWARE_CONFIG=config/hardware.json
```

The default recommendation is `moonshotai/Kimi-K2.6`, which Baseten currently lists as vision-capable and which supports structured outputs and tool calling through the OpenAI-compatible endpoint. Requests use `https://inference.baseten.co/v1`, bounded 429 backoff, and local output validation. Rate limits are account-level, so run tests sequentially rather than in parallel; Baseten's default Basic-unverified limit is 15 requests/minute. Verify the slug and effective limits in your Baseten catalog before spending credits. Unsupported features do not silently fall back to unstructured output. Voice/STT is P1 and has no P0 dependency.

| Area | Values to measure or confirm |
| --- | --- |
| Serial | Arduino USB port and matching firmware baud |
| Arm | Two link lengths; shoulder-origin world position; base yaw; elbow branch (`-1` or `1`); servo zeros, signs, limits; home pose; gripper positions; movement and settling times |
| Bins | Tag-local XY grasp offset and world Z pickup height for every tag |
| Zones | Actual `belt`/staging XYZ coordinates and a reachable clearance height |
| Belt | Neutral/forward pulse widths, measured delivery duration, maximum duration |
| Camera | Source, calibration resolution, pixel-to-bench homography, publication rate and maximum pose age |

World XY lies on the bench and Z points upward. Distances are meters, tag heading is radians, and joint commands are degrees. `base_world_m` is the shoulder origin. The arm model has base yaw, shoulder/elbow pitch, and gripper, with no wrist. Validate approach/retreat paths and natural grasp orientation on the real bench.

For calibration, record at least four non-collinear correspondences in JSON with `image_size`, `pixel_points`, and `world_points_m`, then run:

```sh
python -m vision.calibrate --points '<CONFIRM_CALIBRATION_POINTS_JSON>'
```

Copy its printed homography and image size into camera config. Capture resolution must match calibration. The planar transform assumes the detection plane matches calibration; account for lens distortion and tag height when measuring pickup accuracy.

Inventory tags identify repeatable pickup bins. Each exact `(type, value)` has one unique tag; quantity two means two picks from that bin. Bins must expose one graspable part at the same calibrated location after each pick and contain enough stock. Missing mappings produce warnings; available parts are delivered and the run finishes `incomplete`. There is no stock/depletion sensing.

## Firmware and real hardware

Copy `firmware/pins.example.h` to ignored `firmware/pins.h`; fill the confirmed baud and five servo pins. No physical defaults are supplied. With arduino-cli, the Servo library, and the confirmed board core installed:

```sh
python scripts/build_firmware.py --fqbn '<CONFIRM_BOARD_FQBN>'
```

This stages Arduino's required matching sketch directory and compiles without uploading. Upload with the Arduino IDE or your board's confirmed-port workflow. Native C++ harness tests do not replace compiling for the actual board.

The belt protocol assumes a continuous-rotation servo/drive accepting neutral/forward PWM; confirm the installed drive supports this. A standard positional servo does not provide unlimited timed travel. Position-indexed mechanics require a coordinated conveyor contract change.

Firmware boots without attaching arm servos. The first joint command establishes the initial pose; perform the first home under supervision. It interpolates joints, waits for configured settling, stops the belt before completion, and monitors a 250 ms heartbeat with a 1.5 s timeout. Loss of communication stops the belt and holds current arm targets. Interrupted actuator sessions latch a fault and require inspection/restart.

First test the standalone host with generated stub nodes:

```sh
python -m hcp_host.server --port 9000
python hcp_sdk/hcp_sdk_gen.py --input hcp_sdk/nodes/arm.json --output ./out --host "$HCP_HOST" --port "$HCP_PORT"
python hcp_sdk/hcp_sdk_gen.py --input hcp_sdk/nodes/conveyor.json --output ./out --host "$HCP_HOST" --port "$HCP_PORT"
python hcp_sdk/hcp_sdk_gen.py --input hcp_sdk/nodes/camera.json --output ./out --host "$HCP_HOST" --port "$HCP_PORT"
PYTHONPATH=. python out/arm_hcp_support.py
```

Generated clients discover themselves but report errors until handlers are attached. They never pretend to move hardware. Stop the standalone host before starting the integrated app; both own the HCP port.

For real operation, run these in separate Pi terminals:

```sh
# Owns host, orchestration worker, ingestion, and UI.
python -m ui.app
# Owns the only Arduino connection and registers BOTH actuator nodes.
python -m actuator.arm_node --host "$HCP_HOST" --port "$HCP_PORT"
# Camera node; optionally use --image PATH for a saved calibrated image.
python -m vision.camera_node --host "$HCP_HOST" --port "$HCP_PORT"
```

Runtime launchers regenerate/import `out/<id>_hcp_support.py`. Run from the repo root in its Python environment. HTTP defaults to loopback; pass `--web-bind 0.0.0.0` to access the UI over the event LAN. HCP listens on the LAN. These services have no internet-facing authentication and are intended for the trusted demo network.

Upload a schematic and press Run. Runs are serialized. Tools first stop the belt and home the empty arm, then perform pick → place-and-retreat → advance-and-stop for each unit. The ledger counts delivery only after conveyor completion. Empty the output tray before a new complete run.

## Interfaces and failure behavior

The four frozen contracts are the unchanged upstream node schema, NDJSON envelope, supplied BOM schema, and `bench_inventory.json` shape. See [protocol details](docs/protocol.md).

```json
{"action":"pick","id":"c17","device_id":"arm","payload":{"tag_id":3}}
```

All TCP messages are UTF-8 JSON plus newline, bounded to 1 MiB. Discovery replies echo `REQUEST_HCP_DATA` and its ID, identify the node, and carry its full definition in `payload`. Camera publications are latest snapshots, including empty detections. Socket reads, queued writes, and handlers run independently.

Only `done` resolves a command. Reconnection rediscovers nodes and never replays physical commands. The orchestrator checks schema, requested tags/quantities, fresh poses, arm/belt sequencing, and delivery duration before dispatch. Model call validation permits bounded correction; hardware errors terminate the run. No grip-failure replanning or measured grip feedback is implemented.

HTTP: `POST /api/runs` accepts multipart `pdf` and returns `202 {id,state}`; `GET /api/runs/<id>` reports progress/warnings/deliveries; `GET /api/status` reports nodes and health. Concurrent submissions return 409. Uploads are capped at 16 MiB/eight PDF pages. Run records are in memory and do not resume after restart.

## Verification

```sh
python -m pytest -q
python -m ingestion.parse --pdf '<CONFIRM_DEMO_PDF>' --expected '<CONFIRM_GROUND_TRUTH_JSON>'
```

Tests cover framing and UTF-8 fragmentation, discovery/late nodes, correlation, timeout/reconnect, concurrent context, registry tools, repeated bin quantities, missing parts, rejected actions, IK, saved-image tags, PDF/model constraints, 429 retry limits, serial correlation, firmware timers/watchdog, and a Flask-triggered five-part simulation. Firmware harness tests require a C++ compiler.

At the event, compare the extracted BOM to hand-checked ground truth, measure `advance(2.0)`, verify calibration, then complete at least five consecutive physical picks with no failed grip. Manually compare tray contents to the schematic. Simulation success is separate from physical acceptance.

Voice, missing-part UI polish, grip replanning, Snowflake, and fine-tuned readers remain P1/P2.

## Reference provenance

Reference: [danielzyy/calhacks2025](https://github.com/danielzyy/calhacks2025/tree/f7cf00663244377b9c79270f0175756904819534), pinned at `f7cf00663244377b9c79270f0175756904819534`.

- Generator: retains upstream validation, CLI and output naming; the generated connect/listen/events/response-queue template is extracted into `hcp_sdk/runtime.py` and extended with framing, IDs, independent workers and backoff.
- Node schema: copied unchanged.
- Host: adapts `hcp_client/main.py` and `hcp_executor.py` accept/read/queue and registry-dispatch code.
- Camera: adapts `vision/tag_detections.py` OpenCV AprilTag setup; upstream hardcoded geometry/calibration is not used.

Baseten calls follow its [Chat Completions API](https://docs.baseten.co/reference/inference-api/chat-completions). No ASI1, LeRobot motor layer, microphone stack, or broker is included.
