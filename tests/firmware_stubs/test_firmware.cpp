// These pin/baud values exist only in the host-side test harness.
#define HCP_SERIAL_BAUD 115200
#define ARM_BASE_PIN 2
#define ARM_SHOULDER_PIN 3
#define ARM_ELBOW_PIN 4
#define ARM_GRIPPER_PIN 5
#define BELT_PIN 6
#include "../../firmware/servo_controller.ino"
#include <cassert>

void send(const char *line) { Serial.input = std::string(line) + "\n"; loop(); }
bool contains(const char *text) { return Serial.output.find(text) != std::string::npos; }

int main() {
  setup();
  assert(belt.pin == -1 && joints[0].pin == -1);
  send("B,1,2000"); assert(contains("ERR,1,not_configured"));
  send("C,2,1500,1700,10000"); assert(contains("DONE,2"));
  send("B,3,2000"); assert(beltBusy && belt.pulse == 1700);
  fakeClock = 1000; send("H"); assert(beltBusy && !contains("DONE,3"));
  fakeClock = 1999; send("H"); assert(beltBusy);
  fakeClock = 2000; loop(); assert(!beltBusy && belt.pulse == 1500 && contains("DONE,3"));
  send("B,4,2000"); send("S,5");
  assert(!beltBusy && belt.pulse == 1500 && contains("ERR,4,cancelled") && contains("DONE,5"));
  send("J,6,90,60,100,30,100,50"); assert(armBusy);
  fakeClock = 2149; loop(); assert(armBusy && !contains("DONE,6"));
  fakeClock = 2150; loop(); assert(!armBusy && contains("DONE,6"));
  send("J,7,90,60,100,30,100,0"); send("B,8,100"); assert(contains("ERR,8,busy"));
  send("X,9"); assert(!armBusy && contains("ERR,7,cancelled"));
  send("J,10,190,60,100,30,100,0"); assert(contains("ERR,10,joint_limit"));
  send("B,11,2000"); fakeClock = 4000; loop();
  assert(!beltBusy && !configured && belt.pulse == 1500 && contains("ERR,11,watchdog"));
  send("B,12,20"); assert(contains("ERR,12,not_configured"));
  send("J,13,,90,60,100,30,100,0"); assert(contains("ERR,0,format"));
}
