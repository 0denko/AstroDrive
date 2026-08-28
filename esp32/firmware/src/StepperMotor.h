#pragma once

#include <Arduino.h>

// Non-blocking step generator: run() emits at most one pulse per call, so the
// serial parser keeps running during a slew and a stop always lands.
class StepperMotor {
 public:
  StepperMotor(uint8_t stepPin, uint8_t directionPin, uint8_t enablePin,
               bool enableActiveLow = true);
  void begin();
  void configure(uint8_t stepPin, uint8_t directionPin, uint8_t enablePin,
                 bool enableActiveLow);
  void setEnabled(bool enabled);
  bool isEnabled() const;

  void setMaxSpeed(float stepsPerSecond);
  void setAcceleration(float stepsPerSecondSquared);

  void moveBy(int32_t steps);
  // Free-run at a signed rate in steps/s. This is how tracking is held.
  void setContinuousRate(float stepsPerSecond);
  // Decelerate to a halt, coils still energised.
  void stop();
  // Drop to zero speed without a ramp, coils still energised.
  void halt();

  void run();

  bool isMoving() const;
  float speed() const;
  int32_t position() const;
  void setPosition(int32_t position);

 private:
  enum class Mode : uint8_t { Idle, Position, Continuous };

  void updateSpeed(uint32_t now);
  void applyDirection(bool forward);
  float minimumSpeed() const;

  uint8_t stepPin_;
  uint8_t directionPin_;
  uint8_t enablePin_;
  bool enableActiveLow_;
  bool enabled_ = false;

  Mode mode_ = Mode::Idle;
  int32_t position_ = 0;
  int32_t targetPosition_ = 0;
  float continuousRate_ = 0.0f;
  float speed_ = 0.0f;
  float maxSpeed_ = 2000.0f;
  float acceleration_ = 4000.0f;

  uint32_t lastStepMicros_ = 0;
  uint32_t lastSpeedMicros_ = 0;
  bool directionForward_ = true;
  bool directionValid_ = false;
};
