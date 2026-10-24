"""Hardware layer for the Birdbox pHAT v1.0.

- IR LEDs: constant-current driver set by hardware PWM0 on GPIO12
  (physical pin 32), driven through the kernel's sysfs PWM interface.
  Needs `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4` in
  /boot/firmware/config.txt. LED current is linear with duty cycle:
  0-100 % -> 0-94 mA.
- Light sensor: VEML7700 on I2C bus 1, address 0x10, read in lux.
  Needs I2C enabled (`sudo raspi-config nonint do_i2c 0`).

Anywhere else (or with BIRDCAM_MOCK=1), MockHardware simulates a
day/night light cycle so the UI can be developed without the board.
"""
import math
import os
import random
import time

PWM_CHIP = "/sys/class/pwm/pwmchip0"
LED_CHANNEL = 0            # PWM0 = GPIO12
PWM_PERIOD_NS = 40_000     # 25 kHz, inside the 20-50 kHz the RC filter expects

I2C_BUS = 1
VEML_ADDR = 0x10

# VEML7700 registers
_REG_CONF = 0x00
_REG_ALS = 0x04

# Config word: gain bits 12:11, integration time bits 9:6 (0000 = 100 ms),
# bit 0 = shutdown (0 = on). Two ranges, switched automatically.
_GAIN_X2 = 0b01 << 11
_GAIN_X1_8 = 0b10 << 11
# lux per count at 100 ms integration (Vishay datasheet, rev. 2024)
_RES = {_GAIN_X2: 0.0336, _GAIN_X1_8: 0.5376}
_HIGH_COUNT = 50_000   # switch to the low-gain range above this
_LOW_COUNT = 300       # switch back to high gain below this


def is_mock():
    if os.environ.get("BIRDCAM_MOCK"):
        return True
    if not os.path.isdir(PWM_CHIP):
        return True
    try:
        import smbus2  # noqa: F401
    except ImportError:
        return True
    return False


def create_hardware():
    return MockHardware() if is_mock() else RealHardware()


def _write(path, value, retries=20):
    # Right after export, udev needs a moment to fix the file permissions.
    for i in range(retries):
        try:
            with open(path, "w") as f:
                f.write(str(value))
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(0.05)


class RealHardware:
    mock = False

    def __init__(self):
        from smbus2 import SMBus

        self._pwm = f"{PWM_CHIP}/pwm{LED_CHANNEL}"
        if not os.path.isdir(self._pwm):
            try:
                _write(f"{PWM_CHIP}/export", LED_CHANNEL)
            except OSError:
                pass  # already exported
            for _ in range(40):
                if os.path.isdir(self._pwm):
                    break
                time.sleep(0.05)
        _write(f"{self._pwm}/enable", 0)
        _write(f"{self._pwm}/duty_cycle", 0)
        _write(f"{self._pwm}/period", PWM_PERIOD_NS)
        _write(f"{self._pwm}/enable", 1)

        self.bus = SMBus(I2C_BUS)
        self._gain = _GAIN_X2
        self._configure_sensor()

    # --- LEDs ---

    def set_led_brightness(self, percent):
        """Set LED current as 0-100 % of the 94 mA maximum."""
        percent = max(0.0, min(100.0, float(percent)))
        _write(f"{self._pwm}/duty_cycle", int(PWM_PERIOD_NS * percent / 100))

    # --- light sensor ---

    def _configure_sensor(self):
        self.bus.write_word_data(VEML_ADDR, _REG_CONF, self._gain)
        time.sleep(0.25)  # first conversion after a change is not valid

    def read_light_level(self):
        """Ambient light in lux. Higher = brighter. Blocking, ~0.1-0.4 s."""
        time.sleep(0.12)  # let one 100 ms conversion finish
        count = self.bus.read_word_data(VEML_ADDR, _REG_ALS)

        if self._gain == _GAIN_X2 and count > _HIGH_COUNT:
            self._gain = _GAIN_X1_8
            self._configure_sensor()
            count = self.bus.read_word_data(VEML_ADDR, _REG_ALS)
        elif self._gain == _GAIN_X1_8 and count < _LOW_COUNT:
            self._gain = _GAIN_X2
            self._configure_sensor()
            count = self.bus.read_word_data(VEML_ADDR, _REG_ALS)

        lux = count * _RES[self._gain]
        if lux > 1000:  # Vishay non-linearity correction for bright light
            lux = (6.0135e-13 * lux**4 - 9.3924e-9 * lux**3
                   + 8.1488e-5 * lux**2 + 1.0023 * lux)
        return round(lux, 2)

    def cleanup(self):
        try:
            _write(f"{self._pwm}/duty_cycle", 0)
            _write(f"{self._pwm}/enable", 0)
        finally:
            self.bus.close()


class MockHardware:
    """Simulates daylight in lux with a compressed day/night cycle
    (~4 min period) plus noise, so auto-dimming and the graph work in dev.
    """
    mock = True
    CYCLE_SECONDS = 240

    def __init__(self):
        self._brightness = 0.0
        self._t0 = time.monotonic()

    def set_led_brightness(self, percent):
        self._brightness = max(0.0, min(100.0, percent))

    def read_light_level(self):
        time.sleep(0.12)
        phase = (time.monotonic() - self._t0) / self.CYCLE_SECONDS * 2 * math.pi
        # log-scale swing between ~0.1 lux (night) and ~10 000 lux (day)
        log_lux = 1.5 + 2.5 * math.sin(phase) + random.gauss(0, 0.05)
        return round(10 ** log_lux, 2)

    def cleanup(self):
        self._brightness = 0.0
