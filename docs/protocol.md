# P0 interfaces

Every HCP message contains exactly `action`, `id`, `device_id`, and object `payload`, followed by newline. Commands/discovery/lifecycle require nonempty IDs; publications/status may use an empty ID. Malformed frames close the connection. Reconnection starts fresh discovery and never replays physical commands.

| Direction | Action | Payload / behavior |
| --- | --- | --- |
| Host → node | `REQUEST_HCP_DATA` | `{}` with fresh ID |
| Node → host | `REQUEST_HCP_DATA` | Full definition, same ID, node identity |
| Host → node | Declared command | Typed parameters; device_id identifies target |
| Node → host | `ack` | `{}`; receipt only |
| Node → host | `done` | `{"status":"ok","result":...}` or `{"status":"error","error":"..."}` |
| Camera → host → consumers | `publish` | `{"topic":"context/tags","tags":[{"id":3,"x":0.12,"y":-0.05,"theta":1.57}]}` |
| Ingestion → host context | `publish` | `{"topic":"context/bom","bom":{"components":[...]}}` |
| Node → host | `status/health` | State and diagnostics |

Identity is tied to the registered connection; concurrent duplicate device IDs are rejected. Replies match pending ID and connection. Nodes suppress duplicate command IDs in a bounded session cache; this does not guarantee exactly-once execution across physical/network failures. Uncertain completion ends the run.

`HCPHost.command(device_id, action, payload, timeout)` returns a Future carrying `command_id`; it resolves with the done payload. Registry/context snapshots are copies. In-process context publication/subscriptions connect integrated modules; camera snapshots also reach arm nodes over HCP. Maximum pose age defaults to one second.

## Arduino serial

ASCII, one command per newline. One Python reader, one write lock and one sequence-to-response map are shared by both actuator nodes.

| Line | Meaning |
| --- | --- |
| `C,id,neutral_us,forward_us,max_belt_ms` | Configure belt and enable neutral PWM |
| `J,id,base_deg,shoulder_deg,elbow_deg,gripper_deg,move_ms,settle_ms` | Move all joints, then settle |
| `B,id,duration_ms` | Timed belt advance |
| `S,id` | Stop belt; active advance returns an error |
| `X,id` | Stop belt and cancel joint interpolation while holding targets |
| `H` | Heartbeat |
| `DONE,id` / `ERR,id,reason` | Completion / error |

Firmware rejects overlapping arm/belt motion, malformed numeric commands, oversized lines, and motion before configuration. Local timers and watchdog run without waiting for the LLM. HCP done follows firmware completion, including settling and retreat; it provides no encoder/grip feedback.

## BOM and inventory

The supplied schema is unchanged. Additional checks require positive quantities, nonempty type/value, distinct refdes, and quantity matching refdes count. Matching uses exact strings. Each component/tag has one bin mapping; repeated units reuse it. There is no unit normalization, substitution, stock count, or depletion sensing.

The ledger tracks refdes. Unmatched inventory is warned/skipped and ends incomplete; hardware/model failure ends failed with completed deliveries retained. Complete requires all requested units to finish pick/place/conveyor.
