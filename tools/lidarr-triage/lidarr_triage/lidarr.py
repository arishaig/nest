"""Minimal Lidarr v1 API client.

Endpoints and schemas were checked against Lidarr's OpenAPI spec
(src/Lidarr.Api.V1/openapi.json at v3.1.2.4938; the running
prarr lidarr-plugins build 3.1.2.4913 doesn't serve the spec). Live
responses from that build (2026-09-28) confirmed the shapes of
/album (embedded `artist` + `statistics`), /config/mediamanagement,
/importlist and /system/status; /track and /trackfile are spec-only.
Re-check if Lidarr's major/minor version changes:

  GET    /api/v1/system/status
  GET    /api/v1/album                       (all albums, with statistics)
  GET    /api/v1/album/{id}
  PUT    /api/v1/album/monitor               {albumIds: [...], monitored: bool}
  GET    /api/v1/track?artistId= | ?albumId=
  GET    /api/v1/trackfile?artistId= | ?albumId=
  DELETE /api/v1/trackfile/{id}              (routes through the Recycling Bin
                                              when one is set, else permanent —
                                              MediaFileDeletionService)
  GET    /api/v1/config/mediamanagement      (recycleBin, recycleBinCleanupDays)
  GET    /api/v1/importlist
  GET    /api/v1/system/backup
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

VERIFIED_AGAINST = "3.1.2.4913 (live responses + OpenAPI spec of v3.1.2.4938)"
DEFAULT_URL = "http://192.168.1.116:8686"  # LAN LB; lidarr.arishaig.site is behind Authelia 2FA


class LidarrError(RuntimeError):
    pass


def _parse_time(value: str) -> datetime:
    # Lidarr returns ISO-8601 with 'Z' and up to 7 fractional digits.
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        frac, _, tz = rest.partition("+")
        value = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value)


class Lidarr:
    def __init__(self, url: str | None = None, api_key: str | None = None,
                 client: httpx.Client | None = None):
        url = url or os.environ.get("LIDARR_URL") or DEFAULT_URL
        api_key = api_key or os.environ.get("LIDARR_API_KEY")
        if not api_key:
            raise LidarrError("LIDARR_API_KEY is not set (read it from vault; never commit it)")
        self._client = client or httpx.Client(
            base_url=url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=60.0,
            follow_redirects=False,  # a redirect means Authelia, not Lidarr
        )

    def close(self) -> None:
        self._client.close()

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._client.request(method, path, **kw)
        if r.is_redirect:
            raise LidarrError(f"{method} {path} redirected to {r.headers.get('location')} — "
                              "wrong URL (behind Authelia?); use the LAN LB")
        if r.status_code >= 400:
            raise LidarrError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _get(self, path: str, **params) -> object:
        return self._req("GET", path, params=params or None).json()

    # --- reads ---
    def status(self) -> dict:
        return self._get("/api/v1/system/status")

    def albums(self) -> list[dict]:
        return self._get("/api/v1/album")

    def album(self, album_id: int) -> dict:
        return self._get(f"/api/v1/album/{album_id}")

    def tracks(self, *, artist_id: int | None = None, album_id: int | None = None) -> list[dict]:
        return self._get("/api/v1/track", **_one_of(artist_id=artist_id, album_id=album_id))

    def trackfiles(self, *, artist_id: int | None = None, album_id: int | None = None) -> list[dict]:
        return self._get("/api/v1/trackfile", **_one_of(artist_id=artist_id, album_id=album_id))

    def media_management(self) -> dict:
        return self._get("/api/v1/config/mediamanagement")

    def import_lists(self) -> list[dict]:
        return self._get("/api/v1/importlist")

    def newest_backup_time(self) -> datetime | None:
        backups = self._get("/api/v1/system/backup")
        times = [_parse_time(b["time"]) for b in backups if b.get("time")]
        return max(times) if times else None

    # --- writes (apply mode only) ---
    def set_album_monitored(self, album_id: int, monitored: bool) -> None:
        self._req("PUT", "/api/v1/album/monitor",
                  json={"albumIds": [album_id], "monitored": monitored})

    def delete_trackfile(self, trackfile_id: int) -> None:
        self._req("DELETE", f"/api/v1/trackfile/{trackfile_id}")


def _one_of(*, artist_id: int | None, album_id: int | None) -> dict:
    if (artist_id is None) == (album_id is None):
        raise ValueError("pass exactly one of artist_id / album_id")
    return {"artistId": artist_id} if artist_id is not None else {"albumId": album_id}


def quality_name(trackfile: dict) -> str:
    return ((trackfile.get("quality") or {}).get("quality") or {}).get("name") or "unknown"


def is_flac(trackfile: dict) -> bool:
    return "flac" in quality_name(trackfile).lower()
