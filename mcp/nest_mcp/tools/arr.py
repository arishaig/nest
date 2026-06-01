from typing import Literal
from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client

ArrService = Literal["sonarr", "radarr", "lidarr", "prowlarr"]

_PORTS = {
    "sonarr": 8989,
    "radarr": 7878,
    "lidarr": 8686,
    "prowlarr": 9696,
}

_KEY_MAP = {
    "sonarr": lambda: config.arr.sonarr_key,
    "radarr": lambda: config.arr.radarr_key,
    "lidarr": lambda: config.arr.lidarr_key,
    "prowlarr": lambda: config.arr.prowlarr_key,
}


def _arr_client(service: str):
    port = _PORTS[service]
    key = _KEY_MAP[service]()
    return make_client(
        f"http://{config.arr.arr_host}:{port}",
        headers={"X-Api-Key": key},
    )


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def arr_status(service: ArrService) -> dict:
        """Get version and system status for a *arr service (sonarr, radarr, lidarr, or prowlarr)."""
        async with _arr_client(service) as client:
            resp = await client.get("/api/v3/system/status")
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
            resp = await client.get("/api/v3/queue", params={"pageSize": 50, "includeUnknownSeriesItems": True})
            resp.raise_for_status()
            d = resp.json()
            records = d.get("records", [])
            return {
                "service": service,
                "total": d.get("totalRecords", 0),
                "items": [
                    {
                        "title": r.get("title", ""),
                        "status": r.get("status", ""),
                        "tracked_status": r.get("trackedDownloadStatus", ""),
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
            resp = await client.get("/api/v3/history", params={"pageSize": limit, "sortKey": "date", "sortDir": "desc"})
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
