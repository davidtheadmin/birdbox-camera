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
| `BIRDCAM_ARCHIVE_BWLIMIT` | unset (unlimited) | rsync `--bwlimit` in KB/s — set this if a big backlog pull visibly degrades the live stream (shared USB gadget link) |

Set them in the service file's `[Service]` section (`Environment=...`) if
you want a persistent override rather than passing them ad hoc.
