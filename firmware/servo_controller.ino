// Board, pins and baud are event inputs. See pins.example.h and README.
#include <Arduino.h>
#include <Servo.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#if __has_include("pins.h")
#include "pins.h"
#endif
#if !defined(HCP_SERIAL_BAUD) || !defined(ARM_BASE_PIN) || !defined(ARM_SHOULDER_PIN) || !defined(ARM_ELBOW_PIN) || !defined(ARM_GRIPPER_PIN) || !defined(BELT_PIN)
#error "Configure HCP_SERIAL_BAUD and the five servo pins in pins.h before flashing"
#endif

Servo joints[4], belt;
const int jointPins[4] = {ARM_BASE_PIN, ARM_SHOULDER_PIN, ARM_ELBOW_PIN, ARM_GRIPPER_PIN};
float currentAngles[4], startAngles[4], targetAngles[4];
bool armAttached = false, armBusy = false, beltBusy = false, configured = false;
unsigned long armStart = 0, moveMs = 0, settleMs = 0, beltStart = 0, beltDuration = 0;
unsigned long armId = 0, beltId = 0, lastHeartbeat = 0, maximumBeltMs = 0;
int neutralUs = 0, forwardUs = 0;
char inputLine[160];
unsigned int inputLength = 0;
bool overflowed = false;

void reply(bool ok, unsigned long id, const char *reason = "") {
  Serial.print(ok ? "DONE," : "ERR,");
  Serial.print(id);
  if (!ok) { Serial.print(','); Serial.print(reason); }
  Serial.println();
}

void stopBelt(const char *reason) {
  if (configured) belt.writeMicroseconds(neutralUs);
  if (beltBusy) { reply(false, beltId, reason); beltBusy = false; }
}

void abortAll(const char *reason) {
  stopBelt(reason);
  if (armBusy) { armBusy = false; reply(false, armId, reason); }
  // Hold the last commanded joint positions; never open a held gripper on error.
}

bool number(const char *token, double &value) {
  if (!token || !*token) return false;
  char *end;
  value = strtod(token, &end);
  return *end == '\0' && isfinite(value);
}

void command(char *line) {
  if (strcmp(line, "H") == 0) { lastHeartbeat = millis(); return; }
  if (!*line || *line == ',' || line[strlen(line)-1] == ',' || strstr(line, ",,")) { reply(false, 0, "format"); return; }
  char *tokens[10];
  unsigned int count = 0;
  char *save = NULL;
  for (char *token = strtok_r(line, ",", &save); token && count < 10; token = strtok_r(NULL, ",", &save)) tokens[count++] = token;
  if (count < 2 || strlen(tokens[0]) != 1) { reply(false, 0, "format"); return; }
  double values[9];
  for (unsigned int i = 1; i < count; ++i) {
    if (!number(tokens[i], values[i-1])) { reply(false, 0, "number"); return; }
  }
  if (values[0] <= 0 || values[0] > 4294967295.0 || floor(values[0]) != values[0]) { reply(false, 0, "id"); return; }
  unsigned long id = (unsigned long)values[0];
  char action = tokens[0][0];
  if (action == 'C' && count == 5) {
    if (armBusy || beltBusy || values[1] < 500 || values[1] > 2500 || values[2] < 500 || values[2] > 2500 || values[1] == values[2] || values[3] < 1 || values[3] > 60000) { reply(false, id, "configuration"); return; }
    neutralUs = (int)values[1]; forwardUs = (int)values[2]; maximumBeltMs = (unsigned long)values[3];
    belt.writeMicroseconds(neutralUs); // Set neutral before enabling PWM.
    belt.attach(BELT_PIN);
    configured = true; lastHeartbeat = millis(); reply(true, id); return;
  }
  if (action == 'S' && count == 2) { stopBelt("cancelled"); reply(true, id); return; }
  if (action == 'X' && count == 2) { abortAll("cancelled"); reply(true, id); return; }
  if (!configured) { reply(false, id, "not_configured"); return; }
  if (action == 'J' && count == 8) {
    if (armBusy || beltBusy) { reply(false, id, "busy"); return; }
    for (int i = 0; i < 4; ++i) if (values[i+1] < 0 || values[i+1] > 180) { reply(false, id, "joint_limit"); return; }
    if (values[5] < 1 || values[5] > 30000 || values[6] < 0 || values[6] > 30000) { reply(false, id, "duration"); return; }
    for (int i = 0; i < 4; ++i) {
      targetAngles[i] = (float)values[i+1];
      if (!armAttached) {
        // Initial pose is established during supervised home/calibration.
        currentAngles[i] = targetAngles[i];
        joints[i].write((int)round(currentAngles[i]));
        joints[i].attach(jointPins[i]);
      }
      startAngles[i] = currentAngles[i];
    }
    armAttached = true; armId = id; armStart = millis();
    moveMs = (unsigned long)values[5]; settleMs = (unsigned long)values[6]; armBusy = true;
    return;
  }
  if (action == 'B' && count == 3) {
    if (armBusy || beltBusy) { reply(false, id, "busy"); return; }
    if (values[1] < 1 || values[1] > maximumBeltMs) { reply(false, id, "duration"); return; }
    beltId = id; beltStart = millis(); beltDuration = (unsigned long)values[1];
    beltBusy = true; belt.writeMicroseconds(forwardUs); return;
  }
  reply(false, id, "unknown_command");
}

void setup() { Serial.begin(HCP_SERIAL_BAUD); }

void loop() {
  // Bound serial work per tick so PWM interpolation and timers keep advancing.
  unsigned int bytes = 0;
  while (Serial.available() && bytes++ < 160) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (overflowed) reply(false, 0, "line_too_long");
      else { inputLine[inputLength] = '\0'; command(inputLine); }
      inputLength = 0; overflowed = false;
    } else if (c != '\r') {
      if (inputLength + 1 < sizeof(inputLine)) inputLine[inputLength++] = c;
      else overflowed = true;
    }
  }
  unsigned long now = millis();
  if (configured && now - lastHeartbeat > 1500) {
    abortAll("watchdog"); configured = false;
  }
  if (armBusy) {
    unsigned long elapsed = now - armStart;
    float fraction = elapsed >= moveMs ? 1.0f : (float)elapsed / moveMs;
    for (int i = 0; i < 4; ++i) {
      currentAngles[i] = startAngles[i] + (targetAngles[i] - startAngles[i]) * fraction;
      joints[i].write((int)round(currentAngles[i]));
    }
    if (elapsed >= moveMs + settleMs) { armBusy = false; reply(true, armId); }
  }
  if (beltBusy && now - beltStart >= beltDuration) {
    belt.writeMicroseconds(neutralUs); beltBusy = false; reply(true, beltId);
  }
}
