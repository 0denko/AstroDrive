#pragma once

#include <Arduino.h>

class StepperMotor {
 public:
  StepperMotor(uint8_t stepPin, uint8_t directionPin, uint8_t enablePin,
               bool enableActiveLow = true);

  void begin();
  void setEnabled(bool enabled);
  void step(bool forward, uint32_t pulseMicros = 500);
  bool isEnabled() const;

 private:
  uint8_t stepPin_;
  uint8_t directionPin_;
  uint8_t enablePin_;
  bool enableActiveLow_;
  bool enabled_ = false;
};
