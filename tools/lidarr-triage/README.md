# lidarr-triage

Finds Lidarr albums you keep for one or two songs, using your Spotify
Extended Streaming History as evidence. It then (only if you approve each
album) unmonitors them and deletes the tracks you never play.

Read [`docs/music-lidarr-safety.md`](../../docs/music-lidarr-safety.md) first.
Music files on `Tank/media_root` have **no backups or snapshots**; the Lidarr
Recycling Bin is the only undo, and `apply` refuses to run without it.

## Setup

```bash
cd tools/lidarr-triage
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"   # editable: reports/ and runs/ land here, gitignored

# API key from vault. Env only; never put it in a file in the repo.
export LIDARR_API_KEY="$(ansible-vault view ../../inventory/group_vars/all/vault.yml \
  | awk '/^lidarr_api_key:/ {gsub(/"/,"",$2); print $2}')"
# Default URL is the LAN LB http://192.168.1.116:8686. lidarr.arishaig.site
# is behind Authelia 2FA and would redirect API calls; the client refuses
# redirects for that reason. Override with LIDARR_URL or --url.
```

**Spotify history:** unzip the Extended Streaming History export into
`spotify-history/` at the repo root (gitignored), or anywhere outside the
repo. The tool reads `Streaming_History_Audio_*.json` recursively. It
**fails loudly** on the basic "Account data" export
(`StreamingHistory_music_*.json`, no skip or duration data), because every
play there would look like a real listen. Never copy the history into the
cluster, a PVC or an image.

## Workflow

```bash
# 1. Report (read-only)
lidarr-triage report --history ../../spotify-history
#   -> reports/summary.md, prune.csv, keep.csv, hold.csv, review.csv, review-items.csv

# 2. Review reports/prune.csv. Put y in `approved` for albums to act on.
#    Edit keep_track_ids (semicolon-separated Lidarr track ids) to keep more.
#    Check what you'd freeze at lossy quality:
lidarr-triage quality-report --from reports/prune.csv

# 3. Take a Lidarr backup (docs/music-lidarr-safety.md), then dry-run:
lidarr-triage apply reports/prune.csv
# 4. Act:
lidarr-triage apply reports/prune.csv --apply
#   -> runs/apply-<UTC timestamp>.jsonl

# Anytime: non-FLAC files on unmonitored albums (no more upgrades for those)
lidarr-triage quality-report
```

## How it decides

**Real play:** not `skipped` and `ms_played` ≥ 30 s (`--min-ms`). Podcasts
and other non-track entries are dropped. `skipped: null` (common in older
records) counts as not skipped; the report prints how many there were.

**Matching:** Spotify has names, not MusicBrainz IDs. Artist, album and
track names are normalized:
- casefolded, with accents and punctuation stripped;
- `feat.`/`ft.`/`with` credits dropped;
- `(Deluxe Edition)`, `- Remastered 2011`, `(Live)`, `- Mono` and similar
  qualifiers stripped.

Names are then fuzzy-matched with rapidfuzz: first the album (artist and
album title), then the track within that album.
- **Counted:** matches at ≥ `--threshold` (90).
- **REVIEW items:** matches between `--review-floor` (70) and the threshold.
  So are tracks you played from *another* release (a single, a compilation)
  that match a track on a Lidarr album ("cross-album").
- **Same-name tracks:** an album can hold tracks that normalize to the same
  name ("Song" and "Song (Live)", "- Mono" and "- Stereo"). The tie is broken
  on the un-stripped title, then on live/mono/stereo tags. If it's still tied,
  it's an `ambiguous-track` REVIEW item. Guessing could keep the live cut and
  delete the studio track you actually play.
- **Never actioned:** REVIEW items, always.

**Score per play:** `intent × 0.5^(age / --half-life)`.
- Intentional starts (`clickrow`, `playbtn`, `backbtn`, `clickside`,
  `uriopen`) weigh 1.0.
- Passive starts (`trackdone`, `fwdbtn`, `appload`, `remote`, …) weigh
  `--passive-weight` (0.5).
- The half-life defaults to 3 years.

A track **counts** at a total ≥ `--min-track-score` (0.5): one intentional
play within the half-life, or passive plays adding up to that.

| Bucket | Rule | Acted on? |
|---|---|---|
| KEEP | ≥ `--keep-min-tracks` (3) counting tracks, or the counting tracks are the whole album (singles/EPs) | no |
| PRUNE | 1–2 counting tracks; `keep_tracks` lists them | only rows you mark `approved` |
| REVIEW | would be PRUNE, but a low-confidence, ambiguous or cross-album match touches it, or a track you play has no file | no |
| HOLD | no plays, or only faint ones. Absence of data isn't evidence | no |

Only albums with at least one track file are considered.

## What `apply --apply` does

Preconditions (all or nothing):
- The Recycling Bin is set.
- The newest Lidarr backup is under 60 min old (`--max-backup-age`,
  `--skip-backup-check`).
- No enabled import list has **Monitor Existing** on. It would re-monitor
  pruned albums. Override with `--allow-monitor-existing-lists`.

Per approved album, strictly in order, aborting that album on any failure:
1. Re-read its track files; refuse if they differ from `trackfile_snapshot`.
   Also refuse if a kept track has no file, or if the plan would delete every
   file.
2. `PUT /api/v1/album/monitor` → unmonitored.
3. `GET` the album and confirm it is unmonitored.
4. `DELETE /api/v1/trackfile/{id}` for each non-kept file, one at a time.
   Lidarr moves each into the Recycling Bin.

Unmonitoring first matters. Deleting from a monitored album makes Lidarr
search for, and re-download, the "missing" tracks.

At the end it re-checks that every processed album is still unmonitored and
lists enabled import lists. Every step is appended to the JSONL run log:
- album and artist ids plus MBIDs;
- whether the album was monitored before;
- every track file's id, path, size and quality, before anything changes;
- each unmonitor and delete, and any abort.

## Rollback

For each `delete` event in `runs/apply-*.jsonl`:
1. Move the file from the Recycling Bin (`/Tank/media_root/lidarr-recycle/<artist>/…`)
   back to its logged `path`.
2. Lidarr → artist → Refresh & Scan.
3. Re-monitor the album if `was_monitored` was true: in the UI, or with
   `PUT /api/v1/album/monitor {"albumIds":[id],"monitored":true}`.

Once the bin's cleanup age has passed, the files are gone; re-acquire them
through Lidarr (monitor the album, then search). For a wholesale undo of
Lidarr's state, restore the native backup you took before the run.

## Tests

`pytest`. Everything is synthetic: no real listening history is in the repo,
and CI (`.github/workflows/lidarr-triage-tests.yml`) runs on PRs that touch
this directory. Endpoints were checked against Lidarr's OpenAPI spec for
3.1.2 (see the docstring in `lidarr_triage/lidarr.py`); the tool warns when
the live Lidarr's major.minor differs.
