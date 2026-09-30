"""Fuzzy-match Spotify plays onto Lidarr albums and tracks.

Album first (artist + album title), then the track within that album. Only
matches at or above `threshold` are counted. Everything between
`review_floor` and `threshold` becomes a REVIEW item, never an action. Plays
whose Spotify album doesn't match, but whose track does match a track on a
*different* Lidarr album by the same artist (singles, compilations, deluxe
splits), are also REVIEW items ("cross-album"): they're evidence about that
album that we refuse to count silently.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from .history import Play
from .library import LibAlbum
from .normalize import light, normalize, normalize_artist, recording_tags


@dataclass
class ReviewItem:
    kind: str  # low-confidence-album | low-confidence-track | ambiguous-track | cross-album
    spotify_artist: str
    spotify_album: str
    spotify_track: str
    album_id: int | None
    candidate: str
    confidence: float
    plays: int


@dataclass
class MatchResult:
    # album_id -> track_id -> plays counted for that track
    album_plays: dict[int, dict[int, list[Play]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    album_confidence: dict[int, float] = field(default_factory=dict)
    review: list[ReviewItem] = field(default_factory=list)
    unmatched_plays: int = 0  # artist/album not in library at all


def _album_score(sp_artist: str, sp_album: str, album: LibAlbum) -> float:
    a = fuzz.token_set_ratio(sp_artist, album.norm_artist)
    t = fuzz.token_sort_ratio(sp_album, album.norm_title)
    return min(a, t)


def _best_track(sp_track: str, album: LibAlbum):
    """Best track for a Spotify title: (track, score, ambiguous).

    `normalize` strips Live/Mono/Remaster etc., so "Song" and "Song (Live)"
    on one album tie at 100. Ties are broken on the un-stripped title, then
    on recording-level tags (live/mono/stereo); if still tied the result is
    ambiguous and the caller must not count it — picking by track order could
    keep the live cut and delete the studio track you actually play.
    """
    norm = normalize(sp_track)
    scored = [(fuzz.token_sort_ratio(norm, t.norm), t) for t in album.tracks]
    if not scored:
        return None, 0.0, False
    top = max(s for s, _ in scored)
    tied = [t for s, t in scored if s == top]
    if len(tied) == 1:
        return tied[0], top, False
    exact = [t for t in tied if light(t.title) == light(sp_track)]
    if len(exact) == 1:
        return exact[0], top, False
    tags = recording_tags(sp_track)
    same_kind = [t for t in tied if recording_tags(t.title) == tags]
    if len(same_kind) == 1:
        return same_kind[0], top, False
    return tied[0], top, True


def match_plays(plays: list[Play], library: list[LibAlbum], *,
                threshold: float = 90.0, review_floor: float = 70.0) -> MatchResult:
    result = MatchResult()
    by_artist: dict[str, list[LibAlbum]] = defaultdict(list)
    for album in library:
        by_artist[album.norm_artist].append(album)
    artist_names = list(by_artist)

    # Group plays by (artist, album, track) so each distinct Spotify track is matched once.
    groups: dict[tuple[str, str, str], list[Play]] = defaultdict(list)
    for p in plays:
        groups[(p.artist, p.album, p.track)].append(p)

    album_cache: dict[tuple[str, str], tuple[LibAlbum | None, float, list[LibAlbum]]] = {}

    for (sp_artist, sp_album, sp_track), group in groups.items():
        n_artist, n_album = normalize_artist(sp_artist), normalize(sp_album)
        key = (n_artist, n_album)
        if key not in album_cache:
            candidates: list[LibAlbum] = []
            for name, _score, _ in process.extract(n_artist, artist_names, scorer=fuzz.token_set_ratio,
                                                   score_cutoff=review_floor, limit=5):
                candidates.extend(by_artist[name])
            best, best_score = None, 0.0
            for album in candidates:
                s = _album_score(n_artist, n_album, album)
                if s > best_score:
                    best, best_score = album, s
            album_cache[key] = (best, best_score, candidates)
        album, a_score, candidates = album_cache[key]

        if album is not None and a_score >= threshold:
            track, t_score, ambiguous = _best_track(sp_track, album)
            if track is not None and t_score >= threshold and ambiguous:
                result.review.append(ReviewItem("ambiguous-track", sp_artist, sp_album, sp_track,
                                                album.id, f"{album.title} / {track.title} (+ same-name tracks)",
                                                round(t_score, 1), len(group)))
            elif track is not None and t_score >= threshold:
                result.album_plays[album.id][track.id].extend(group)
                prev = result.album_confidence.get(album.id, 100.0)
                result.album_confidence[album.id] = min(prev, a_score, t_score)
            elif track is not None and t_score >= review_floor:
                result.review.append(ReviewItem("low-confidence-track", sp_artist, sp_album, sp_track,
                                                album.id, f"{album.title} / {track.title}",
                                                round(t_score, 1), len(group)))
            else:
                _cross_album(result, group, sp_artist, sp_album, sp_track, candidates,
                             exclude=album.id, threshold=threshold)
            continue

        if album is not None and a_score >= review_floor:
            result.review.append(ReviewItem("low-confidence-album", sp_artist, sp_album, sp_track,
                                            album.id, f"{album.artist} / {album.title}",
                                            round(a_score, 1), len(group)))
            continue

        if not _cross_album(result, group, sp_artist, sp_album, sp_track, candidates,
                            exclude=None, threshold=threshold):
            result.unmatched_plays += len(group)
    return result


def _cross_album(result, group, sp_artist, sp_album, sp_track, candidates, *,
                 exclude, threshold) -> bool:
    """Record this track as a cross-album REVIEW item if another album by a
    matching artist has it. Returns True if anything was recorded."""
    found = False
    for album in candidates:
        if album.id == exclude:
            continue
        track, score, _ambiguous = _best_track(sp_track, album)
        if track is not None and score >= threshold:
            result.review.append(ReviewItem("cross-album", sp_artist, sp_album, sp_track,
                                            album.id, f"{album.title} / {track.title}",
                                            round(score, 1), len(group)))
            found = True
    return found
