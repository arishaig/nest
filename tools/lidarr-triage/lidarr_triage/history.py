"""Load Spotify Extended Streaming History.

Only the *Extended* history (``Streaming_History_Audio_*.json``) carries the
fields triage needs: ``ms_played``, ``skipped``, ``reason_start``. The basic
"Account data" export (``StreamingHistory_music_*.json``: ``endTime``,
``msPlayed``, no skip data) is rejected loudly rather than silently scored as
if every play were a real listen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AUDIO_GLOB = "Streaming_History_Audio_*.json"
BASIC_GLOB = "StreamingHistory_music_*.json"
REQUIRED_KEYS = ("ms_played", "skipped")


class HistoryFormatError(ValueError):
    """The input isn't Spotify Extended Streaming History."""


@dataclass(frozen=True)
class Play:
    ts: datetime  # UTC, when playback *ended* (Spotify's `ts`)
    ms_played: int
    artist: str  # album artist (master_metadata_album_artist_name)
    album: str
    track: str
    uri: str
    reason_start: str
    reason_end: str
    skipped: bool  # null in older records -> False (counted in stats)
    incognito: bool

    def is_real(self, min_ms: int) -> bool:
        """A real play: not skipped and at least `min_ms` long."""
        return not self.skipped and self.ms_played >= min_ms


@dataclass
class LoadStats:
    files: int = 0
    records: int = 0
    non_track: int = 0  # podcasts, audiobooks, video, missing metadata
    skipped_null: int = 0
    plays: int = 0
    first: datetime | None = None
    last: datetime | None = None
    per_file: dict[str, int] = field(default_factory=dict)


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _check_record(rec: object, source: Path) -> None:
    if not isinstance(rec, dict):
        raise HistoryFormatError(f"{source}: expected a list of objects")
    missing = [k for k in REQUIRED_KEYS if k not in rec]
    if missing:
        hint = ""
        if "msPlayed" in rec or "endTime" in rec:
            hint = (
                " This looks like the basic 'Account data' export. Request "
                "'Extended streaming history' from Spotify instead."
            )
        raise HistoryFormatError(
            f"{source}: record has no {', '.join(missing)} — not Extended "
            f"Streaming History.{hint}"
        )


def find_history_files(directory: Path) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        raise HistoryFormatError(f"{directory}: not a directory")
    basic = sorted(directory.rglob(BASIC_GLOB))
    files = sorted(directory.rglob(AUDIO_GLOB))
    if basic and not files:
        raise HistoryFormatError(
            f"{directory}: found only basic 'Account data' files "
            f"({basic[0].name}, …). Request 'Extended streaming history' from "
            "Spotify; the basic export has no skip or play-duration data."
        )
    if not files:
        raise HistoryFormatError(f"{directory}: no {AUDIO_GLOB} files found")
    return files


def load_history(directory: Path) -> tuple[list[Play], LoadStats]:
    """Load every audio play from `directory` (searched recursively).

    Raises HistoryFormatError on the basic export, or on any record missing
    `ms_played`/`skipped`. Non-track entries (podcasts etc.) are dropped and
    counted; nothing else is filtered here — callers decide what's "real".
    """
    stats = LoadStats()
    plays: list[Play] = []
    for path in find_history_files(directory):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise HistoryFormatError(f"{path}: expected a JSON list")
        stats.files += 1
        count = 0
        for rec in data:
            stats.records += 1
            _check_record(rec, path)
            uri = rec.get("spotify_track_uri") or ""
            track = rec.get("master_metadata_track_name")
            artist = rec.get("master_metadata_album_artist_name")
            if not uri.startswith("spotify:track:") or not track or not artist:
                stats.non_track += 1
                continue
            skipped = rec.get("skipped")
            if skipped is None:
                stats.skipped_null += 1
            play = Play(
                ts=_parse_ts(rec["ts"]),
                ms_played=int(rec["ms_played"] or 0),
                artist=artist,
                album=rec.get("master_metadata_album_album_name") or "",
                track=track,
                uri=uri,
                reason_start=rec.get("reason_start") or "unknown",
                reason_end=rec.get("reason_end") or "unknown",
                skipped=bool(skipped),
                incognito=bool(rec.get("incognito_mode")),
            )
            plays.append(play)
            count += 1
            if stats.first is None or play.ts < stats.first:
                stats.first = play.ts
            if stats.last is None or play.ts > stats.last:
                stats.last = play.ts
        stats.per_file[path.name] = count
    stats.plays = len(plays)
    return plays, stats
