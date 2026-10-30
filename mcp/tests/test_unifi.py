import httpx

from nest_mcp.tools import unifi
from helpers import load_tools


class FakeSession:
    def __init__(self, routes):
        self.routes = routes

    async def get(self, path, **kwargs):
        for key, val in self.routes.items():
            if path == key or (key.endswith("*") and path.startswith(key[:-1])):
                return httpx.Response(200, json=val)
        return httpx.Response(200, json={"data": []})


def _patch(monkeypatch, routes):
    monkeypatch.setattr(unifi, "get_session", lambda: FakeSession(routes))


async def test_port_forwarding(monkeypatch):
    _patch(monkeypatch, {"/proxy/network/api/s/default/rest/portforward":
                         {"data": [{"name": "https", "enabled": True, "proto": "tcp",
                                    "dst_port": "443", "fwd": "192.168.1.117", "fwd_port": "443"}]}})
    out = await load_tools(unifi)["unifi_port_forwarding"]()
    assert out[0]["name"] == "https" and out[0]["dst_ip"] == "192.168.1.117"


async def test_clients_active_filter_and_sort(monkeypatch):
    _patch(monkeypatch, {"/proxy/network/api/s/default/stat/sta": {"data": [
        {"hostname": "zeta", "is_wired": True, "rx_bytes": 2 * 1024 * 1024},
        {"hostname": "alpha", "essid": "wifi"},
        {"hostname": "idle"},  # no essid/wired -> filtered when active_only
    ]}})
    out = await load_tools(unifi)["unifi_clients"]()
    assert [c["hostname"] for c in out] == ["alpha", "zeta"]
    assert out[1]["rx_bytes_mb"] == 2


async def test_devices(monkeypatch):
    _patch(monkeypatch, {"/proxy/network/api/s/default/stat/device": {"data": [
        {"name": "ap1", "model": "U6", "version": "6.0", "num_sta": 3},
    ]}})
    out = await load_tools(unifi)["unifi_devices"]()
    assert out[0]["firmware"] == "6.0" and out[0]["num_sta"] == 3


async def test_network_stats(monkeypatch):
    _patch(monkeypatch, {"/proxy/network/api/s/default/stat/health": {"data": [
        {"subsystem": "wan", "status": "ok", "num_user": 10, "wan_ip": "1.2.3.4"},
    ]}})
    out = await load_tools(unifi)["unifi_network_stats"]()
    assert out["wan"]["status"] == "ok" and out["wan"]["wan_ip"] == "1.2.3.4"


def _fw_routes():
    return {
        "/proxy/network/v2/api/site/default/firewall/zones":
            [{"_id": "z1", "name": "internal"}, {"_id": "z2", "name": "iot"}],
        "/proxy/network/api/s/default/rest/networkconf":
            {"data": [{"_id": "n1", "name": "LAN"}]},
        "/proxy/network/v2/api/site/default/firewall-policies": [
            {"name": "allow-dns", "enabled": True, "action": "ALLOW", "index": 1,
             "protocol": "udp", "predefined": False,
             "source": {"zone_id": "z2", "matching_target": "NETWORK", "network_ids": ["n1"]},
             "destination": {"zone_id": "z1", "matching_target": "IP", "ips": ["192.168.7.7"], "port": "53"}},
            {"name": "block-iot", "enabled": False, "action": "BLOCK", "index": 2,
             "protocol": "all", "predefined": False,
             "source": {"zone_id": "z2", "matching_target": "ANY"},
             "destination": {"zone_id": "z1", "matching_target": "WEB",
                             "web_domains": ["a.com", "b.com", "c.com", "d.com", "e.com"]}},
            {"name": "system", "predefined": True, "action": "ALLOW", "index": 3,
             "source": {"zone_id": "z1"}, "destination": {"zone_id": "z1"}},
        ],
    }


async def test_firewall_summary(monkeypatch):
    _patch(monkeypatch, _fw_routes())
    out = await load_tools(unifi)["unifi_firewall_summary"]()
    assert out["custom_rules"] == {"total": 2, "enabled": 1, "disabled": 1}
    assert out["zones_resolved"] is True
    assert "iot → internal" in out["by_zone_pair"]


