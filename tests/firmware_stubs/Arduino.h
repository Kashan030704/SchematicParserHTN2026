#pragma once
#include <string>
#include <sstream>
inline unsigned long fakeClock = 0;
inline unsigned long millis() { return fakeClock; }
class FakeSerial {
 public:
  std::string input, output;
  void begin(unsigned long) {}
  int available() { return (int)input.size(); }
  int read() { char c = input.front(); input.erase(0, 1); return c; }
  template <typename T> void print(T value) { std::ostringstream s; s << value; output += s.str(); }
  void println() { output += "\n"; }
};
inline FakeSerial Serial;
