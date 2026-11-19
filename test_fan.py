"""Plain unittest for fan.py and the fan settings (python3 -m unittest test_fan)."""
import tempfile
import unittest
from pathlib import Path

from config import Config, DEFAULTS
from fan import FanController
from hardware import MockHardware


class FakeCamera:
    def __init__(self):
        self.recording = False

    def recording_state(self):
        return {"recording": self.recording}


def make(cfg=None, temp=50.0):
    config = dict(DEFAULTS)
    config.update(cfg or {})
    hw, cam = MockHardware(), FakeCamera()
    box = {"temp": temp}
    fan = FanController(hw, config, cam, read_temp=lambda: box["temp"])
    return fan, hw, cam, box


class DecideTest(unittest.TestCase):
    def test_hysteresis(self):
        fan, hw, cam, box = make()            # on 65, off 55
        for temp, expect in [(50, False), (60, False), (65, True), (60, True),
                             (56, True), (55, False), (60, False)]:
            box["temp"] = temp
            fan._tick()
            self.assertEqual(hw.aux_on, expect, f"at {temp} °C")

    def test_quiet_while_recording_until_limit(self):
        fan, hw, cam, box = make()
        box["temp"] = 70
        fan._tick()
        self.assertTrue(hw.aux_on)
        cam.recording = True
        fan._tick()
        self.assertFalse(hw.aux_on)           # 70 < 78: keep quiet
        box["temp"] = 78
        fan._tick()
        self.assertTrue(hw.aux_on)            # too hot: fan wins
        box["temp"] = 70
        fan._tick()
        self.assertFalse(hw.aux_on)
        cam.recording = False
        fan._tick()
        self.assertTrue(hw.aux_on)            # recording over, still hot

    def test_quiet_can_be_disabled(self):
        fan, hw, cam, box = make({"fan_quiet_recording": 0}, temp=70)
        cam.recording = True
        fan._tick()
        self.assertTrue(hw.aux_on)

    def test_manual_modes(self):
        fan, hw, cam, box = make({"fan_mode": "on"}, temp=20)
        fan._tick()
        self.assertTrue(hw.aux_on)
        fan.config["fan_mode"] = "off"
        box["temp"] = 84
        fan._tick()
        self.assertFalse(hw.aux_on)

    def test_no_temperature_means_off(self):
        fan, hw, cam, box = make(temp=None)
        fan._tick()
        self.assertFalse(hw.aux_on)
        self.assertEqual(fan.state()["reason"], "no temperature reading")

    def test_stop_switches_off(self):
        fan, hw, cam, box = make(temp=80)
        fan._tick()
        self.assertTrue(hw.aux_on)
        fan.stop()          # also safe when the thread never started
        self.assertFalse(hw.aux_on)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Config(Path(self.dir.name) / "config.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_order_enforced(self):
        applied, errors = self.cfg.update({"fan_off_c": 70})   # above on (65)
        self.assertIn("fan_off_c", errors)
        self.assertEqual(self.cfg["fan_off_c"], 55.0)
        applied, errors = self.cfg.update({"fan_on_c": 80})    # above max (78)
        self.assertIn("fan_on_c", errors)
        applied, errors = self.cfg.update({"fan_on_c": 70, "fan_off_c": 60, "fan_max_c": 80})
        self.assertFalse(errors)
        self.assertEqual((self.cfg["fan_off_c"], self.cfg["fan_on_c"]), (60.0, 70.0))

    def test_mode_choices(self):
        applied, errors = self.cfg.update({"fan_mode": "turbo"})
        self.assertIn("fan_mode", errors)


if __name__ == "__main__":
    unittest.main()
