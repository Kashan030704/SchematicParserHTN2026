# Schematic to Fetch architecture and project handoff

This document describes the repository as implemented, not just the desired demo.
Use [README.md](../README.md) for commands and [protocol.md](protocol.md) for payloads.

## 1. What we are building

A Mac reads an electronics schematic through Baseten, presents a proposed bill of materials
beside the Pi's camera detections, and waits for a human to edit and approve it. It then
commands one Raspberry Pi HTTP node controlling a **DJI RoboMaster EP** arm, gripper
and chassis over USB/RNDIS. Cups are delivered to a fixed collection point.

The plan is one-shot: fulfill each approved component type once, in order. The robot transports
one representative labeled **cup per type**. Required quantities are displayed; the robot
does not count individual parts or repeat a pickup for each unit.

The current vision decision is **image-based terminal approach below the plan**, not a second
planning system. The Pi aligns a selected tag using pixel position and apparent size, then
executes the scripted grip. No camera intrinsic matrix, camera-to-gripper transform,
physical tag size, world-coordinate tag pose, IK, or LLM-controlled visual steering is used.

### Status and limits

Implemented and exercised without hardware:

- real Baseten extraction, vision review and seven-group classification, with mocked models in automated tests;
- editable approval UI, sequential controller, no automatic physical command retries;
- Pi HTTP state machine and EP plaintext motion adapter;
- AprilTag pixel-feature extraction on synthetic images;
- bounded terminal visual controller and explicit simulation of convergence;
- subprocess dry-run and actual localhost HTTP/TCP integration tests.

Not established by those tests:

- reliable extraction/review across arbitrary schematics; a live synthetic upload has passed;
- EP USB networking, plaintext command behavior, live H.264 decoding or response timing;
- visual-controller gains/signs/tolerances on the physical mounting;
- any successful physical grip, cup transport, placement or delivery;

Do not treat software test success as a completed physical demonstration.

## 2. Machines and connections

| Component | Process/location | Connection |
| --- | --- | --- |
| Browser | Mac browser | Mac Flask UI at loopback port 5002 |
| Ingestion/controller | Mac, `python -m ui.app` | HTTPS to Baseten; HTTP to the robot node |
| Robot node | Pi, `python -m actuator.main` | HTTP on port 8081 by example |
| EP control | RoboMaster EP | Pi → EP TCP 40923 over USB/RNDIS |
| EP video | RoboMaster EP | Pi receives H.264 TCP 40921 |

The Pi's Wi-Fi address is what the Mac uses. The EP's USB/RNDIS address is what the Pi uses.
They are different interfaces and addresses. USB does not require the Mac to join the EP's
access point: Mac and Pi can stay on an internet-connected LAN.

The **Mac** waits for the robot's successful drop log and immediately authorizes its return
from the collection point to observe.

HTTP is the deployed HCP transport in this version. The repository's earlier NDJSON TCP
discovery/codegen implementation is retained only as a reference compatibility library.
Do not launch its port-9000 host for this workflow.

## 3. Repository walkthrough

### `ingestion/`

`parse.py` reuses the existing schematic-rendering path. PDFs/images become bounded raster
images and are sent together in one vision call. `bom_schema.json` now describes:

```json
{"resistors": 2, "capacitors": 1, "diodes/LED": 3}
```

Execution keys are the exact registered group vocabulary, not physical coordinates. Values are positive
integer quantities. Malformed JSON, duplicate keys, invalid quantities and nonfinite values
are rejected. The proposal still requires human review: valid JSON does not mean the
schematic was interpreted correctly.

The execution contract is the flat object above, not the old
`components: [{type, value, quantity, refdes}]` payload. Existing saved BOMs using that
shape must be converted before submission.

The incoming frontend's PDF/JPEG/PNG/BMP/TIFF uploads are retained. `sch.py` additionally
parses legacy KiCad and EAGLE XML `.sch` locally. It keeps rich component records and the
original flat BOM. Hierarchical designs and modern `.kicad_sch` files are rejected, not partly read.

