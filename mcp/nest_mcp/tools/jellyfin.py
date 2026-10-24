from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    # Jellyfin 12 disables the legacy X-Emby-Token header by default.
    return {"Authorization": f'MediaBrowser Token="{config.jellyfin.key}"'}


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def jellyfin_sessions() -> list[dict]:
        """List active Jellyfin playback sessions: who is watching what and their progress."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/Sessions")
            resp.raise_for_status()
            sessions = resp.json()
            active = [s for s in sessions if s.get("NowPlayingItem")]
            return [
                {
                    "user": s.get("UserName", ""),
                    "client": s.get("Client", ""),
                    "device": s.get("DeviceName", ""),
                    "playing": s.get("NowPlayingItem", {}).get("Name", ""),
                    "type": s.get("NowPlayingItem", {}).get("Type", ""),
                    "series": s.get("NowPlayingItem", {}).get("SeriesName", ""),
                    "position_pct": round(
                        s.get("PlayState", {}).get("PositionTicks", 0) /
                        max(s.get("NowPlayingItem", {}).get("RunTimeTicks", 1), 1) * 100, 1
                    ),
                    "is_paused": s.get("PlayState", {}).get("IsPaused", False),
                    "transcoding": s.get("TranscodingInfo") is not None,
                }
                for s in active
            ]

    @mcp.tool()
    async def jellyfin_library_stats() -> list[dict]:
        """Get item counts per Jellyfin library (movies, shows, episodes, music, etc.)."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/Library/VirtualFolders")
            resp.raise_for_status()
            folders = resp.json()
            result = []
            for folder in folders:
                counts_resp = await client.get(
                    "/Items/Counts",
                    params={"parentId": folder.get("ItemId", "")},
                )
                counts = counts_resp.json() if counts_resp.is_success else {}
                result.append({
                    "name": folder.get("Name", ""),
                    "type": folder.get("CollectionType", ""),
                    "locations": folder.get("Locations", []),
                    "movie_count": counts.get("MovieCount", 0),
                    "series_count": counts.get("SeriesCount", 0),
                    "episode_count": counts.get("EpisodeCount", 0),
                    "song_count": counts.get("SongCount", 0),
                    "album_count": counts.get("AlbumCount", 0),
                    "book_count": counts.get("BookCount", 0),
                })
            return result

    @mcp.tool()
    async def jellyfin_refresh(
        series: str = "", replace_metadata: bool = False,
        created_paths: list[str] | None = None, deleted_paths: list[str] | None = None,
    ) -> dict:
        """[DESTRUCTIVE] Ask Jellyfin to rescan. With no `series` it scans every library for new/changed/removed files (like the dashboard's Scan All Libraries). With a series title it deep-refreshes every series item with that exact title; replace_metadata=True discards its current metadata and re-identifies it from the folder name (use after renaming or moving a series folder). To tell Jellyfin about moved or renamed folders without a full scan, pass `created_paths` / `deleted_paths` (absolute paths as Jellyfin sees them, e.g. /data/media/tv/Show (1959)); it then scans just those paths, the way Sonarr's Jellyfin connection does. Returns immediately; the scan runs in the background. Confirm with the user first."""
        async with make_client(config.jellyfin.url, headers=_headers(), timeout=60.0) as client:
            if created_paths or deleted_paths:
                updates = [{"Path": p, "UpdateType": "Deleted"} for p in deleted_paths or []] + \
                    [{"Path": p, "UpdateType": "Created"} for p in created_paths or []]
                resp = await client.post("/Library/Media/Updated", json={"Updates": updates})
                resp.raise_for_status()
                return {"notified": updates}
            if not series:
                resp = await client.post("/Library/Refresh")
                resp.raise_for_status()
                return {"refreshing": "all libraries"}
            found = await client.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true", "searchTerm": series})
            found.raise_for_status()
            hits = [i for i in found.json().get("Items", []) if (i.get("Name") or "").lower() == series.lower()]
            if not hits:
                raise ValueError(f"No Jellyfin series named {series!r}")
            for item in hits:
                resp = await client.post(f"/Items/{item['Id']}/Refresh", params={
                    "Recursive": "true", "MetadataRefreshMode": "FullRefresh", "ImageRefreshMode": "FullRefresh",
                    "ReplaceAllMetadata": str(replace_metadata).lower(), "ReplaceAllImages": "false",
                })
                resp.raise_for_status()
            return {"refreshing": [{"id": i["Id"], "name": i.get("Name"), "year": i.get("ProductionYear")} for i in hits],
                    "replace_metadata": replace_metadata}

    @mcp.tool()
    async def jellyfin_series(search: str = "", limit: int = 50) -> list[dict]:
        """List Jellyfin series with their item id, year, folder path, TVDB/TMDB ids. `search` filters by title. Use it to find the id jellyfin_identify needs, to see which folder a series really points at, or to spot duplicate/stale series."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/Items", params={
                "IncludeItemTypes": "Series", "Recursive": "true", "searchTerm": search or None,
                "Fields": "Path,ProviderIds", "SortBy": "SortName", "Limit": limit,
            })
            resp.raise_for_status()
            return [
                {"id": i["Id"], "name": i.get("Name"), "year": i.get("ProductionYear"), "path": i.get("Path"),
                 "tvdb": (i.get("ProviderIds") or {}).get("Tvdb"), "tmdb": (i.get("ProviderIds") or {}).get("Tmdb")}
                for i in resp.json().get("Items", [])
            ]

    @mcp.tool()
    async def jellyfin_tasks(action: str = "list", task: str = "") -> list[dict] | dict:
        """Jellyfin scheduled tasks (library scan, metadata refresh, ...). action='list' shows each task with state (Idle/Running/Cancelling), progress and last result; 'start' / 'stop' [DESTRUCTIVE] act on the task whose name matches `task` exactly (case-insensitive). Library scans run one at a time, so a hung 'Scan Media Library' blocks every other scan: stop it, then start it again. Confirm with the user before start/stop."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/ScheduledTasks")
            resp.raise_for_status()
            tasks = resp.json()
            if action == "list":
                return [
                    {"name": t.get("Name"), "id": t.get("Id"), "state": t.get("State"),
                     "progress_pct": t.get("CurrentProgressPercentage"),
                     "last_result": (t.get("LastExecutionResult") or {}).get("Status"),
                     "last_end": (t.get("LastExecutionResult") or {}).get("EndTimeUtc")}
                    for t in tasks
                ]
            if action not in ("start", "stop"):
                raise ValueError("action must be list, start or stop")
            hit = [t for t in tasks if (t.get("Name") or "").lower() == task.lower()]
            if not hit:
                raise ValueError(f"No Jellyfin task named {task!r}")
            if action == "start":
                r = await client.post(f"/ScheduledTasks/Running/{hit[0]['Id']}")
            else:
                r = await client.delete(f"/ScheduledTasks/Running/{hit[0]['Id']}")
            r.raise_for_status()
            return {"action": action, "task": hit[0]["Name"]}

    @mcp.tool()
    async def jellyfin_identify(
        series_id: str, search_name: str = "", search_year: int = 0, tvdb_id: str = "", tmdb_id: str = "",
        imdb_id: str = "", apply: bool = False,
    ) -> dict:
        """[DESTRUCTIVE when apply=True] Jellyfin's Identify for one series (find it with jellyfin_library_stats or jellyfin_refresh output; tunarr_search_programs shows the Jellyfin id as an identifier). With apply=False it searches the metadata providers and lists candidates (name, year, provider ids) so you can see what Jellyfin would match. With apply=True and one of `tvdb_id` / `tmdb_id` / `imdb_id` (providers differ by search: a candidate may carry only a TMDB id) it picks the candidate with that id and applies it, replacing the series' metadata and images; this is how to fix a series that Jellyfin matched to the wrong show (e.g. a 1959 series folder identified as the 2002 revival). `search_name`/`search_year` default to the series' current name and year. Confirm with the user first."""
        async with make_client(config.jellyfin.url, headers=_headers(), timeout=60.0) as client:
            item = await client.get("/Items", params={"Ids": series_id, "Fields": "ProviderIds"})
            item.raise_for_status()
            items = item.json().get("Items", [])
            if not items:
                raise ValueError(f"No Jellyfin item with id {series_id!r}")
            cur = items[0]
            info: dict = {"Name": search_name or cur.get("Name"), "ProviderIds": {}}
            if search_year or (not search_name and cur.get("ProductionYear")):
                info["Year"] = search_year or cur["ProductionYear"]
            wanted = {k: v for k, v in (("Tvdb", tvdb_id), ("Tmdb", tmdb_id), ("Imdb", imdb_id)) if v}
            resp = await client.post("/Items/RemoteSearch/Series", json={"SearchInfo": info, "ItemId": series_id})
            resp.raise_for_status()
            results = resp.json()
            rows = [{"name": r.get("Name"), "year": r.get("ProductionYear"), "provider_ids": r.get("ProviderIds", {}),
                     "overview": (r.get("Overview") or "")[:120]} for r in results]
            current = {"name": cur.get("Name"), "year": cur.get("ProductionYear"), "provider_ids": cur.get("ProviderIds", {})}
            if not apply:
                return {"current": current, "candidates": rows}
            if not wanted:
                raise ValueError("apply=True needs tvdb_id, tmdb_id or imdb_id to choose a candidate")
            match = [r for r in results if all(str((r.get("ProviderIds") or {}).get(k)) == str(v) for k, v in wanted.items())]
            if not match:
                raise ValueError(f"No candidate with {wanted}; candidates: {rows}")
            applied = await client.post(f"/Items/RemoteSearch/Apply/{series_id}", params={"ReplaceAllImages": "true"}, json=match[0])
            applied.raise_for_status()
            return {"current": current, "applied": rows[results.index(match[0])]}

    @mcp.tool()
    async def jellyfin_livetv_tuners() -> dict:
        """List Jellyfin's Live TV tuner hosts and guide (EPG) providers: type, a label (the friendly name if set, else the URL's host), URL (query string dropped), max concurrent streams ("unlimited" when Jellyfin's TunerCount is 0) and, for guides, how many manual channel mappings exist (0 means everything is auto-matched). Shows where the Live TV channel list comes from."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/System/Configuration/livetv")
            resp.raise_for_status()
            conf = resp.json()

            def _host(url: str) -> str:
                return urlsplit(url).netloc or url

            return {
                "tuners": [
                    {
                        "id": t.get("Id", ""),
                        "type": t.get("Type", ""),
                        "label": t.get("FriendlyName") or _host(t.get("Url") or ""),
                        "url": (t.get("Url") or "").split("?")[0],
                        "import_favorites_only": t.get("ImportFavoritesOnly", False),
                        "max_streams": t.get("TunerCount") or "unlimited",
                    }
                    for t in conf.get("TunerHosts", [])
                ],
                "guide_providers": [
                    {
                        "id": p.get("Id", ""),
                        "type": p.get("Type", ""),
                        "label": _host(p.get("Path") or ""),
                        "path": (p.get("Path") or "").split("?")[0],
                        "manual_channel_mappings": len(p.get("ChannelMappings") or []),
                    }
                    for p in conf.get("ListingProviders", [])
                ],
            }

    @mcp.tool()
    async def jellyfin_livetv_channels(
        search: str = "", limit: int = 100, offset: int = 0
    ) -> dict:
        """List Jellyfin's Live TV channels as clients see them, sorted by number, with a summary: total, number range, counts per number band (hundreds) and per source service (only when Jellyfin reports one), and names that appear more than once. Use it to spot stale or duplicate channels after renumbering. `search` matches the name; page with limit/offset."""
        params = {"startIndex": 0, "addCurrentProgram": "false"}
        if search:
            params["searchTerm"] = search
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/LiveTv/Channels", params=params)
            resp.raise_for_status()
            body = resp.json()
        items = body.get("Items", [])

        def _num(c: dict) -> float:
            try:
                return float(c.get("Number") or "inf")
            except ValueError:
                return float("inf")

        items.sort(key=_num)
        by_service: dict[str, int] = {}
        by_band: dict[str, int] = {}
        names: dict[str, int] = {}
        for c in items:
            if c.get("ServiceName"):
                by_service[c["ServiceName"]] = by_service.get(c["ServiceName"], 0) + 1
            n = _num(c)
            band = "unnumbered" if n == float("inf") else f"{int(n // 100) * 100}-{int(n // 100) * 100 + 99}"
            by_band[band] = by_band.get(band, 0) + 1
            names[c.get("Name", "")] = names.get(c.get("Name", ""), 0) + 1
        numbered = [c.get("Number") for c in items if c.get("Number")]
        summary = {
            "total": body.get("TotalRecordCount", len(items)),
            "first_number": numbered[0] if numbered else None,
            "last_number": numbered[-1] if numbered else None,
            "by_number_band": by_band,
            "duplicate_names": {n: k for n, k in names.items() if k > 1 and n},
            "channels": [
                {
                    "number": c.get("Number", ""),
                    "name": c.get("Name", ""),
                    "id": c.get("Id", ""),
                    "service": c.get("ServiceName", ""),
                    "type": c.get("ChannelType", ""),
                }
                for c in items[offset:offset + limit]
            ],
        }
        if by_service:
            summary["by_service"] = by_service
        return summary
