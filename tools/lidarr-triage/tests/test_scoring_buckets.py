from datetime import timedelta

import pytest

from lidarr_triage.buckets import HOLD, KEEP, PRUNE, REVIEW, decide
from lidarr_triage.history import Play
from lidarr_triage.library import LibAlbum, LibTrack
from lidarr_triage.match import match_plays
from lidarr_triage.normalize import normalize
from lidarr_triage.score import ScoreConfig, play_weight

from .conftest import NOW


def play(artist, album, track, *, days_ago=10, reason="clickrow", ms=200_000, skipped=False):
    return Play(ts=NOW - timedelta(days=days_ago), ms_played=ms, artist=artist, album=album,
                track=track, uri="spotify:track:x", reason_start=reason, reason_end="trackdone",
                skipped=skipped, incognito=False)


def album(id, artist, title, tracks):
    a = LibAlbum(id=id, artist_id=id * 100, artist=artist, title=title, monitored=True)
    for n, t in enumerate(tracks, 1):
        tid = id * 10 + n
        a.tracks.append(LibTrack(id=tid, title=t, norm=normalize(t), trackfile_id=tid * 10,
                                 quality="FLAC", flac=True))
    return a


CFG = ScoreConfig(now=NOW)


def test_decay_halves_at_half_life():
    fresh = play_weight(play("a", "b", "c", days_ago=0), CFG)
    old = play_weight(play("a", "b", "c", days_ago=int(3 * 365.25)), CFG)
    assert fresh == pytest.approx(1.0)
    assert old == pytest.approx(0.5, rel=0.01)


def test_passive_plays_weigh_less():
    assert play_weight(play("a", "b", "c", days_ago=0, reason="trackdone"), CFG) == pytest.approx(0.5)
    assert play_weight(play("a", "b", "c", days_ago=0, reason="playbtn"), CFG) == pytest.approx(1.0)


def _run(library, plays, cfg=CFG):
    real = [p for p in plays if p.is_real(cfg.min_ms)]
    return {d.album.album.id: d for d in decide(library, match_plays(real, library), cfg)}


def test_buckets():
    lib = [
        album(1, "Band", "Big Album", ["One", "Two", "Three", "Four"]),
        album(2, "Band", "One Hit", ["Hit", "Filler A", "Filler B"]),
        album(3, "Band", "Never Played", ["X", "Y"]),
        album(4, "Band", "Faint", ["Old", "Other"]),
    ]
    plays = [
        play("Band", "Big Album", "One"), play("Band", "Big Album", "Two"),
        play("Band", "Big Album (Deluxe Edition)", "Three - Remastered 2011"),
        play("Band", "One Hit", "Hit"),
        play("Band", "Faint", "Old", days_ago=365 * 12, reason="trackdone"),  # weight ~0.03
    ]
    d = _run(lib, plays)
    assert d[1].bucket == KEEP
    assert d[2].bucket == PRUNE
    assert [t.title for t in d[2].keep] == ["Hit"]
    assert sorted(t.title for t in d[2].delete) == ["Filler A", "Filler B"]
    assert d[3].bucket == HOLD and d[3].reason == "no plays"
    assert d[4].bucket == HOLD and d[4].reason == "only faint plays"


def test_skipped_and_short_plays_do_not_count():
    lib = [album(1, "Band", "LP", ["A", "B", "C"])]
    plays = [play("Band", "LP", "A", skipped=True), play("Band", "LP", "B", ms=10_000)]
    assert _run(lib, plays)[1].bucket == HOLD


def test_single_where_kept_tracks_are_everything_is_keep():
    lib = [album(1, "Band", "Single", ["Song", "Song (Instrumental)"])]
    plays = [play("Band", "Single", "Song"), play("Band", "Single", "Song (Instrumental)")]
    d = _run(lib, plays)[1]
    assert d.bucket == KEEP and d.reason == "kept tracks are the whole album"


def test_low_confidence_album_never_prunes():
    lib = [album(1, "Band", "Greatest Hits Volume One", ["Hit", "B", "C"])]
    plays = [play("Band", "Greatest Hits Vol 2", "Hit")]
    result = match_plays(plays, lib)
    assert result.review and result.review[0].kind == "low-confidence-album"
    assert _run(lib, plays)[1].bucket == HOLD


def test_cross_album_match_moves_prune_to_review():
    lib = [
        album(1, "Band", "Studio LP", ["Hit", "Deep Cut", "Filler"]),
        album(2, "Band", "Hit (Single)", ["Hit"]),
    ]
    # Played "Deep Cut" from the LP, "Filler" from a compilation Lidarr doesn't have.
    plays = [play("Band", "Studio LP", "Deep Cut"), play("Band", "Now That's Music 42", "Filler")]
    d = _run(lib, plays)
    assert d[1].bucket == REVIEW
    assert any(r.kind == "cross-album" for r in d[1].review_items)


def test_threshold_is_configurable_and_album_below_floor_ignored():
    lib = [album(1, "Band", "LP", ["A", "B", "C"])]
    plays = [play("Totally Different Artist", "Unrelated", "A")]
    result = match_plays(plays, lib)
    assert result.unmatched_plays == 1 and not result.review


def test_studio_track_wins_tie_with_live_version():
    lib = [album(1, "Band", "LP (Deluxe)", ["Song (Live)", "Song", "Other", "Filler"])]
    d = _run(lib, [play("Band", "LP (Deluxe)", "Song")])[1]
    assert d.bucket == PRUNE
    assert [t.title for t in d.keep] == ["Song"]
    assert "Song (Live)" in [t.title for t in d.delete]


def test_remaster_play_prefers_untagged_track_over_live():
    lib = [album(1, "Band", "LP", ["Song (Live)", "Song", "Other"])]
    d = _run(lib, [play("Band", "LP", "Song - 2011 Remaster")])[1]
    assert [t.title for t in d.keep] == ["Song"]


def test_unresolvable_same_name_tracks_go_to_review_not_prune():
    lib = [album(1, "Band", "LP", ["Song - Mono", "Song - Stereo", "Other"])]
    plays = [play("Band", "LP", "Song - 2011 Remaster"), play("Band", "LP", "Other")]
    result = match_plays(plays, lib)
    assert [r.kind for r in result.review] == ["ambiguous-track"]
    d = _run(lib, plays)[1]
    assert d.bucket == REVIEW
    assert "Song - Mono" in [t.title for t in d.delete]  # which is exactly why it isn't PRUNE


def test_played_track_without_file_is_review():
    a = album(1, "Band", "LP", ["Hit", "B", "C"])
    a.tracks[0].trackfile_id = None
    d = _run([a], [play("Band", "LP", "Hit")])[1]
    assert d.bucket == REVIEW and "no file" in d.reason
