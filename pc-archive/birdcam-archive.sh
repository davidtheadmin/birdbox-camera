#!/usr/bin/env bash
# birdcam-archive.sh — pull recordings from the Pi into a long-term local
# archive, tell the Pi how much of it is now safely archived, and prune the
# local archive to a size cap. Before deleting anything to get under the
# cap, it re-encodes old recordings to HEVC (often 60-80 % smaller), and drops
# clips that are too short to be real visits.
#
# Run hourly via birdcam-archive.timer. See CLAUDE.md ("Recording
# retention") for the full design this implements, in particular the
# marker handshake this depends on: the Pi will only delete a recording
# once this script has told it, via captures/.archived_through, that the
# recording is safely archived here.
#
# Usage: birdcam-archive.sh [--dry-run] [--prune-only]
#   --prune-only  skip the pull and the marker; just enforce the cap (also
#                 how the prune/compress step is tested without a Pi)
set -uo pipefail

PI_HOST="${BIRDCAM_PI_HOST:-birdcam2}"   # an ~/.ssh/config alias or user@host
PI_PORT="${BIRDCAM_PI_PORT:-22}"
PI_CAPTURES="~/birdbox-camera/captures"
ARCHIVE_DIR="${BIRDCAM_ARCHIVE_DIR:-$HOME/birdcam-archive}"
CAP_GB="${BIRDCAM_ARCHIVE_CAP_GB:-100}"
CAP_BYTES="${BIRDCAM_ARCHIVE_CAP_BYTES:-$(( CAP_GB * 1024 * 1024 * 1024 ))}"
MIN_REAL_SECONDS="${BIRDCAM_ARCHIVE_MIN_SECONDS:-5}"  # shorter clips aren't real visits
COMPRESS="${BIRDCAM_ARCHIVE_COMPRESS:-1}"              # 0 = old behaviour, delete only
CRF="${BIRDCAM_ARCHIVE_CRF:-28}"                       # x265 quality: lower = better/bigger
MIN_AGE_DAYS=30
QUIET_SECONDS=300                        # ignore recordings modified more recently than this
BWLIMIT="${BIRDCAM_ARCHIVE_BWLIMIT:-}"   # KB/s, e.g. "2000"; empty = unlimited

DRY_RUN=0
PRUNE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --prune-only) PRUNE_ONLY=1 ;;
        *) echo "unknown option: $arg"; exit 2 ;;
    esac
done

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

now=$(date +%s)

if [ "$PRUNE_ONLY" -eq 0 ]; then
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

cutoff=$(( now - QUIET_SECONDS ))
marker_cache="$ARCHIVE_DIR/.last_marker"
prev=0
[ -f "$marker_cache" ] && prev=$(cat "$marker_cache" 2>/dev/null || echo 0)

