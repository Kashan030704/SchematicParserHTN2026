# Camera-free HCP: JSON formats

There are two different messages: a **BOM submitted to the backend over HTTP**, and a **command sent from the HCP host to the arm node over TCP**. You normally submit only the BOM; the backend builds the commands automatically. The Pi looks up its own calibrated targets. No camera tags, encoder readings, or arbitrary network-provided joint angles are used.

## 1. BOM JSON: what to submit

Minimal file or UI textarea content:

```json
{
  "components": [
    {"component_id": "resistor:10k", "qty": 2},
    {"component_id": "ic:NE555", "qty": 1}
  ]
}
```

- `components`: ordered list of requested parts.
- `component_id`: exact, case-sensitive identifier from a slot's `components` list in `palette.yaml`.
- `qty`: positive integer; two means two complete pick/place cycles from the same slot. A feeder/operator must present another part at the same grasp target; the software cannot detect stock.

The sample maps `resistor:10k` to `slot-A`, `capacitor:0.1uF` to `slot-B`, and `ic:NE555` to `slot-C`. The example above executes A, A, C. An unknown component rejects the whole plan before any session begins; it is never silently skipped. Backend runs are limited to 200 picks.

Existing ingestion output also works without changing ingestion:

```json
{
  "components": [
    {"type": "resistor", "value": "10k", "quantity": 2, "refdes": ["R1", "R2"]}
  ]
}
```

The planner forms `component_id` as `type + ":" + value`. Use `qty` **or** `quantity`, never both. Optional `refdes` must match the quantity; supplied non-null references must be unique. No case/unit normalization is performed: `10K` and `10k` are different IDs.

For HTTP only, wrap the BOM in `{"bom": ...}`:

```sh
curl -X POST http://127.0.0.1:5002/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"bom":{"components":[{"component_id":"resistor:10k","qty":2}]}}'
```

The response is HTTP 202 with `{"id":"<HTTP run ID>","state":"queued"}`. Poll `GET /api/runs/<HTTP run ID>` for the plan, completed cycles and any error. Submission is asynchronous: a 202 does not mean validation or movement succeeded. Another active run gives 409. The outer HTTP object cannot select a driver or enable hardware.

## 2. TCP envelope: host ↔ arm node

Every message has exactly these four fields:

| Field | Meaning |
| --- | --- |
| `action` | Command name, `ack`, `done`, discovery, or `status/health` |
| `id` | Unique command correlation ID; replies echo it. Health messages use `""`. |
| `device_id` | `palette_arm` for this node; the host's discovery request uses `host`. |
| `payload` | Object containing the declared arguments, or reply data |

Wire encoding is **UTF-8 compact JSON followed by one newline (`\n`)**. Each object occupies one line; there is no array around multiple messages. Both endpoints retain partial lines and split coalesced messages. Frame limit: 1 MiB. Do not pretty-print multiline JSON on the TCP socket. Pretty-printing HTTP BOMs is fine.

The arm is a **TCP client**, not a server. It connects to the backend's listener (default port 9000). The backend sends:

```json
{"action":"REQUEST_HCP_DATA","id":"d1","device_id":"host","payload":{}}
```

The node responds with the same action and ID, `device_id: "palette_arm"`, and its full [node definition](../hcp_sdk/nodes/palette_arm.json) as the payload. The existing registry learns the available commands. This reuses the reference SDK generator/schema and existing framed runtime; launching `arm.hcp_node` generates and attaches the client automatically.

## 3. Session and pick commands

The backend performs this sequence, waiting for each motion's matching `done`:

```text
get_state → begin_run → pick_place(unit 1) → pick_place(unit 2) → … → end_run
                  ↳ heartbeat runs concurrently until end_run completes
```

`get_state` returns readiness, `hardware` boolean, a `palette_id` fingerprint, commanded-angle estimates, heartbeat lease and a conservative per-command timeout. The backend verifies the node mode and compares the fingerprint with its own loaded palette. Physical mode must be explicitly enabled on both processes and armed locally; no network command enables or re-arms it.

Command arguments:

| Action | Required payload |
| --- | --- |
| `get_state` | `{}` |
| `begin_run` | `run_id` string, `palette_id` string, `hardware` boolean |
| `pick_place` | `run_id` string, `unit` integer, `component_id` string, `slot_id` string |
| `heartbeat` | `run_id` string |
| `end_run` | `run_id` string |
| `abort_run` | `run_id` string |

