# Birdbox camera — project context

A Raspberry Pi nest-box wildlife camera: live MJPEG stream, IR illumination that
switches itself on at night, motion-triggered recording, and a single-page web UI.

## Hardware

| Part | Detail |
|---|---|
| Camera host | Raspberry Pi **Zero 2 W**, hostname `birdcam`, Raspberry Pi OS Trixie (Debian), kernel 6.18.39 aarch64, 512 MB RAM |
| Camera | **Camera Module 3 Wide NoIR** (`imx708_wide_noir`), 102° horizontal FOV |
| Optics | **10x clip-on macro lens** fixed in front of the module — required, see "Focus" |
| Illumination | 6-LED IR array driven via transistor, PWM brightness |
| Light sensor | LDR on an RC-timing circuit (charge a cap, count loop iterations until pin goes high) |
| Second Pi | **Pi 4B**, hostname `birdnetpi`, runs BirdNET-Pi; also acts as the Zero's USB host, router and power source |

The camera is **fixed** — it cannot be moved, and the subject sits at **15 cm**.
There is no glazing between lens and subject.

## Code layout

```
app.py            Flask app, all HTTP routes, wires everything together
camera.py         CameraManager: rpicam-vid MJPEG pipeline, snapshots,
                  recording, mp4 transcode worker, thumbnails
ledcontrol.py     LED auto/manual control loop + sensor history ring buffer
hardware.py       GPIO, PWM, LDR read, mock-mode fallback
motion.py         Motion detection by frame differencing (needs Pillow)
cleanup.py        Pi-side card-space cleanup loop; see "Recording retention"
config.py         DEFAULTS + EDITABLE validation, persisted to config.json
status.py         uptime / CPU temp / wifi / disk
templates/index.html, static/app.js, static/style.css
test_cleanup.py   Plain unittest for cleanup.py (no pytest dependency)
pc-archive/       PC-side archive script + systemd user timer (runs on the
                  PC, not the Pi — see "Recording retention" and its README)
```

`config.json` (git-ignored) holds the live tuned values. `captures/` (also
git-ignored) holds recordings + `.json` sidecars with duration/fps metadata.

### Key invariants

- **Only one process can own the camera.** `rpicam-still` fails with "Pipeline
  handler in use" while the app runs — `sudo systemctl stop birdcam` first.
- **rpicam flags are start-up only.** Changing flip, focus or stream settings
  calls `camera.restart_pipeline()`. The relevant keys are listed in
  `config_set()` in `app.py`; add new camera flags there too.
- The camera streams **continuously**, whether or not a browser is connected —
  motion detection and snapshots depend on that shared frame buffer.
- `BIRDCAM_MOCK=1` runs the whole app with synthetic frames and fake GPIO, for
  laptop development. Power/reboot endpoints are deliberately no-ops in mock.

## Tuned settings (as deployed)

| Setting | Value | Why |
|---|---|---|
| `flip_v` | 1 | Camera is mounted upside down (`flip_h`=0) |
| `af_mode` / `lens_position` | `manual` / **2.67** | See Focus below |
| `max_bright` | 60 | IR LED duty cap |
| `sensor_interval` | 60 | LDR read is slow and blocking; 2 s was wasteful |
| `level_min` / `level_max` | 5000 / 100000 | Linear mode; recalibrated for current scale |
| stream | 1280x720 @ 15 fps, q80 | |

## Focus — solved, don't re-litigate

The Wide NoIR **could not focus at 15 cm** on its own, despite the datasheet
claiming a 5 cm minimum. Verified thoroughly: `--lens-position` *is* applied
(metadata confirms 0.0 vs 10.0), the AF motor *does* move (AF sweeps show the
contrast metric climbing 400 → 2500), libcamera is current (0.7.2), and the
module reports correctly. Sweeping 0→15 dioptres never brought 15 cm into focus.

