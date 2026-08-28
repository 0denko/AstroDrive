#include "StepperMotor.h"

#include <math.h>

namespace {
constexpr uint32_t kSpeedUpdateIntervalMicros = 1000;
constexpr uint32_t kSpeedUpdateCeilingMicros = 50000;
constexpr uint32_t kStepPulseMicros = 3;
// TMC2208 needs DIR settled before the rising edge; only paid on reversal.
constexpr uint32_t kDirectionSetupMicros = 5;
constexpr float kMinSpeed = 1.0f;
}  // namespace

StepperMotor::StepperMotor(uint8_t stepPin, uint8_t directionPin,
                           uint8_t enablePin, bool enableActiveLow)
    : stepPin_(stepPin), directionPin_(directionPin), enablePin_(enablePin),
      enableActiveLow_(enableActiveLow) {}

void StepperMotor::begin() {
  pinMode(stepPin_, OUTPUT);
  pinMode(directionPin_, OUTPUT);
  pinMode(enablePin_, OUTPUT);
  digitalWrite(stepPin_, LOW);
  directionValid_ = false;
  lastStepMicros_ = micros();
  lastSpeedMicros_ = lastStepMicros_;
  setEnabled(false);
}

void StepperMotor::configure(uint8_t stepPin, uint8_t directionPin,
                             uint8_t enablePin, bool enableActiveLow) {
  halt();
  stepPin_ = stepPin;
  directionPin_ = directionPin;
  enablePin_ = enablePin;
  enableActiveLow_ = enableActiveLow;
  begin();
}

void StepperMotor::setEnabled(bool enabled) {
  if (!enabled) halt();
  enabled_ = enabled;
  digitalWrite(enablePin_, (enableActiveLow_ ? !enabled : enabled) ? HIGH : LOW);
}

bool StepperMotor::isEnabled() const { return enabled_; }

void StepperMotor::setMaxSpeed(float stepsPerSecond) {
  maxSpeed_ = stepsPerSecond > kMinSpeed ? stepsPerSecond : kMinSpeed;
}

void StepperMotor::setAcceleration(float stepsPerSecondSquared) {
  acceleration_ = stepsPerSecondSquared > 1.0f ? stepsPerSecondSquared : 1.0f;
}

void StepperMotor::moveBy(int32_t steps) {
  targetPosition_ = position_ + steps;
  continuousRate_ = 0.0f;
  mode_ = Mode::Position;
  lastSpeedMicros_ = micros();
  lastStepMicros_ = lastSpeedMicros_;
}

void StepperMotor::setContinuousRate(float stepsPerSecond) {
  if (stepsPerSecond > maxSpeed_) stepsPerSecond = maxSpeed_;
  if (stepsPerSecond < -maxSpeed_) stepsPerSecond = -maxSpeed_;
  continuousRate_ = stepsPerSecond;
  mode_ = Mode::Continuous;
  lastSpeedMicros_ = micros();
}

void StepperMotor::stop() {
  if (mode_ == Mode::Continuous) {
    continuousRate_ = 0.0f;
    return;
  }
  // Retarget to wherever the current ramp can actually bring us to rest.
  const int32_t braking =
      static_cast<int32_t>((speed_ * speed_) / (2.0f * acceleration_));
  targetPosition_ = position_ + (speed_ >= 0.0f ? braking : -braking);
  mode_ = Mode::Position;
}

void StepperMotor::halt() {
  speed_ = 0.0f;
  continuousRate_ = 0.0f;
  targetPosition_ = position_;
  mode_ = Mode::Idle;
}

void StepperMotor::updateSpeed(uint32_t now) {
  uint32_t elapsed = now - lastSpeedMicros_;
  if (elapsed < kSpeedUpdateIntervalMicros) return;
  lastSpeedMicros_ = now;
  if (elapsed > kSpeedUpdateCeilingMicros) elapsed = kSpeedUpdateCeilingMicros;

  float desired = 0.0f;
  if (mode_ == Mode::Continuous) {
    desired = continuousRate_;
  } else if (mode_ == Mode::Position) {
    const int32_t remaining = targetPosition_ - position_;
    if (remaining != 0) {
      const float distance = fabsf(static_cast<float>(remaining));
      float envelope = sqrtf(2.0f * acceleration_ * distance);
      if (envelope > maxSpeed_) envelope = maxSpeed_;
      desired = remaining > 0 ? envelope : -envelope;
    }
  }

  const float maxDelta = acceleration_ * (elapsed / 1000000.0f);
  const float delta = desired - speed_;
  if (delta > maxDelta) {
    speed_ += maxDelta;
  } else if (delta < -maxDelta) {
    speed_ -= maxDelta;
  } else {
    speed_ = desired;
  }

  if (mode_ == Mode::Position && targetPosition_ == position_ &&
      fabsf(speed_) < kMinSpeed) {
    speed_ = 0.0f;
    mode_ = Mode::Idle;
  }
}

void StepperMotor::applyDirection(bool forward) {
  if (directionValid_ && directionForward_ == forward) return;
  digitalWrite(directionPin_, forward ? HIGH : LOW);
  directionForward_ = forward;
  directionValid_ = true;
  delayMicroseconds(kDirectionSetupMicros);
}

void StepperMotor::run() {
  if (!enabled_) return;

  const uint32_t now = micros();
  updateSpeed(now);

  const float magnitude = fabsf(speed_);
  if (magnitude < kMinSpeed) {
    lastStepMicros_ = now;
    return;
  }

  const uint32_t interval = static_cast<uint32_t>(1000000.0f / magnitude + 0.5f);
  if (now - lastStepMicros_ < interval) return;
  // advance by one whole interval, or however late the loop was gets added to every step period
  lastStepMicros_ += interval;
  // more than a full interval behind means a real stall, so resync rather than sprint to catch up
  if (now - lastStepMicros_ > interval) lastStepMicros_ = now;

  const bool forward = speed_ > 0.0f;
  applyDirection(forward);
  digitalWrite(stepPin_, HIGH);
  delayMicroseconds(kStepPulseMicros);
  digitalWrite(stepPin_, LOW);
  position_ += forward ? 1 : -1;
}

bool StepperMotor::isMoving() const {
  return enabled_ && fabsf(speed_) >= kMinSpeed;
}

float StepperMotor::speed() const { return speed_; }

int32_t StepperMotor::position() const { return position_; }

void StepperMotor::setPosition(int32_t position) {
  targetPosition_ += position - position_;
  position_ = position;
}
