import datetime
from typing import Literal
import httpx
from mcp.server.mcpserver import MCPServer
from nest_mcp import config
from nest_mcp.http_client import make_client

ArrService = Literal["sonarr", "radarr", "lidarr", "prowlarr"]

_PORTS = {
    "sonarr": 30989,
    "radarr": 30878,
    "lidarr": 30686,
    "prowlarr": 30696,
}

_KEY_MAP = {
    "sonarr": lambda: config.arr.sonarr_key,
    "radarr": lambda: config.arr.radarr_key,
    "lidarr": lambda: config.arr.lidarr_key,
    "prowlarr": lambda: config.arr.prowlarr_key,
}

# arr_queue page size cap, and how many ids arr_queue_remove sends per request
# (bulk deletes of more than ~6 time out and apply only partly).
_QUEUE_MAX_PAGE_SIZE = 200
_QUEUE_REMOVE_BATCH = 4

_API_VERSION = {
    "sonarr": "v3",
    "radarr": "v3",
    "lidarr": "v1",
    "prowlarr": "v1",
}


def _arr_client(service: str, timeout: float = 10.0):
    port = _PORTS[service]
    key = _KEY_MAP[service]()
    return make_client(
        f"http://{config.arr.arr_host}:{port}",
        headers={"X-Api-Key": key},
        timeout=timeout,
    )


def _today() -> str:
    return datetime.date.today().isoformat()


def register(mcp: MCPServer) -> None:
    register_write(mcp)

    @mcp.tool()
    async def arr_status(service: ArrService) -> dict:
        """Get version and system status for a *arr service (sonarr, radarr, lidarr, or prowlarr)."""
        async with _arr_client(service) as client:
            resp = await client.get(f"/api/{_API_VERSION[service]}/system/status")
            resp.raise_for_status()
            d = resp.json()
            return {
                "service": service,
                "version": d.get("version", ""),
                "build_time": d.get("buildTime", ""),
                "startup_path": d.get("startupPath", ""),
                "app_data": d.get("appData", ""),
                "os_name": d.get("osName", ""),
            }

    @mcp.tool()
    async def arr_queue(service: Literal["sonarr", "radarr", "lidarr"], page: int = 1, page_size: int = 50) -> dict:
        """Get the current download queue for a *arr service (sonarr, radarr, or lidarr).

        Paged: `page` is 1-based, `page_size` is capped at 200, and `has_more` says whether
        another page exists. Each item carries `download_id` (the download client's id), so
        several episodes of one multi-episode download can be grouped."""
        page = max(1, page)
        page_size = max(1, min(page_size, _QUEUE_MAX_PAGE_SIZE))
        async with _arr_client(service) as client:
            resp = await client.get(
                f"/api/{_API_VERSION[service]}/queue",
                params={"page": page, "pageSize": page_size, "includeUnknownSeriesItems": True},
            )
            resp.raise_for_status()
            d = resp.json()
            records = d.get("records", [])
            total = d.get("totalRecords", 0)
            return {
                "service": service,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_more": page * page_size < total,
                "duplicates": _queue_duplicates(records),
                "items": [
                    {
                        "id": r.get("id"),
                        "download_id": r.get("downloadId", ""),
                        "title": r.get("title", ""),
                        "status": r.get("status", ""),
                        "tracked_status": r.get("trackedDownloadStatus", ""),
                        "messages": [m for s in r.get("statusMessages", []) for m in s.get("messages", [])][:3],
                        "size_mb": round(r.get("size", 0) / 1024 / 1024),
                        "sizeleft_mb": round(r.get("sizeleft", 0) / 1024 / 1024),
                        "timeleft": r.get("timeleft", ""),
                        "protocol": r.get("protocol", ""),
                        "indexer": r.get("indexer", ""),
                        "warning": _queue_warning(r),
                    }
                    for r in records
                ],
            }

    @mcp.tool()
    async def arr_health(service: ArrService) -> list[dict]:
        """Health checks of a *arr service (GET /health): failing indexers or download clients, unreachable metadata/update servers (e.g. radarr.servarr.com 521/525), missing root folders. Check this first when a *arr search hangs or times out. Empty list = healthy."""
        async with _arr_client(service) as client:
            resp = await client.get(f"/api/{_API_VERSION[service]}/health")
            resp.raise_for_status()
            return [
                {"type": h.get("type", ""), "source": h.get("source", ""), "message": h.get("message", ""), "wiki": h.get("wikiUrl", "")}
                for h in resp.json()
            ]

    @mcp.tool()
    async def arr_history(service: Literal["sonarr", "radarr", "lidarr"], limit: int = 20) -> list[dict]:
        """Get recent download history (grabs, imports, failures) for a *arr service."""
        async with _arr_client(service) as client:
            resp = await client.get(f"/api/{_API_VERSION[service]}/history", params={"pageSize": limit, "sortKey": "date", "sortDir": "desc"})
            resp.raise_for_status()
            records = resp.json().get("records", [])
            return [
                {
                    "date": r.get("date", ""),
                    "event_type": r.get("eventType", ""),
                    "source_title": r.get("sourceTitle", ""),
                    "quality": r.get("quality", {}).get("quality", {}).get("name", ""),
                    "indexer": r.get("data", {}).get("indexer", ""),
                }
                for r in records
            ]

    @mcp.tool()
    async def prowlarr_indexers() -> list[dict]:
        """List all indexers configured in Prowlarr with their enabled status and protocol."""
        async with _arr_client("prowlarr") as client:
            resp = await client.get("/api/v1/indexer")
            resp.raise_for_status()
            indexers = resp.json()
            return [
                {
                    "id": i.get("id"),
                    "name": i.get("name", ""),
                    "enabled": i.get("enable", False),
                    "protocol": i.get("protocol", ""),
                    "privacy": i.get("privacy", ""),
                    "status": i.get("status", {}).get("lastRssSyncMessage", "") if i.get("status") else "",
                }
                for i in sorted(indexers, key=lambda x: x.get("name", ""))
            ]


