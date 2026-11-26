import re
import time

from mcp.server.mcpserver import MCPServer

from nest_mcp import config
from nest_mcp.http_client import make_client


_K8S_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def register(mcp: MCPServer) -> None:

    @mcp.tool()
    async def loki_logs(
        container: str | None = None,
        host: str | None = None,
        unit: str | None = None,
        query: str | None = None,
        namespace: str | None = None,
        pod: str | None = None,
        app: str | None = None,
        lines: int = 50,
        since_minutes: int = 60,
    ) -> dict:
        """Query logs from Loki across all homelab hosts.

        Covers every LXC (docker, monitoring, scrutiny, seedbox,
        fileserver, mcp, backup), the AdGuard Pis (adguard, dns-secondary),
        and the VPS. Unlike docker_logs, this works for any host and supports
        historical queries up to 30 days back.

        Kubernetes pod logs carry the labels namespace, pod and container (Alloy
        derives them from the log file path). With bjw-s app-template charts the
        main container is literally called "app", so container="radarr" matches
        nothing: use app="radarr" (or namespace="media" + pod="radarr").

        Args:
            namespace: k8s namespace (e.g. "media", "nest-mcp").
            pod:       k8s pod name prefix (e.g. "radarr" matches radarr-5d8f...).
            app:       Alias for pod: the workload name (e.g. "radarr", "sabnzbd").
            container: Filter by container name. For Docker hosts the container
                       (e.g. "jellyfin"); in k8s the container ("app" for most media apps).
            host:      Filter by host (e.g. "docker", "seedbox", "scrutiny",
                       "monitoring", "fileserver", "mcp",
                       "backup", "adguard", "dns-secondary", "vps-proxy").
            unit:      Filter by systemd unit for journal logs (e.g. "sshd.service").
            query:     A FULL LogQL query, e.g. `{namespace="media", pod=~"sabnzbd(-.*)?"} |~ "idle job"`; when set it replaces ALL the other filters (container/host/unit/namespace/pod/app), so a bare search word is invalid (HTTP 400). To search one pod's lines, write the selector and the line filter yourself.
            lines:     Maximum log lines to return (default 50, max 500).
            since_minutes: How far back to look (default 60, max 10080 = 7 days).
        """
        lines = min(max(1, lines), 500)
        since_minutes = min(max(1, since_minutes), 10080)

        if query:
            logql = query
        else:
            selectors = []
            workload = pod or app
            for label, value in (("namespace", namespace), ("pod", workload)):
                if value is None:
                    continue
                if not _K8S_NAME.match(value):
                    return {"error": f"Invalid {label} {value!r}: only letters, digits, '.', '_' and '-' are allowed."}
                selectors.append(f'namespace="{value}"' if label == "namespace" else f'pod=~"{re.escape(value)}(-.*)?"')
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
                    "namespace": labels.get("namespace", ""),
                    "pod": labels.get("pod", ""),
                    "container": labels.get("container", ""),
                    "unit": labels.get("unit", ""),
                    "line": line,
                }
                for ts, labels, line in entries
            ],
        }
