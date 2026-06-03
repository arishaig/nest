import asyncio
import json

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run

# qBittorrent WebUI is bound to localhost:8090 inside the seedbox.
# AuthSubnetWhitelistEnabled allows requests from the host without credentials.
_QB_BASE = "http://localhost:8090/api/v2"


async def _ssh(cmd: str) -> str:
    return await ssh_run(config.seedbox.host, cmd, key=config.seedbox.ssh_key)


async def _qb(path: str, params: str = "") -> str:
    qs = f"?{params}" if params else ""
    return await _ssh(f"curl -sf '{_QB_BASE}{path}{qs}'")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def seedbox_torrents(
        filter: str = "all",
        category: str = "",
    ) -> list[dict]:
        """List torrents on the seedbox qBittorrent instance.

        Args:
            filter: One of all, downloading, seeding, completed, paused, active, inactive, stalled.
            category: Optional category name to filter by.
        """
        params = f"filter={filter}"
        if category:
            params += f"&category={category}"
        raw = await _qb("/torrents/info", params)
        try:
            torrents = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return [{"error": "Failed to parse qBittorrent response", "raw": raw[:300]}]
        return [
            {
                "name": t.get("name", ""),
                "state": t.get("state", ""),
                "progress": round(t.get("progress", 0) * 100, 1),
                "size_gb": round(t.get("size", 0) / 1024**3, 2),
                "downloaded_gb": round(t.get("downloaded", 0) / 1024**3, 2),
                "upload_speed_kb": round(t.get("upspeed", 0) / 1024, 1),
                "download_speed_kb": round(t.get("dlspeed", 0) / 1024, 1),
                "ratio": round(t.get("ratio", 0), 3),
                "eta": t.get("eta", -1),
                "category": t.get("category", ""),
                "added_on": t.get("added_on", 0),
                "hash": t.get("hash", "")[:8],
            }
            for t in torrents
        ]

    @mcp.tool()
    async def seedbox_stats() -> dict:
        """Get qBittorrent global transfer stats: speeds, session totals, and free disk space."""
        transfer_raw, maindata_raw = await asyncio.gather(
            _qb("/transfer/info"),
            _qb("/sync/maindata"),
        )
        try:
            info = json.loads(transfer_raw)
            free = json.loads(maindata_raw).get("server_state", {}).get("free_space_on_disk", 0)
        except (json.JSONDecodeError, ValueError) as e:
            return {"error": f"Failed to parse qBittorrent response: {e}"}
        return {
            "dl_speed_kb": round(info.get("dl_info_speed", 0) / 1024, 1),
            "up_speed_kb": round(info.get("up_info_speed", 0) / 1024, 1),
            "dl_session_gb": round(info.get("dl_info_data", 0) / 1024**3, 2),
            "up_session_gb": round(info.get("up_info_data", 0) / 1024**3, 2),
            "free_space_gb": round(free / 1024**3, 2),
            "connection_status": info.get("connection_status", ""),
        }
