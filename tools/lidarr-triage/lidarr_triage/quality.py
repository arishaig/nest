"""Read-only report: kept tracks that aren't FLAC.

Unmonitored albums stop getting quality upgrades, so anything pruned (or
about to be) is frozen at its current quality. Two sources:

- default: every *unmonitored* album in Lidarr that still has files
  (covers albums already pruned by `apply`);
- `--from prune.csv`: the kept tracks of rows in a triage report (preview,
  before anything is unmonitored).
"""

from __future__ import annotations

import csv
from pathlib import Path

from .apply import _int_set
from .lidarr import Lidarr, is_flac, quality_name

FIELDS = ["album_id", "artist", "album", "monitored", "track_id", "track", "quality", "path"]


def from_unmonitored(lidarr: Lidarr) -> list[dict]:
    rows = []
    for a in lidarr.albums():
        if a.get("monitored") or not (a.get("statistics") or {}).get("trackFileCount"):
            continue
        rows += _album_rows(lidarr, a["id"], (a.get("artist") or {}).get("artistName", ""),
                            a.get("title", ""), False, keep=None)
    return rows


def from_report(lidarr: Lidarr, csv_path: Path) -> list[dict]:
    rows = []
    with Path(csv_path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows += _album_rows(lidarr, int(r["album_id"]), r.get("artist", ""), r.get("album", ""),
                                None, keep=_int_set(r.get("keep_track_ids", "")))
    return rows


def _album_rows(lidarr: Lidarr, album_id: int, artist: str, title: str,
                monitored: bool | None, keep: set[int] | None) -> list[dict]:
    files = {f["id"]: f for f in lidarr.trackfiles(album_id=album_id)}
    out = []
    for t in lidarr.tracks(album_id=album_id):
        f = files.get(t.get("trackFileId") or 0)
        if f is None or is_flac(f) or (keep is not None and t["id"] not in keep):
            continue
        out.append({"album_id": album_id, "artist": artist, "album": title,
                    "monitored": "" if monitored is None else monitored,
                    "track_id": t["id"], "track": t.get("title", ""),
                    "quality": quality_name(f), "path": f.get("path", "")})
    return out


def write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["artist"].casefold(), r["album"].casefold())))
