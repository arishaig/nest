import httpx
import ipaddress
from mcp.server.mcpserver import MCPServer
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


async def _fetch_groups(session: UniFiSession) -> dict[str, dict]:
    """Firewall address/port groups keyed by _id (what policies reference via *_group_id)."""
    payload = await _fetch_json(session, "/proxy/network/api/s/default/rest/firewallgroup")
    if not payload:
        return {}
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    return {g["_id"]: g for g in items if "_id" in g}


def _fmt_side(side: dict, networks: dict[str, str], groups: dict[str, dict] | None = None) -> str:
    target = side.get("matching_target", "ANY")
    if target == "ANY":
        return "any"
    if target == "IP":
        gid = side.get("ip_group_id")
        if gid and not side.get("ips"):
            g = (groups or {}).get(gid)
            if g is None:
                return f"group:{gid}"
            return f"group:{g.get('name', gid)} [{', '.join(g.get('group_members', []))}]"
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


# Allowlist, not denylist: wlanconf also carries x_passphrase, PPSKs and RADIUS
# secrets, and new secret-bearing fields must not leak by default.
_WLAN_FIELDS = (
    "enabled", "hide_ssid", "is_guest", "security", "wpa_mode", "wpa_enc",
    "wpa3_support", "wpa3_transition", "wpa3_fast_roaming", "wpa3_enhanced_192",
    "pmf_mode", "sae_anti_clogging", "group_rekey", "fast_roaming_enabled",
    "bss_transition", "iapp_enabled", "uapsd_enabled", "wlan_band", "wlan_bands",
    "mlo_enabled", "minrssi_enabled", "minrssi", "dtim_mode", "dtim_ng", "dtim_na",
    "dtim_6e", "minrate_setting_preference", "minrate_ng_enabled",
    "minrate_ng_data_rate_kbps", "minrate_na_enabled", "minrate_na_data_rate_kbps",
    "l2_isolation", "proxy_arp", "bc_filter_enabled", "mcastenhance_enabled",
    "mac_filter_enabled", "mac_filter_policy", "no2ghz_oui",
    "optimize_iot_wifi_connectivity", "enhanced_iot", "schedule_enabled",
    "private_preshared_keys_enabled", "radius_das_enabled", "ap_group_ids",
)


def _fmt_wlan(w: dict, networks: dict[str, str]) -> dict:
    net_id = w.get("networkconf_id", "")
    out = {"name": w.get("name", ""), "network": networks.get(net_id, net_id)}
    out.update({k: w[k] for k in _WLAN_FIELDS if k in w})
    return out


def _fmt_policy(
    p: dict, zones: dict[str, str], networks: dict[str, str], groups: dict[str, dict] | None = None
) -> dict:
    src = p["source"]
    dst = p["destination"]
    port = dst.get("port", "")
    return {
        "name": p.get("name", ""),
        "enabled": p.get("enabled", True),
        "action": p.get("action", ""),
        "src_zone": zones.get(src.get("zone_id", ""), src.get("zone_id", "?")),
        "src": _fmt_side(src, networks, groups),
        "dst_zone": zones.get(dst.get("zone_id", ""), dst.get("zone_id", "?")),
        "dst": _fmt_side(dst, networks, groups),
        "protocol": p.get("protocol", "all"),
        "dst_port": port,
        "index": p.get("index", 0),
    }


def _member_matches(member: str, ip: str) -> bool:
    """Does a firewall-group member (IP, CIDR, or a-b range) cover this IP?"""
    try:
        addr = ipaddress.ip_address(ip)
        if "-" in member:
            lo, hi = (ipaddress.ip_address(x.strip()) for x in member.split("-", 1))
            return lo <= addr <= hi
        return addr in ipaddress.ip_network(member, strict=False)
    except ValueError:
        return False


def _valid_member(member: str) -> bool:
    try:
        if "-" in member:
            lo, hi = (ipaddress.ip_address(x.strip()) for x in member.split("-", 1))
            return lo <= hi
        ipaddress.ip_network(member, strict=False)
        return True
    except ValueError:
        return False


