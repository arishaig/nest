import asyncio

from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.ssh_client import ssh_run


async def _ssh_primary(cmd: str) -> str:
    return await ssh_run(config.adguard_host.host, cmd, user=config.adguard_host.user, key=config.adguard_host.ssh_key)


async def _ssh_secondary(cmd: str) -> str:
    return await ssh_run(
        config.adguard_host.host_secondary, cmd, user=config.adguard_host.user_secondary, key=config.adguard_host.ssh_key
    )


async def _gather(cmd: str) -> dict:
    primary, secondary = await asyncio.gather(
        _ssh_primary(cmd),
        _ssh_secondary(cmd),
        return_exceptions=True,
    )
    return {
        "primary": primary if not isinstance(primary, Exception) else {"error": str(primary)},
        "secondary": secondary if not isinstance(secondary, Exception) else {"error": str(secondary)},
    }


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def adguard_fail2ban_status() -> dict:
        """Show fail2ban jail status and currently banned IPs on both AdGuard Pis (primary 192.168.7.7, secondary 192.168.7.8)."""
        return await _gather("fail2ban-client status sshd 2>/dev/null && echo '---' && fail2ban-client status")

    @mcp.tool()
    async def adguard_service_status() -> dict:
        """Show systemd status for unbound, AdGuardHome, and the dns-watchdog timer on both AdGuard Pis (primary 192.168.7.7, secondary 192.168.7.8)."""
        cmd = (
            "systemctl status unbound --no-pager -l | head -15; echo '==='; "
            "systemctl status AdGuardHome --no-pager -l | head -15; echo '==='; "
            "systemctl status dns-watchdog.timer --no-pager -l | head -15; "
            "true"
        )
        return await _gather(cmd)