`classification.py` defines a strict source-to-group assignment schema. With the default
seven-group catalog, extracted PDF/image/SCH components and raw manual JSON are classified
by the configured model. Each original label must appear exactly once with a registered
group, confidence and reason. Local code sums the original quantities and attaches IDs
from `tag_map.yaml`; the model cannot choose counts or tag IDs. Unusual components receive
the closest group with low confidence. Ambiguous matches also appear in the review UI.
Explicit manual JSON already naming registered groups needs no model call.

Classification failures preserve the original BOM for manual correction, with no automatic
retry. Unknown labels cannot pass approval. For legacy tag maps without group definitions,
the earlier exact-label mapping remains available, including offline SCH parsing.

### `orchestrator/`

- `baseten_client.py`: structured-output BOM extraction, separate advisory vision review,
  and component classification. PDF/image proposals normally use three calls; SCH/raw JSON
  uses one classification call. All use the configured vision model.
  Endpoint/model are configurable; key is read from an environment-variable name.
  There is no fake fallback if Baseten fails, and no automatic 429 retry.
- `ingest.py`: small wrapper connecting the UI to existing ingestion.
- `commands.py`: optional one-call language intent parser with capability descriptions
  and strict local validation. Fetch/current-BOM requests only create/show
  confirmation proposals; status/detection are read-only; stop is latched. Exact stop
  phrases bypass the model. An explicitly selected `demo` grammar works without an API
  call and never substitutes for a failed Baseten request.
- `schematic_advice.py`: preserved frontend feature with deterministic component rules.
  These supplement the separate PDF/image vision review in `baseten_client.py`, validated
  by `ingestion/review.py`. Vision findings include evidence, damage consequences, suggested
  checks, and conditional parts-only CAD estimates. Unknown costs remain null. Review
  failure leaves successful BOM extraction available; edited BOMs mark the review stale.
  Local component checks also work for SCH/manual input without a model call.
  They never alter the approved BOM, dispatch commands or trigger replanning.
- `nodes.py`: HTTP JSON client for the RoboMaster Pi. Bounded response sizes, explicit
  timeouts, no redirects, no ambient proxy for LAN requests, no command retries.
- `controller.py`: freezes the approved BOM, starts a run, and dispatches the sequential
  robot calls. Missing cups are reported and skipped. Other errors halt and request
  a robot stop.

There is no ongoing LLM agent choosing arbitrary motor commands. The vision model proposes
parts; an optional separate command model interprets text/voice requests into one permitted
intent. Neither model has approval authority. The deterministic controller chooses the
fixed operation sequence; visual corrections are the Pi executor's responsibility.

### `ui/`

`app.py` provides the browser, upload/parse endpoint, preview, explicit approval, progress
polling and stop. `static/index.html`, `app.js`, and `style.css` implement the front end.
The incoming light-themed layout, requested-parts list, schematic suggestions and progress
cards are integrated with this gate, not the retired palette backend. Source SCH records,
raw extraction JSON, and per-component group/tag/confidence/reasons remain available for review.
Editing grouped totals marks classification and schematic review as stale; it does not
overwrite the source mapping. Node health polling is read-only; simulated nodes are labeled.
Flat BOM insertion order survives proposal serialization and becomes the approved pick order.
`static/bg3d.js` adds the incoming scroll-reactive circuit board, bokeh, LED satellites
and grid. Its pinned, MIT-licensed Three.js dependency is served from `static/vendor/`,
keeping the same-origin script policy intact. It never reads or writes run state;
no WebGL means a plain background, and reduced-motion settings disable animation.

`static/commands.js` adds an isolated optional text/push-to-talk panel. Browser speech
recognition (not Baseten STT) requires explicit microphone consent and may use a remote
browser-provider service. Recognized final text takes the same `/commands` path as typing;
exact recognized stops use the existing `/stop` path. Transcripts never approve motion.
Unsupported speech/permission failure leaves the original UI and typed input intact.
The microphone is not always-on, no background restart occurs, and a physical stop remains necessary.

All UI proposals are `fulfillment` requests with an editable BOM. Text/voice requests
use the same approval gate as uploads and manual BOMs.

Before approval, upload and detection do not cause arm/chassis movement. The robot must already
be positioned at observe. Approval contains the edited BOM and `approved: true`; duplicate
approval is rejected. There is one active run, no resume/replay endpoint, and a latched stop.

History lives in memory, capped at 50 records. Restarting a process does not resume a task.
Browser mutation requests use a per-process anti-CSRF token. There is no user-account system,
database, production deployment server, or durable job queue.

### `actuator/`

- `main.py`: Pi startup, CLI flags, signal cleanup, camera initialization, HTTP server.
  `--dry-run` uses explicit simulated perception/motion.
  `--inspect-camera` is a diagnostic-only session, not a camera-only architecture:
  it prints detections and cannot run motion or expose motion endpoints.
- `server.py`: robot state machine, run IDs, approved types, single-attempt enforcement,
  fresh-detection checks, exclusive normal-operation lock, urgent stop.
- `motion.py`: serializes EP plaintext commands over one socket. Buffers split replies,
  validates success, times out, latches faults, and never retries an uncertain command.
  Arm configuration is in mm and converted to the plaintext API's cm. Chassis is meters/degrees.
- `perception.py`: extracts tag36h11 IDs, corner pixels, center pixels and mean side length.
  Live frames come through a small PyAV H.264 adapter using the same EP control session.
  It does not compute a metric pose.
- `approach.py`: selects exactly one requested tag, takes a configured coarse route,
  performs bounded image-space corrections, gates the grip, and retraces successful
  approach translations. It does not change the BOM, substitute tags, or replan.

### Configuration and entry points

- `tag_map.yaml`: tag36h11 IDs 0 → resistors, 2 → capacitors, 6 → integrated circuits,
  7 → motors, 1 → inductors, 5 → custom printed circuit boards, 8 → diodes/LED.
  Group descriptions and the closest-group policy also live here. Unique ID per group.
- `poses.yaml`: dummy arm targets, chassis collection/return legs, tag-indexed coarse routes,
  visual setpoint, control directions, time/travel/step caps and dwell settings.
  `calibrated: false` and `visual_approach.taught: false` block hardware.
- `config.yaml`: Mac robot-node URL and Baseten endpoint/model/key-env name.
- `config.demo.yaml`: an explicit local robot-node URL and demo command grammar for prototype testing.
  Always check node health says simulated; command mode alone does not simulate hardware.
- `config/__init__.py`: YAML parsing and validation, including duplicate-key rejection.
- `main.py`/`cli.py`: offline `run --bom ... --dry-run` acceptance entry point. It exercises
  the robot HTTP contract in-process and prints commands; it cannot enable real hardware.
- `sample_bom.json`: manual fixture for offline tests, not the ingestion implementation.
- `requirements-backend.txt`: Mac.
- `requirements-pi.txt`: Pi, including OpenCV and PyAV, without the legacy DJI Python package.
- `requirements.txt`: local simulation/development/tests.

Use ignored `config.local.yaml`, `poses.local.yaml`, and optionally `tag_map.local.yaml`
for event settings. Environment variables/secrets are not committed and `.env` is not
automatically loaded.
`BASETEN_VISION_MODEL` and optional `BASETEN_COMMAND_MODEL` are separate. A missing command
model does not break uploads/manual proposals. No audio model is required for browser STT.

### `hcp_sdk/` and `hcp_host/`

The unchanged reference node schema, JSON-to-client generator, framed TCP runtime, registry,
and host remain regression-tested compatibility/reference infrastructure. The live HTTP node
code uses `hcp_host/http.py` for shared request/authentication conventions.

The old deployable arm/camera/palette node definitions and loader were removed so
they cannot be mistaken for the new robot deployment. Generated `out/` files are ignored;
old files left there are not active code and should not be launched.

### `tests/` and `docs/`

Tests cover model request shape, schema rejection, approval, command ordering, tag image
features, visual convergence/failures, stop behavior, socket framing, reference codegen and
actual HTTP to the simulated robot.

Documentation covers the current architecture and API. Historical `ours.md`, `theirs.md`
and the previous simulation screenshot are retained as project material, not current setup
instructions. User-local ignored hardware files have not been erased.

## 4. Exactly what the robot does

For an approved entry `"resistors": 2`, with tag_map binding resistors to ID 0:

1. The Mac starts a run with the approved types.
2. The Mac asks for a fresh detection at observe. An absent resistors cup is flagged and skipped.
3. The Mac sends `/grasp_place` with the same run ID and type.
4. The Pi marks this type attempted, preventing replay.
5. The Pi commands the observe/carry arm pose. It takes another fresh observation of tag 0.
6. It executes the configured short coarse translation legs for tag 0. These are
   dead-reckoning waypoints, not distances inferred from the image.
7. The visual executor repeatedly takes a fresh frame **between stopped, bounded moves**:
   - tag right/left of the taught horizontal target → a small lateral step;
   - horizontally aligned but too small/large → a small forward/backward step;
   - center and size within tolerances → count one stable frame;
   - require several consecutive acceptable frames before allowing the grip.
8. The arm runs its script: open gripper, move to the common grab pose, close, lift to
   observe/carry. There is no grip-success sensor.
9. With the cup held, it reverses the successful approach translations in reverse order.
   This returns to the **commanded estimate** of the starting observe station.
10. It drives the configured `collection_wp` leg from observe to the collection point.
11. It moves to the drop pose, opens the gripper, then retracts to clear the collection point.
12. The Pi returns the successful grasp/drive/drop log.
13. The Mac immediately requests `/return`; the robot drives its configured return leg
    and commands observe. The next type may now begin.

Quantity 2 remains display information. Exactly one resistors cup is commanded.

A successful command is not a measured physical success. The application reports
`physical_delivery_verified: false` even after the run completes.

## 5. What “no metric calibration” means

The visual controller consumes only image features. It does not need lens intrinsics,
a metric tag size, a pixel-to-world transform or camera-to-gripper extrinsic estimation.

It still needs a meaningful **taught image target**:

- At a real graspable arrangement, record the tag's center and mean side length in the
  configured 640×360 stream.
- Keep camera/gimbal orientation fixed relative to the chassis.
- Use consistent printed tag size, tag mounting/height, cup height and gripping appendage.
- Check that the configured lateral/forward signs actually reduce the corresponding errors.
- Verify the common arm grip script works at the taught visual target.

The desired center need not be the image's geometric center. Apparent size by itself is
not a guarantee that the gripper is aligned. Tag tilt can change apparent size; severe
skew is rejected, but this is not full orientation estimation.

The chassis has two translation controls here. It cannot independently correct three
arbitrary image features. Horizontal error and apparent-size error drive motion; vertical
error must also meet tolerance to permit grip, otherwise the executor stops. This is
appropriate only for a consistent camera/tag/cup arrangement.

Default software limits are 1 cm correction steps, 0.05 m/s, 3 stable frames,
40 corrections, 0.30 m cumulative terminal travel and 45 seconds. These are template
bounds, not hardware measurements. Coarse paths are separately capped at four translation
legs and 1 m cumulative commanded travel; yaw changes are disallowed in approach so the
retraced translation math remains valid.

The original “no feedback anywhere” wording is now superseded **only for terminal approach**.
Coarse travel, grip, approach retrace, collection travel/drop and home remain open-loop.
The system does not sense obstacles, cup contents, grip force, delivery success or collection-point occupancy.
The drop target is fixed: verify that the collection area stays clear for each cup.

## 6. Failure behavior

Before attempting pickup:

- unknown type → confirmation rejected;
- known type absent from observe image → warning and skip;
- missing coarse route, invalid settings or untaught hardware → reject before execution.

Once an approach starts:

- target disappears or appears more than once → halt;
- frame repeats, is stale, wrong resolution, or detection is malformed → halt;
- tag is clipped, too small/large or severely skewed → halt;
- error does not improve, vertical alignment fails, or a budget expires → halt;
- socket/SDK failure or operator stop → halt.

No blind search, tag substitution, fixed-position fallback, auto-regrip, replanning or
automatic recovery drive is performed. In particular, a failed approach does **not**
trigger the successful-path retrace.

The Pi sends `quit;` and disconnects on stop. The Mac requests Pi `/estop`. This is best-effort network/software safety, not a certified emergency-stop
circuit. A wedged process, broken link or failed power stage can defeat software stop.
A supervisor and accessible physical power/stop controls are required.

The frame timestamp is taken by the Pi decoder, not by a synchronized EP exposure clock.
Stale/repeated-frame rejection cannot prove the camera/decoder has zero end-to-end latency.
Real stream latency must be checked during slow-speed bring-up.

