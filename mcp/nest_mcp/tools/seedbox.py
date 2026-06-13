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
    async def vpn_status() -> dict:
        """Current VPN/gluetun exit IP, country, and city for the seedbox.

        Queries the gluetun HTTP control server on the seedbox to confirm torrent
        traffic is routing through the expected VPN exit node.
        """
        raw = await _ssh(
            "docker exec gluetun wget -qO- http://localhost:8000/v1/publicip/ip 2>/dev/null"
            " || echo '{\"error\": \"gluetun control server not reachable\"}'"
        )
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Failed to parse gluetun response", "raw": raw[:200]}
        return {
            "public_ip": data.get("public_ip", ""),
            "country": data.get("country", ""),
            "city": data.get("city", ""),
            "organization": data.get("organization", ""),
        }

    @mcp.tool()
    async def torrent_trackers(hash: str) -> list[dict]:
        """Tracker status for a specific torrent — use seedbox_torrents to get the hash first.

        Args:
            hash: Full or 8-char torrent hash from seedbox_torrents output.
        """
        raw = await _qb("/torrents/trackers", f"hash={hash}")
        try:
            trackers = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return [{"error": "Failed to parse tracker response", "raw": raw[:300]}]
        # Filter out DHT/PEX pseudo-trackers (url doesn't start with http)
        return [
            {
                "url": t.get("url", ""),
                "status": t.get("status", 0),
                "num_peers": t.get("num_peers", -1),
                "num_seeds": t.get("num_seeds", -1),
                "num_leeches": t.get("num_leeches", -1),
                "msg": t.get("msg", ""),
            }
            for t in trackers
            if t.get("url", "").startswith("http")
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
