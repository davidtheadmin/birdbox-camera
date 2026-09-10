#!/usr/bin/env bash
# birdcam-archive.sh — pull recordings from the Pi into a long-term local
# archive, tell the Pi how much of it is now safely archived, and prune the
# local archive to a size cap.
#
# Run hourly via birdcam-archive.timer. See CLAUDE.md ("Recording
# retention") for the full design this implements, in particular the
# marker handshake this depends on: the Pi will only delete a recording
# once this script has told it, via captures/.archived_through, that the
# recording is safely archived here.
#
# Usage: birdcam-archive.sh [--dry-run]
set -uo pipefail

PI_HOST="birdcam@192.168.2.11"
PI_PORT=2222
PI_CAPTURES="~/birdbox-camera/captures"
ARCHIVE_DIR="${BIRDCAM_ARCHIVE_DIR:-$HOME/birdcam-archive}"
CAP_GB="${BIRDCAM_ARCHIVE_CAP_GB:-100}"
MIN_AGE_DAYS=30
QUIET_SECONDS=300                        # ignore recordings modified more recently than this
BWLIMIT="${BIRDCAM_ARCHIVE_BWLIMIT:-}"   # KB/s, e.g. "2000"; empty = unlimited

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SSH_OPTS=(-p "$PI_PORT" -o ConnectTimeout=10 -o BatchMode=yes)
log() { echo "[$(date '+%F %T')] $*"; }

# Recordings are named rec_YYYYMMDD_HHMMSS by camera.py — that filename is
# the recording's true capture time. Prefer it over mtime for retention
# ordering: converting a .mjpeg to .mp4 rewrites mtime to the conversion
# time, which could otherwise make an old, already-archived recording look
# newer than it is. Parsed as UTC (the trailing "UTC" below) so this always
# agrees with cleanup.py's capture_time() on the Pi, regardless of either
# machine's local timezone. Prints nothing (caller should fall back to
# mtime) if the name doesn't match.
capture_time() {
    local base
    base=$(basename -- "$1")
    if [[ "$base" =~ ^rec_([0-9]{4})([0-9]{2})([0-9]{2})_([0-9]{2})([0-9]{2})([0-9]{2})\. ]]; then
        date -d "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:${BASH_REMATCH[6]} UTC" +%s
    fi
}

mkdir -p "$ARCHIVE_DIR"

# ---------- pull ----------
# No --delete: the Pi prunes its own old files independently. Mirroring
# those deletions into the archive would destroy the thing the archive
# exists for — do not add it here.

rsync_args=(-av --partial -e "ssh ${SSH_OPTS[*]}")
[ -n "$BWLIMIT" ] && rsync_args+=(--bwlimit="$BWLIMIT")

rsync "${rsync_args[@]}" "${PI_HOST}:${PI_CAPTURES}/" "$ARCHIVE_DIR/"
rc=$?

if [ "$rc" -ne 0 ]; then
    # Codes rsync/ssh use for "couldn't reach the host at all" — the Pi is
    # frequently off or mid-USB-renegotiation, and that's routine, not an
    # error worth alerting on.
    case "$rc" in
        255|12|10|30|35)
            log "Pi unreachable (rsync exit $rc) — skipping this run"
            exit 0
            ;;
        *)
            log "rsync failed (exit $rc) — not advancing the marker, not pruning"
            exit 1
            ;;
    esac
fi

log "pull complete"

# ---------- marker handshake ----------
# Advance the marker only to the newest recording that's unambiguously
# finished: anything touched in the last few minutes might still be being
# written or transcoded on the Pi, so leave it for next run.

now=$(date +%s)
cutoff=$(( now - QUIET_SECONDS ))
marker_cache="$ARCHIVE_DIR/.last_marker"
prev=0
[ -f "$marker_cache" ] && prev=$(cat "$marker_cache" 2>/dev/null || echo 0)

newest=0
shopt -s nullglob
for f in "$ARCHIVE_DIR"/*.mjpeg "$ARCHIVE_DIR"/*.mp4; do
    mtime=$(stat -c %Y "$f")
    [ "$mtime" -le "$cutoff" ] || continue   # touched too recently: might still be mid-write/transcode
    ctime=$(capture_time "$f")
    [ -n "$ctime" ] || ctime=$mtime          # fallback for an oddly-named file
    [ "$ctime" -gt "$newest" ] && newest=$ctime
done
shopt -u nullglob

if [ "$newest" -gt "$prev" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] would advance archive marker to $newest (was $prev)"
    else
        # Write via a remote temp file + rename so a dropped connection
        # mid-write never leaves a truncated marker on the Pi.
        if printf '%s' "$newest" | ssh "${SSH_OPTS[@]}" "$PI_HOST" \
            "cat > ${PI_CAPTURES}/.archived_through.tmp && mv ${PI_CAPTURES}/.archived_through.tmp ${PI_CAPTURES}/.archived_through"
        then
            echo "$newest" > "$marker_cache"
            log "advanced archive marker to $newest"
        else
            log "failed to write the marker on the Pi — will retry next run"
        fi
    fi
else
    log "nothing newly archivable (newest=$newest, marker already at $prev)"
fi

# ---------- prune to size cap ----------
# Everything (recordings, sidecars, snapshots) counts against the cap, but
# only recordings older than MIN_AGE_DAYS are ever deletion candidates —
# snapshots never, and nothing from the last 30 days regardless of cap.

cap_bytes=$(( CAP_GB * 1024 * 1024 * 1024 ))
age_cutoff=$(( now - MIN_AGE_DAYS * 86400 ))

total=$(du -sb "$ARCHIVE_DIR" 2>/dev/null | cut -f1)
total=${total:-0}

if [ "$total" -le "$cap_bytes" ]; then
    log "archive size OK ($(( total / 1024 / 1024 )) MB, cap ${CAP_GB} GB)"
else
    log "archive over cap ($(( total / 1024 / 1024 )) MB > ${CAP_GB} GB) — pruning oldest first"

    candidates=()
    shopt -s nullglob
    for f in "$ARCHIVE_DIR"/*.mjpeg "$ARCHIVE_DIR"/*.mp4; do
        ctime=$(capture_time "$f")
        [ -n "$ctime" ] || ctime=$(stat -c %Y "$f")
        [ "$ctime" -le "$age_cutoff" ] || continue
        size=$(stat -c %s "$f")
        candidates+=("$ctime|$size|$f")
    done
    shopt -u nullglob

    if [ "${#candidates[@]}" -eq 0 ]; then
        log "WARNING: archive exceeds ${CAP_GB} GB cap but nothing is older than ${MIN_AGE_DAYS} days — leaving it over cap"
    else
        mapfile -t sorted < <(printf '%s\n' "${candidates[@]}" | sort -t'|' -k1,1n)
        for entry in "${sorted[@]}"; do
            [ "$total" -le "$cap_bytes" ] && break
            ctime="${entry%%|*}"; rest="${entry#*|}"
            size="${rest%%|*}"; path="${rest#*|}"
            sidecar="${path%.*}.json"
            if [ "$DRY_RUN" -eq 1 ]; then
                log "[dry-run] would delete $path ($size bytes, captured $ctime)"
            else
                rm -f -- "$path" "$sidecar"
                log "pruned $path ($size bytes)"
            fi
            total=$(( total - size ))
        done
        if [ "$total" -gt "$cap_bytes" ]; then
            log "WARNING: still over ${CAP_GB} GB cap after pruning everything eligible (30-day floor)"
        fi
    fi
fi
