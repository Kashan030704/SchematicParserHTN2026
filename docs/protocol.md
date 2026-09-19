# HCP HTTP contract — RoboMaster + conveyor

This is the current actuator contract. The earlier NDJSON discovery/ack/done code is retained
only as a regression-tested reference library; no generated TCP clients are deployed here.

## Payloads

BOM, proposed by Baseten then editable by the human:

```json
{"R3":2,"C1":1,"LED_RX":3}
```

Counts are parts requested for display. Execute once per unique key, not per count.
No silently skipped unknown mappings: the confirm gate rejects types absent from tag_map.yaml.
A known type missing from the current image is reported and skipped.

Pi GET /detect:

```json
{"ts":1750000000,"rollup":{"R3":{"count":2,"present":true},"LED_RX":{"count":1,"present":true}}}
```

Only detected tags appear. count is visible tags/cups, NOT inventory inside the cup.
This GET never moves the robot. It requires the observe state and exclusive access;
a request during motion returns 409, away from observe returns 412.
A camera failure is an error, not an empty successful detection.

Detection also includes `frame` and `tags` for the executor/diagnostic preview:

```json
{"frame":{"seq":42,"width":640,"height":360,"captured_monotonic":12345.0},
 "tags":[{"id":0,"center_px":[320,180],"side_px":80,
          "corners_px":[[280,140],[360,140],[360,220],[280,220]]}]}
```

These fields accompany, not replace, `ts` and `rollup`. `captured_monotonic` is the
Pi decoder's local timestamp; never compare it to the Mac's clock. Pixel features
are not metric poses. The HTTP preview remains read-only; the Pi executor takes
fresh observations between its bounded terminal-approach movements internally.

## Execution sequence

The Mac freezes an approved BOM, generates a fresh run_id, and performs:

1. Pi POST /begin {"run_id":"r1","types":["R3","C1"]} -> {"ok":true,...}. No movement.
2. For each approved type:
   - Pi GET /detect. If absent, report missing and continue with the next type.
   - Pi POST /grasp_place {"run_id":"r1","type":"R3"}. Wait for:
     {"ok":true,"type":"R3","steps":[{"op":"grasp","ok":true},{"op":"drive","ok":true},{"op":"drop","ok":true}],...}
   - Arduino POST /advance {"seconds":3}. Wait for {"ok":true} AFTER motor OFF.
   - Pi POST /return {"run_id":"r1","type":"R3"}. Wait for return + observe step acknowledgements.
3. Pi POST /end {"run_id":"r1"} -> {"ok":true}.

The Pi never contacts the Arduino. /grasp_place returns at the conveyor, not at home.
The Mac advances the belt only after the full successful drop log, even in simulation.
The `grasp` operation internally performs coarse translation, fresh-image terminal
alignment, scripted grip, then retraces successful approach translations. Top-level
plan operations and request bodies are unchanged. The result additionally includes
`approach` with the selected tag ID, alignment trace, frame count and commanded travel.
run_id is the additional authorization/correlation field needed to enforce approval,
exclude overlapping runs and reject repeat attempts. It is not a physical verification.

## Errors and stops

- All node operations return JSON; non-2xx, malformed JSON/log, timeout or explicit failure halts.
- Nodes must not accept overlapping motion/advance. Detection and motion are mutually exclusive.
- Motion IDs are never replayed or automatically retried.
- On failure or operator stop: Pi POST /estop {}; Arduino POST /stop {}, dispatched concurrently.
- Stop is latched until local inspection/restart. A new approval does not clear it.
- Pi quits its SDK connection instead of making uncertain recovery/home movements.
- A transport error may mean an action happened but its reply was lost. Do not assume nothing moved.
- Network/SDK acknowledgements do not verify physical arrival, pickup, drop or belt delivery.
- Safety is best effort over LAN, not a certified emergency-stop circuit.

## Mac browser API

- GET / -> UI; embeds a per-process anti-CSRF token.
- GET /status, GET /detect -> state and no-motion preview.
- GET /nodes -> read-only Pi/conveyor health, connectivity and simulation status.
- POST /proposals multipart schematic=<PDF/JPEG/PNG/BMP/TIFF> -> one real Baseten call, awaiting_confirmation.
- POST /proposals multipart schematic=<SCH> -> local legacy KiCad/EAGLE parsing, awaiting_confirmation; no model needed.
- POST /proposals {"bom":{"R3":2}} -> explicit manual proposal for API testing; same approval gate.
- POST /proposals/<id>/approve {"approved":true,"bom":{"R3":2}} -> 202, starts once.
- GET /proposals/<id> -> progress/result. States: awaiting_confirmation, running, complete,
  complete_with_missing, halted.
- POST /stop -> requests both node stops.

Every browser POST requires X-HCP-UI-Token from the page. Run history is in-memory,
bounded to 50 records; restarting the UI does not resume any run.

Proposals retain the flat `bom` contract and preserve its insertion order. Optional review
metadata includes `component_details` (original SCH type/value/quantity/refdes records),
`warnings` for unmapped labels, and `llm_schematic_suggestions`. Despite that historical
field name, these suggestions are deterministic rules, explicitly marked
`suggestions_kind: "rule_based_advisory"`; they are not another LLM call. Advice is recomputed
from the edited BOM on approval and is never executed. A source SCH record is not an approved
motion instruction. Unconfigured types block approval rather than being silently dropped.

## Security and deployment

Pi hardware mode requires Authorization: Bearer <HCP_NODE_TOKEN> (24+ characters).
The conveyor firmware must enforce the same header. Never commit the shared token or Baseten key.
Mac UI binds loopback; Pi defaults loopback too and requires explicit --bind for LAN access.
Use a trusted LAN/firewall; HTTP alone does not encrypt tokens. There is no public-internet deployment.

Arduino board, driver, pins, networking library, and firmware are pending hardware confirmation.
Only a Python HTTP conveyor simulator currently exists; see firmware/README.md.

## Image-based approach boundary

The updated executor uses camera feedback for terminal alignment only. It does not
ask the LLM to replan, change the selected type/tag, estimate a metric tag pose or
use camera intrinsic/extrinsic calibration. Coarse travel, scripted grip, approach
retrace, belt drop and return are still commanded open-loop motions.

The target is a taught pixel center/apparent size. Short lateral/forward steps
minimize horizontal/size errors; vertical alignment gates completion. Require
multiple fresh aligned frames, fixed heading, consistent tag/cup geometry and
verified control directions. Repeated/stale/lost/duplicate tags, poor geometry,
non-improvement, time/travel/step exhaustion or SDK errors halt without gripping.
No blind search, alternative tag selection or automatic recovery is included.

After a successful grip, retracing the commanded approach establishes only an
estimated return to observe; it is not localization and cannot eliminate drift.
The controller still advances the belt on a successful commanded drop, not a sensor.
