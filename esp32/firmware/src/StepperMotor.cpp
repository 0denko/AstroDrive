#include "StepperMotor.h"

StepperMotor::StepperMotor(uint8_t stepPin, uint8_t directionPin,
                           uint8_t enablePin, bool enableActiveLow)
    : stepPin_(stepPin), directionPin_(directionPin), enablePin_(enablePin),
      enableActiveLow_(enableActiveLow) {}

void StepperMotor::begin() {
  pinMode(stepPin_, OUTPUT);
  pinMode(directionPin_, OUTPUT);
  pinMode(enablePin_, OUTPUT);
  digitalWrite(stepPin_, LOW);
  setEnabled(false);
}

void StepperMotor::configure(uint8_t stepPin, uint8_t directionPin,
                             uint8_t enablePin, bool enableActiveLow) {
  stepPin_ = stepPin;
  directionPin_ = directionPin;
  enablePin_ = enablePin;
  enableActiveLow_ = enableActiveLow;
  begin();
}

void StepperMotor::setEnabled(bool enabled) {
  enabled_ = enabled;
  digitalWrite(enablePin_, (enableActiveLow_ ? !enabled : enabled) ? HIGH : LOW);
}

void StepperMotor::step(bool forward, uint32_t pulseMicros) {
  if (!enabled_) return;
  digitalWrite(directionPin_, forward ? HIGH : LOW);
  digitalWrite(stepPin_, HIGH);
  delayMicroseconds(pulseMicros);
  digitalWrite(stepPin_, LOW);
  delayMicroseconds(pulseMicros);
}

bool StepperMotor::isEnabled() const { return enabled_; }