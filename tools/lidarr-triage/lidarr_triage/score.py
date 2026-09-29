"""Per-play weights and per-album scoring.

weight(play) = intent_weight(reason_start) * 0.5 ** (age_years / half_life)

- Intentional starts (you picked the track) weigh 1.0; passive ones (the
  previous track ended, autoplay/radio, app resume) weigh `passive_weight`.
- A play `half_life` years old counts half as much as one today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .history import Play
from .library import LibAlbum

# Spotify reason_start values where the user chose this track.
INTENTIONAL_STARTS = frozenset({"clickrow", "playbtn", "backbtn", "clickside", "uriopen"})
DAYS_PER_YEAR = 365.25


@dataclass
class ScoreConfig:
    min_ms: int = 30_000
    half_life_years: float = 3.0
    passive_weight: float = 0.5
    # A track "counts" at >= this: one intentional play within the half-life,
    # or passive plays adding up to the same.
    min_track_score: float = 0.5
    keep_min_tracks: int = 3  # KEEP at >= this many counting tracks; PRUNE at 1..keep_min_tracks-1
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def play_weight(play: Play, cfg: ScoreConfig) -> float:
    intent = 1.0 if play.reason_start in INTENTIONAL_STARTS else cfg.passive_weight
    age_years = max(0.0, (cfg.now - play.ts).total_seconds() / 86400 / DAYS_PER_YEAR)
    return intent * 0.5 ** (age_years / cfg.half_life_years)


@dataclass
class TrackScore:
    track_id: int
    title: str
    trackfile_id: int | None
    quality: str
    flac: bool
    real_plays: int = 0
    intentional_plays: int = 0
    ms: int = 0
    score: float = 0.0
    last_played: datetime | None = None

    def counts(self, cfg: ScoreConfig) -> bool:
        return self.score >= cfg.min_track_score


@dataclass
class AlbumScore:
    album: LibAlbum
    tracks: list[TrackScore]
    confidence: float | None  # lowest match confidence among counted plays

    @property
    def score(self) -> float:
        return sum(t.score for t in self.tracks)

    @property
    def total_ms(self) -> int:
        return sum(t.ms for t in self.tracks)

    @property
    def played_tracks(self) -> list[TrackScore]:
        return [t for t in self.tracks if t.real_plays]

    def counting_tracks(self, cfg: ScoreConfig) -> list[TrackScore]:
        return [t for t in self.tracks if t.counts(cfg)]


def score_album(album: LibAlbum, plays_by_track: dict[int, list[Play]],
                confidence: float | None, cfg: ScoreConfig) -> AlbumScore:
    tracks = []
    for t in album.tracks:
        ts = TrackScore(t.id, t.title, t.trackfile_id, t.quality, t.flac)
        for p in plays_by_track.get(t.id, ()):
            if not p.is_real(cfg.min_ms):
                continue
            ts.real_plays += 1
            ts.intentional_plays += p.reason_start in INTENTIONAL_STARTS
            ts.ms += p.ms_played
            ts.score += play_weight(p, cfg)
            if ts.last_played is None or p.ts > ts.last_played:
                ts.last_played = p.ts
        tracks.append(ts)
    return AlbumScore(album, tracks, confidence)
