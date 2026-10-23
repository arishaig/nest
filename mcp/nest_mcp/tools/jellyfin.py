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
    async def jellyfin_livetv_tuners() -> dict:
        """List Jellyfin's Live TV tuner hosts and guide (EPG) providers: type, name, URL (query string dropped), and channel mapping count. Shows where the Live TV channel list comes from."""
        async with make_client(config.jellyfin.url, headers=_headers()) as client:
            resp = await client.get("/System/Configuration/livetv")
            resp.raise_for_status()
            conf = resp.json()
            return {
                "tuners": [
                    {
                        "id": t.get("Id", ""),
                        "type": t.get("Type", ""),
                        "name": t.get("FriendlyName", ""),
                        "url": (t.get("Url") or "").split("?")[0],
                        "import_favorites_only": t.get("ImportFavoritesOnly", False),
                        "tuner_count": t.get("TunerCount", 0),
                    }
                    for t in conf.get("TunerHosts", [])
                ],
                "guide_providers": [
                    {
                        "id": p.get("Id", ""),
                        "type": p.get("Type", ""),
                        "name": p.get("Name", ""),
                        "path": (p.get("Path") or "").split("?")[0],
                        "mapped_channels": len(p.get("ChannelMappings") or []),
                    }
                    for p in conf.get("ListingProviders", [])
                ],
            }

    @mcp.tool()
    async def jellyfin_livetv_channels(
        search: str = "", limit: int = 100, offset: int = 0
    ) -> dict:
        """List Jellyfin's Live TV channels as clients see them, sorted by number, with a summary: total, number range, counts per source service, and names that appear more than once. Use it to spot stale or duplicate channels after renumbering. `search` matches the name; page with limit/offset."""
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
        names: dict[str, int] = {}
        for c in items:
            svc = c.get("ServiceName", "") or "?"
            by_service[svc] = by_service.get(svc, 0) + 1
            names[c.get("Name", "")] = names.get(c.get("Name", ""), 0) + 1
        numbered = [c.get("Number") for c in items if c.get("Number")]
        return {
            "total": body.get("TotalRecordCount", len(items)),
            "first_number": numbered[0] if numbered else None,
            "last_number": numbered[-1] if numbered else None,
            "by_service": by_service,
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