## 7. Missing inputs and physical checks

| Area | Still needed | Why |
| --- | --- | --- |
| Robot | Confirm EP with working arm/gripper and USB data connection | S1 is not the specified manipulator |
| Pi | Hostname/SSH access, OS/Python, Wi-Fi IP, USB/RNDIS interface/endpoint | Separate LAN and robot connections |
| Camera | Live stream works; fixed gimbal orientation; fresh detection latency | Image corrections must correspond to the current view |
| Tags/cups | Registered seven-group IDs, consistent tag print size/mounting and cup appendages | Same image target must mean the same grasp arrangement |
| Visual target | Target center/side in pixels; tolerances; signs; tested step speed | No metric calibration, but a graspable image target is essential |
| Arm | Observe/carry, common grab, drop poses; force and dwells | Templates are zeros and must never be treated as safe |
| Coarse paths | Relative translation legs per tag, or deliberate empty routes | Bring tags into the terminal controller's limited capture region |
| Collection route | Observe-to-collection and return legs, clearances | Drop is a fixed open-loop waypoint |
| Floor/layout | Clear paths and collection point, acceptable repeatability/drift | No obstacle or collection-occupancy sensing |
| Network | Pi URL, trusted LAN, shared node token | Robot node must be reachable/authenticated |
| Baseten | Current API key, vision slug, structured-output support/quota | Validate against the current provider deployment and your schematics |
| Safety | Physical stops, supervisor, safe initial state and one-cup test procedure | Software acknowledgement does not establish physical safety |

## 8. Bring-up order from here

### A. Prove the code without physical motion

```bash
source .venv/bin/activate
python -m pytest -q
python -m main run --bom sample_bom.json --dry-run
```

Expect three type-level cycles, visible ALIGN corrections, then drop/return ordering.
The simulator's image response is intentionally simple; it is not evidence of real dynamics.

### B. Test real ingestion with the simulated robot

Run the Pi simulator, configure its local node URL, set
Baseten key/model in the Mac backend environment, and launch the browser UI. Upload your
schematic, correct the BOM and approve. Verify nothing happens before approval.

### C. Establish the Pi/EP link and inspect the image

Install Pi requirements, confirm the EP USB endpoint, then use:

```bash
python -m actuator.main --inspect-camera --ep-host <CONFIRMED_RNDIS_IP>
```

Read-only inspection does not require taught motion poses and cannot send movement commands.
Print tags with the quiet-zone border, check IDs/pixel features, and stop with Ctrl-C.
Do not run inspection and the motion server as competing EP control sessions.

### D. Teach/verify the robot configuration under supervision

Use a known-safe local/manual hardware procedure to establish the arm poses and graspable
cup/tag arrangement. Record image targets, check directions at minimal motion, verify routes
and carry/drop clearance. Then mark the configuration taught/verified and start the Pi
HTTP node from the actual observe station.

Start with one cup. Confirm tag-loss and stop behavior before trusting a longer run.

### E. Integrate one real cup, then repeated cycles

First demonstrate one complete approved cycle. Then test multiple types and repeated
round trips. Measure misses/drift manually: the current software cannot verify delivery.
Only claim the physical demo complete after repeated successful observed runs.

## 9. Change summary

Compared with the earlier custom-arm repository:

- removed SG90/PCA9685 hardware drivers, custom arm/IK/serial paths, old palette runtime,
  tuning CLI, world-pose camera runtime and obsolete orchestration/demo layers;
- replaced the active deployment with Mac approval/controller + Pi RoboMaster HTTP;
- kept real ingestion, changed its output to the new editable type→quantity BOM;
- validate drop completion before immediately requesting return to observe;
- added local image-based terminal approach without changing top-level BOM/run commands;
- added fault latching, fresh/unique tag checks, bounded corrections, no-motion inspection,
  simulation and integration coverage;
- rewrote architecture/API/setup docs and retained upstream codegen/schema provenance.
- merged the incoming light frontend, multi-format/SCH uploads, requested-parts and
  progress displays, and rule-based schematic advice into the approval-gated architecture.

Removed tracked modules remain recoverable from Git history. No unrelated ignored user
calibration files or credentials were deleted. Secrets remain local and ignored.
