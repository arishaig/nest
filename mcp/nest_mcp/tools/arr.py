import datetime
from typing import Literal
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

_API_VERSION = {
    "sonarr": "v3",
    "radarr": "v3",
    "lidarr": "v1",
    "prowlarr": "v1",
}


def _arr_client(service: str):
    port = _PORTS[service]
    key = _KEY_MAP[service]()
    return make_client(
        f"http://{config.arr.arr_host}:{port}",
        headers={"X-Api-Key": key},
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
    async def arr_queue(service: Literal["sonarr", "radarr", "lidarr"]) -> dict:
        """Get the current download queue for a *arr service (sonarr, radarr, or lidarr)."""
        async with _arr_client(service) as client:
            resp = await client.get(f"/api/{_API_VERSION[service]}/queue", params={"pageSize": 50, "includeUnknownSeriesItems": True})
            resp.raise_for_status()
            d = resp.json()
            records = d.get("records", [])
            return {
                "service": service,
                "total": d.get("totalRecords", 0),
                "items": [
                    {
                        "id": r.get("id"),
                        "title": r.get("title", ""),
                        "status": r.get("status", ""),
                        "tracked_status": r.get("trackedDownloadStatus", ""),
                        "messages": [m for s in r.get("statusMessages", []) for m in s.get("messages", [])][:3],
                        "size_mb": round(r.get("size", 0) / 1024 / 1024),
                        "sizeleft_mb": round(r.get("sizeleft", 0) / 1024 / 1024),
                        "timeleft": r.get("timeleft", ""),
                        "protocol": r.get("protocol", ""),
                        "indexer": r.get("indexer", ""),
                    }
                    for r in records
                ],
            }

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


def _release_rows(releases: list[dict], limit: int) -> list[dict]:
    """Compact view of Sonarr/Radarr release candidates, best first, with why each was rejected."""
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
        """[DESTRUCTIVE] Remove items from a *arr download queue (ids from arr_queue), e.g. downloads that completed but are stuck on an import warning. blocklist=True also blocklists the release so it is not grabbed again (use for bad/fake releases); remove_from_client=True also deletes the download from the download client. Re-search afterwards to get a replacement. Confirm with the user first."""
        if not queue_ids:
            raise ValueError("queue_ids is empty")
        async with _arr_client(service) as client:
            resp = await client.request(
                "DELETE", f"/api/{_API_VERSION[service]}/queue/bulk",
                params={"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower()},
                json={"ids": queue_ids},
            )
            resp.raise_for_status()
            return {"removed": queue_ids, "blocklisted": blocklist, "removed_from_client": remove_from_client}

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
    async def radarr_releases(movie_id: int, limit: int = 15) -> list[dict]:
        """Interactive search: release candidates for one movie from every indexer, approved first, each with its rejection reasons. The explanation for an empty search. Read-only: nothing is grabbed."""
        async with _arr_client("radarr") as client:
            resp = await client.get("/api/v3/release", params={"movieId": movie_id})
            resp.raise_for_status()
            return _release_rows(resp.json(), limit)

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
    async def prowlarr_search(query: str, kind: Literal["search", "tvsearch", "movie"] = "search", limit: int = 15) -> list[dict]:
        """Search every Prowlarr indexer for a release (kind: search, tvsearch or movie). Read-only: shows what the indexers have (title, size, seeders, age) without grabbing anything; use it to tell a wrong indexer or category from a release that simply does not exist."""
        async with _arr_client("prowlarr") as client:
            resp = await client.get("/api/v1/search", params={"query": query, "type": kind, "limit": limit * 3})
            resp.raise_for_status()
            rows = [
                {
                    "title": r.get("title", ""), "indexer": r.get("indexer", ""), "protocol": r.get("protocol", ""),
                    "size_mb": round((r.get("size") or 0) / 1024 / 1024), "seeders": r.get("seeders"),
                    "age_days": r.get("age"),
                    "categories": [c.get("name") for c in (r.get("categories") or [])][:3],
                }
                for r in resp.json()
            ]
            rows.sort(key=lambda r: -(r["seeders"] or 0))
            return rows[:limit]
