"""Synthetic fixtures only — never real listening history."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def rec(artist, album, track, *, days_ago=10, ms=200_000, skipped=False, reason_start="clickrow",
        uri="spotify:track:x", **extra):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = {
        "ts": ts, "ms_played": ms, "skipped": skipped, "reason_start": reason_start,
        "reason_end": "trackdone", "incognito_mode": False, "spotify_track_uri": uri,
        "master_metadata_track_name": track, "master_metadata_album_artist_name": artist,
        "master_metadata_album_album_name": album,
    }
    r.update(extra)
    return r


@pytest.fixture
def history_dir(tmp_path):
    def write(records, name="Streaming_History_Audio_2024.json"):
        (tmp_path / name).write_text(json.dumps(records), encoding="utf-8")
        return tmp_path
    return write


def lidarr_album(id, artist, title, *, artist_id=None, monitored=True, files=1):
    artist_id = artist_id or id * 100
    return {"id": id, "artistId": artist_id, "title": title, "monitored": monitored,
            "foreignAlbumId": f"mb-album-{id}",
            "artist": {"artistName": artist, "foreignArtistId": f"mb-artist-{artist_id}"},
            "statistics": {"trackFileCount": files}}


def lidarr_track(id, album_id, title, trackfile_id):
    return {"id": id, "albumId": album_id, "title": title, "trackFileId": trackfile_id or 0,
            "hasFile": bool(trackfile_id)}


def lidarr_file(id, album_id, quality="FLAC", path=None):
    return {"id": id, "albumId": album_id, "path": path or f"/data/media/music/a/{id}.flac",
            "size": 1000 + id, "quality": {"quality": {"id": 1, "name": quality}},
            "dateAdded": "2024-01-01T00:00:00Z"}
