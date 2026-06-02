from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from nest_mcp.tools.unifi import get_session


@asynccontextmanager
async def lifespan(server):
    # Pre-authenticate UniFi so the first tool call doesn't bear the login latency
    session = get_session()
    try:
        yield
    finally:
        await session.aclose()


# Home icon — indigo house SVG
_ICON = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%234F46E5' d='M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z'/%3E%3C/svg%3E"

mcp = FastMCP(
    name="Nest",
    instructions=(
        "Live homelab status and control. "
        "Covers Proxmox VMs and containers, PBS backups, disk health (Scrutiny), "
        "Docker services, Home Assistant devices and areas, UniFi network and clients, "
        "AdGuard DNS, Prometheus alerts, Jellyfin, the *arr media stack, "
        "and the VPS proxy (Docker, WireGuard, fail2ban). "
        "Start sessions with lab_health_summary for a full snapshot."
    ),
    icons=[Icon(src=_ICON, mimeType="image/svg+xml", sizes=["24x24"])],
    website_url="https://mcp.arishaig.site",
    lifespan=lifespan,
)

from nest_mcp.tools import proxmox, adguard, homeassistant, arr, prometheus, scrutiny, unifi, jellyfin, jellyseerr, vps, docker_host, pbs, local, summary  # noqa: E402

proxmox.register(mcp)
adguard.register(mcp)
homeassistant.register(mcp)
arr.register(mcp)
prometheus.register(mcp)
scrutiny.register(mcp)
unifi.register(mcp)
jellyfin.register(mcp)
jellyseerr.register(mcp)
vps.register(mcp)
docker_host.register(mcp)
pbs.register(mcp)
local.register(mcp)
summary.register(mcp)
