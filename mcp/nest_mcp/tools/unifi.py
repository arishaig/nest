import httpx
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


async def _fetch_json(session: UniFiSession, path: str) -> list | dict | None:
    try:
        resp = await session.get(path)
        text = resp.text.strip()
        if not text:
            return None
        return resp.json()
    except (httpx.HTTPStatusError, ValueError):
        return None


async def _fetch_zones(session: UniFiSession) -> dict[str, str]:
    for path in [
        "/proxy/network/v2/api/site/default/firewall/zones",
        "/proxy/network/v2/api/site/default/zones",
    ]:
        payload = await _fetch_json(session, path)
        if payload is None:
            continue
        items = payload if isinstance(payload, list) else payload.get("data", [])
        if items and isinstance(items[0], dict) and "_id" in items[0]:
            return {z["_id"]: z.get("name", z["_id"]) for z in items}
    return {}


async def _fetch_networks(session: UniFiSession) -> dict[str, str]:
    payload = await _fetch_json(session, "/proxy/network/api/s/default/rest/networkconf")
    if not payload:
        return {}
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    return {n["_id"]: n.get("name", n["_id"]) for n in items if "_id" in n}


async def _fetch_policies(session: UniFiSession) -> list:
    payload = await _fetch_json(session, "/proxy/network/v2/api/site/default/firewall-policies")
    if not payload:
        return []
    return payload if isinstance(payload, list) else payload.get("data", [])


def _fmt_side(side: dict, networks: dict[str, str]) -> str:
    target = side.get("matching_target", "ANY")
    if target == "ANY":
        return "any"
    if target == "IP":
        ips = side.get("ips", [])
        return ", ".join(ips) if ips else "any"
    if target == "NETWORK":
        ids = side.get("network_ids", [])
        return ", ".join(networks.get(i, i) for i in ids) if ids else "any"
    if target == "CLIENT":
        macs = side.get("client_macs", [])
        return ", ".join(macs) if macs else "any"
    if target == "REGION":
        regions = side.get("regions", [])
        return "regions: " + ", ".join(regions)
    if target == "WEB":
        domains = side.get("web_domains", [])
        preview = domains[:4]
        suffix = f" +{len(domains)-4}" if len(domains) > 4 else ""
        return "web: " + ", ".join(preview) + suffix
    if target == "APP":
        ids = side.get("app_ids", [])
        return f"apps ({len(ids)})"
    return target


def _fmt_policy(p: dict, zones: dict[str, str], networks: dict[str, str]) -> dict:
    src = p["source"]
    dst = p["destination"]
    port = dst.get("port", "")
    return {
        "name": p.get("name", ""),
        "enabled": p.get("enabled", True),
        "action": p.get("action", ""),
        "src_zone": zones.get(src.get("zone_id", ""), src.get("zone_id", "?")),
        "src": _fmt_side(src, networks),
        "dst_zone": zones.get(dst.get("zone_id", ""), dst.get("zone_id", "?")),
        "dst": _fmt_side(dst, networks),
        "protocol": p.get("protocol", "all"),
        "dst_port": port,
        "index": p.get("index", 0),
    }


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def unifi_port_forwarding() -> list[dict]:
        """List all UniFi port-forwarding (DNAT) rules with protocol, external port, destination IP/port, and enabled state."""
        session = get_session()
        resp = await session.get("/proxy/network/api/s/default/rest/portforward")
        data = resp.json().get("data", [])
        return [
            {
                "name": r.get("name", ""),
                "enabled": r.get("enabled", False),
                "protocol": r.get("proto", "tcp_udp"),
                "src_port": r.get("dst_port", ""),
                "dst_ip": r.get("fwd", ""),
                "dst_port": r.get("fwd_port", ""),
                "interface": r.get("pfwd_interface", "any"),
                "src_filter": r.get("src", "any"),
            }
            for r in data
        ]

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

    @mcp.tool()
    async def unifi_firewall_summary() -> dict:
        """Summarise custom UniFi zone-based firewall rules.

        Returns counts of enabled/disabled rules and a breakdown by zone pair, listing
        each named rule with its action. Predefined system rules are excluded. Use
        unifi_firewall_rules for full detail or to search/filter specific rules.
        """
        session = get_session()
        zones, networks, policies = (
            await _fetch_zones(session),
            await _fetch_networks(session),
            await _fetch_policies(session),
        )

        custom = [p for p in policies if not p.get("predefined")]
        enabled = [p for p in custom if p.get("enabled", True)]

        by_pair: dict[str, dict] = {}
        for p in sorted(custom, key=lambda x: x.get("index", 0)):
            src_zone = zones.get(p["source"].get("zone_id", ""), p["source"].get("zone_id", "?"))
            dst_zone = zones.get(p["destination"].get("zone_id", ""), p["destination"].get("zone_id", "?"))
            pair = f"{src_zone} → {dst_zone}"
            if pair not in by_pair:
                by_pair[pair] = {"allow": 0, "block": 0, "disabled": 0, "rules": []}
            entry = by_pair[pair]
            action = p.get("action", "")
            if not p.get("enabled", True):
                entry["disabled"] += 1
            elif action == "ALLOW":
                entry["allow"] += 1
            else:
                entry["block"] += 1
            name = p.get("name", "")
            if name:
                entry["rules"].append(
                    ("✓" if p.get("enabled", True) else "✗")
                    + f" {action} {name}"
                )

        return {
            "zones_resolved": bool(zones),
            "custom_rules": {
                "total": len(custom),
                "enabled": len(enabled),
                "disabled": len(custom) - len(enabled),
            },
            "by_zone_pair": by_pair,
        }

    @mcp.tool()
    async def unifi_firewall_rules(
        zone: str = "",
        action: str = "",
        name: str = "",
        include_predefined: bool = False,
    ) -> list[dict]:
        """List formatted UniFi zone-based firewall rules with optional filters.

        Args:
            zone: Filter to rules where source or destination zone name contains this
                  string (case-insensitive). E.g. "iot", "external", "internal".
            action: Filter by action: "allow" or "block" (case-insensitive).
            name: Filter rules whose name contains this string (case-insensitive).
            include_predefined: Include system-generated default rules (default False).

        Returns rules sorted by index with resolved zone names, source/destination
        descriptions, protocol, and destination port.
        """
        session = get_session()
        zones, networks, policies = (
            await _fetch_zones(session),
            await _fetch_networks(session),
            await _fetch_policies(session),
        )

        if not include_predefined:
            policies = [p for p in policies if not p.get("predefined")]

        rules = [_fmt_policy(p, zones, networks) for p in policies]
        rules.sort(key=lambda r: r["index"])

        if zone:
            z = zone.lower()
            rules = [r for r in rules if z in r["src_zone"].lower() or z in r["dst_zone"].lower()]
        if action:
            a = action.upper()
            rules = [r for r in rules if r["action"] == a]
        if name:
            n = name.lower()
            rules = [r for r in rules if n in r["name"].lower()]

        return rules
