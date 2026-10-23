from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
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


def describe_failure(exc: BaseException | None) -> str | None:
    """Client-safe text for a backend HTTP failure, or None for anything else.

    The SDK hides the text of unexpected exceptions, so a dead backend shows up
    as a bare "Error executing tool X". Names the error type, status and URL
    path only: never the query string, headers or response body.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        u = exc.request.url
        return f"HTTP {exc.response.status_code} from {exc.request.method} {u.host}{u.path}"
    if isinstance(exc, httpx.RequestError):
        try:
            host = exc.request.url.host
        except RuntimeError:
            host = "?"
        detail = str(exc) or "no detail"
        return f"{type(exc).__name__} reaching {host}: {detail}"
    return None


class NestMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            reason = describe_failure(exc.__cause__)
            if reason is None:
                raise
            raise ToolError(f"Error executing tool {name}: {reason}") from exc


mcp = NestMCPServer(
    name="Nest",
    instructions=(
        "Live homelab status and control. "
        "Covers Proxmox VMs and containers, PBS backups, disk health (Scrutiny), "
        "Kubernetes (Talos) pods/events/nodes and Traefik ingress health, "
        "ZFS ARC and NFS server stats, "
        "monitoring stack (Prometheus/Loki/Grafana), "
        "Home Assistant devices, areas and automations, UniFi network and clients, "
        "AdGuard DNS (stats, rewrites, plus host-level fail2ban/service status "
        "on the AdGuard Pis), Prometheus alerts, Jellyfin, Tunarr (channels, lineups, "
        "libraries, scans, schedules, settings), Dispatcharr (live-TV channels, streams, "
        "quality probing), the *arr media stack, "
        "seedbox (qBittorrent torrents, tracker status, VPN/gluetun exit IP), "
        "the VPS proxy (Docker, WireGuard, fail2ban). "
        "Start sessions with lab_health_summary for a full snapshot."
    ),
    icons=[Icon(src=_ICON, mimeType="image/webp")],
    website_url="https://mcp.arishaig.site",
    lifespan=lifespan,
)

from nest_mcp.tools import proxmox, adguard, adguard_host, homeassistant, arr, prometheus, scrutiny, unifi, jellyfin, jellyseerr, vps, docker_host, pbs, local, summary, mealie, seedbox, certs, loki, media_files, kubernetes, infra, tunarr, dispatcharr  # noqa: E402

proxmox.register(mcp)
adguard.register(mcp)
adguard_host.register(mcp)
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
media_files.register(mcp)
kubernetes.register(mcp)
infra.register(mcp)
tunarr.register(mcp)
dispatcharr.register(mcp)
