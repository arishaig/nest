from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import UniFiSession

_session: UniFiSession | None = None


def get_session() -> UniFiSession:
    global _session
    if _session is None:
        _session = UniFiSession(
            url=config.unifi.url,
            username=config.unifi.username,
            password=config.unifi.password,
            verify_tls=config.unifi.verify_tls,
        )
    return _session


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def unifi_clients(active_only: bool = True) -> list[dict]:
        """List UniFi network clients with hostname, IP, MAC, access point, and signal strength."""
        session = get_session()
        resp = await session.get("/proxy/network/api/s/default/stat/sta")
        data = resp.json().get("data", [])
        clients = [
            {
                "hostname": c.get("hostname", c.get("name", c.get("mac", ""))),
                "ip": c.get("ip", ""),
                "mac": c.get("mac", ""),
                "network": c.get("network", ""),
                "ap_mac": c.get("ap_mac", ""),
                "rssi": c.get("rssi", 0),
                "signal": c.get("signal", 0),
                "uptime": c.get("uptime", 0),
                "rx_bytes_mb": round(c.get("rx_bytes", 0) / 1024 / 1024),
                "tx_bytes_mb": round(c.get("tx_bytes", 0) / 1024 / 1024),
                "is_wired": c.get("is_wired", False),
            }
            for c in data
            if not active_only or c.get("essid") or c.get("is_wired")
        ]
        return sorted(clients, key=lambda x: x["hostname"])

    @mcp.tool()
    async def unifi_devices() -> list[dict]:
        """List all UniFi network devices (APs, switches, gateways) with status and firmware."""
        session = get_session()
        resp = await session.get("/proxy/network/api/s/default/stat/device")
        data = resp.json().get("data", [])
        return [
            {
                "name": d.get("name", d.get("hostname", "")),
                "model": d.get("model", ""),
                "mac": d.get("mac", ""),
                "ip": d.get("ip", ""),
                "state": d.get("state", 0),
                "uptime": d.get("uptime", 0),
                "firmware": d.get("version", ""),
                "num_sta": d.get("num_sta", 0),
                "satisfaction": d.get("satisfaction", 0),
            }
            for d in sorted(data, key=lambda x: x.get("name", ""))
        ]

    @mcp.tool()
    async def unifi_network_stats() -> dict:
        """Get UniFi WAN uplink status, client counts, and current throughput."""
        session = get_session()
        resp = await session.get("/proxy/network/api/s/default/stat/health")
        subsystems = resp.json().get("data", [])
        result: dict = {}
        for s in subsystems:
            name = s.get("subsystem", "")
            result[name] = {
                "status": s.get("status", ""),
                "num_user": s.get("num_user", 0),
                "num_guest": s.get("num_guest", 0),
                "tx_bytes_r": s.get("tx_bytes-r", 0),
                "rx_bytes_r": s.get("rx_bytes-r", 0),
                "gw_mac": s.get("gw_mac", ""),
                "gw_ip": s.get("gw_ip", ""),
                "wan_ip": s.get("wan_ip", ""),
            }
        return result
