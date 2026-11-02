#!/usr/bin/env bash
# Birdbox camera: turn a freshly flashed Raspberry Pi OS Lite card into a
# working birdcam (Pi 3A+ with Birdbox pHAT, Camera Module 3 NoIR, VEML7700,
# I2S mic).
#
# 1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, with OS
#    customisation: hostname birdcamN, user "birdcam", your WiFi, SSH on.
# 2. Copy this file onto the card's boot partition ("bootfs" in Windows).
# 3. Boot the Pi, SSH in, and run:
#        sudo bash /boot/firmware/birdcam-setup.sh
#    It installs everything, then reboots. About 10-15 minutes on a 3A+.
# 4. After the reboot, check it:
#        sudo bash /boot/firmware/birdcam-setup.sh --check
#
# Safe to run again; every step skips what's already done.
#
# Options:
#   --check        only run the hardware/service checks
#   --branch NAME  git branch to install (default: main)
#   --no-reboot    don't reboot at the end
set -euo pipefail

REPO_URL="https://github.com/davidtheadmin/birdbox-camera.git"
BRANCH="main"
APP_USER="birdcam"
CONFIG_TXT="/boot/firmware/config.txt"
DO_REBOOT=1
CHECK_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --branch) BRANCH="$2"; shift ;;
        --no-reboot) DO_REBOOT=0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS + 1)); }
FAILS=0

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0 $*"; exit 1
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "User '$APP_USER' doesn't exist. Set it in Raspberry Pi Imager (OS customisation) and reflash."
    exit 1
fi
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
APP_DIR="$APP_HOME/birdbox-camera"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

# ---------------------------------------------------------------- checks ---

run_checks() {
    say "Checks"
    FAILS=0
    if rpicam-hello --list-cameras 2>/dev/null | grep -q imx708; then
        ok "camera found (imx708)"
    elif systemctl is-active --quiet birdcam; then
        ok "camera not listed, but birdcam is running and owns it (expected)"
    else
        bad "camera not found: check the ribbon (contacts face the board on the Pi 3A+)"
    fi
    if [ -d /sys/class/pwm/pwmchip0 ]; then ok "hardware PWM (LEDs)"; else bad "no pwmchip0: pwm-2chan overlay missing, or not rebooted yet"; fi
    # Read the ALS register the way hardware.py does; i2cdetect's quick-write
    # probe can miss the VEML7700 even when it works fine.
    if python3 -c 'from smbus2 import SMBus; b = SMBus(1); b.read_word_data(0x10, 0x04); b.close()' 2>/dev/null; then
        ok "light sensor VEML7700 at 0x10"
    else
        bad "no I2C device at 0x10: check the sensor wiring / Qwiic cable"
    fi
    if arecord -l 2>/dev/null | grep -qi googlevoi; then ok "I2S sound card (mic)"; else bad "no I2S sound card: googlevoicehat overlay missing, or not rebooted yet"; fi
    if systemctl is-enabled --quiet birdcam 2>/dev/null; then ok "birdcam service enabled"; else bad "birdcam service not enabled"; fi
    if systemctl is-active --quiet birdcam; then
        ok "birdcam running: http://$(hostname).local:8080"
        if command -v curl >/dev/null; then
            st=$(curl -s --max-time 5 http://127.0.0.1:8080/api/status || true)
            if echo "$st" | grep -q '"audio"'; then
                if echo "$st" | python3 -c 'import json,sys; a=json.load(sys.stdin)["audio"]; sys.exit(0 if a.get("available") else 1)'; then
                    ok "mic delivering audio"
                else
                    bad "app running but no audio from the mic: check the six I2S wires (L/R to GND)"
                fi
            fi
        fi
    else
        bad "birdcam not running: journalctl -u birdcam -n 50"
    fi
    echo
    if [ "$FAILS" -eq 0 ]; then echo "All good."; else echo "$FAILS problem(s) above."; fi
}

if [ "$CHECK_ONLY" -eq 1 ]; then
    run_checks
    exit 0
fi

# ----------------------------------------------------------------- setup ---

say "Waiting for the network"
for _ in $(seq 1 30); do
    if getent hosts github.com >/dev/null; then break; fi
    sleep 2
done
getent hosts github.com >/dev/null || { echo "No internet. Check the WiFi settings from Imager."; exit 1; }
ok "online"

say "Installing packages (the slow part)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl python3-flask python3-smbus2 python3-pil python3-numpy \
    ffmpeg i2c-tools alsa-utils
# Picamera2: 1080p30 H.264 recordings alongside the live view (no GUI deps)
apt-get install -y -q --no-install-recommends python3-picamera2
ok "packages"

say "Enabling I2C, PWM and I2S"
raspi-config nonint do_i2c 0
add_line() {
    grep -qxF "$1" "$CONFIG_TXT" || { echo "$1" >> "$CONFIG_TXT"; echo "  added: $1"; }
}
if ! grep -q "Birdbox pHAT" "$CONFIG_TXT"; then
    printf '\n[all]\n# Birdbox pHAT: IR LEDs (GPIO12) + AUX (GPIO13) on hardware PWM; I2S mic\n' >> "$CONFIG_TXT"
fi
add_line "dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4"
add_line "dtparam=i2s=on"
add_line "dtoverlay=googlevoicehat-soundcard"
ok "$CONFIG_TXT"

say "User groups"
groups_wanted=""
for g in video i2c gpio audio; do
    getent group "$g" >/dev/null && groups_wanted="$groups_wanted,$g"
done
usermod -aG "${groups_wanted#,}" "$APP_USER"
ok "$APP_USER in ${groups_wanted#,}"

say "Getting the birdcam software ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch -q origin
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout -q "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull -q --ff-only
    ok "updated $APP_DIR"
else
    sudo -u "$APP_USER" git clone -q -b "$BRANCH" "$REPO_URL" "$APP_DIR"
    ok "cloned to $APP_DIR"
fi
sudo -u "$APP_USER" mkdir -p "$APP_DIR/captures"

say "Service and power buttons"
sed -e "s#/home/birdcam#$APP_HOME#g" -e "s#^User=.*#User=$APP_USER#" \
    "$APP_DIR/birdcam.service" > /etc/systemd/system/birdcam.service
systemctl daemon-reload
systemctl enable -q birdcam
echo "$APP_USER ALL=(root) NOPASSWD: /usr/sbin/shutdown" > /etc/sudoers.d/birdcam-power
chmod 440 /etc/sudoers.d/birdcam-power
visudo -cq -f /etc/sudoers.d/birdcam-power
ok "birdcam.service enabled, restart/power buttons allowed"

say "Done"
echo "  After the reboot: http://$(hostname).local:8080"
echo "  Then check with:  sudo bash $0 --check"
if [ "$DO_REBOOT" -eq 1 ]; then
    echo "  Rebooting in 10 s to load the overlays (Ctrl+C to skip)..."
    sleep 10
    reboot
else
    echo "  Reboot to load the overlays: sudo reboot"
fi
