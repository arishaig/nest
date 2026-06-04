import time

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.http_client import make_client


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def loki_logs(
        container: str | None = None,
        host: str | None = None,
        unit: str | None = None,
        query: str | None = None,
        lines: int = 50,
        since_minutes: int = 60,
    ) -> dict:
        """Query logs from Loki across all homelab hosts.

        Covers every LXC (docker, monitoring, scrutiny, seedbox, musicbrainz,
        fileserver, mcp, backup) and the VPS. Unlike docker_logs, this works
        for any host and supports historical queries up to 30 days back.

        Args:
            container: Filter by Docker container name (e.g. "jellyfin", "traefik").
            host:      Filter by host (e.g. "docker", "seedbox", "scrutiny",
                       "monitoring", "musicbrainz", "fileserver", "mcp",
                       "backup", "vps-proxy").
            unit:      Filter by systemd unit for journal logs (e.g. "sshd.service").
            query:     Raw LogQL query — overrides container/host/unit filters.
            lines:     Maximum log lines to return (default 50, max 500).
            since_minutes: How far back to look (default 60, max 10080 = 7 days).
        """
        lines = min(max(1, lines), 500)
        since_minutes = min(max(1, since_minutes), 10080)

        if query:
            logql = query
        else:
            selectors = []
            if host:
                selectors.append(f'host="{host}"')
            if container:
                selectors.append(f'container="{container}"')
            if unit:
                selectors.append(f'unit="{unit}"')
            if not selectors:
                selectors.append('job=~"docker|journal"')
            logql = "{" + ", ".join(selectors) + "}"

        now = int(time.time())
        start = now - (since_minutes * 60)

        async with make_client(config.loki.url) as client:
            resp = await client.get(
                "/loki/api/v1/query_range",
                params={
                    "query": logql,
                    "start": start,
                    "end": now,
                    "limit": lines,
                    "direction": "backward",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        streams = data.get("data", {}).get("result", [])
        entries = []
        for stream in streams:
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                ts_s = int(ts_ns) / 1e9
                entries.append((ts_s, labels, line))

        entries.sort(key=lambda e: e[0], reverse=True)
        entries = entries[:lines]

        return {
            "query": logql,
            "lines_returned": len(entries),
            "logs": [
                {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "host": labels.get("host", ""),
                    "container": labels.get("container", ""),
                    "unit": labels.get("unit", ""),
                    "line": line,
                }
                for ts, labels, line in entries
            ],
        }