async def test_firewall_rules_filters_and_formats(monkeypatch):
    _patch(monkeypatch, _fw_routes())
    tools = load_tools(unifi)
    allf = await tools["unifi_firewall_rules"]()
    assert len(allf) == 2  # predefined excluded
    # NETWORK side resolves to network name; WEB side previews + overflow suffix
    dns = next(r for r in allf if r["name"] == "allow-dns")
    assert dns["src"] == "LAN" and dns["dst"] == "192.168.7.7"
    web = next(r for r in allf if r["name"] == "block-iot")
    assert web["dst"].startswith("web: ") and "+1" in web["dst"]
    # filters
    assert len(await tools["unifi_firewall_rules"](action="allow")) == 1
    assert len(await tools["unifi_firewall_rules"](name="iot")) == 1
    assert len(await tools["unifi_firewall_rules"](zone="iot")) == 2


async def test_wlan_config_allowlists_fields_and_filters(monkeypatch):
    _patch(monkeypatch, {
        "/proxy/network/api/s/default/rest/networkconf": {"data": [{"_id": "n1", "name": "The Nest"}]},
        "/proxy/network/api/s/default/rest/wlanconf": {"data": [
            {"name": "The Nest", "networkconf_id": "n1", "pmf_mode": "required",
             "fast_roaming_enabled": True, "x_passphrase": "hunter2",
             "private_preshared_keys": [{"password": "ppsk"}], "x_iapp_key": "k"},
            {"name": "Branch Office", "networkconf_id": "n2", "pmf_mode": "optional"},
        ]},
    })
    tools = load_tools(unifi)
    out = await tools["unifi_wlan_config"]()
    assert [w["name"] for w in out] == ["Branch Office", "The Nest"]
    nest = out[1]
    assert nest["network"] == "The Nest" and nest["pmf_mode"] == "required"
    assert nest["fast_roaming_enabled"] is True
    assert not {"x_passphrase", "private_preshared_keys", "x_iapp_key"} & nest.keys()
    assert out[0]["network"] == "n2"  # unresolved id falls back to raw id

    filtered = await tools["unifi_wlan_config"](name="nest")
    assert [w["name"] for w in filtered] == ["The Nest"]


class WriteSession(FakeSession):
    def __init__(self, routes):
        super().__init__(routes)
        self.puts = []

    async def put(self, path, **kwargs):
        self.puts.append((path, kwargs["json"]))
        return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})


def _access_routes(members):
    return {
        "/proxy/network/v2/api/site/default/firewall/zones":
            [{"_id": "zi", "name": "IoT"}, {"_id": "zx", "name": "External"}],
        "/proxy/network/api/s/default/rest/networkconf":
            {"data": [{"_id": "n4", "name": "IoT net", "firewall_zone_id": "zi"}]},
        "/proxy/network/api/s/default/stat/sta": {"data": [
            {"hostname": "LGwebOSTV", "ip": "192.168.4.172", "mac": "14:7F:67:CC:BA:32",
             "network_id": "n4", "user_id": "u1", "essid": "x"},
            {"hostname": "other", "ip": "192.168.4.9", "mac": "aa:bb:cc:dd:ee:ff", "network_id": "n4", "essid": "x"},
        ]},
        "/proxy/network/api/s/default/rest/firewallgroup": {"data": [
            {"_id": "g1", "name": "No Internet", "group_type": "address-group", "group_members": members},
            {"_id": "g2", "name": "Ports", "group_type": "port-group", "group_members": ["80"]},
        ]},
        "/proxy/network/v2/api/site/default/firewall-policies": [
            {"name": "NTP", "enabled": True, "action": "ALLOW", "index": 1, "protocol": "udp",
             "source": {"zone_id": "zi", "matching_target": "ANY"},
             "destination": {"zone_id": "zx", "matching_target": "ANY", "port": "123"}},
            {"name": "Block No Internet", "enabled": True, "action": "BLOCK", "index": 2, "protocol": "all",
             "source": {"zone_id": "zi", "matching_target": "IP", "matching_target_type": "OBJECT", "ip_group_id": "g1"},
             "destination": {"zone_id": "zx", "matching_target": "ANY"}},
            {"name": "Allow IoT Internet", "enabled": True, "action": "ALLOW", "index": 3, "protocol": "all",
             "source": {"zone_id": "zi", "matching_target": "ANY"},
             "destination": {"zone_id": "zx", "matching_target": "ANY"}},
        ],
    }


async def test_rules_resolve_ip_group_members(monkeypatch):
    _patch(monkeypatch, _access_routes(["192.168.4.172"]))
    rules = await load_tools(unifi)["unifi_firewall_rules"](name="block")
    assert rules[0]["src"] == "group:No Internet [192.168.4.172]"


async def test_firewall_groups_lists_usage(monkeypatch):
    _patch(monkeypatch, _access_routes(["192.168.4.0/30"]))
    out = await load_tools(unifi)["unifi_firewall_groups"](name="no int")
    assert out == [{"name": "No Internet", "type": "address-group", "members": ["192.168.4.0/30"],
                    "used_by": ["Block No Internet (source)"]}]


