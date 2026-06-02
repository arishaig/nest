from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"X-Api-Key": config.jellyseerr.key}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def jellyseerr_requests(status: str = "pending") -> list[dict]:
        """List Jellyseerr media requests. Status: 'pending', 'approved', 'declined', 'available', or '' for all."""
        params: dict = {"take": 50, "sort": "added"}
        if status:
            # 1=pending, 2=approved, 3=declined, 4=available, 5=processing
            status_map = {"pending": 1, "approved": 2, "declined": 3, "available": 4, "processing": 5}
            if status in status_map:
                params["filter"] = status
        async with make_client(config.jellyseerr.url, headers=_headers()) as client:
            resp = await client.get("/api/v1/request", params=params)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return [
                {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "type": r.get("type", ""),
                    "title": r.get("media", {}).get("tmdbId", ""),
                    "requested_by": r.get("requestedBy", {}).get("displayName", ""),
                    "created_at": r.get("createdAt", ""),
                    "media_status": r.get("media", {}).get("status", 0),
                }
                for r in results
            ]

    @mcp.tool()
    async def jellyseerr_stats() -> dict:
        """Get Jellyseerr request counts by status and total media in library."""
        async with make_client(config.jellyseerr.url, headers=_headers()) as client:
            resp = await client.get("/api/v1/request/count")
            resp.raise_for_status()
            return resp.json()
