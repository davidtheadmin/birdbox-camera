# birdcam web UI

Web UI for the birdbox camera Pi (see `birdcam-ui-briefing.md` for the full
brief). One Flask process owns everything: the sensor/LED loop, the camera
pipeline, and the web server — required because lgpio locks GPIO pins
per-process.

## Pi setup (Raspberry Pi OS Lite, Pi 3A+ with Birdbox pHAT)

```sh
sudo apt install -y git python3-flask python3-smbus2 i2c-tools
sudo raspi-config nonint do_i2c 0
echo 'dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Checks: `rpicam-hello --list-cameras` lists `imx708_wide_noir`,
`i2cdetect -y 1` shows `10`, `ls /sys/class/pwm` shows `pwmchip0`.

## Run

On the Pi:

```sh
python3 app.py
# → http://birdcam.local:8080
```

On the dev laptop (no GPIO/camera — simulated sensor + synthetic video):

```sh
pip install -r requirements.txt pillow
BIRDCAM_MOCK=1 python app.py        # bash
$env:BIRDCAM_MOCK=1; python app.py  # powershell
```

Mock mode also kicks in automatically when the PWM chip, `smbus2` or
`rpicam-vid` aren't present.

## Layout

| File | Role |
|---|---|
| `app.py` | Flask app + API routes, the single entrypoint |
| `hardware.py` | Birdbox pHAT: IR LED current via hardware PWM0 (GPIO12), VEML7700 light sensor in lux (I2C 0x10) + mock |
| `ledcontrol.py` | background sensor→LED loop (absorbed from `birdcam_lights.py`), auto/manual, history |
| `camera.py` | single rpicam-vid MJPEG pipeline: live stream, snapshots, recordings + mock camera |
| `status.py` | uptime / CPU temp / wifi / disk from /proc and /sys |
| `config.py` | defaults + `config.json` persistence, edited from the settings UI |

Captures land in `captures/` (gitignored). Recordings are raw `.mjpeg`
(concatenated JPEG frames) with a `.json` sidecar; the gallery plays them
back by re-streaming, VLC plays the files directly, and
`ffmpeg -framerate <fps> -i rec.mjpeg -c copy rec.avi` remuxes if needed.

## Notes

- Tuning constants (LED thresholds, stream size/fps/quality) live in the
  Settings panel and persist to `config.json`.
- Run at boot with systemd:
  `sudo cp birdcam.service /etc/systemd/system/ && sudo systemctl enable --now birdcam`.
