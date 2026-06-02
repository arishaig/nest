from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def prometheus_query(query: str) -> dict:
        """Execute an instant PromQL query against Prometheus. Returns the raw result vector."""
        async with make_client(config.prometheus.url) as client:
            resp = await client.get("/api/v1/query", params={"query": query})
            resp.raise_for_status()
            d = resp.json()
            return {
                "status": d["status"],
                "result_type": d.get("data", {}).get("resultType", ""),
                "result": d.get("data", {}).get("result", []),
            }

    @mcp.tool()
    async def prometheus_targets() -> list[dict]:
        """List all Prometheus scrape targets with their up/down status and last scrape time."""
        async with make_client(config.prometheus.url) as client:
            resp = await client.get("/api/v1/targets")
            resp.raise_for_status()
            active = resp.json().get("data", {}).get("activeTargets", [])
            return [
                {
                    "job": t.get("labels", {}).get("job", ""),
                    "instance": t.get("labels", {}).get("instance", ""),
                    "health": t.get("health", ""),
                    "last_scrape": t.get("lastScrape", ""),
                    "last_error": t.get("lastError", ""),
                    "scrape_duration_ms": round(t.get("lastScrapeDuration", 0) * 1000, 1),
                }
                for t in sorted(active, key=lambda x: (x.get("labels", {}).get("job", ""), x.get("labels", {}).get("instance", "")))
            ]

    @mcp.tool()
    async def prometheus_alerts() -> list[dict]:
        """List all currently firing Prometheus alerts with labels and annotations."""
        async with make_client(config.prometheus.url) as client:
            resp = await client.get("/api/v1/alerts")
            resp.raise_for_status()
            alerts = resp.json().get("data", {}).get("alerts", [])
            firing = [a for a in alerts if a.get("state") == "firing"]
            return [
                {
                    "name": a.get("labels", {}).get("alertname", ""),
                    "state": a.get("state", ""),
                    "severity": a.get("labels", {}).get("severity", ""),
                    "instance": a.get("labels", {}).get("instance", ""),
                    "summary": a.get("annotations", {}).get("summary", ""),
                    "active_since": a.get("activeAt", ""),
                    "labels": a.get("labels", {}),
                }
                for a in firing
            ]
