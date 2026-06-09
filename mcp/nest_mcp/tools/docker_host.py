import json
import re

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.http_client import make_client
from nest_mcp.ssh_client import ssh_run


async def _ssh(cmd: str) -> str:
    return await ssh_run(config.docker_host.host, cmd, key=config.docker_host.ssh_key)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def docker_containers() -> list[dict]:
        """List all Docker containers on the main docker LXC (running and stopped) with name, image, status, and ports."""
        output = await _ssh(
            r"""docker ps -a --format '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","ports":"{{.Ports}}"}'"""
        )
        containers = []
        for line in output.splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    containers.append({"raw": line})
        return containers

    @mcp.tool()
    async def docker_logs(container: str, lines: int = 50) -> dict:
        """Fetch the most recent log lines from a named Docker container on the main docker LXC.

        Args:
            container: Exact container name (e.g. "jellyfin", "traefik").
            lines: Number of tail lines to return (default 50, max 500).
        """
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', container):
            return {"error": "Invalid container name — only alphanumerics, hyphens, dots, and underscores allowed."}
        lines = min(max(1, lines), 500)
        output = await _ssh(f"docker logs --tail {lines} --timestamps {container} 2>&1")
        return {"container": container, "lines_requested": lines, "logs": output}

    @mcp.tool()
    async def traefik_routes() -> list[dict]:
        """List all active Traefik HTTP routes from the k8s Traefik API.

        Returns router name, the Host/path match rule, service, and any middlewares applied.
        Queries the Traefik dashboard API at 192.168.1.110:8080 (hostPort on Talos node).
        """
        async with make_client(config.traefik.url) as client:
            resp = await client.get("/api/http/routers")
            resp.raise_for_status()
            routers = resp.json()
        return [
            {
                "router": r.get("name", ""),
                "rule": r.get("rule", ""),
                "service": r.get("service", ""),
                "middlewares": r.get("middlewares", []),
                "status": r.get("status", ""),
                "tls": r.get("tls") is not None,
            }
            for r in sorted(routers, key=lambda x: x.get("name", ""))
        ]

    @mcp.tool()
    async def wg_tunnel_status() -> dict:
        """Show WireGuard tunnel status on the docker LXC: peer handshake age, endpoint, and traffic counters."""
        output = await _ssh("wg show 2>/dev/null || echo 'wg not available'")
        return {"wg_show": output}
