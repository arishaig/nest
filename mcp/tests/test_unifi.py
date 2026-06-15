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