async def test_internet_access_blocked_when_in_group(monkeypatch):
    _patch(monkeypatch, _access_routes(["192.168.4.170-192.168.4.175"]))
    out = await load_tools(unifi)["unifi_client_internet_access"]("lgwebostv")
    assert out["verdict"] == "blocked" and out["decided_by"]["name"] == "Block No Internet"
    assert [r["name"] for r in out["partial_rules_before"]] == ["NTP"]
    assert out["zone"] == "IoT"


async def test_internet_access_allowed_when_not_in_group(monkeypatch):
    _patch(monkeypatch, _access_routes(["192.168.4.59"]))
    out = await load_tools(unifi)["unifi_client_internet_access"]("14:7f:67:cc:ba:32")
    assert out["verdict"] == "allowed" and out["decided_by"]["name"] == "Allow IoT Internet"


async def test_internet_access_unknown_client(monkeypatch):
    _patch(monkeypatch, _access_routes([]))
    out = await load_tools(unifi)["unifi_client_internet_access"]("nope")
    assert "no active client" in out["error"]


async def test_group_member_preview_then_apply(monkeypatch):
    sess = WriteSession(_access_routes(["192.168.4.59"]))
    monkeypatch.setattr(unifi, "get_session", lambda: sess)
    tool = load_tools(unifi)["unifi_firewall_group_member"]
    preview = await tool("no internet", "192.168.4.172")
    assert preview["applied"] is False and sess.puts == []
    assert preview["resulting_members"] == ["192.168.4.59", "192.168.4.172"]
    assert preview["used_by_rules"] == ["Block No Internet (BLOCK, on)"]
    done = await tool("no internet", "192.168.4.172", apply=True)
    assert done["applied"] is True
    path, body = sess.puts[0]
    assert path.endswith("/rest/firewallgroup/g1")
    assert body["group_members"] == ["192.168.4.59", "192.168.4.172"] and body["name"] == "No Internet"


async def test_group_member_remove_noop_and_rejections(monkeypatch):
    sess = WriteSession(_access_routes(["192.168.4.59"]))
    monkeypatch.setattr(unifi, "get_session", lambda: sess)
    tool = load_tools(unifi)["unifi_firewall_group_member"]
    assert "no change" in (await tool("No Internet", "192.168.4.1", remove=True, apply=True))["note"]
    assert "not a valid" in (await tool("No Internet", "banana"))["error"]
    assert "port-group" in (await tool("Ports", "192.168.4.1"))["error"]
    assert "no firewall group" in (await tool("Nope", "192.168.4.1"))["error"]
    assert sess.puts == []


async def test_set_fixed_ip(monkeypatch):
    sess = WriteSession(_access_routes([]))
    monkeypatch.setattr(unifi, "get_session", lambda: sess)
    tool = load_tools(unifi)["unifi_set_fixed_ip"]
    assert (await tool("lgwebostv"))["applied"] is False and sess.puts == []
    assert "in use by other" in (await tool("lgwebostv", ip="192.168.4.9"))["error"]
    assert (await tool("lgwebostv", apply=True))["applied"] is True
    path, body = sess.puts[0]
    assert path.endswith("/rest/user/u1")
    assert body == {"use_fixedip": True, "fixed_ip": "192.168.4.172", "network_id": "n4"}


async def test_internet_access_zone_names_unresolved(monkeypatch):
    routes = _access_routes(["192.168.4.172"])
    routes.pop("/proxy/network/v2/api/site/default/firewall/zones")
    _patch(monkeypatch, routes)
    out = await load_tools(unifi)["unifi_client_internet_access"]("lgwebostv")
    assert out["verdict"] == "unknown" and "did not resolve" in out["note"]
    assert [r["name"] for r in out["matching_rules"]] == ["NTP", "Block No Internet", "Allow IoT Internet"]


async def test_zones_from_zone_matrix_shape(monkeypatch):
    routes = _access_routes([])
    routes.pop("/proxy/network/v2/api/site/default/firewall/zones")
    routes["/proxy/network/v2/api/site/default/firewall/zone-matrix"] = [
        {"zone_id": "zi", "name": "IoT"}, {"zone_id": "zx", "name": "External"}]
    _patch(monkeypatch, routes)
    out = await load_tools(unifi)["unifi_client_internet_access"]("lgwebostv")
    assert out["verdict"] == "allowed" and out["zone"] == "IoT"