newest=0
shopt -s nullglob
for f in "$ARCHIVE_DIR"/*.mjpeg "$ARCHIVE_DIR"/*.mp4 "$ARCHIVE_DIR"/*.h264; do
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

fi   # PRUNE_ONLY

# ---------- prune to size cap ----------
# Everything (recordings, sidecars, snapshots) counts against the cap, but
# only recordings older than MIN_AGE_DAYS are ever touched: snapshots never,
# and nothing from the last 30 days regardless of cap. By then the Pi has
# long deleted them, so rsync never pulls an original back over a
# compressed copy.
#
# Over the cap, in this order:
#   1. oldest first, each eligible mp4 that isn't HEVC yet: if it's shorter
#      than MIN_REAL_SECONDS (or unreadable) delete it, otherwise re-encode
#      it to HEVC in place, same name. Stop once under the cap.
#   2. only if still over: delete oldest first, compressed or not.
# Compressing before deleting matters: otherwise a freshly compressed clip,
# still the oldest, would be the first thing deleted on the next run.

cap_bytes=$CAP_BYTES
age_cutoff=$(( now - MIN_AGE_DAYS * 86400 ))

total=$(du -sb "$ARCHIVE_DIR" 2>/dev/null | cut -f1)
total=${total:-0}

clip_seconds() {   # duration in whole seconds, empty if unreadable
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -d. -f1
}
clip_codec() {
    ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$1" 2>/dev/null
}
size_with_wav() {
    local f=$1 size wav
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
    wav="${f%.*}.wav"
    [ -f "$wav" ] && size=$(( size + $(stat -c %s "$wav") ))
    echo "$size"
}
remove_clip() {    # path, size, why
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] would delete $1 (+ .json/.wav; $2 bytes): $3"
    else
        rm -f -- "$1" "${1%.*}.json" "${1%.*}.wav"
        log "deleted $1 ($2 bytes): $3"
    fi
    total=$(( total - $2 ))
}

# Re-encode one mp4 to HEVC in place. Prints the bytes saved; prints nothing
# (and leaves the original untouched) if anything looks wrong.
compress_clip() {
    local f=$1 tmp in_s out_s old_size new_size
    tmp="${f%.mp4}.hevc.tmp.mp4"
    in_s=$(clip_seconds "$f")
    nice -n 19 ffmpeg -nostdin -v error -y -i "$f" -map 0:v:0 -map '0:a?' \
        -c:v libx265 -crf "$CRF" -preset medium -tag:v hvc1 -x265-params log-level=error \
        -c:a copy -map_metadata 0 -movflags +faststart "$tmp" || { rm -f -- "$tmp"; return; }
    out_s=$(clip_seconds "$tmp")
    if [ "$(clip_codec "$tmp")" != "hevc" ] || [ -z "$out_s" ] \
       || [ "$(( out_s > in_s ? out_s - in_s : in_s - out_s ))" -gt 1 ]; then
        log "compression check failed for $f (${in_s}s -> ${out_s:-?}s) — keeping the original" >&2
        rm -f -- "$tmp"; return
    fi
    old_size=$(stat -c %s "$f"); new_size=$(stat -c %s "$tmp")
    if [ "$new_size" -ge "$old_size" ]; then
        rm -f -- "$tmp"; return
    fi
    touch -r "$f" "$tmp"          # keep the date; ordering uses the name anyway
    mv -f -- "$tmp" "$f"
    echo $(( old_size - new_size ))
}

if [ "$total" -le "$cap_bytes" ]; then
    log "archive size OK ($(( total / 1024 / 1024 )) MB, cap $(( cap_bytes / 1024 / 1024 )) MB)"
else
    log "archive over cap ($(( total / 1024 / 1024 )) MB > $(( cap_bytes / 1024 / 1024 )) MB)"

    candidates=()
    shopt -s nullglob
    for f in "$ARCHIVE_DIR"/*.mjpeg "$ARCHIVE_DIR"/*.mp4 "$ARCHIVE_DIR"/*.h264; do
        case "$f" in *.tmp.mp4) continue ;; esac   # leftover from an interrupted run
        ctime=$(capture_time "$f")
        [ -n "$ctime" ] || ctime=$(stat -c %Y "$f")
        [ "$ctime" -le "$age_cutoff" ] || continue
        candidates+=("$ctime|$f")
    done
    shopt -u nullglob
    rm -f -- "$ARCHIVE_DIR"/*.hevc.tmp.mp4 2>/dev/null

    if [ "${#candidates[@]}" -eq 0 ]; then
        log "WARNING: archive exceeds the cap but nothing is older than ${MIN_AGE_DAYS} days — leaving it over cap"
    else
        mapfile -t sorted < <(printf '%s\n' "${candidates[@]}" | sort -t'|' -k1,1n)

        # --- pass 1: drop junk, compress the rest ---
        can_compress=0
        if [ "$COMPRESS" -eq 1 ]; then
            # no "| grep -q" here: with pipefail, grep quitting early makes
            # ffmpeg die of SIGPIPE and the whole test count as false
            encoders=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
            if command -v ffprobe >/dev/null && [[ "$encoders" == *libx265* ]]; then
                can_compress=1
            else
                log "WARNING: ffmpeg with libx265 not found — deleting instead of compressing"
            fi
        fi
        if [ "$can_compress" -eq 1 ]; then
            for entry in "${sorted[@]}"; do
                [ "$total" -le "$cap_bytes" ] && break
                path="${entry#*|}"
                [ -f "$path" ] || continue
                case "$path" in *.mp4) ;; *) continue ;; esac
                [ "$(clip_codec "$path")" = "hevc" ] && continue   # done earlier
                secs=$(clip_seconds "$path")
                size=$(size_with_wav "$path")
                if [ -z "$secs" ]; then
                    remove_clip "$path" "$size" "unreadable"
                    continue
                fi
                if [ "$secs" -lt "$MIN_REAL_SECONDS" ]; then
                    remove_clip "$path" "$size" "only ${secs}s, not a real visit"
                    continue
                fi
                if [ "$DRY_RUN" -eq 1 ]; then
                    log "[dry-run] would compress $path (${secs}s, $size bytes) to HEVC"
                    total=$(( total - size * 7 / 10 ))   # rough guess, dry run only
                    continue
                fi
                saved=$(compress_clip "$path")
                if [ -n "$saved" ]; then
                    total=$(( total - saved ))
                    log "compressed $path: ${secs}s, saved $(( saved / 1024 / 1024 )) MB"
                fi
            done
        fi

        # --- pass 2: still over? delete oldest first ---
        for entry in "${sorted[@]}"; do
            [ "$total" -le "$cap_bytes" ] && break
            path="${entry#*|}"
            [ -f "$path" ] || continue
            remove_clip "$path" "$(size_with_wav "$path")" "over cap, oldest"
        done
        if [ "$total" -gt "$cap_bytes" ]; then
            log "WARNING: still over the cap after pruning everything eligible (${MIN_AGE_DAYS}-day floor)"
        fi
    fi
fi
