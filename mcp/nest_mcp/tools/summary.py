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


_K8S_JOBS = [
    "traefik-k8s", "authelia", "redis", "postgres",
    "sonarr", "radarr", "lidarr", "prowlarr", "bazarr",
]


async def _k8s() -> dict:
    scrape_query = 'up{job=~"' + "|".join(_K8S_JOBS) + '"}'
    # kube-state-metrics is scraped by Prometheus — use it for pod-phase summary
    pod_query = 'count by (phase) (kube_pod_status_phase{phase!="Running",phase!="Succeeded"} == 1)'
    node_query = 'kube_node_status_condition{condition="Ready",status="true"}'

    async with make_client(config.prometheus.url) as prom:
        async with make_client(config.traefik.url) as traefik:
            up_resp, overview_resp, pod_resp, node_resp = await asyncio.gather(
                prom.get("/api/v1/query", params={"query": scrape_query}),
                traefik.get("/api/overview"),
                prom.get("/api/v1/query", params={"query": pod_query}),
                prom.get("/api/v1/query", params={"query": node_query}),
                return_exceptions=True,
            )

    services: dict[str, str] = {}
    if isinstance(up_resp, httpx.Response):
        up_resp.raise_for_status()
        for r in up_resp.json().get("data", {}).get("result", []):
            job = r["metric"].get("job", "")
            services[job] = "up" if r["value"][1] == "1" else "down"

    down_names = [k for k, v in services.items() if v == "down"]

    traefik_info: dict = {"error": str(overview_resp)} if isinstance(overview_resp, Exception) else {}
    if isinstance(overview_resp, httpx.Response):
        overview_resp.raise_for_status()
        http = overview_resp.json().get("http", {})
        traefik_info = {
            "routers": http.get("routers", {}).get("total", 0),
            "router_errors": http.get("routers", {}).get("errors", 0),
            "services": http.get("services", {}).get("total", 0),
            "service_errors": http.get("services", {}).get("errors", 0),
        }

    unhealthy_pods: dict[str, int] = {}
    if isinstance(pod_resp, httpx.Response) and pod_resp.is_success:
        for r in pod_resp.json().get("data", {}).get("result", []):
            phase = r["metric"].get("phase", "")
            count = int(float(r["value"][1]))
            if count > 0:
                unhealthy_pods[phase] = count

    nodes_ready: list[str] = []
    nodes_not_ready: list[str] = []
    if isinstance(node_resp, httpx.Response) and node_resp.is_success:
        for r in node_resp.json().get("data", {}).get("result", []):
            node_name = r["metric"].get("node", "")
            if r["value"][1] == "1":
                nodes_ready.append(node_name)
            else:
                nodes_not_ready.append(node_name)

    return {
        "traefik": traefik_info,
        "scraped_services": {
            "up": len(services) - len(down_names),
            "down": len(down_names),
            "down_names": down_names,
        },
        "pods": {
            "unhealthy": unhealthy_pods,
        },
        "nodes": {
            "ready": len(nodes_ready),
            "not_ready": nodes_not_ready,
        },
    }


async def _monitoring() -> dict:
    async with make_client(config.prometheus.url) as prom:
        async with make_client(config.loki.url) as loki:
            async with make_client(config.grafana.url) as grafana:
                prom_health, loki_ready, grafana_health, down_q = await asyncio.gather(
                    prom.get("/-/healthy"),
                    loki.get("/ready"),
                    grafana.get("/api/health"),
                    prom.get("/api/v1/query", params={"query": "count(up == 0) or vector(0)"}),
                    return_exceptions=True,
                )

    def _status(resp: object) -> str:
        if isinstance(resp, Exception):
            return f"error: {resp}"
        assert isinstance(resp, httpx.Response)
        return "ok" if resp.status_code < 300 else f"error: {resp.status_code}"

    targets_down = 0
    if isinstance(down_q, httpx.Response):
        result = down_q.json().get("data", {}).get("result", [])
        if result:
            targets_down = int(float(result[0]["value"][1]))

    return {
        "prometheus": _status(prom_health),
        "loki": _status(loki_ready),
        "grafana": _status(grafana_health),
        "scrape_targets_down": targets_down,
    }


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
        if d.get("type", "") not in ("ugw", "udm", "uxg")
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


def _parse_adguard_stats(d: dict) -> dict:
    queries = d.get("num_dns_queries", 0)
    blocked = d.get("num_blocked_filtering", 0)
    return {
        "queries_today": queries,
        "blocked_pct": round(blocked / queries * 100, 1) if queries else 0.0,
        "avg_processing_ms": round(d.get("avg_processing_time", 0) * 1000, 2),
    }


async def _dns() -> dict:
    auth = (config.adguard.username, config.adguard.password)

    async def _fetch(url: str) -> dict:
        async with make_client(url, verify=config.adguard.verify_tls) as client:
            resp = await client.get("/control/stats", auth=auth)
            resp.raise_for_status()
            return _parse_adguard_stats(resp.json())

    primary, secondary = await asyncio.gather(
        _fetch(config.adguard.url),
        _fetch(config.adguard.url_secondary),
        return_exceptions=True,
    )
    return {
        "primary": primary if not isinstance(primary, Exception) else {"error": str(primary)},
        "secondary": secondary if not isinstance(secondary, Exception) else {"error": str(secondary)},
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
    k8s_degraded = sections.get("k8s", {}).get("scraped_services", {}).get("down", 0) > 0
    monitoring_degraded = any(
        v not in ("ok",) and isinstance(v, str)
        for v in [
            sections.get("monitoring", {}).get("prometheus", ""),
            sections.get("monitoring", {}).get("loki", ""),
            sections.get("monitoring", {}).get("grafana", ""),
        ]
    ) or sections.get("monitoring", {}).get("scrape_targets_down", 0) > 0
    if alerts or has_error or k8s_degraded or monitoring_degraded:
        return "degraded"
    return "ok"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def lab_health_summary() -> dict:
        """Single-call homelab health summary covering Proxmox, PBS, disks, k8s, VPS, UniFi, Home Assistant, DNS, monitoring stack, and Prometheus alerts. Use this to orient at the start of a session."""
        keys = ["proxmox", "backups", "disks", "k8s", "monitoring", "vps", "unifi", "homeassistant", "dns", "alerts"]
        raw = await asyncio.gather(
            _proxmox(),
            _pbs(),
            _scrutiny(),
            _k8s(),
            _monitoring(),
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
