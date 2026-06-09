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


_ICON = "https://mcp.arishaig.site/nest_logo.webp"

mcp = FastMCP(
    name="Nest",
    instructions=(
        "Live homelab status and control. "
        "Covers Proxmox VMs and containers, PBS backups, disk health (Scrutiny), "
        "Kubernetes (Talos) pod and Traefik ingress health, monitoring stack (Prometheus/Loki/Grafana), "
        "Home Assistant devices and areas, UniFi network and clients, "
        "AdGuard DNS, Prometheus alerts, Jellyfin, the *arr media stack, "
        "and the VPS proxy (Docker, WireGuard, fail2ban). "
        "Start sessions with lab_health_summary for a full snapshot."
    ),
    icons=[Icon(src=_ICON, mimeType="image/webp")],
    website_url="https://mcp.arishaig.site",
    lifespan=lifespan,
)

from nest_mcp.tools import proxmox, adguard, homeassistant, arr, prometheus, scrutiny, unifi, jellyfin, jellyseerr, vps, docker_host, pbs, local, summary, mealie, seedbox, certs, loki  # noqa: E402

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
mealie.register(mcp)
seedbox.register(mcp)
certs.register(mcp)
loki.register(mcp)
