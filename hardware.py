"""Hardware layer: GPIO access for the LED array and LDR light sensor.

Exactly one process may own GPIO 17/18 (lgpio locks pins per process), so
this module is only ever instantiated once, by the web app.

On the Pi, RealHardware drives the LEDs via software PWM and reads the LDR
with the RC charge-time method. Anywhere else (or with BIRDCAM_MOCK=1),
MockHardware simulates a day/night light cycle so the UI can be developed
without GPIO present.
"""
import math
import os
import random
import time

LDR_PIN = 18   # BCM; physical pin 12. LDR + 10uF cap, RC charge-time method.
IR_PIN = 17    # BCM; physical pin 11. NPN low-side switch for 6-LED array.
PWM_FREQ = 1000  # Hz

LDR_TIMEOUT_COUNT = 100000  # bail-out for the charge-count loop


def is_mock():
    if os.environ.get("BIRDCAM_MOCK"):
        return True
    try:
        import RPi.GPIO  # noqa: F401
        return False
    except ImportError:
        return True


def create_hardware():
    return MockHardware() if is_mock() else RealHardware()


class RealHardware:
    mock = False

    def __init__(self):
        import RPi.GPIO as GPIO
        self.GPIO = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(IR_PIN, GPIO.OUT)
        self.pwm = GPIO.PWM(IR_PIN, PWM_FREQ)
        self.pwm.start(0)

    def set_led_brightness(self, percent):
        """Set LED duty cycle, 0-100."""
        self.pwm.ChangeDutyCycle(max(0.0, min(100.0, percent)))

    def read_light_level(self):
        """RC charge-time LDR read. Blocking, ~0.3s + count time.

        Higher = darker. Typical: ~20-800 in light, up to ~6000 in darkness.
        """
        GPIO = self.GPIO
        count = 0
        GPIO.setup(LDR_PIN, GPIO.OUT)
        GPIO.output(LDR_PIN, GPIO.LOW)
        time.sleep(0.3)  # drain the capacitor
        GPIO.setup(LDR_PIN, GPIO.IN)
        while GPIO.input(LDR_PIN) == GPIO.LOW and count < LDR_TIMEOUT_COUNT:
            count += 1
        return count

    def cleanup(self):
        try:
            self.pwm.stop()
        finally:
            self.GPIO.cleanup()


class MockHardware:
    """Simulates the LDR with a compressed day/night cycle (~4 min period)
    plus noise, so auto-dimming and the history graph are exercised in dev.
    """
    mock = True
    CYCLE_SECONDS = 240

    def __init__(self):
        self._brightness = 0.0
        self._t0 = time.monotonic()

    def set_led_brightness(self, percent):
        self._brightness = max(0.0, min(100.0, percent))

    def read_light_level(self):
        time.sleep(0.3)  # mimic the capacitor-drain delay
        phase = (time.monotonic() - self._t0) / self.CYCLE_SECONDS * 2 * math.pi
        # swing between ~100 (bright) and ~5100 (dark)
        base = 2600 + 2500 * math.sin(phase)
        return max(0, int(base + random.gauss(0, 80)))

    def cleanup(self):
        self._brightness = 0.0