_MAX_SEARCH_EPISODES = 50  # one command per call keeps a typo from hammering every indexer


def _est_mbps(size_bytes: int, runtime_min: int) -> float | None:
    if not runtime_min or not size_bytes:
        return None
    return round(size_bytes * 8 / (runtime_min * 60) / 1_000_000, 1)


def _queue_warning(r: dict) -> str:
    """Flag a queue item that looks stuck: nothing left to download but still marked downloading."""
    if (r.get("size") or 0) > 0 and (r.get("sizeleft") or 0) == 0 and r.get("status") in ("downloading", "queued", "paused"):
        return "0 MB left but still not imported (status=%s): check sabnzbd_status / sabnzbd_history, or a stuck import" % r.get("status")
    return ""


def _queue_duplicates(records: list[dict]) -> list[dict]:
    """Queue items that target the same movie/episode/album, which means a re-grab loop or a double grab."""
    groups: dict[tuple, list] = {}
    for r in records:
        for key in ("movieId", "episodeId", "albumId"):
            if r.get(key):
                groups.setdefault((key, r[key]), []).append(r)
    return [
        {"target": f"{k}={v}", "queue_ids": [r.get("id") for r in rs], "titles": [r.get("title", "") for r in rs]}
        for (k, v), rs in groups.items() if len(rs) > 1
    ]


def _release_rows(releases: list[dict], limit: int, runtime_min: int = 0) -> list[dict]:
    """Compact view of Sonarr/Radarr release candidates, best first, with why each was rejected.

    With a runtime (minutes) each row also carries est_mbps, the average bitrate if the
    file's size is spread over the whole runtime (container + audio included)."""
    rows = [
        {
            "title": r.get("title", ""),
            "indexer": r.get("indexer", ""),
            "protocol": r.get("protocol", ""),
            "quality": (r.get("quality") or {}).get("quality", {}).get("name", ""),
            "size_mb": round((r.get("size") or 0) / 1024 / 1024),
            "age_days": r.get("age"),
            "seeders": r.get("seeders"),
            "approved": not r.get("rejected", False),
            "rejections": r.get("rejections") or [],
            "guid": r.get("guid", ""),
            "indexer_id": r.get("indexerId"),
            "custom_format_score": r.get("customFormatScore", 0),
            "custom_formats": [c.get("name", "") for c in (r.get("customFormats") or [])],
            "quality_weight": r.get("qualityWeight"),
            "est_mbps": _est_mbps(r.get("size") or 0, runtime_min),
        }
        for r in releases
    ]
    rows.sort(key=lambda r: (not r["approved"], -(r["seeders"] or 0)))
    return rows[:limit]


