import asyncio

from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _auth() -> tuple[str, str]:
    return (config.adguard.username, config.adguard.password)


async def _fetch_stats(url: str) -> dict:
    async with make_client(url, verify=config.adguard.verify_tls) as client:
        resp = await client.get("/control/stats", auth=_auth())
        resp.raise_for_status()
        d = resp.json()
        return {
            "num_dns_queries": d.get("num_dns_queries", 0),
            "num_blocked_filtering": d.get("num_blocked_filtering", 0),
            "num_replaced_safebrowsing": d.get("num_replaced_safebrowsing", 0),
            "num_replaced_parental": d.get("num_replaced_parental", 0),
            "avg_processing_time_ms": round(d.get("avg_processing_time", 0) * 1000, 2),
            "top_queried_domains": d.get("top_queried_domains", [])[:10],
            "top_blocked_domains": d.get("top_blocked_domains", [])[:10],
            "top_clients": d.get("top_clients", [])[:10],
        }


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def adguard_stats() -> dict:
        """Get AdGuard Home 24-hour DNS statistics from both primary (192.168.7.7) and secondary (192.168.7.8): query counts, top blocked domains and clients."""
        primary, secondary = await asyncio.gather(
            _fetch_stats(config.adguard.url),
            _fetch_stats(config.adguard.url_secondary),
            return_exceptions=True,
        )
        return {
            "primary": primary if not isinstance(primary, Exception) else {"error": str(primary)},
            "secondary": secondary if not isinstance(secondary, Exception) else {"error": str(secondary)},
        }

    @mcp.tool()
    async def adguard_list_rewrites() -> list[dict]:
        """List all custom DNS rewrite rules configured in AdGuard Home."""
        async with make_client(config.adguard.url, verify=config.adguard.verify_tls) as client:
            resp = await client.get("/control/rewrite/list", auth=_auth())
            resp.raise_for_status()
            return sorted(resp.json(), key=lambda x: x.get("domain", ""))

    @mcp.tool()
    async def adguard_query_log(limit: int = 50, search: str = "") -> list[dict]:
        """Get recent DNS queries from AdGuard Home, optionally filtered by domain search term."""
        params: dict = {"limit": limit}
        if search:
            params["search"] = search
        async with make_client(config.adguard.url, verify=config.adguard.verify_tls) as client:
            resp = await client.get("/control/querylog", params=params, auth=_auth())
            resp.raise_for_status()
            entries = resp.json().get("data", [])
            return [
                {
                    "time": e.get("time", ""),
                    "question": e.get("question", {}).get("name", ""),
                    "qtype": e.get("question", {}).get("type", ""),
                    "answer": [a.get("value", "") for a in e.get("answer", [])],
                    "client": e.get("client", ""),
                    "reason": e.get("reason", ""),
                    "blocked": e.get("reason", "") not in ("", "NotFilteredNotFound", "NotFilteredWhiteList"),
                    "elapsed_ms": round(e.get("elapsedMs", 0), 2),
                }
                for e in entries
            ]

    @mcp.tool()
    async def adguard_add_rewrite(domain: str, answer: str) -> dict:
        """[DESTRUCTIVE] Add a DNS rewrite rule to AdGuard Home. Immediately affects DNS resolution for all network clients. Confirm the exact domain and answer with the user before calling."""
        async with make_client(config.adguard.url, verify=config.adguard.verify_tls) as client:
            resp = await client.post("/control/rewrite/add", json={"domain": domain, "answer": answer}, auth=_auth())
            resp.raise_for_status()
            return {"added": True, "domain": domain, "answer": answer}

    @mcp.tool()
    async def adguard_delete_rewrite(domain: str, answer: str) -> dict:
        """[DESTRUCTIVE] Delete a DNS rewrite rule from AdGuard Home. Immediately affects DNS resolution for all network clients. Confirm the exact domain and answer with the user before calling."""
        async with make_client(config.adguard.url, verify=config.adguard.verify_tls) as client:
            resp = await client.post("/control/rewrite/delete", json={"domain": domain, "answer": answer}, auth=_auth())
            resp.raise_for_status()
            return {"deleted": True, "domain": domain, "answer": answer}