Example beginning a simulated run (replace the fingerprint placeholder with the matching value from `get_state`; the backend handles this for you):

```json
{"action":"begin_run","id":"c1","device_id":"palette_arm","payload":{"run_id":"run-1","palette_id":"<matching palette fingerprint>","hardware":false}}
```

Example one pick:

```json
{"action":"pick_place","id":"c2","device_id":"palette_arm","payload":{"run_id":"run-1","unit":1,"component_id":"resistor:10k","slot_id":"slot-A"}}
```

Here `id: "c2"` identifies **one command**, `run_id: "run-1"` identifies **the whole arm session**, and `unit: 1` identifies **the first expanded pick**. Unit numbers increase without gaps. A fresh command ID cannot repeat an already completed unit. The HTTP run ID and arm session ID are separate; the latter appears as `session_id` in HTTP run status.

The node verifies that the component actually belongs to the requested local slot, then performs home → open → approach → grasp → close/settle → lift → place approach → drop → open/settle → home. Targets and pulse/slew limits come from the Pi's local palette/config, not the message. Every joint and the gripper use the same validated slew/settle driver.

Receipt acknowledgement:

```json
{"action":"ack","id":"c2","device_id":"palette_arm","payload":{}}
```

`ack` means **received**, not accepted or completed. The generated client validates the command arguments and runs the handler on a separate worker. Success produces a `done` with `payload.status: "ok"` and `payload.result`: current node state plus the matching `unit`, `component_id`, and `slot_id`. State includes `hardware`, `palette_id`, `commanded_angles`, `motion_steps` and `physical_delivery_verified: false`. A failure instead looks like:

```json
{"action":"done","id":"c2","device_id":"palette_arm","payload":{"status":"error","error":"Component does not belong to the requested slot"}}
```

In either case `id` matches the original command. **The backend never advances on `ack`.** With SG90s, a successful `done` is only completed PWM commands plus timed settling; it does not prove arrival, a successful grip, or tray contents.

While a pick is moving, the backend sends separate heartbeats (each with a fresh command ID):

```json
{"action":"heartbeat","id":"h1","device_id":"palette_arm","payload":{"run_id":"run-1"}}
```

The default lease is five seconds; the backend renews at least every half second during normal operation. Heartbeat/state/abort handlers do not queue behind the motion worker. Periodic `status/health` messages contain the same readiness and target-estimate fields and are independent of command completion.

Normal completion:

```json
{"action":"end_run","id":"c3","device_id":"palette_arm","payload":{"run_id":"run-1"}}
```

This homes, disables PWM and closes the driver. Simulation can start a fresh driver next time; physical mode returns to `disarmed` and needs local `ARM`/`START` again. Arming without beginning a run expires after 30 seconds.

Emergency cancellation of that session:

```json
{"action":"abort_run","id":"c4","device_id":"palette_arm","payload":{"run_id":"run-1"}}
```

Abort, heartbeat expiry and link loss cancel the ramp and attempt PWM disable **without an additional homing trajectory**. An abort for another/nonexistent session is refused. Commands are not automatically retried after timeout or disconnect because the physical outcome is uncertain. Reconnecting does not replay commands or re-arm. I2C errors fail the command, and failed output-disable latches a node fault requiring inspection/restart.

## Safety and current verification

The TCP link and Flask UI are demo interfaces with **no authentication or TLS**. Defaults are loopback; use only an isolated trusted network if splitting machines. A palette fingerprint detects mismatched targets, not malicious nodes, and does not validate physical calibration or wiring. The Pi owns pulse calibration, joint limits, safe startup assumptions and the actual arm driver.

Software watchdogs are best effort: a dead/hung process or I2C bus can leave the PCA9685 generating its last PWM. Provide a physical power/output cutoff and support the arm before disabling holding force. Review all swept paths and replenish repeated slot picks. Test an empty, unloaded arm before attempting components.

Automated checks exercise real loopback TCP/HTTP, generated discovery, five simulated picks, ACK-vs-DONE, independent heartbeats, palette/mode mismatch rejection, cancellation, reconnect/disarm, and fake-I2C failures. **Physical motion, grip reliability and actual electronic components have not been verified by these tests.**

```sh
python -m pytest -q tests/test_palette_hcp.py tests/test_transport.py tests/test_cli.py
```
