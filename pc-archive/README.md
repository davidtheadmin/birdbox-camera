# PC-side archive

Pulls `captures/` off the Pi into a local long-term archive, tells the Pi
which recordings are now safely archived, and prunes the local archive to
a 100 GB cap. Runs hourly via a systemd user timer. Full design: see
"Recording retention" in `CLAUDE.md`.

## Install (on the PC, e.g. `pebbles`)

```sh
mkdir -p ~/bin ~/.config/systemd/user
cp birdcam-archive.sh ~/bin/
chmod +x ~/bin/birdcam-archive.sh
cp birdcam-archive.service birdcam-archive.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now birdcam-archive.timer
```

Check it: `systemctl --user list-timers birdcam-archive.timer`,
`journalctl --user -u birdcam-archive.service`.

## Over the cap: compress first, delete last

Only recordings older than 30 days are ever touched. When the archive is
over the cap, oldest first, each one that isn't HEVC yet is either deleted
(shorter than `BIRDCAM_ARCHIVE_MIN_SECONDS`, or unreadable) or re-encoded to
HEVC in place, same file name, until the archive is back under the cap.
Only if everything eligible is already compressed does it delete the
oldest. HEVC plays in mpv/VLC; Firefox on Linux doesn't play it.

`--prune-only` runs just this step, without the Pi.

## Before pointing it at the real archive

Test against a scratch directory first:

```sh
BIRDCAM_ARCHIVE_DIR=/tmp/birdcam-archive-test ~/bin/birdcam-archive.sh --dry-run
```

`--dry-run` pulls for real (rsync has no destructive dry-run-only mode that
still lets you inspect what landed) but only *prints* what marker update
and pruning it would do — it deletes nothing and writes nothing back to the
Pi.

## Config knobs (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `BIRDCAM_ARCHIVE_DIR` | `~/birdcam-archive` | Local archive location |
| `BIRDCAM_ARCHIVE_CAP_GB` | `100` | Prune target |
| `BIRDCAM_ARCHIVE_COMPRESS` | `1` | Before deleting to get under the cap, re-encode old mp4s to HEVC (needs ffmpeg with libx265). `0` = delete only |
| `BIRDCAM_ARCHIVE_CRF` | `28` | x265 quality for that: lower = better and bigger |
| `BIRDCAM_ARCHIVE_MIN_SECONDS` | `5` | Old clips shorter than this (or unreadable) are deleted rather than compressed |
| `BIRDCAM_PI_HOST` / `BIRDCAM_PI_PORT` | `birdcam2` / `22` | Which Pi to pull from (an `~/.ssh/config` alias works) |
| `BIRDCAM_ARCHIVE_BWLIMIT` | unset (unlimited) | rsync `--bwlimit` in KB/s — set this if a big backlog pull visibly degrades the live stream (shared USB gadget link) |

Set them in the service file's `[Service]` section (`Environment=...`) if
you want a persistent override rather than passing them ad hoc.