def _source_matches(side: dict, client: dict, groups: dict[str, dict]) -> bool | None:
    """True/False if the policy source covers the client; None if undeterminable here."""
    target = side.get("matching_target", "ANY")
    if target == "ANY":
        return True
    ip, mac, net = client.get("ip", ""), client.get("mac", "").lower(), client.get("network_id", "")
    if target == "IP":
        members = list(side.get("ips", []))
        if side.get("ip_group_id") and not members:
            members = groups.get(side["ip_group_id"], {}).get("group_members", [])
        hit = any(_member_matches(m, ip) for m in members)
        return not hit if side.get("match_opposite_ips") else hit
    if target == "NETWORK":
        hit = net in side.get("network_ids", [])
        return not hit if side.get("match_opposite_networks") else hit
    if target == "CLIENT":
        return mac in [m.lower() for m in side.get("client_macs", [])]
    return None


def _find_client(clients: list[dict], query: str) -> tuple[dict | None, str]:
    q = query.strip().lower()
    exact = [c for c in clients if q in (c.get("ip", ""), c.get("mac", "").lower())]
    hits = exact or [
        c for c in clients
        if q in (c.get("hostname") or c.get("name") or "").lower() or q in (c.get("name") or "").lower()
    ]
    if not hits:
        return None, f"no active client matches {query!r}"
    if len(hits) > 1:
        names = ", ".join(f"{c.get('hostname') or c.get('name') or c.get('mac')} ({c.get('ip', '')})" for c in hits)
        return None, f"{query!r} is ambiguous: {names}"
    return hits[0], ""


async def _fetch_clients(session: UniFiSession) -> list[dict]:
    resp = await session.get("/proxy/network/api/s/default/stat/sta")
    return resp.json().get("data", [])


