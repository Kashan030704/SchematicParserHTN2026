# Conveyor firmware — hardware selection pending

The old servo/PWM serial sketch has been deleted. It controlled different hardware.

Do not flash a guessed board/driver combination. Confirm:
- networked board (e.g. Uno R4 WiFi, Nano 33 IoT, MKR WiFi, ESP32, or shield);
- motor driver, PWM/direction/standby pins, active levels and motor supply;
- Wi-Fi credentials, IP/port and shared HCP_NODE_TOKEN.

Required firmware contract:
- GET /health -> {"node":"conveyor","simulated":false}
- POST /advance {"seconds":3} -> {"ok":true}, **after** motor OFF.
- POST /stop {} -> motor OFF immediately, latch stopped until local restart, {"ok":true}.
- Accept only finite durations 0.1..30 seconds. Invalid/overlapping requests must not start a motor.
- Boot with motor OFF; use a local millis()-based deadline to stop even if the Mac disappears.
- Continue servicing /stop while advancing: an uninterruptible delay() would defeat the stop endpoint.
- Motor OFF on Wi-Fi/client failure, request parsing error, and shutdown where supported.
- Require Authorization: Bearer <HCP_NODE_TOKEN>; bound headers/body size and parsing time.
- No automatic replay after reconnect; one advance per successful robot drop.

Motor runs on its own rated supply, never from an Arduino I/O pin or logic regulator.
Connect grounds appropriately for the selected driver. Confirm wiring against the actual
board/driver documentation before enabling outputs. Keep a physical power cutoff.

Meanwhile, `python -m actuator.conveyor_node --port 8082` supplies the HTTP simulator.
It does not drive GPIO, serial, PWM, or any hardware. Use --real-time to test interrupted advances.