**Fix: a 10x macro lens in front of the module, with manual `lens_position 2.67`.**
The added optics do the close-focus work, so the module's own lens sits low in
its range. The spacing between macro lens and module is a critical dimension —
if focus drifts, check that first.

Note `libcamera` logs `No static properties available for 'imx708_wide_noir'`
on every run. Harmless, but it means dioptre→distance mapping is uncalibrated;
tune focus empirically, never from the 1/distance formula.

## Networking — read this before debugging connectivity

The Zero's Wi-Fi only works with a window open, so it runs over a **USB gadget
link to the Pi 4B**, which supplies power *and* network over **one cable**
(non-negotiable constraint — the Zero's PWR IN is unused).

```
PC 192.168.2.22 ──wifi──> 4B 192.168.2.11 ──USB──> Zero 192.168.7.2
                             (192.168.7.1)          (wifi 192.168.2.12, unreliable)
```

- Web UI: **http://192.168.2.11:8080** (4B forwards 8080 → `192.168.7.2:8080`)
- SSH: **`ssh -p 2222 birdcam@192.168.2.11`** (forwards → `192.168.7.2:22`)
- The `192.168.7.x` network exists only between the two Pis; the PC cannot
  reach it directly. Everything goes through the 4B.

### Pieces that make it self-heal (do not remove)

| Where | What | Why |
|---|---|---|
| Zero | `/boot/firmware/config.txt`: `dtoverlay=dwc2`; `cmdline.txt`: `modules-load=dwc2,g_ether` + fixed `g_ether.dev_addr`/`host_addr` MACs | Random MACs each boot break NM's profile matching. `cmdline.txt` must stay **one line** |
| Zero | `usb-gadget-rebind.service` | Re-binds `g_ether` 15 s after boot. **Required** — without it the UDC reports `not attached`. Disabling it broke the link |
| Zero | `/etc/NetworkManager/conf.d/10-usb0-managed.conf` | Otherwise NM leaves `usb0` `unmanaged` and it never comes up |
| 4B | `usb0-up.path` + `usb0-up.service` | Reapplies `nmcli connection up usb-host` when `usb0` reappears. A udev `RUN+=` rule does **not** work (D-Bus isn't up yet) |
| 4B | iptables + `iptables-persistent` | MASQUERADE on `wlan0` **and on `usb0`** — the second one is hairpin NAT, needed or SSH/HTTP replies never return |

## Recording retention — Pi cleanup + PC archive

The Pi's 58 GB SD card is the live view; the PC (`pebbles`) is the archive of
record. Two independent jobs cooperate through an explicit handshake so the
Pi never deletes a recording the PC hasn't archived yet:

- **Pi side** (`cleanup.py`, a daemon thread started from `app.py` alongside
  `led`/`camera`/`motion`): every `cleanup_interval` seconds, if free space on
  the `captures/` filesystem drops below `min_free_gb`, deletes recordings
  oldest-first (by capture time, sidecar `.json` included) until it's back
  above target.
- **PC side** (`pc-archive/birdcam-archive.sh`, hourly via a systemd user
  timer — see `pc-archive/README.md` to install): `rsync`-pulls
  `captures/` into `~/birdcam-archive` (**never `--delete`** — the Pi prunes
  its own files; mirroring that into the archive would destroy the archive's
  entire purpose), then prunes the local archive to a 100 GB cap, oldest
  first, never touching the last 30 days or any `.jpg`.

### The handshake

After a successful pull, the PC works out the newest recording it has fully
archived (ignoring anything touched in the last 5 minutes — it may still be
mid-write or mid-transcode on the Pi) and writes that recording's **capture
time** over SSH to `captures/.archived_through` on the Pi. `cleanup.py` will
only delete recordings at or before that marker.

**Ordering is by capture time, not mtime — this matters.** Both
`cleanup.py` and `birdcam-archive.sh` parse the true recording start time
out of the `rec_YYYYMMDD_HHMMSS` filename (as UTC, so the Pi and the PC
agree regardless of either machine's local timezone) rather than trusting
the filesystem mtime for retention *ordering* and the marker value. mtime
is still used for exactly one thing: the PC script's "was this touched in
the last 5 minutes" quiet-period check, where the actual mtime is the right
signal. The reason: manually converting a `.mjpeg` to `.mp4` (see
`camera.py`'s `convert_to_mp4`) rewrites the file's mtime to the conversion
time. Since conversion is now a manual, on-demand action rather than
immediate-after-recording, that drift can be large — an old, already-
archived recording converted today would look newer than everything else
by mtime, corrupting both the deletion order and the marker comparison.
Confirmed live against real Pi data: a same-day recording converted earlier
had a later mtime than one converted afterward despite happening first —
capture-time parsing picked the correct "newest" recording where mtime did
not.

**If the marker file is missing** (first run ever, or the PC hasn't
completed a pull yet), that's treated as "nothing archived" — normal
cleanup deletes nothing, only the emergency path below can.

**Emergency exception:** if free space falls below `emergency_free_gb`
regardless of the marker (e.g. the PC has been off for weeks), `cleanup.py`
deletes unarchived recordings too, oldest first, logging a clear `WARNING`
each time. This state is also surfaced in the UI — the gallery header shows
"⚠ emergency cleanup active" — not just in logs.

### Never deleted, by either side

- `.jpg` snapshots — never, regardless of space, age, or the emergency path.
- The Pi: the recording currently being written (`camera.recording_state()`)
  or mid-transcode (`camera.transcoding_names()`).
- The PC: anything from the last 30 days, even if that leaves the archive
  over its cap (logs a warning instead — a cap breach is recoverable,
  deleting last week's nesting footage is not).

### Config (Settings → Cleanup)

| Key | Default | Meaning |
|---|---|---|
| `cleanup_enabled` | `1` | Master switch |
| `min_free_gb` | `8.0` | Target free space |
| `emergency_free_gb` | `2.0` | Below this, ignore the archive marker |
| `cleanup_interval` | `300` | Seconds between checks |

`emergency_free_gb` must be less than `min_free_gb`; `config.py` rejects a
save that violates this.

### Testing

`test_cleanup.py` (plain `unittest`, no pytest dependency — run with
`python3 -m unittest test_cleanup`) exercises `cleanup.py` against a scratch
directory with staggered capture times/sizes: oldest-first, sidecars follow
their recording, `.jpg` immune even when stuck in emergency, marker
respected, emergency override, in-progress/transcoding files untouched, and
a dedicated regression test for the capture-time-vs-mtime fix above (a file
with an artificially recent mtime but an old, archived capture time must
still be deleted; a newer, unarchived file with an older-looking mtime must
still survive). Develop Pi-side changes with `BIRDCAM_MOCK=1` on the
laptop. Test `birdcam-archive.sh` with `--dry-run` against a scratch
`BIRDCAM_ARCHIVE_DIR` before ever pointing it at the real archive.

## Open issues

1. **LDR saturates.** `LDR_TIMEOUT_COUNT = 100000` in `hardware.py` is the
   ceiling. Deep twilight reads ~5000; anything darker pins at 100000, so all
   night-time darkness is indistinguishable. The reading is a raw loop count,
   not a physical unit — it shifts with CPU speed and library versions.
   *Fix:* swap the LDR capacitor smaller (10 µF → 1 µF) to cut charge time ~10x,
   or raise the timeout (costs CPU — it's a tight Python loop).
2. **IR LEDs feed back into the LDR.** Lights on → sensor reads bright → lights
   dim → reads dark → cycles. Shielding isn't possible. Mitigations added:
   `auto_mode: hysteresis` (two thresholds + `auto_dwell` minimum switch
   interval) and `sample_dark` (blank the LEDs during each read so the sensor
   measures ambient only). If coupling is strong, hysteresis alone is *not*
   enough — simulation showed only blanking is reliable. Needs field tuning.
3. **`smooth` is a response rate, not inertia.** `current += (target - current)
   * smooth` — 1.0 snaps instantly, 0.1 is slow. It was set to 0.81 (almost no
   damping), which contributed to the oscillation. Worth renaming the UI label.
4. **ffmpeg may not be installed** on the Pi. Without it, recordings stay MJPEG
   and the "to mp4" button is disabled (`sudo apt install ffmpeg`). Software
   `libx264` on a Zero 2 W is well under real-time; the hardware encoder
   (`h264_v4l2m2m`) is tried first.
5. **Persistent journald doesn't work.** `/var/log/journal` stays empty despite
   `Storage=persistent`, a valid committed machine-id, no tmpfs over `/var/log`,
   no config drop-ins, and a writable filesystem. Unresolved. Workaround: a cron
   heartbeat writing to `/home/birdcam/heartbeat.log` every minute — gaps mark
   reboots.
6. **Power.** Everything runs off one USB cable. `vcgencmd get_throttled` has
   read `0x0` through a full night with LEDs on, so it's holding, but any
   unexplained hang should check this first. Rebooting the 4B hard-cuts the
   Zero — `sudo shutdown -h now` on the Zero first. An unclean cut once
   truncated both netplan YAMLs to 0 bytes and killed all networking.
7. **Motion detection needs Pillow** (`python3-pil`), else the card shows
   "unavailable". Not yet enabled in production; IR-lit frames are noisier, so
   night sensitivity likely needs to be lower than day.

## Permissions

What an agent working from this doc may and may not do to the physical hardware.

**Allowed without asking each time:**
- `sudo systemctl restart|stop|start birdcam` — the normal deploy step. Check
  `/api/status` first and refuse if `recording.recording` is `true`; a restart
  kills any in-progress recording.
- Running `rpicam-still` / `rpicam-vid` directly, but only after
  `sudo systemctl stop birdcam`, and `start` it again afterward (see the
  single-owner rule below).
- Read-only inspection: `git pull`, `curl` against the app's own API,
  `journalctl`, `systemctl status`, etc.

**Never do — stop and ask first, even mid-task:**
- Reboot or shut down either Pi — `reboot`, `shutdown`, `poweroff`, or the
  app's own `/api/system/reboot` / `/api/system/shutdown` endpoints.
- Touch the **Pi 4B** (`192.168.2.11`) beyond using it as the network hop to
  the Zero. It powers *and* routes the Zero over one USB cable, so rebooting
  or shutting it down hard-cuts the Zero's power (see "Power" above). An
  unclean cut has already zeroed out both netplan YAMLs once.

**Camera single-owner rule:** only one process may hold the camera at a time.
`birdcam` owns it while running, so `rpicam-still`/`rpicam-vid` fail with
"Pipeline handler in use" unless `birdcam` is stopped first — and it must be
restarted afterward so the live stream, motion detection, and recording
resume.

## Deploy workflow

Edit on the PC → commit → push → on the Pi:

```
cd ~/birdbox-camera && git pull && sudo systemctl restart birdcam
```

Current work is on branch `feature/recording-retention` (branched off
`feature/add_flip_autofocus_onoff`); the Pi is deployed and running it as of
this writing. `pc-archive/`'s systemd user timer is installed and enabled on
the PC (`pebbles`), pulling hourly.
`config.json` and `captures/` are git-ignored, so pulls won't clobber settings.

The **power/restart buttons in the header need a sudoers entry** on the Pi or
they return an error:

```
echo 'birdcam ALL=(root) NOPASSWD: /usr/sbin/shutdown' | sudo tee /etc/sudoers.d/birdcam-power
sudo chmod 440 /etc/sudoers.d/birdcam-power
```

## Cosmetic, expected, not a bug

Daylight footage looks **pink/magenta**. That's the NoIR module with no IR-cut
filter — foliage reflects IR strongly and white balance can't compensate for a
spectrum it wasn't designed for. Irrelevant at night, which is when it matters.
