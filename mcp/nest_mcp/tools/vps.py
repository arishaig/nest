import json

import httpx
from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run


async def _ssh(cmd: str) -> str:
    return await ssh_run(config.vps.host, cmd, user=config.vps.ssh_user, key=config.vps.ssh_key)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def vps_nftables_rules() -> dict:
        """Get the live nftables input chain rules on the VPS, showing exactly what traffic is currently allowed or blocked."""
        output = await _ssh("nft list chain inet filter input")
        return {"rules": output}

    @mcp.tool()
    async def vps_docker_containers() -> list[dict]:
        """List all Docker containers on the VPS (running and stopped) with name, image, status, and ports."""
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
    async def vps_fail2ban_status() -> dict:
        """Show fail2ban jail status and currently banned IPs on the VPS."""
        output = await _ssh(
            "fail2ban-client status sshd 2>/dev/null && echo '---' && fail2ban-client status"
        )
        return {"status": output}

    @mcp.tool()
    async def vultr_instance_status() -> dict:
        """Get power status, server health, region, and uptime for the VPS from the Vultr API."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.vultr.com/v2/instances/{config.vps.instance_id}",
                headers={"Authorization": f"Bearer {config.vps.vultr_api_key}"},
            )
            resp.raise_for_status()
            d = resp.json().get("instance", {})
            return {
                "label": d.get("label"),
                "status": d.get("status"),
                "power_status": d.get("power_status"),
                "server_status": d.get("server_status"),
                "main_ip": d.get("main_ip"),
                "region": d.get("region"),
                "plan": d.get("plan"),
                "uptime": d.get("uptime"),
                "os": d.get("os"),
            }
