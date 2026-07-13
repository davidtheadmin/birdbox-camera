# birdcam web UI

Web UI for the birdbox camera Pi (see `birdcam-ui-briefing.md` for the full
brief). One Flask process owns everything: the sensor/LED loop, the camera
pipeline, and the web server — required because lgpio locks GPIO pins
per-process.

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

Mock mode also kicks in automatically when `RPi.GPIO` / `rpicam-vid`
aren't present.

## Layout

| File | Role |
|---|---|
| `app.py` | Flask app + API routes, the single entrypoint |
| `hardware.py` | GPIO layer (LED PWM on 17, LDR on 18) + mock |
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
- `birdcam_lights.py` is superseded by the app and must NOT be run while
  the app is running (GPIO single-owner). Delete it once the app is
  verified on the Pi.
- Wrap in systemd later: `ExecStart=/usr/bin/python3 /home/birdcam/birdcam/app.py`.
