import asyncio
import json

import httpx
from mcp.server.fastmcp import FastMCP

from nest_mcp import config
from nest_mcp.http_client import make_client
from nest_mcp.ssh_client import ssh_run
from nest_mcp.tools.unifi import get_session


async def _proxmox() -> dict:
    headers = {"Authorization": f"PVEAPIToken={config.proxmox.token}"}
    node = config.proxmox.node
    async with make_client(config.proxmox.url, verify=config.proxmox.verify_tls, headers=headers) as client:
        node_resp, lxc_resp, qemu_resp, storage_resp = await asyncio.gather(
            client.get(f"/api2/json/nodes/{node}/status"),
            client.get(f"/api2/json/nodes/{node}/lxc"),
            client.get(f"/api2/json/nodes/{node}/qemu"),
            client.get(f"/api2/json/nodes/{node}/storage"),
        )
        for r in (node_resp, lxc_resp, qemu_resp, storage_resp):
            r.raise_for_status()

        d = node_resp.json()["data"]
        containers = lxc_resp.json()["data"]
        vms = qemu_resp.json()["data"]
        stores = storage_resp.json()["data"]

    stopped_ctrs = [c.get("name", str(c["vmid"])) for c in containers if c["status"] != "running"]
    stopped_vms = [v.get("name", str(v["vmid"])) for v in vms if v["status"] != "running"]

    return {
        "node": {
            "cpu_pct": round(d.get("cpu", 0) * 100, 1),
            "mem_used_gb": round(d.get("memory", {}).get("used", 0) / 1024**3, 2),
            "mem_total_gb": round(d.get("memory", {}).get("total", 0) / 1024**3, 2),
            "uptime_hours": round(d.get("uptime", 0) / 3600, 1),
        },
        "containers": {
            "running": len(containers) - len(stopped_ctrs),
            "stopped": len(stopped_ctrs),
            "stopped_names": stopped_ctrs,
        },
        "vms": {
            "running": len(vms) - len(stopped_vms),
            "stopped": len(stopped_vms),
            "stopped_names": stopped_vms,
        },
        "storage": [
            {"name": s["storage"], "used_pct": round(s.get("used_fraction", 0) * 100, 1)}
            for s in sorted(stores, key=lambda x: x["storage"])
        ],
    }


async def _pbs() -> dict:
    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        ticket_resp = await client.post(
            f"{config.pbs.url}/api2/json/access/ticket",
            data={"username": config.pbs.username, "password": config.pbs.password},
        )
        ticket_resp.raise_for_status()
        ticket = ticket_resp.json()["data"]["ticket"]
        headers = {"Cookie": f"PBSAuthCookie={ticket}"}

        tasks_resp, ds_resp = await asyncio.gather(
            client.get(
                f"{config.pbs.url}/api2/json/nodes/{config.pbs.node}/tasks",
                headers=headers,
                params={"limit": 5, "typefilter": "backup"},
            ),
            client.get(f"{config.pbs.url}/api2/json/admin/datastore", headers=headers),
        )
        tasks_resp.raise_for_status()
        ds_resp.raise_for_status()

    tasks = tasks_resp.json().get("data", [])[:3]
    datastores = ds_resp.json().get("data", [])

    return {
        "recent": [
            {
                "worker_id": t.get("worker_id"),
                "status": t.get("status"),
                "started": t.get("starttime"),
            }
            for t in tasks
        ],
        "datastores": [
            {
                "name": d.get("store"),
                "used_gb": round(d.get("used", 0) / 1024**3, 1),
                "avail_gb": round(d.get("avail", 0) / 1024**3, 1),
            }
            for d in datastores
        ],
    }


async def _scrutiny() -> list[dict]:
    async with make_client(config.scrutiny.url) as client:
        resp = await client.get("/api/summary")
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("summary", {})

    result = []
    for _, info in data.items():
        device = info.get("device", {})
        smart = info.get("smart", {})
        result.append({
            "device": device.get("device_name", ""),
            "model": device.get("model_name", ""),
            "smart_status": smart.get("Status", 0),
            "temp_c": smart.get("temp", 0),
        })
    return sorted(result, key=lambda x: x["device"])


def _parse_docker_ps(output: str) -> dict:
    containers = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    stopped = [c["name"] for c in containers if not c.get("status", "").startswith("Up")]
    return {
        "running": len(containers) - len(stopped),
        "stopped": len(stopped),
        "stopped_names": stopped,
    }


_DOCKER_PS_FMT = r"""docker ps -a --format '{"name":"{{.Names}}","status":"{{.Status}}"}'"""


async def _docker_lxc() -> dict:
    output = await ssh_run(config.docker_host.host, _DOCKER_PS_FMT, key=config.docker_host.ssh_key)
    return _parse_docker_ps(output)


async def _vps() -> dict:
    docker_out, wg_out, f2b_out = await asyncio.gather(
        ssh_run(config.vps.host, _DOCKER_PS_FMT, user=config.vps.ssh_user, key=config.vps.ssh_key),
        ssh_run(config.vps.host, "wg show 2>/dev/null || echo 'wg not available'", user=config.vps.ssh_user, key=config.vps.ssh_key),
        ssh_run(config.vps.host, "fail2ban-client status 2>/dev/null | grep -E 'Number of jail|Jail list' || echo 'n/a'", user=config.vps.ssh_user, key=config.vps.ssh_key),
        return_exceptions=True,
    )

    docker = _parse_docker_ps(docker_out) if isinstance(docker_out, str) else {"error": str(docker_out)}
    wireguard = wg_out.strip() if isinstance(wg_out, str) else f"error: {wg_out}"
    fail2ban = f2b_out.strip() if isinstance(f2b_out, str) else f"error: {f2b_out}"

    return {"docker": docker, "wireguard": wireguard, "fail2ban": fail2ban}


async def _unifi() -> dict:
    session = get_session()
    health_resp, devices_resp = await asyncio.gather(
        session.get("/proxy/network/api/s/default/stat/health"),
        session.get("/proxy/network/api/s/default/stat/device"),
    )
    health = health_resp.json().get("data", [])
    devices_data = devices_resp.json().get("data", [])

    wan: dict = {}
    wired = 0
    wireless = 0
    for s in health:
        name = s.get("subsystem", "")
        if name == "wan":
            wan = {"status": s.get("status", ""), "ip": s.get("wan_ip", "")}
        elif name == "lan":
            wired = s.get("num_user", 0)
        elif name == "wlan":
            wireless = s.get("num_user", 0)

    devices = [
        {
            "name": d.get("name", d.get("hostname", "")),
            "satisfaction": d.get("satisfaction", 0),
            "state": d.get("state", 0),
        }
        for d in sorted(devices_data, key=lambda x: x.get("name", ""))
    ]

    return {
        "wan": wan,
        "clients": {"wired": wired, "wireless": wireless},
        "devices": devices,
    }


async def _homeassistant() -> dict:
    headers = {"Authorization": f"Bearer {config.homeassistant.token}"}
    async with make_client(config.homeassistant.url, headers=headers) as client:
        resp = await client.get("/api/states")
        resp.raise_for_status()
        states = resp.json()

    unavailable = sorted(s["entity_id"] for s in states if s["state"] == "unavailable")
    return {
        "total_entities": len(states),
        "unavailable": len(unavailable),
        "unavailable_names": unavailable[:10],
    }


async def _dns() -> dict:
    auth = (config.adguard.username, config.adguard.password)
    async with make_client(config.adguard.url) as client:
        resp = await client.get("/control/stats", auth=auth)
        resp.raise_for_status()
        d = resp.json()

    queries = d.get("num_dns_queries", 0)
    blocked = d.get("num_blocked_filtering", 0)
    return {
        "queries_today": queries,
        "blocked_pct": round(blocked / queries * 100, 1) if queries else 0.0,
        "avg_processing_ms": round(d.get("avg_processing_time", 0) * 1000, 2),
    }


async def _alerts() -> list[dict]:
    async with make_client(config.prometheus.url) as client:
        resp = await client.get("/api/v1/alerts")
        resp.raise_for_status()
        alerts = resp.json().get("data", {}).get("alerts", [])

    return [
        {
            "name": a.get("labels", {}).get("alertname", ""),
            "severity": a.get("labels", {}).get("severity", ""),
            "instance": a.get("labels", {}).get("instance", ""),
            "summary": a.get("annotations", {}).get("summary", ""),
        }
        for a in alerts
        if a.get("state") == "firing"
    ]


def _overall_status(alerts: list, sections: dict) -> str:
    if any(a.get("severity") == "critical" for a in alerts):
        return "critical"
    has_error = any(isinstance(v, dict) and "error" in v for k, v in sections.items() if k != "alerts")
    if alerts or has_error:
        return "degraded"
    return "ok"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def lab_health_summary() -> dict:
        """Single-call homelab health summary covering Proxmox, PBS, disks, Docker, VPS, UniFi, Home Assistant, DNS, and Prometheus alerts. Use this to orient at the start of a session."""
        keys = ["proxmox", "backups", "disks", "docker", "vps", "unifi", "homeassistant", "dns", "alerts"]
        raw = await asyncio.gather(
            _proxmox(),
            _pbs(),
            _scrutiny(),
            _docker_lxc(),
            _vps(),
            _unifi(),
            _homeassistant(),
            _dns(),
            _alerts(),
            return_exceptions=True,
        )
        sections = {
            k: (v if not isinstance(v, Exception) else {"error": str(v)})
            for k, v in zip(keys, raw)
        }
        alerts = sections["alerts"] if isinstance(sections["alerts"], list) else []
        return {"status": _overall_status(alerts, sections), **sections}