def register(mcp: MCPServer) -> None:

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
    async def unifi_wlan_config(name: str = "") -> list[dict]:
        """List UniFi WiFi (SSID) settings for diagnosing per-SSID connectivity issues.

        Covers security (WPA mode, WPA3/transition, PMF, group rekey), roaming
        (802.11r, BSS transition), bands/MLO, minimum RSSI, DTIM, min data rates,
        isolation/proxy-ARP/multicast settings, and the bound network. Passphrases,
        PPSKs and RADIUS secrets are never returned.

        Args:
            name: Filter to SSIDs whose name contains this string (case-insensitive).
        """
        session = get_session()
        networks = await _fetch_networks(session)
        resp = await session.get("/proxy/network/api/s/default/rest/wlanconf")
        wlans = [_fmt_wlan(w, networks) for w in resp.json().get("data", [])]
        if name:
            n = name.lower()
            wlans = [w for w in wlans if n in w["name"].lower()]
        return sorted(wlans, key=lambda w: w["name"])

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
        zones, networks, policies, groups = (
            await _fetch_zones(session),
            await _fetch_networks(session),
            await _fetch_policies(session),
            await _fetch_groups(session),
        )

        if not include_predefined:
            policies = [p for p in policies if not p.get("predefined")]

        rules = [_fmt_policy(p, zones, networks, groups) for p in policies]
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


    @mcp.tool()
    async def unifi_firewall_groups(name: str = "") -> list[dict]:
        """List UniFi firewall groups (IP address groups and port groups) with their members and the firewall rules that reference each. IoT internet access is controlled by which address group a device's IP is in, so use this to see who is in e.g. the "No Internet" group.

        Args:
            name: Filter to groups whose name contains this string (case-insensitive).
        """
        session = get_session()
        groups, policies = await _fetch_groups(session), await _fetch_policies(session)
        used: dict[str, list[str]] = {}
        for p in policies:
            for side in ("source", "destination"):
                for key in ("ip_group_id", "port_group_id"):
                    gid = p.get(side, {}).get(key)
                    if gid:
                        used.setdefault(gid, []).append(f"{p.get('name', '')} ({side})")
        out = [
            {
                "name": g.get("name", ""),
                "type": g.get("group_type", ""),
                "members": g.get("group_members", []),
                "used_by": used.get(gid, []),
            }
            for gid, g in groups.items()
        ]
        if name:
            n = name.lower()
            out = [g for g in out if n in g["name"].lower()]
        return sorted(out, key=lambda g: g["name"])

    @mcp.tool()
    async def unifi_client_internet_access(client: str) -> dict:
        """Work out whether a UniFi client can reach the internet, from the firewall policy list. Resolves the client (IP, MAC, or hostname substring), then walks enabled policies from its zone to External in index order, matching the source against its IP / network / MAC / address-group membership. First fully-matching rule wins; rules limited to a port or destination are listed as `partial` (e.g. an NTP-only allow) and do not decide the verdict. This is a policy-level inference, not a packet test: verify with the device when it matters.

        Args:
            client: IP, MAC, or hostname/name substring of an active client.
        """
        session = get_session()
        clients, zones, networks, policies, groups = (
            await _fetch_clients(session),
            await _fetch_zones(session),
            await _fetch_networks(session),
            await _fetch_policies(session),
            await _fetch_groups(session),
        )
        c, err = _find_client(clients, client)
        if c is None:
            return {"error": err}

        conf = {}
        resp = await _fetch_json(session, "/proxy/network/api/s/default/rest/networkconf")
        if resp:
            items = resp.get("data", []) if isinstance(resp, dict) else resp
            conf = next((n for n in items if n.get("_id") == c.get("network_id")), {})
        client_zone = conf.get("firewall_zone_id", "")
        external = {zid for zid, zn in zones.items() if "external" in zn.lower()}

        decided: dict | None = None
        partial: list[dict] = []
        undetermined: list[str] = []
        for p in sorted(policies, key=lambda x: x.get("index", 0)):
            if not p.get("enabled", True) or p["destination"].get("zone_id") not in external:
                continue
            if client_zone and p["source"].get("zone_id") != client_zone:
                continue
            m = _source_matches(p["source"], c, groups)
            if m is None:
                undetermined.append(p.get("name", ""))
                continue
            if not m:
                continue
            rule = _fmt_policy(p, zones, networks, groups)
            dst = p["destination"]
            narrow = dst.get("matching_target", "ANY") != "ANY" or dst.get("port") or p.get("protocol", "all") != "all"
            if narrow:
                partial.append(rule)
            else:
                decided = rule
                break

        verdict = "unknown" if decided is None else ("allowed" if decided["action"] == "ALLOW" else "blocked")
        return {
            "client": c.get("hostname") or c.get("name") or c.get("mac", ""),
            "ip": c.get("ip", ""),
            "mac": c.get("mac", ""),
            "network": networks.get(c.get("network_id", ""), c.get("network", "")),
            "zone": zones.get(client_zone, client_zone or "unresolved (all zones considered)"),
            "fixed_ip": c.get("fixed_ip") if c.get("use_fixedip") else None,
            "verdict": verdict,
            "decided_by": decided,
            "partial_rules_before": partial,
            "undetermined_rules": undetermined,
        }

    @mcp.tool()
    async def unifi_firewall_group_member(
        group: str, member: str, remove: bool = False, apply: bool = False
    ) -> dict:
        """[DESTRUCTIVE when apply=True] Add (or with remove=True, remove) an IP, CIDR, or a-b range in a UniFi IP address group. Group membership is what the zone firewall rules key on (e.g. the "No Internet" group blocks internet for its members), so this changes live traffic immediately. Defaults to apply=False, which only previews: current members, the rules that reference the group, and the resulting list. Show that to the user and confirm before passing apply=True. Refuses port groups and IPv6 groups.

        Args:
            group: Exact group name (case-insensitive).
            member: IPv4 address, CIDR, or "a.b.c.d-e.f.g.h" range.
            remove: Remove instead of add.
            apply: False (default) = preview only. True = write the change.
        """
        member = member.strip()
        if not _valid_member(member):
            return {"error": f"{member!r} is not a valid IP, CIDR, or range"}
        session = get_session()
        groups, policies = await _fetch_groups(session), await _fetch_policies(session)
        gid, g = next(((i, x) for i, x in groups.items() if x.get("name", "").lower() == group.strip().lower()), (None, None))
        if g is None:
            return {"error": f"no firewall group named {group!r}", "groups": sorted(x.get("name", "") for x in groups.values())}
        if g.get("group_type") != "address-group":
            return {"error": f"{g.get('name')!r} is a {g.get('group_type')}; only IPv4 address-groups are supported"}

        current = list(g.get("group_members", []))
        updated = [m for m in current if m != member] if remove else current + ([] if member in current else [member])
        refs = [
            f"{p.get('name', '')} ({p.get('action', '')}, {'on' if p.get('enabled', True) else 'off'})"
            for p in policies
            if gid in (p.get("source", {}).get("ip_group_id"), p.get("destination", {}).get("ip_group_id"))
        ]
        result = {
            "group": g.get("name"), "action": "remove" if remove else "add", "member": member,
            "current_members": current, "resulting_members": updated, "used_by_rules": refs,
        }
        if updated == current:
            return {**result, "applied": False, "note": "no change: member already " + ("absent" if remove else "present")}
        if not apply:
            return {**result, "applied": False, "note": "preview only; call again with apply=True to write"}

        resp = await session.put(
            f"/proxy/network/api/s/default/rest/firewallgroup/{gid}",
            json={**g, "group_members": updated},
        )
        rc = resp.json().get("meta", {}).get("rc", "")
        if rc != "ok":
            return {**result, "applied": False, "error": f"controller returned rc={rc!r}"}
        fresh = (await _fetch_groups(session)).get(gid, {})
        return {**result, "applied": True, "members_now": fresh.get("group_members", [])}

    @mcp.tool()
    async def unifi_set_fixed_ip(client: str, ip: str = "", apply: bool = False) -> dict:
        """[DESTRUCTIVE when apply=True] Give a UniFi client a DHCP reservation (fixed IP). Needed before putting a device into an IP-based firewall group, otherwise a DHCP change silently drops it out. `ip` defaults to the client's current address. Refuses an address another client is using. Defaults to apply=False (preview only); confirm with the user before apply=True. The client keeps its old lease until it next renews or reconnects.

        Args:
            client: IP, MAC, or hostname/name substring of an active client.
            ip: IPv4 address to reserve; empty = the client's current IP.
            apply: False (default) = preview only. True = write the reservation.
        """
        session = get_session()
        clients = await _fetch_clients(session)
        c, err = _find_client(clients, client)
        if c is None:
            return {"error": err}
        target = (ip or c.get("ip", "")).strip()
        try:
            ipaddress.IPv4Address(target)
        except ValueError:
            return {"error": f"{target!r} is not a valid IPv4 address"}
        other = next((o for o in clients if o.get("ip") == target and o.get("mac") != c.get("mac")), None)
        if other:
            return {"error": f"{target} is in use by {other.get('hostname') or other.get('mac')}"}
        uid = c.get("user_id") or c.get("_id")
        result = {
            "client": c.get("hostname") or c.get("name") or c.get("mac", ""),
            "mac": c.get("mac", ""), "current_ip": c.get("ip", ""), "reserve_ip": target,
            "already_reserved": bool(c.get("use_fixedip") and c.get("fixed_ip") == target),
        }
        if result["already_reserved"]:
            return {**result, "applied": False, "note": "no change: already reserved"}
        if not apply:
            return {**result, "applied": False, "note": "preview only; call again with apply=True to write"}
        if not uid:
            return {**result, "applied": False, "error": "controller gave no user id for this client"}
        resp = await session.put(
            f"/proxy/network/api/s/default/rest/user/{uid}",
            json={"use_fixedip": True, "fixed_ip": target, "network_id": c.get("network_id", "")},
        )
        rc = resp.json().get("meta", {}).get("rc", "")
        if rc != "ok":
            return {**result, "applied": False, "error": f"controller returned rc={rc!r}"}
        return {**result, "applied": True}
