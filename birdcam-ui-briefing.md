# Birdcam Web UI — Project Briefing

## What this is

A birdbox camera running on a Raspberry Pi Zero 2 W. The Pi runs 24/7. I want a web UI served from the Pi itself so I can check in from my laptop or phone via browser (`http://birdcam.local:8080`). This repo contains the existing GPIO scripts; the web app should be built in this repo and deployed to the Pi.

## Hardware / environment

- **Board:** Raspberry Pi Zero 2 W running Ubuntu (not Raspberry Pi OS). Hostname `birdcam`, user `birdcam`, reachable at `birdcam.local` on the LAN.
- **Camera:** Raspberry Pi Camera Module 3 Wide NoIR (`imx708_wide_noir`), works with `rpicam-vid` / `rpicam-still` (rpicam-apps, libcamera v0.7).
- **IR/LED array:** 6 LEDs (currently red for testing, will become 850 nm IR), each with its own resistor, +5V common, switched low-side through an NPN transistor on **GPIO 17 (BCM) = physical pin 11**. Dimmable via software PWM at 1000 Hz (`RPi.GPIO`, lgpio backend).
- **Light sensor:** LDR + 10 µF capacitor, RC charge-time method on **GPIO 18 (BCM) = physical pin 12**. No ADC on this board. Reading takes ~0.3 s (capacitor drain) + count time. Typical values: ~20–800 in light, up to ~6000 in darkness. Higher = darker.
- **Python:** Python 3, `RPi.GPIO` installed system-wide via apt (uses lgpio underneath).

## Existing code in this repo

- `birdcam_lights.py` — the current sensor→LED loop: reads the LDR, maps the level linearly to PWM brightness (LEVEL_MIN=800 → 0%, LEVEL_MAX=4000 → MAX_BRIGHT=80%), with exponential smoothing (factor 0.3). This logic must be **absorbed into the web app**, not run alongside it (see GPIO constraint below).
- `led_dim_test.py` — throwaway manual PWM test, can be ignored/deleted.

## Critical constraint: single GPIO owner

The lgpio backend locks pins per-process ("GPIO not allocated" errors otherwise). **Exactly one process may own GPIO 17 and 18.** Therefore the web app itself must run the sensor/LED loop as a background thread and expose control over it — do NOT design a separate script plus a web server both touching GPIO.

## Features to build

### 1. Live video feed
- Embed the camera stream in the main page.
- Suggested approach: MJPEG stream endpoint (simplest, fine on LAN) or MediaMTX/WebRTC if you think it's worth it on a Pi Zero 2 W. Keep CPU headroom in mind — this board is small and also encodes video.
- The camera is currently used ad hoc via `rpicam-vid -t 0 --inline --listen -o tcp://0.0.0.0:8888`. The web app should manage the camera process itself.

### 2. Snapshot & recording
- **Snapshot button** → capture a still, save to a captures directory on the Pi.
- **Record button** → start/stop video recording (with visible recording indicator and duration).
- Note: `rpicam-still`/`rpicam-vid` can't use the camera while another process holds it — the streaming approach must allow stills/recordings (e.g. serve stream and captures from one camera pipeline, or briefly pause/restart the stream).

### 3. Gallery
- Page listing saved snapshots and recordings, newest first, with thumbnails, view/download, and delete.
- Show **free disk space** somewhere visible (SD card, recordings can fill it).

### 4. LED control
- **Auto/Manual toggle.**
  - Auto = the sensor loop controls brightness (current `birdcam_lights.py` behavior).
  - Manual = a 0–100% slider overrides it.
- Show current mode, current brightness %, and the live light-sensor reading.

### 5. Sensor readout / mini graph
- Current light level and LED brightness as live numbers.
- Nice-to-have: small rolling graph of the light level over the last hours (in-memory or lightweight persistence is fine).

### 6. Status bar
- Uptime, CPU temperature, wifi signal strength, free disk space.

## Non-goals for now (don't build yet, but don't design against them)
- Motion-triggered recording
- Daily timelapse
- Notifications

## Tech preferences
- Python web framework (Flask or FastAPI — your call, keep it simple).
- Plain HTML/JS/CSS frontend is fine; no build step preferred (this deploys to a Pi Zero via scp/git). A single-page UI is enough.
- Must run fine on 512 MB RAM alongside video encoding.
- Should be startable as a single command (`python3 app.py` or similar) — I'll wrap it in systemd later so it starts on boot.

## Workflow / deployment context
- Development happens on my Windows laptop in this repo; deployment to the Pi via `scp` or `git pull` (git on the Pi is planned but not set up yet).
- **Important:** code touching GPIO or the camera can only be tested on the Pi itself. Structure the app so the hardware layer is isolated (e.g. a `hardware.py` module) with a mock/dev mode so the web UI can be developed and previewed on the laptop without GPIO/camera present.

## Reference: the current sensor/LED logic to absorb

```python
LDR_PIN = 18
IR_PIN = 17
LEVEL_MIN = 800     # this bright or brighter -> LEDs off
LEVEL_MAX = 4000    # this dark or darker -> LEDs at MAX_BRIGHT
MAX_BRIGHT = 80     # duty-cycle cap
SMOOTH = 0.3        # exponential smoothing factor

def read_ldr():                      # RC charge-time, ~0.3s+ per read
    count = 0
    GPIO.setup(LDR_PIN, GPIO.OUT)
    GPIO.output(LDR_PIN, GPIO.LOW)
    time.sleep(0.3)
    GPIO.setup(LDR_PIN, GPIO.IN)
    while GPIO.input(LDR_PIN) == GPIO.LOW and count < 100000:
        count += 1
    return count
```

PWM: `GPIO.PWM(17, 1000)`, brightness = duty cycle 0–100. These tuning constants should be configurable (config file or UI), not hardcoded.
