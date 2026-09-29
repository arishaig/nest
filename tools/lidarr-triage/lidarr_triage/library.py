"""Snapshot of the Lidarr library, shaped for matching."""

from __future__ import annotations

from dataclasses import dataclass, field

from .lidarr import Lidarr, LidarrError, is_flac, quality_name
from .normalize import normalize, normalize_artist


@dataclass
class LibTrack:
    id: int
    title: str
    norm: str
    trackfile_id: int | None
    quality: str = ""
    flac: bool = False


@dataclass
class LibAlbum:
    id: int
    artist_id: int
    artist: str
    title: str
    monitored: bool
    album_mbid: str = ""
    artist_mbid: str = ""
    tracks: list[LibTrack] = field(default_factory=list)

    @property
    def norm_artist(self) -> str:
        return normalize_artist(self.artist)

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def trackfile_ids(self) -> list[int]:
        return sorted({t.trackfile_id for t in self.tracks if t.trackfile_id})


def load_library(lidarr: Lidarr, progress=None) -> list[LibAlbum]:
    """Albums that have at least one track file, with their tracks and files.

    Two calls per artist (tracks + trackfiles) instead of per album.
    """
    albums: dict[int, LibAlbum] = {}
    by_artist: dict[int, list[int]] = {}
    raw = lidarr.albums()
    # The fixtures follow the OpenAPI spec; if the live response shape differs
    # (no embedded statistics/artist), fail instead of silently reporting
    # every album as HOLD.
    if raw and not any("statistics" in a for a in raw):
        raise LidarrError("GET /api/v1/album returned no 'statistics'; API shape changed?")
    if raw and not any((a.get("artist") or {}).get("artistName") for a in raw):
        raise LidarrError("GET /api/v1/album returned no embedded artist names; API shape changed?")
    for a in raw:
        if not (a.get("statistics") or {}).get("trackFileCount"):
            continue
        artist = a.get("artist") or {}
        albums[a["id"]] = LibAlbum(
            id=a["id"],
            artist_id=a["artistId"],
            artist=artist.get("artistName") or "",
            title=a.get("title") or "",
            monitored=bool(a.get("monitored")),
            album_mbid=a.get("foreignAlbumId") or "",
            artist_mbid=artist.get("foreignArtistId") or "",
        )
        by_artist.setdefault(a["artistId"], []).append(a["id"])

    for n, artist_id in enumerate(sorted(by_artist), 1):
        if progress:
            progress(n, len(by_artist))
        files = {f["id"]: f for f in lidarr.trackfiles(artist_id=artist_id)}
        for t in lidarr.tracks(artist_id=artist_id):
            album = albums.get(t.get("albumId"))
            if album is None:
                continue
            tf_id = t.get("trackFileId") or None
            tf = files.get(tf_id) if tf_id else None
            album.tracks.append(LibTrack(
                id=t["id"],
                title=t.get("title") or "",
                norm=normalize(t.get("title") or ""),
                trackfile_id=tf_id if tf else None,
                quality=quality_name(tf) if tf else "",
                flac=is_flac(tf) if tf else False,
            ))
    return list(albums.values())
