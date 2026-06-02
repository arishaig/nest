import json

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
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
    async def wg_tunnel_status() -> dict:
        """Show WireGuard tunnel status on the docker LXC: peer handshake age, endpoint, and traffic counters."""
        output = await _ssh("wg show 2>/dev/null || echo 'wg not available'")
        return {"wg_show": output}
