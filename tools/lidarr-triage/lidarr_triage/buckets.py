"""Sort scored albums into KEEP / PRUNE / HOLD / REVIEW.

- KEEP:   >= keep_min_tracks tracks with a counting score.
- PRUNE:  1..keep_min_tracks-1 counting tracks. Keep those; the rest of the
          album's files are deletion candidates.
- HOLD:   no real plays, or only faint ones (decayed/passive below
          min_track_score). Absence of evidence isn't evidence: never actionable.
- REVIEW: anything a PRUNE decision could be wrong about — the album or one of
          its tracks matched below threshold or ambiguously, a cross-album
          match points at it (you may have played its track from a single or
          compilation), or a track you play has no file.

Edge case: a PRUNE whose kept tracks already cover every track file (singles,
2-track EPs) has nothing to delete, so it is KEEP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .match import MatchResult, ReviewItem
from .score import AlbumScore, ScoreConfig, score_album
from .library import LibAlbum

KEEP, PRUNE, HOLD, REVIEW = "KEEP", "PRUNE", "HOLD", "REVIEW"


@dataclass
class Decision:
    bucket: str
    album: AlbumScore
    reason: str
    keep: list = field(default_factory=list)  # TrackScore
    delete: list = field(default_factory=list)  # LibTrack with files
    review_items: list[ReviewItem] = field(default_factory=list)


def decide(library: list[LibAlbum], matches: MatchResult, cfg: ScoreConfig) -> list[Decision]:
    reviews_by_album: dict[int, list[ReviewItem]] = {}
    for item in matches.review:
        if item.album_id is not None:
            reviews_by_album.setdefault(item.album_id, []).append(item)

    decisions = []
    for album in library:
        scored = score_album(album, matches.album_plays.get(album.id, {}),
                             matches.album_confidence.get(album.id), cfg)
        counting = scored.counting_tracks(cfg)
        reviews = reviews_by_album.get(album.id, [])

        if not counting:
            reason = "no plays" if not scored.played_tracks and not reviews else "only faint plays"
            if reviews and not scored.played_tracks:
                reason = "no confident plays (see REVIEW items)"
            decisions.append(Decision(HOLD, scored, reason, review_items=reviews))
            continue

        if len(counting) >= cfg.keep_min_tracks:
            decisions.append(Decision(KEEP, scored, f"{len(counting)} tracks played",
                                      keep=counting, review_items=reviews))
            continue

        missing = [t.title for t in counting if not t.trackfile_id]
        if missing:
            decisions.append(Decision(REVIEW, scored, "played track has no file: " + ", ".join(missing),
                                      keep=counting, review_items=reviews))
            continue

        keep_ids = {t.track_id for t in counting}
        delete = [t for t in album.tracks if t.trackfile_id and t.id not in keep_ids]
        if not delete:
            decisions.append(Decision(KEEP, scored, "kept tracks are the whole album",
                                      keep=counting, review_items=reviews))
            continue
        if reviews:
            kinds = sorted({r.kind for r in reviews})
            decisions.append(Decision(REVIEW, scored, "would PRUNE, but " + ", ".join(kinds),
                                      keep=counting, delete=delete, review_items=reviews))
            continue
        decisions.append(Decision(PRUNE, scored, f"only {len(counting)} track(s) played",
                                  keep=counting, delete=delete))
    return decisions
