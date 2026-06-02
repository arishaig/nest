from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"X-Emby-Token": config.jellyfin.key}


def register(mcp: FastMCP) -> None:

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
