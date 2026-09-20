# Schematic to Fetch HTTP contract — RoboMaster

This is the current actuator contract. The earlier NDJSON discovery/ack/done code is retained
only as a regression-tested reference library; no generated TCP clients are deployed here.

## Payloads

Grouped BOM, derived from the original extraction then editable by the human:

```json
{"resistors":2,"capacitors":1,"diodes/LED":3}
```

Counts are parts requested for display. Execute once per unique key, not per count.
No silently skipped unknown mappings: the confirm gate rejects types absent from tag_map.yaml.
A known type missing from the current image is reported and skipped.

Pi GET /detect:

```json
{"ts":1750000000,"rollup":{"resistors":{"count":2,"present":true},"diodes/LED":{"count":1,"present":true}}}
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

1. Pi POST /begin {"run_id":"r1","types":["resistors","capacitors"]} -> {"ok":true,...}. No movement.
2. For each approved type:
   - Pi GET /detect. If absent, report missing and continue with the next type.
   - Pi POST /grasp_place {"run_id":"r1","type":"resistors"}. Wait for:
     {"ok":true,"type":"resistors","steps":[{"op":"grasp","ok":true},{"op":"drive","ok":true},{"op":"drop","ok":true}],...}
   - Pi POST /return {"run_id":"r1","type":"resistors"}. Wait for return + observe step acknowledgements.
3. Pi POST /end {"run_id":"r1"} -> {"ok":true}.

`/grasp_place` ends at the fixed collection point. The Mac calls `/return` immediately
after validating the successful grasp/drive/drop log, including in simulation.
The `grasp` operation internally performs coarse translation, fresh-image terminal
alignment, scripted grip, then retraces successful approach translations. Top-level
plan operations and request bodies are unchanged. The result additionally includes
`approach` with the selected tag ID, alignment trace, frame count and commanded travel.
run_id is the additional authorization/correlation field needed to enforce approval,
exclude overlapping runs and reject repeat attempts. It is not a physical verification.

## Errors and stops

- All node operations return JSON; non-2xx, malformed JSON/log, timeout or explicit failure halts.
- The robot node must not accept overlapping motion. Detection and motion are mutually exclusive.
- Motion IDs are never replayed or automatically retried.
- On failure or operator stop: Pi POST /estop {}.
- Stop is latched until local inspection/restart. A new approval does not clear it.
- Pi quits its SDK connection instead of making uncertain recovery/home movements.
- A transport error may mean an action happened but its reply was lost. Do not assume nothing moved.
- Network/SDK acknowledgements do not verify physical arrival, pickup, drop or delivery.
- Safety is best effort over LAN, not a certified emergency-stop circuit.

## Mac browser API

- GET / -> UI; embeds a per-process anti-CSRF token.
- GET /status, GET /detect -> state (including registered group names, tag IDs and descriptions) and no-motion preview.
- GET /nodes -> read-only RoboMaster Pi health, connectivity and simulation status.
- POST /proposals multipart schematic=<PDF/JPEG/PNG/BMP/TIFF> -> original BOM extraction, separate vision review, then LLM grouping, awaiting_confirmation.
- POST /proposals multipart schematic=<SCH> -> local legacy KiCad/EAGLE extraction then LLM grouping, awaiting_confirmation.
- POST /proposals {"bom":{"resistors":2}} -> explicit grouped proposal without a model call; raw component labels instead trigger classification. Same approval gate.
- POST /proposals/<id>/approve {"approved":true,"bom":{"resistors":2}} -> 202, starts once.
- GET /proposals/<id> -> progress/result. States: awaiting_confirmation, running, complete,
  complete_with_missing, halted.
- POST /stop -> requests a RoboMaster stop.
- GET /commands/capabilities -> command mode, configured cup types, described permitted
  capabilities and approval requirements. This is a fixed high-level capability catalog,
  not resurrection of legacy TCP discovery.
- POST /commands {"text":"fetch resistors", "proposal_id":null} -> one validated intent, plus
  either a pending proposal, read-only result, clarification or latched stop. `proposal_id`
  is optional and selects the saved pending BOM for `current_bom`; it is not approval.

Command intents contain exactly `action`, `parts`, `question`. Actions are
`fetch`, `current_bom`, `detect`, `status`, `stop`, `clarify`. Fetch parts are
unique configured `type` / positive integer `quantity` entries. Only clarify accepts a
nonempty question. Extraneous parameters,
unknown actions/types and malformed output are rejected before dispatch. These intents
come from the interpreter; `/commands` does not accept client-supplied raw action JSON.

All proposals use `kind: "fulfillment"` and require explicit BOM approval. The
one-active-run lock and single-use approval semantics apply to all requests. Commands cannot edit/interleave
with active runs or reset a stopped session. No voice or LLM output counts as approval.

Exact stop phrases bypass model construction and command interpretation locks. Other
Baseten commands make one structured-output call, never auto-retry or fall back. Explicit
demo mode instead uses a small local grammar and is labeled in every command response.
Speech is a browser input adapter; there is no server audio-upload endpoint in this prototype.

Every browser POST requires X-HCP-UI-Token from the page. Run history is in-memory,
bounded to 50 records; restarting the UI does not resume any run.

Proposals retain the flat `bom` contract and preserve its insertion order. Optional review
metadata includes `component_details` (original SCH type/value/quantity/refdes records),
`warnings` for unmapped labels and uncertain classifications, and `llm_schematic_suggestions`. Despite that historical
field name, these suggestions are deterministic rules, explicitly marked
`suggestions_kind: "rule_based_advisory"`; they are not another LLM call. With grouping enabled,
local advice uses preserved source components rather than broad group totals and is never executed. A source SCH record is not an approved
motion instruction. Unconfigured types block approval rather than being silently dropped.

The default `tag_map.yaml` registers seven groups and the closest-group classification policy.
Proposals preserve `raw_bom` and return `component_classification`: one row per original label,
containing `component`, `quantity`, `group`, `tag_id`, `confidence` and `reason`. `group_tags`
maps group names to locally configured IDs. `bom` contains the locally summed group counts.
`tagged_bom` pairs the current quantity and full registered tag identifier under each group name:

```json
{"resistors":{"quantity":3,"tag_id":"tag36h11[0]"},"capacitors":{"quantity":2,"tag_id":"tag36h11[2]"}}
```

This field is included in proposal creation, retrieval and command responses containing a
proposal. It is derived from the current BOM on every response, including after approved edits.
Unused groups are omitted; unresolved labels have `tag_id: null`. Tags come from configuration,
never from model output. The editable/approval `bom` remains the flat name-to-quantity object.

`classification_method` is `llm` or `explicit_groups` (already-grouped input, no model call).
`classification_bom` records the initial grouped totals; edits set `classification_stale`.
The model must choose exactly one of the registered groups for every entry; unknown or forced
matches get low confidence and a reason. Medium-confidence matches also raise review warnings.
The validator rejects omissions, duplicates, invented groups, extra quantities and tag IDs.

On classification failure, `classification_error` is returned with the preserved raw BOM;
there is no retry or fabricated grouping. A human can correct the editable BOM using the
registered group names. Legacy tag maps without group definitions retain exact-label behavior.

PDF/image uploads additionally return `schematic_review` after a separate structured-output
call using the same rendered pages. It contains `summary`, `limitations`, and `findings`
(severity, components, evidence, consequence, recommendation, estimated damage cost).
Costs are illustrative parts-only CAD ranges with a required `basis`; both bounds are
null when unknown, otherwise finite, nonnegative, and ordered. `ingestion/review.py` defines
the strict schema. These fields cannot change the BOM or dispatch motion.
`schematic_review_model` identifies the model and `schematic_review_bom` records the original
requirements, while `schematic_review_grouped_bom` records the corresponding group totals.
BOM edits set `schematic_review_stale` without silently replacing the review
or making another model call. A failed review instead returns `schematic_review_error`
alongside the valid BOM and local component checks. There is no automatic retry.
SCH/manual proposals do not request an image review, but raw components still need a model
for classification. Explicit manual group JSON remains offline.

## Security and deployment

Pi hardware mode requires Authorization: Bearer <HCP_NODE_TOKEN> (24+ characters).
Never commit the shared token or Baseten key.
Mac UI binds loopback; Pi defaults loopback too and requires explicit --bind for LAN access.
Use a trusted LAN/firewall; HTTP alone does not encrypt tokens. There is no public-internet deployment.

## Image-based approach boundary

The updated executor uses camera feedback for terminal alignment only. It does not
ask the LLM to replan, change the selected type/tag, estimate a metric tag pose or
use camera intrinsic/extrinsic calibration. Coarse travel, scripted grip, approach
retrace, collection-point drop and return are still commanded open-loop motions.

The target is a taught pixel center/apparent size. Short lateral/forward steps
minimize horizontal/size errors; vertical alignment gates completion. Require
multiple fresh aligned frames, fixed heading, consistent tag/cup geometry and
verified control directions. Repeated/stale/lost/duplicate tags, poor geometry,
non-improvement, time/travel/step exhaustion or SDK errors halt without gripping.
No blind search, alternative tag selection or automatic recovery is included.

After a successful grip, retracing the commanded approach establishes only an
estimated return to observe; it is not localization and cannot eliminate drift.
The collection point has no occupancy sensor; a successful drop acknowledgement is not
independent physical-delivery verification.
