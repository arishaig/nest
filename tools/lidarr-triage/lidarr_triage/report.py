"""Write per-bucket CSV + a markdown summary.

`prune.csv` is the file you review and hand back to `apply`: fill the
`approved` column (y/yes/x) on albums to act on. You may edit
`keep_track_ids` to keep more (or different) tracks; `apply` recomputes the
files to delete from it. `trackfile_snapshot` lets `apply` refuse albums that
changed since the report.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .buckets import HOLD, KEEP, PRUNE, REVIEW, Decision
from .match import MatchResult

PRUNE_FIELDS = [
    "approved", "album_id", "artist", "album", "confidence", "counting_tracks", "score",
    "total_minutes", "keep_track_ids", "keep_tracks", "keep_non_flac", "delete_tracks",
    "delete_count", "trackfile_snapshot", "reason",
]
ALBUM_FIELDS = [
    "album_id", "artist", "album", "monitored", "confidence", "counting_tracks", "played_tracks",
    "score", "total_minutes", "reason", "played",
]
REVIEW_ITEM_FIELDS = [
    "kind", "spotify_artist", "spotify_album", "spotify_track", "album_id", "candidate",
    "confidence", "plays",
]


def _ids(values) -> str:
    return ";".join(str(v) for v in values)


def _minutes(ms: int) -> str:
    return f"{ms / 60000:.1f}"


def _played(d: Decision) -> str:
    return " | ".join(f"{t.title} ({t.real_plays}x, {t.score:.2f})"
                      for t in sorted(d.album.played_tracks, key=lambda t: -t.score))


def _album_row(d: Decision) -> dict:
    a = d.album
    return {
        "album_id": a.album.id,
        "artist": a.album.artist,
        "album": a.album.title,
        "monitored": a.album.monitored,
        "confidence": "" if a.confidence is None else f"{a.confidence:.1f}",
        "counting_tracks": len(d.keep),
        "played_tracks": len(a.played_tracks),
        "score": f"{a.score:.2f}",
        "total_minutes": _minutes(a.total_ms),
        "reason": d.reason,
        "played": _played(d),
    }


def _prune_row(d: Decision) -> dict:
    a = d.album
    return {
        "approved": "",
        "album_id": a.album.id,
        "artist": a.album.artist,
        "album": a.album.title,
        "confidence": "" if a.confidence is None else f"{a.confidence:.1f}",
        "counting_tracks": len(d.keep),
        "score": f"{a.score:.2f}",
        "total_minutes": _minutes(a.total_ms),
        "keep_track_ids": _ids(t.track_id for t in d.keep),
        "keep_tracks": " | ".join(t.title for t in d.keep),
        "keep_non_flac": " | ".join(f"{t.title} [{t.quality}]" for t in d.keep if t.trackfile_id and not t.flac),
        "delete_tracks": " | ".join(t.title for t in d.delete),
        "delete_count": len(d.delete),
        "trackfile_snapshot": _ids(a.album.trackfile_ids),
        "reason": d.reason,
    }


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_reports(out: Path, decisions: list[Decision], matches: MatchResult, header: list[str]) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    by = {b: [d for d in decisions if d.bucket == b] for b in (KEEP, PRUNE, HOLD, REVIEW)}
    alpha = lambda d: (d.album.album.artist.casefold(), d.album.album.title.casefold())  # noqa: E731

    paths = {
        PRUNE: out / "prune.csv",
        KEEP: out / "keep.csv",
        HOLD: out / "hold.csv",
        REVIEW: out / "review.csv",
        "items": out / "review-items.csv",
        "summary": out / "summary.md",
    }
    _write_csv(paths[PRUNE], PRUNE_FIELDS, [_prune_row(d) for d in sorted(by[PRUNE], key=alpha)])
    _write_csv(paths[KEEP], ALBUM_FIELDS, [_album_row(d) for d in sorted(by[KEEP], key=lambda d: -d.album.score)])
    _write_csv(paths[HOLD], ALBUM_FIELDS, [_album_row(d) for d in sorted(by[HOLD], key=alpha)])
    review_rows = []
    for d in sorted(by[REVIEW], key=alpha):
        row = _prune_row(d)
        row["reason"] = d.reason
        review_rows.append(row)
    _write_csv(paths[REVIEW], PRUNE_FIELDS, review_rows)
    _write_csv(paths["items"], REVIEW_ITEM_FIELDS,
               [vars(r) for r in sorted(matches.review, key=lambda r: (-r.confidence, r.spotify_artist))])

    lines = ["# lidarr-triage report", "", *header, "",
             "| Bucket | Albums | Meaning |", "|---|---:|---|",
             f"| KEEP | {len(by[KEEP])} | enough distinct tracks played |",
             f"| PRUNE | {len(by[PRUNE])} | 1–2 tracks played; review `prune.csv`, mark `approved` |",
             f"| REVIEW | {len(by[REVIEW])} | would prune, but a match is uncertain — never actioned |",
             f"| HOLD | {len(by[HOLD])} | no (or only faint) plays — never actioned |",
             "", f"Uncertain matches: {len(matches.review)} (`review-items.csv`). "
             f"Plays not matching anything in the library: {matches.unmatched_plays}.", ""]
    if by[PRUNE]:
        lines += ["## PRUNE (first 50)", "", "| Artist | Album | Keep | Delete | Kept non-FLAC |", "|---|---|---|---:|---|"]
        for d in sorted(by[PRUNE], key=alpha)[:50]:
            r = _prune_row(d)
            lines.append(f"| {r['artist']} | {r['album']} | {r['keep_tracks']} | {r['delete_count']} | {r['keep_non_flac']} |")
    paths["summary"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths
