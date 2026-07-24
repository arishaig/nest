import json

import httpx

from nest_mcp.tools import vps, docker_host, seedbox, infra
from helpers import load_tools, patch_http, patch_ssh


# ---- vps ----------------------------------------------------------------

async def test_vps_nftables(monkeypatch):
    patch_ssh(monkeypatch, vps, "chain input { drop }")
    out = await load_tools(vps)["vps_nftables_rules"]()
    assert "drop" in out["rules"]


async def test_vps_docker_containers_parses_and_tolerates_bad_lines(monkeypatch):
    lines = json.dumps({"name": "traefik", "image": "traefik", "status": "Up", "ports": "443"}) + "\nnot-json"
    patch_ssh(monkeypatch, vps, lines)
    out = await load_tools(vps)["vps_docker_containers"]()
    assert out[0]["name"] == "traefik"
    assert out[1] == {"raw": "not-json"}


async def test_vultr_status(monkeypatch):
    real = httpx.AsyncClient
    handler = lambda req: httpx.Response(200, json={"instance": {"label": "vps", "status": "active",
                                                                 "power_status": "running", "main_ip": "1.2.3.4"}})
    monkeypatch.setattr(vps.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    out = await load_tools(vps)["vultr_instance_status"]()
    assert out["status"] == "active" and out["main_ip"] == "1.2.3.4"


# ---- docker_host --------------------------------------------------------

async def test_docker_logs_rejects_bad_name(monkeypatch):
    patch_ssh(monkeypatch, docker_host, "logs")
    out = await load_tools(docker_host)["docker_logs"](container="bad;name")
    assert "error" in out


async def test_docker_logs_ok(monkeypatch):
    patch_ssh(monkeypatch, docker_host, "2026 line1\n2026 line2")
    out = await load_tools(docker_host)["docker_logs"](container="jellyfin", lines=10)
    assert out["container"] == "jellyfin" and "line1" in out["logs"]


async def test_traefik_routes_sorted(monkeypatch):
    patch_http(monkeypatch, docker_host, {"/api/http/routers": [
        {"name": "z@k", "rule": "Host(`z`)", "service": "z", "tls": {}},
        {"name": "a@k", "rule": "Host(`a`)", "service": "a"},
    ]})
    out = await load_tools(docker_host)["traefik_routes"]()
    assert [r["router"] for r in out] == ["a@k", "z@k"]
    assert out[1]["tls"] is True and out[0]["tls"] is False


async def test_wg_tunnel_status(monkeypatch):
    patch_ssh(monkeypatch, docker_host, "peer: abc\nlatest handshake: 1 min")
    out = await load_tools(docker_host)["wg_tunnel_status"]()
    assert "peer" in out["wg_show"]


# ---- seedbox ------------------------------------------------------------

async def test_seedbox_torrents(monkeypatch):
    payload = json.dumps([{"name": "iso", "state": "seeding", "progress": 1.0,
                           "size": 1024**3, "ratio": 1.234, "hash": "abcdef1234"}])
    patch_ssh(monkeypatch, seedbox, lambda host, cmd: payload)
    out = await load_tools(seedbox)["seedbox_torrents"]()
    assert out[0]["progress"] == 100.0 and out[0]["hash"] == "abcdef12"


async def test_seedbox_torrents_bad_json(monkeypatch):
    patch_ssh(monkeypatch, seedbox, "garbage")
    out = await load_tools(seedbox)["seedbox_torrents"]()
    assert "error" in out[0]


async def test_vpn_status(monkeypatch):
    patch_ssh(monkeypatch, seedbox, json.dumps({"public_ip": "9.9.9.9", "country": "NL", "city": "AMS"}))
    out = await load_tools(seedbox)["vpn_status"]()
    assert out["public_ip"] == "9.9.9.9" and out["country"] == "NL"


async def test_torrent_trackers_filters_pseudo(monkeypatch):
    payload = json.dumps([
        {"url": "** [DHT] **", "status": 2},
        {"url": "http://tr.test/announce", "status": 2, "num_seeds": 5},
    ])
    patch_ssh(monkeypatch, seedbox, lambda host, cmd: payload)
    out = await load_tools(seedbox)["torrent_trackers"](hash="abc")
    assert len(out) == 1 and out[0]["url"].startswith("http")


async def test_seedbox_stats(monkeypatch):
    def responder(host, cmd):
        if "transfer/info" in cmd:
            return json.dumps({"dl_info_speed": 2048, "connection_status": "connected"})
        return json.dumps({"server_state": {"free_space_on_disk": 5 * 1024**3}})
    patch_ssh(monkeypatch, seedbox, responder)
    out = await load_tools(seedbox)["seedbox_stats"]()
    assert out["dl_speed_kb"] == 2.0 and out["free_space_gb"] == 5.0


# ---- infra --------------------------------------------------------------

async def test_zfs_arc_stats(monkeypatch):
    # arcstats format is "name type value" (3 whitespace fields per line).
    arcstats = "hits 4 6\nmisses 4 2\nsize 4 1073741824\nc 4 1073741824\nc_max 4 2147483648\n"
    patch_ssh(monkeypatch, infra, arcstats)
    out = await load_tools(infra)["zfs_arc_stats"]()
    assert out["hit_rate_pct"] == 75.0  # 6 hits / (6+2)
    assert out["size_gb"] == 1.0 and out["max_gb"] == 2.0


async def test_nfs_status(monkeypatch):
    def responder(host, cmd):
        if "nfsstat" in cmd:
            return "Server packet stats..."
        return "address: 192.168.1.110\nminor version: 1\n"
    patch_ssh(monkeypatch, infra, responder)
    out = await load_tools(infra)["nfs_status"]()
    assert out["active_mounts"] == ["192.168.1.110 (NFSv4.1)"]
    assert "Server" in out["nfsstat_server"]


async def test_proxmox_boot_diagnostics_with_history(monkeypatch):
    output = "\n".join([
        "===PERSISTENT===",
        "yes",
        "===BOOTS===",
        "0 abc123 Wed 2026-07-22 boot",
        "-1 def456 Thu 2026-07-23 boot",
        "===KERNEL_WARN===",
        "Jul 23 09:00:00 proxmox kernel: mce: [Hardware Error]: something bad",
        "===OOM===",
        "Jul 23 09:00:01 proxmox kernel: Out of memory: Killed process 1234 (qemu-system-x86)",
        "===SYS_ERR===",
        "Jul 23 09:00:02 proxmox pvedaemon[1]: worker exited",
    ])
    patch_ssh(monkeypatch, infra, output)
    out = await load_tools(infra)["proxmox_boot_diagnostics"]()
    assert out["persistent_journal_enabled"] is True
    assert out["previous_boot_log_available"] is True
    assert "Hardware Error" in out["kernel_warnings"]
    assert "Killed process" in out["oom_events"]
    assert "pvedaemon" in out["system_errors"]


async def test_proxmox_boot_diagnostics_no_persistent_journal(monkeypatch):
    output = "\n".join([
        "===PERSISTENT===",
        "no",
        "===BOOTS===",
        "0 abc123 Wed 2026-07-22 boot",
        "===KERNEL_WARN===",
        "Failed to determine boot id from '-1' relative to current",
        "===OOM===",
        "(none found)",
        "===SYS_ERR===",
        "Failed to determine boot id from '-1' relative to current",
    ])
    patch_ssh(monkeypatch, infra, output)
    out = await load_tools(infra)["proxmox_boot_diagnostics"]()
    assert out["persistent_journal_enabled"] is False
    assert out["previous_boot_log_available"] is False


async def test_proxmox_boot_diagnostics_ssh_error(monkeypatch):
    async def fake_ssh_run(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(infra, "ssh_run", fake_ssh_run)
    out = await load_tools(infra)["proxmox_boot_diagnostics"]()
    assert "error" in out


async def test_proxmox_disk_topology(monkeypatch):
    output = "\n".join([
        "===ZPOOL===",
        "  pool: Tank",
        " state: ONLINE",
        "  scan: scrub repaired 0B in 04:12:00 with 0 errors",
        "config:",
        "        NAME        STATE     READ WRITE CKSUM",
        "        Tank        ONLINE       0     0     0",
        "          sdc       ONLINE       0     0     0",
        "          sdf       ONLINE       0     0     0",
        "===LSBLK===",
        "NAME SERIAL       WWN                MODEL              SIZE",
        "sdc  ZA1234AB     0x5000c50092ed4199 ST8000AS0002-1NA17Z 7.3T",
        "sde  ZA5678CD     0x5000c500a1b44bdb ST8000AS0002-1NA17Z 7.3T",
        "===BYPATH===",
        "pci-0000:00:17.0-ata-3 -> ../../sdc",
        "pci-0000:00:17.0-ata-5 -> ../../sde",
    ])
    patch_ssh(monkeypatch, infra, output)
    out = await load_tools(infra)["proxmox_disk_topology"]()
    assert "Tank" in out["zpool_status"] and "sdc" in out["zpool_status"]
    assert "sde" not in out["zpool_status"]  # not a Tank member in this fixture
    assert "ZA5678CD" in out["disk_serials"]
    assert "ata-5" in out["disk_by_path"]


async def test_proxmox_disk_topology_ssh_error(monkeypatch):
    async def fake_ssh_run(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(infra, "ssh_run", fake_ssh_run)
    out = await load_tools(infra)["proxmox_disk_topology"]()
    assert "error" in out
