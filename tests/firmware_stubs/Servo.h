#pragma once
class Servo {
 public:
  int angle = -1, pulse = -1, pin = -1;
  void attach(int value) { pin = value; }
  void write(int value) { angle = value; }
  void writeMicroseconds(int value) { pulse = value; }
};
