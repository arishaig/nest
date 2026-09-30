import json

import pytest

from lidarr_triage.history import HistoryFormatError, load_history
from lidarr_triage.normalize import normalize, normalize_artist

from .conftest import rec


@pytest.mark.parametrize("raw,expected", [
    ("Abbey Road (Remastered 2019)", "abbey road"),
    ("Come Together - Remastered 2009", "come together"),
    ("Hey Jude - 2015 Remaster", "hey jude"),
    ("Rumours (Super Deluxe)", "rumours"),
    ("Rumours (Deluxe Edition)", "rumours"),
    ("Song (feat. Drake)", "song"),
    ("Song feat. Drake", "song"),
    ("Song [Live at Wembley]", "song"),
    ("Song - Live", "song"),
    ("Love Me Do - Mono / Remastered", "love me do"),
    ("Don't Stop Me Now", "don t stop me now"),
    ("Sigur Rós", "sigur ros"),
    ("(Live)", "live"),  # never normalized to nothing
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_artist_keeps_qualifier_words_but_drops_feat():
    assert normalize_artist("Simon & Garfunkel") == "simon and garfunkel"
    assert normalize_artist("Live") == "live"
    assert normalize_artist("Artist feat. Other") == "artist"


def test_rejects_basic_account_data_files(tmp_path):
    (tmp_path / "StreamingHistory_music_0.json").write_text(json.dumps(
        [{"endTime": "2024-01-01 10:00", "artistName": "A", "trackName": "T", "msPlayed": 1000}]))
    with pytest.raises(HistoryFormatError, match="basic 'Account data'"):
        load_history(tmp_path)


def test_rejects_records_without_skip_data(history_dir):
    bad = rec("A", "B", "C")
    del bad["skipped"]
    with pytest.raises(HistoryFormatError, match="no skipped"):
        load_history(history_dir([bad]))


def test_rejects_basic_shape_under_audio_filename(history_dir):
    with pytest.raises(HistoryFormatError, match="Extended"):
        load_history(history_dir([{"endTime": "x", "msPlayed": 5, "artistName": "A", "trackName": "T"}]))


def test_drops_non_tracks_and_reports_stats(history_dir):
    d = history_dir([
        rec("A", "Album", "One"),
        rec("A", "Album", "Two", skipped=None),
        rec(None, None, None, uri=None, episode_name="Podcast"),
    ])
    plays, stats = load_history(d)
    assert [p.track for p in plays] == ["One", "Two"]
    assert stats.non_track == 1 and stats.skipped_null == 1
    assert plays[1].skipped is False


def test_real_play_rule(history_dir):
    plays, _ = load_history(history_dir([
        rec("A", "B", "ok", ms=30_000),
        rec("A", "B", "short", ms=29_999),
        rec("A", "B", "skipped", ms=200_000, skipped=True),
    ]))
    assert [p.track for p in plays if p.is_real(30_000)] == ["ok"]