def register_write(mcp: MCPServer) -> None:
    """Sonarr, Radarr and Prowlarr tools beyond status: library lookup, add, and search.

    Deliberately no delete-series or delete-movie tool: removing from the library can
    delete files and nobody should be one confirmed prompt away from that.
    """

    # ── Shared ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def arr_config(service: Literal["sonarr", "radarr"]) -> dict:
        """Quality profiles, root folders and tags of Sonarr or Radarr: the IDs and paths that sonarr_add_series and radarr_add_movie need."""
        v = _API_VERSION[service]
        async with _arr_client(service) as client:
            out = {}
            for key, path in (("quality_profiles", "qualityprofile"), ("root_folders", "rootfolder"), ("tags", "tag")):
                resp = await client.get(f"/api/{v}/{path}")
                resp.raise_for_status()
                out[key] = [
                    {k: i.get(k) for k in ("id", "name", "label", "path", "freeSpace") if k in i}
                    for i in resp.json()
                ]
            return out

    # ── Sonarr ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def sonarr_lookup(term: str) -> list[dict]:
        """Search for a TV series to add (TVDB-backed). `term` is a title, or 'tvdb:12345'. Shows whether each hit is already in the Sonarr library (in_library, series_id)."""
        async with _arr_client("sonarr") as client:
            resp = await client.get("/api/v3/series/lookup", params={"term": term})
            resp.raise_for_status()
            return [
                {
                    "title": s.get("title", ""), "year": s.get("year"), "tvdb_id": s.get("tvdbId"),
                    "imdb_id": s.get("imdbId", ""), "status": s.get("status", ""),
                    "seasons": len(s.get("seasons") or []), "overview": (s.get("overview") or "")[:200],
                    "in_library": bool(s.get("id")), "series_id": s.get("id"),
                }
                for s in resp.json()[:15]
            ]

    @mcp.tool()
    async def sonarr_series(search: str = "") -> list[dict]:
        """List series in the Sonarr library with episode counts (have/total), optionally filtered by a case-insensitive title substring. Use series_id with the other sonarr_* tools."""
        async with _arr_client("sonarr") as client:
            resp = await client.get("/api/v3/series")
            resp.raise_for_status()
            needle = search.lower()
            rows = []
            for s in resp.json():
                if needle and needle not in (s.get("title") or "").lower():
                    continue
                st = s.get("statistics") or {}
                rows.append({
                    "series_id": s.get("id"), "title": s.get("title", ""), "year": s.get("year"),
                    "monitored": s.get("monitored", False), "status": s.get("status", ""),
                    "episodes_have": st.get("episodeFileCount", 0), "episodes_total": st.get("episodeCount", 0),
                    "path": s.get("path", ""),
                })
            return sorted(rows, key=lambda r: r["title"])

    @mcp.tool()
    async def sonarr_missing_episodes(series_id: int, season: int = -1) -> list[dict]:
        """Episodes of a series that have aired but have no file, with monitored flags. Optionally one season. These are what sonarr_search_episodes can go and fetch."""
        async with _arr_client("sonarr") as client:
            resp = await client.get("/api/v3/episode", params={"seriesId": series_id})
            resp.raise_for_status()
            return [
                {
                    "episode_id": e["id"], "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
                    "title": e.get("title", ""), "air_date": e.get("airDate", ""), "monitored": e.get("monitored", False),
                }
                for e in sorted(resp.json(), key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)))
                if not e.get("hasFile") and (season < 0 or e.get("seasonNumber") == season)
                and e.get("airDate", "9999") <= _today()
            ]

    @mcp.tool()
    async def sonarr_add_series(
        tvdb_id: int, quality_profile_id: int, root_folder: str, monitor: str = "all",
        search: bool = True, series_type: str = "standard",
    ) -> dict:
        """[DESTRUCTIVE] Add a series to Sonarr by TVDB id (find it with sonarr_lookup; profile ids and root folders from arr_config) and, with search=True, start looking for every wanted episode. monitor: all, future, missing, existing, firstSeason, lastSeason, pilot, none. Adding is not deletable from here. Confirm with the user first."""
        async with _arr_client("sonarr") as client:
            resp = await client.get("/api/v3/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
            resp.raise_for_status()
            hits = [s for s in resp.json() if s.get("tvdbId") == tvdb_id]
            if not hits:
                raise ValueError(f"Sonarr has no series with tvdb id {tvdb_id}")
            body = {
                **hits[0], "qualityProfileId": quality_profile_id, "rootFolderPath": root_folder,
                "monitored": monitor != "none", "seasonFolder": True, "seriesType": series_type,
                "addOptions": {"monitor": monitor, "searchForMissingEpisodes": search, "searchForCutoffUnmetEpisodes": False},
            }
            added = await client.post("/api/v3/series", json=body)
            added.raise_for_status()
            d = added.json()
            return {"series_id": d.get("id"), "title": d.get("title"), "path": d.get("path"), "searching": search}

    @mcp.tool()
    async def sonarr_search_episodes(
        series_id: int, episode_ids: list[int] | None = None, season: int = -1, missing: bool = False,
    ) -> dict:
        """[DESTRUCTIVE] Make Sonarr monitor and search for specific episodes (episode_ids from sonarr_missing_episodes), a whole season of a series, or every aired-but-missing episode of it (missing=True). Capped at 50 episodes per call; the grab happens in the background, watch arr_queue. Use sonarr_releases first to see why a search finds nothing. Confirm with the user first."""
        async with _arr_client("sonarr") as client:
            ids = list(episode_ids or [])
            if missing or season >= 0:
                resp = await client.get("/api/v3/episode", params={"seriesId": series_id})
                resp.raise_for_status()
                for e in resp.json():
                    wanted = (missing and not e.get("hasFile") and e.get("airDate", "9999") <= _today()) \
                        or (season >= 0 and e.get("seasonNumber") == season)
                    if wanted and e["id"] not in ids:
                        ids.append(e["id"])
            if not ids:
                raise ValueError("Nothing to search: pass episode_ids, a season, or missing=True")
            if len(ids) > _MAX_SEARCH_EPISODES:
                raise ValueError(f"{len(ids)} episodes selected; search at most {_MAX_SEARCH_EPISODES} per call")
            mon = await client.put("/api/v3/episode/monitor", json={"episodeIds": ids, "monitored": True})
            mon.raise_for_status()
            cmd = await client.post("/api/v3/command", json={"name": "EpisodeSearch", "episodeIds": ids})
            cmd.raise_for_status()
            return {"searching": len(ids), "episode_ids": ids, "command_id": cmd.json().get("id")}

    @mcp.tool()
    async def sonarr_releases(episode_id: int, limit: int = 15) -> list[dict]:
        """Interactive search: release candidates for one episode from every indexer, approved first, each with its rejection reasons (e.g. 'Not an upgrade', 'Unknown Series', quality not wanted). The explanation for an empty search. Read-only: nothing is grabbed."""
        async with _arr_client("sonarr") as client:
            resp = await client.get("/api/v3/release", params={"episodeId": episode_id})
            resp.raise_for_status()
            return _release_rows(resp.json(), limit)

    @mcp.tool()
    async def sonarr_episode_files(series_id: int, limit: int = 200) -> dict:
        """Files Sonarr has imported for a series: folder path, per-file relative path, size, quality and release group, plus the files per season. Use it to check what is really on disk against what the series is called (e.g. a folder named for the wrong year). Read-only."""
        async with _arr_client("sonarr") as client:
            series = await client.get(f"/api/v3/series/{series_id}")
            series.raise_for_status()
            files = await client.get("/api/v3/episodefile", params={"seriesId": series_id})
            files.raise_for_status()
            rows = sorted(files.json(), key=lambda f: (f.get("seasonNumber", 0), f.get("relativePath", "")))
            sd = series.json()
            by_season: dict = {}
            for f in rows:
                by_season[f.get("seasonNumber")] = by_season.get(f.get("seasonNumber"), 0) + 1
            return {
                "series": f"{sd.get('title')} ({sd.get('year')})", "tvdb_id": sd.get("tvdbId"), "path": sd.get("path", ""),
                "files": len(rows), "files_by_season": by_season,
                "items": [
                    {
                        "relative_path": f.get("relativePath", ""), "size_mb": round((f.get("size") or 0) / 1024 / 1024),
                        "quality": (f.get("quality") or {}).get("quality", {}).get("name", ""),
                        "release_group": f.get("releaseGroup", ""),
                    }
                    for f in rows[:limit]
                ],
            }

    @mcp.tool()
    async def sonarr_set_series_path(series_id: int, path: str, move_files: bool = True) -> dict:
        """[DESTRUCTIVE] Change a series' folder in Sonarr. With move_files=True Sonarr renames/moves the existing folder on disk to `path` (the safe way to fix a wrongly named folder: Sonarr keeps tracking the files). `path` must sit under one of Sonarr's root folders (see arr_config). Jellyfin and Tunarr still need a rescan afterwards. Confirm with the user first."""
        async with _arr_client("sonarr") as client:
            roots = await client.get("/api/v3/rootfolder")
            roots.raise_for_status()
            allowed = [r.get("path", "").rstrip("/") for r in roots.json()]
            if not any(path.rstrip("/").startswith(root + "/") for root in allowed if root):
                raise ValueError(f"{path!r} is not under a Sonarr root folder ({', '.join(allowed)})")
            cur = await client.get(f"/api/v3/series/{series_id}")
            cur.raise_for_status()
            series = cur.json()
            old = series.get("path", "")
            if old.rstrip("/") == path.rstrip("/"):
                return {"series_id": series_id, "path": old, "changed": False}
            put = await client.put(f"/api/v3/series/{series_id}", params={"moveFiles": str(move_files).lower()},
                                   json={**series, "path": path})
            put.raise_for_status()
            return {"series_id": series_id, "title": series.get("title"), "old_path": old,
                    "path": put.json().get("path"), "moved_files": move_files, "changed": True}

    @mcp.tool()
    async def sonarr_refresh_series(series_id: int, rescan: bool = False) -> dict:
        """Make Sonarr refresh a series' metadata from TheTVDB (default) or rescan its folder on disk for new/removed files (rescan=True; use after renaming or moving a folder outside Sonarr). Returns the command id; it runs in the background."""
        async with _arr_client("sonarr") as client:
            name = "RescanSeries" if rescan else "RefreshSeries"
            cmd = await client.post("/api/v3/command", json={"name": name, "seriesId": series_id})
            cmd.raise_for_status()
            return {"command": name, "series_id": series_id, "command_id": cmd.json().get("id")}

    @mcp.tool()
    async def arr_queue_remove(
        service: Literal["sonarr", "radarr", "lidarr"], queue_ids: list[int],
        blocklist: bool = False, remove_from_client: bool = True,
    ) -> dict:
        """[DESTRUCTIVE] Remove items from a *arr download queue (ids from arr_queue), e.g. downloads that completed but are stuck on an import warning. blocklist=True also blocklists the release so it is not grabbed again (use for bad/fake releases); remove_from_client=True also deletes the download from the download client. Large lists are sent in small batches, and ids that are already gone count as success (reported in `already_gone`). If a batch fails, the error says what was removed so far. Re-search afterwards to get a replacement. Confirm with the user first."""
        if not queue_ids:
            raise ValueError("queue_ids is empty")
        removed: list[int] = []
        already_gone: list[int] = []
        base = f"/api/{_API_VERSION[service]}/queue"
        params = {"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower()}
        async with _arr_client(service) as client:
            for start in range(0, len(queue_ids), _QUEUE_REMOVE_BATCH):
                chunk = queue_ids[start:start + _QUEUE_REMOVE_BATCH]
                try:
                    resp = await client.request("DELETE", f"{base}/bulk", params=params, json={"ids": chunk})
                    if resp.status_code == 404:
                        # Some id in the batch is already gone; go one by one so the rest still get removed.
                        for qid in chunk:
                            one = await client.request("DELETE", f"{base}/{qid}", params=params)
                            if one.status_code == 404:
                                already_gone.append(qid)
                            else:
                                one.raise_for_status()
                                removed.append(qid)
                        continue
                    resp.raise_for_status()
                    removed.extend(chunk)
                except httpx.HTTPError as e:
                    raise RuntimeError(
                        f"Stopped on batch {chunk}: {e}. Removed so far: {removed}; already gone: {already_gone}; "
                        f"not attempted: {queue_ids[start + len(chunk):]}. The failed batch may be partly applied, "
                        "so re-check arr_queue before retrying."
                    ) from e
        return {
            "removed": removed, "already_gone": already_gone,
            "blocklisted": blocklist, "removed_from_client": remove_from_client,
        }

    # ── Radarr ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def radarr_lookup(term: str) -> list[dict]:
        """Search for a movie to add (TMDB-backed). `term` is a title (add the year to narrow it), or 'tmdb:123' / 'imdb:tt123'. Shows whether each hit is already in Radarr (in_library, movie_id)."""
        async with _arr_client("radarr") as client:
            resp = await client.get("/api/v3/movie/lookup", params={"term": term})
            resp.raise_for_status()
            return [
                {
                    "title": m.get("title", ""), "year": m.get("year"), "tmdb_id": m.get("tmdbId"),
                    "imdb_id": m.get("imdbId", ""), "overview": (m.get("overview") or "")[:200],
                    "in_library": bool(m.get("id")), "movie_id": m.get("id"),
                }
                for m in resp.json()[:15]
            ]

    @mcp.tool()
    async def radarr_movies(search: str = "", missing_only: bool = False) -> list[dict]:
        """List movies in Radarr (title substring filter; missing_only=True for monitored movies without a file). Use movie_id with the other radarr_* tools."""
        async with _arr_client("radarr") as client:
            resp = await client.get("/api/v3/movie")
            resp.raise_for_status()
            needle = search.lower()
            return sorted(
                (
                    {
                        "movie_id": m.get("id"), "title": m.get("title", ""), "year": m.get("year"),
                        "monitored": m.get("monitored", False), "has_file": m.get("hasFile", False),
                        "status": m.get("status", ""),
                    }
                    for m in resp.json()
                    if (not needle or needle in (m.get("title") or "").lower())
                    and (not missing_only or (m.get("monitored") and not m.get("hasFile")))
                ),
                key=lambda r: r["title"],
            )

    @mcp.tool()
    async def radarr_add_movie(tmdb_id: int, quality_profile_id: int, root_folder: str, search: bool = True) -> dict:
        """[DESTRUCTIVE] Add a movie to Radarr by TMDB id (find it with radarr_lookup; profile ids and root folders from arr_config) and, with search=True, start looking for it. Confirm with the user first."""
        async with _arr_client("radarr") as client:
            resp = await client.get("/api/v3/movie/lookup", params={"term": f"tmdb:{tmdb_id}"})
            resp.raise_for_status()
            hits = [m for m in resp.json() if m.get("tmdbId") == tmdb_id]
            if not hits:
                raise ValueError(f"Radarr has no movie with tmdb id {tmdb_id}")
            body = {
                **hits[0], "qualityProfileId": quality_profile_id, "rootFolderPath": root_folder,
                "monitored": True, "minimumAvailability": "released",
                "addOptions": {"searchForMovie": search},
            }
            added = await client.post("/api/v3/movie", json=body)
            added.raise_for_status()
            d = added.json()
            return {"movie_id": d.get("id"), "title": d.get("title"), "path": d.get("path"), "searching": search}

    @mcp.tool()
    async def radarr_search_movie(movie_id: int) -> dict:
        """[DESTRUCTIVE] Make Radarr monitor a movie already in its library and search indexers for it now (e.g. one whose file went missing). The grab happens in the background, watch arr_queue. Use radarr_releases first to see why a search finds nothing. Confirm with the user first."""
        async with _arr_client("radarr") as client:
            resp = await client.get(f"/api/v3/movie/{movie_id}")
            resp.raise_for_status()
            movie = resp.json()
            if not movie.get("monitored"):
                put = await client.put(f"/api/v3/movie/{movie_id}", json={**movie, "monitored": True})
                put.raise_for_status()
            cmd = await client.post("/api/v3/command", json={"name": "MoviesSearch", "movieIds": [movie_id]})
            cmd.raise_for_status()
            return {"searching": movie.get("title"), "movie_id": movie_id, "command_id": cmd.json().get("id")}

    @mcp.tool()
    async def radarr_releases(movie_id: int, limit: int = 15, timeout_s: int = 120) -> list[dict] | dict:
        """Interactive search: release candidates for one movie from every indexer, approved first, each with its rejection reasons, `guid` + `indexer_id` (the handles radarr_grab needs), custom-format score/names, quality weight and `est_mbps` (size / the movie's runtime). Interactive searches query every indexer and can legitimately take over a minute: `timeout_s` (default 120, max 600) is how long to wait. On timeout you get an `error` object, not an exception (then check arr_health and Prowlarr). Read-only: nothing is grabbed."""
        timeout_s = max(10, min(timeout_s, 600))
        try:
            async with _arr_client("radarr", timeout=float(timeout_s)) as client:
                # Runtime is only for est_mbps; never let a failed lookup block the search itself.
                movie = await client.get(f"/api/v3/movie/{movie_id}")
                runtime = int((movie.json().get("runtime") or 0)) if movie.is_success else 0
                resp = await client.get("/api/v3/release", params={"movieId": movie_id})
                resp.raise_for_status()
                return _release_rows(resp.json(), limit, runtime)
        except httpx.TimeoutException:
            return {"error": (
                f"Radarr accepted the search but didn't respond within {timeout_s}s. "
                "Check arr_health(radarr) (metadata/indexer failures), prowlarr_health, and k8s_pod_logs namespace=media; "
                "prowlarr_search can find releases while Radarr is wedged."
            )}

    @mcp.tool()
    async def radarr_grab(guid: str, indexer_id: int) -> dict:
        """[DESTRUCTIVE] Grab one release from radarr_releases (its `guid` + `indexer_id`) and send it to Radarr's download client. Radarr only keeps search results in its cache for about 30 minutes, so call this soon after radarr_releases (an expired cache gives a clear error: search again). Radarr tracks ONE file per movie: if the import is accepted it REPLACES the current file. A lighter file is a downgrade, so Radarr usually rejects it as "Not an upgrade" and it sits in arr_queue until radarr_manual_import(force=True). To keep the current file and add a second version, use prowlarr_grab + media_place_version instead. Watch progress with sabnzbd_queue / arr_queue. Confirm with the user first."""
        async with _arr_client("radarr", timeout=60.0) as client:
            resp = await client.post("/api/v3/release", json={"guid": guid, "indexerId": indexer_id})
            if resp.status_code == 404:
                return {"error": "Radarr no longer has that release in its search cache (about 30 minutes). Run radarr_releases again and grab right away."}
            resp.raise_for_status()
            d = resp.json()
            return {"grabbed": d.get("title", ""), "indexer": d.get("indexer", ""), "size_mb": round((d.get("size") or 0) / 1024 / 1024),
                    "replaces_current_file": True, "next": "watch sabnzbd_queue / arr_queue"}

    @mcp.tool()
    async def radarr_rename_files(movie_id: int, apply: bool = False) -> dict:
        """[DESTRUCTIVE when apply=True] Preview (default) or run Radarr's rename for one movie's tracked file, so Radarr's own file name follows its naming format. Needed before media_place_version when the existing file's name does not follow the folder name (Jellyfin only groups versions named `Name (Year)` + optional ` - label`). The preview lists current -> new paths, and reports Radarr's file and folder formats, whether 'Rename Movies' is enabled (if off the preview is empty), and whether the format would produce names Jellyfin groups. Changing those Radarr settings is the user's decision, not this tool's. Confirm with the user before apply=True."""
        async with _arr_client("radarr", timeout=60.0) as client:
            naming = await client.get("/api/v3/config/naming")
            naming.raise_for_status()
            nd = naming.json()
            fmt, folder_fmt = nd.get("standardMovieFormat", ""), nd.get("movieFolderFormat", "")
            rest = fmt[len(folder_fmt):].strip() if folder_fmt and fmt.lower().startswith(folder_fmt.lower()) else None
            # Same test Jellyfin applies to a file name vs its folder: nothing, or "-", "_", "." or "[" after it.
            ok = rest is not None and (rest == "" or rest[0] in "-_.[")
            prev = await client.get("/api/v3/rename", params={"movieId": movie_id})
            prev.raise_for_status()
            changes = [{"movie_file_id": r.get("movieFileId"), "from": r.get("existingPath", ""), "to": r.get("newPath", "")} for r in prev.json()]
            out = {"naming_format": fmt, "folder_format": folder_fmt, "rename_movies_enabled": nd.get("renameMovies", False),
                   "format_groups_in_jellyfin": ok, "changes": changes, "applied": False}
            if not nd.get("renameMovies", False):
                out["warning"] = ("Radarr's 'Rename Movies' is OFF, so it keeps scene file names and the preview stays empty. "
                                  "Turning it on (Settings > Media Management) is a global setting that affects every future import: the user's call.")
            if not ok:
                out["warning2"] = ("The file format does not continue the folder format with ' - ', '[', '_' or '.', so renamed files would NOT group as Jellyfin versions; "
                                   "change the movie file format first, e.g. '{Movie CleanTitle} ({Release Year}) - {Quality Full}'.")
            if not apply:
                return out
            if not changes:
                return {**out, "note": "Nothing to rename: the file already matches Radarr's naming format."}
            cmd = await client.post("/api/v3/command", json={
                "name": "RenameFiles", "movieId": movie_id, "files": [c["movie_file_id"] for c in changes]})
            cmd.raise_for_status()
            return {**out, "applied": True, "command_id": cmd.json().get("id"),
                    "next": "jellyfin_refresh with the folder in created_paths/deleted_paths"}

    @mcp.tool()
    async def radarr_manual_import(
        movie_id: int, folder: str, import_mode: Literal["move", "copy"] = "move", apply: bool = False, force: bool = False,
    ) -> dict:
        """[DESTRUCTIVE when apply=True] The "replace" path: have Radarr import a finished download as the movie's tracked file. `folder` is the completed download folder as Radarr sees it (/data/usenet/complete/...; see sabnzbd_history `storage`). apply=False (default) lists what Radarr would import and why anything is rejected. apply=True imports every candidate without rejections using import_mode (move or copy), replacing the movie's current file. A lighter file is a downgrade and is rejected as "Not an upgrade": force=True also imports candidates whose ONLY rejections are upgrade-related (other rejections, e.g. sample or unknown movie, are never forced). To keep the current file as well, use media_place_version instead. Confirm with the user first."""
        if ".." in folder.split("/") or not folder.startswith("/data/usenet/"):
            raise ValueError("folder must be a completed-download path under /data/usenet/ (see sabnzbd_history storage)")
        async with _arr_client("radarr", timeout=120.0) as client:
            resp = await client.get("/api/v3/manualimport", params={"folder": folder, "movieId": movie_id, "filterExistingFiles": "true"})
            resp.raise_for_status()
            cands = resp.json()
            rows = [{
                "path": c.get("path", ""), "size_gb": round((c.get("size") or 0) / 1024**3, 2),
                "quality": (c.get("quality") or {}).get("quality", {}).get("name", ""),
                "release_group": c.get("releaseGroup", ""),
                "rejections": [r.get("reason", "") for r in (c.get("rejections") or [])],
            } for c in cands]
            if not apply:
                return {"candidates": rows, "import_mode": import_mode, "applied": False}
            def _forceable(c: dict) -> bool:
                rej = [r.get("reason", "").lower() for r in (c.get("rejections") or [])]
                return not rej or (force and all("upgrade" in r for r in rej))

            ok = [c for c in cands if _forceable(c)]
            if not ok:
                return {"candidates": rows, "applied": False, "error": "Every candidate has rejections; nothing to import." + ("" if force else " If they are 'Not an upgrade' (a deliberate downgrade), retry with force=True.")}
            files = [{k: v for k, v in {
                "path": c.get("path"), "movieId": movie_id, "quality": c.get("quality"), "languages": c.get("languages"),
                "releaseGroup": c.get("releaseGroup"), "downloadId": c.get("downloadId"), "indexerFlags": c.get("indexerFlags"),
            }.items() if v is not None} for c in ok]
            cmd = await client.post("/api/v3/command", json={"name": "ManualImport", "importMode": import_mode, "files": files})
            cmd.raise_for_status()
            return {"candidates": rows, "applied": True, "importing": [f["path"] for f in files],
                    "import_mode": import_mode, "command_id": cmd.json().get("id")}

    # ── Prowlarr (read-only) ──────────────────────────────────────────────────

    @mcp.tool()
    async def prowlarr_health() -> dict:
        """Prowlarr health warnings (indexers failing, disabled by repeated errors, app sync problems) plus per-indexer failure state: why searches quietly return nothing."""
        async with _arr_client("prowlarr") as client:
            health = await client.get("/api/v1/health")
            health.raise_for_status()
            status = await client.get("/api/v1/indexerstatus")
            status.raise_for_status()
            names = {}
            idx = await client.get("/api/v1/indexer")
            idx.raise_for_status()
            names = {i["id"]: i.get("name", "") for i in idx.json()}
            return {
                "health": [{"type": h.get("type"), "source": h.get("source"), "message": h.get("message")} for h in health.json()],
                "indexer_failures": [
                    {"indexer": names.get(s.get("indexerId"), s.get("indexerId")),
                     "disabled_till": s.get("disabledTill", ""), "most_recent_failure": s.get("mostRecentFailure", "")}
                    for s in status.json()
                ],
            }

    @mcp.tool()
    async def prowlarr_search(
        query: str, kind: Literal["search", "tvsearch", "movie"] = "search", limit: int = 15, runtime_min: int = 0,
        protocol: Literal["any", "usenet", "torrent"] = "any",
    ) -> list[dict]:
        """Search every Prowlarr indexer for a release (kind: search, tvsearch or movie). Read-only: shows what the indexers have (title, size, seeders, age, `guid` + `indexer_id` for prowlarr_grab) without grabbing anything; use it to tell a wrong indexer or category from a release that simply does not exist, or as the fallback when radarr_releases hangs. Pass `runtime_min` (the movie's runtime) to get `est_mbps` per result. `protocol="usenet"` drops torrent results (the only ones prowlarr_grab can use; torrent guids are also huge magnet links). Download URLs are deliberately not shown (they carry an API key)."""
        async with _arr_client("prowlarr", timeout=60.0) as client:
            resp = await client.get("/api/v1/search", params={"query": query, "type": kind, "limit": limit * 3})
            resp.raise_for_status()
            rows = [
                {
                    "title": r.get("title", ""), "indexer": r.get("indexer", ""), "protocol": r.get("protocol", ""),
                    "size_mb": round((r.get("size") or 0) / 1024 / 1024), "seeders": r.get("seeders"),
                    "age_days": r.get("age"),
                    "categories": [c.get("name") for c in (r.get("categories") or [])][:3],
                    "guid": r.get("guid", ""), "indexer_id": r.get("indexerId"),
                    "est_mbps": _est_mbps(r.get("size") or 0, runtime_min),
                }
                for r in resp.json()
                if protocol == "any" or r.get("protocol") == protocol
            ]
            rows.sort(key=lambda r: -(r["seeders"] or 0))
            return rows[:limit]

    @mcp.tool()
    async def prowlarr_grab(guid: str, indexer_id: int, protocol: Literal["usenet", "torrent"] = "usenet") -> dict:
        """[DESTRUCTIVE] Send one prowlarr_search result (its `guid` + `indexer_id`) to Prowlarr's SABnzbd download client WITHOUT involving Radarr: the fallback when radarr_releases hangs, and the first half of "add a second version" (then media_place_version once it completes). Usenet only: pass the result's `protocol`; torrent releases (and magnet guids) are refused, because this path has no torrent client. Refuses unless Prowlarr has an enabled SABnzbd client whose category is not one Radarr/Sonarr watch (otherwise the arrs would import it over the current file): set a dedicated category such as `manual` on the Prowlarr client (Settings > Download Clients). Watch with sabnzbd_queue / sabnzbd_history (filter category). Confirm with the user first."""
        if protocol != "usenet" or guid.startswith("magnet:"):
            return {"error": "prowlarr_grab is usenet-only (SABnzbd); this release is a torrent. Pick a usenet result (prowlarr_search protocol=\"usenet\")."}
        async with _arr_client("prowlarr", timeout=60.0) as pclient:
            dc = await pclient.get("/api/v1/downloadclient")
            dc.raise_for_status()
            sab = [c for c in dc.json() if c.get("enable") and "sabnzbd" in (c.get("implementation") or c.get("name") or "").lower()]
            if not sab:
                return {"error": "Prowlarr has no enabled SABnzbd download client. Add one in Prowlarr (Settings > Download Clients) with a dedicated category such as `manual`, then retry."}
            cat = ""
            for f in sab[0].get("fields", []):
                if f.get("name") in ("category", "movieCategory"):
                    cat = f.get("value") or ""
            watched: set[str] = set()
            for svc in ("radarr", "sonarr"):
                async with _arr_client(svc) as aclient:
                    r = await aclient.get(f"/api/{_API_VERSION[svc]}/downloadclient")
                    if r.is_success:
                        for c in r.json():
                            for f in c.get("fields", []):
                                if f.get("name") in ("movieCategory", "tvCategory", "category") and f.get("value"):
                                    watched.add(f["value"])
            if not cat or cat in watched:
                return {"error": f"Prowlarr's SABnzbd client uses category {cat or '(none)'!r}, which Radarr/Sonarr also watch ({sorted(watched)}). "
                                 "Give the Prowlarr client its own category (e.g. `manual`) so the arrs don't import this download over the current file."}
            resp = await pclient.post("/api/v1/search", json={"guid": guid, "indexerId": indexer_id})
            if resp.status_code == 404:
                return {"error": "Prowlarr no longer knows that release; run prowlarr_search again and grab right away."}
            resp.raise_for_status()
            return {"sent_to": sab[0].get("name", "SABnzbd"), "category": cat, "next": "watch sabnzbd_queue / sabnzbd_history (category filter)"}
