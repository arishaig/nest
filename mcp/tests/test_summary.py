import httpx

from nest_mcp.tools import summary, kubernetes
from helpers import load_tools, patch_http, patch_ssh


# ---- pure helpers -------------------------------------------------------

def test_parse_docker_ps():
    out = summary._parse_docker_ps(
        '{"name":"a","status":"Up 2 days"}\n{"name":"b","status":"Exited (0)"}\nbad\n')
    assert out["running"] == 1 and out["stopped"] == 1 and out["stopped_names"] == ["b"]


def test_parse_adguard_stats():
    out = summary._parse_adguard_stats(
        {"num_dns_queries": 1000, "num_blocked_filtering": 250, "avg_processing_time": 0.004})
    assert out["queries_today"] == 1000 and out["blocked_pct"] == 25.0
    assert out["avg_processing_ms"] == 4.0
    assert summary._parse_adguard_stats({})["blocked_pct"] == 0.0


def test_overall_status_branches():
    assert summary._overall_status([{"severity": "critical"}], {}) == "critical"
    assert summary._overall_status([{"severity": "warning"}], {}) == "degraded"
    assert summary._overall_status([], {"proxmox": {"error": "x"}}) == "degraded"
    assert summary._overall_status(
        [], {"k8s": {"scraped_services": {"down": 1}}}) == "degraded"
    assert summary._overall_status(
        [], {"k8s_nodes": [{"name": "alpha", "ready": False}]}) == "degraded"
    assert summary._overall_status(
        [], {"k8s_nodes": [{"name": "alpha", "ready": True}],
             "monitoring": {"prometheus": "ok", "loki": "ok", "grafana": "ok",
                            "scrape_targets_down": 0}}) == "ok"
    assert summary._overall_status(
        [], {"monitoring": {"prometheus": "ok", "loki": "ok", "grafana": "ok",
                            "scrape_targets_down": 0}}) == "ok"


# ---- combined backend stub for the aggregate tool ----------------------

def _make_client_routes():
    return {
        "/api2/json/nodes/proxmox/status": {"data": {"cpu": 0.1, "memory": {"used": 4 * 1024**3, "total": 16 * 1024**3}, "uptime": 7200}},
        "/api2/json/nodes/proxmox/lxc": {"data": [{"vmid": 100, "name": "docker", "status": "running"}]},
        "/api2/json/nodes/proxmox/qemu": {"data": [{"vmid": 110, "name": "alpha", "status": "running"}]},
        "/api2/json/nodes/proxmox/storage": {"data": [{"storage": "local", "used_fraction": 0.5}]},
        "/api/summary": {"data": {"summary": {"w": {"device": {"device_name": "sda"}, "smart": {"Status": 0, "temp": 30}}}}},
        "/api/v1/query": {"data": {"result": []}},
        "/api/overview": {"http": {"routers": {"total": 5, "errors": 0}, "services": {"total": 5, "errors": 0}}},
        "/-/healthy": {},
        "/ready": {},
        "/api/health": {},
        "/api/states": [{"entity_id": "sensor.x", "state": "on"}],
        "/control/stats": {"num_dns_queries": 100, "num_blocked_filtering": 10, "avg_processing_time": 0.001},
        "/api/v1/alerts": {"data": {"alerts": []}},
    }


def _pbs_handler(request):
    p = request.url.path
    if p.endswith("/access/ticket"):
        return httpx.Response(200, json={"data": {"ticket": "T"}})
    if p.endswith("/tasks"):
        return httpx.Response(200, json={"data": []})
    return httpx.Response(200, json={"data": [{"store": "ds", "used": 1024**3, "avail": 1024**3}]})


class _FakeSession:
    async def get(self, path, **kwargs):
        if path.endswith("/stat/health"):
            return httpx.Response(200, json={"data": [{"subsystem": "wan", "status": "ok", "wan_ip": "1.2.3.4"},
                                                      {"subsystem": "lan", "num_user": 5}]})
        return httpx.Response(200, json={"data": [{"name": "ap1", "satisfaction": 99, "type": "uap"}]})


def _k8s_handler(request):
    path = request.url.path
    if path == "/api/v1/nodes":
        return httpx.Response(200, json={"items": [
            {"metadata": {"name": "alpha"},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]})
    if path == "/api/v1/pods":
        return httpx.Response(200, json={"items": [
            {"metadata": {"namespace": "media", "name": "radarr"},
             "spec": {"nodeName": "alpha", "containers": [{}]},
             "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}},
        ]})
    return httpx.Response(404, json={})


async def test_k8s_nodes_section(monkeypatch):
    monkeypatch.setattr(kubernetes, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_k8s_handler),
                                                  base_url="https://k8s"))
    out = await summary._k8s_nodes()
    assert out == [{"name": "alpha", "ready": True, "pods_total": 1, "pods_running": 1,
                     "pods_completed": 0, "pods_not_ready": 0, "problem_pods": []}]


async def test_lab_health_summary_aggregates(monkeypatch):
    patch_http(monkeypatch, summary, _make_client_routes())
    monkeypatch.setattr(kubernetes, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_k8s_handler),
                                                  base_url="https://k8s"))
    # NOTE: summary.httpx is the global httpx module, shared with the helper that
    # builds make_client's mock. Honor an explicit transport (make_client's case)
    # and only fall back to the PBS handler for _pbs's transport-less construction.
    real = httpx.AsyncClient

    def fake_async_client(*a, **k):
        transport = k.get("transport") or httpx.MockTransport(_pbs_handler)
        return real(transport=transport, base_url=k.get("base_url", ""))
    monkeypatch.setattr(summary.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(summary, "get_session", lambda: _FakeSession())

    def ssh_responder(host, cmd):
        if "docker ps" in cmd:
            return '{"name":"caddy","status":"Up 1 day"}'
        return "ok"
    patch_ssh(monkeypatch, summary, ssh_responder)

    out = await load_tools(summary)["lab_health_summary"]()
    assert out["status"] in ("ok", "degraded")
    assert out["proxmox"]["containers"]["running"] == 1
    assert out["disks"][0]["device"] == "sda"
    assert out["vps"]["docker"]["running"] == 1
    assert out["unifi"]["wan"]["ip"] == "1.2.3.4"
    assert out["dns"]["primary"]["queries_today"] == 100
    assert out["homeassistant"]["total_entities"] == 1
    assert out["k8s_nodes"][0]["name"] == "alpha" and out["k8s_nodes"][0]["pods_total"] == 1


async def test_proxmox_section_directly(monkeypatch):
    patch_http(monkeypatch, summary, _make_client_routes())
    out = await summary._proxmox()
    assert out["node"]["mem_total_gb"] == 16.0
    assert out["storage"][0]["used_pct"] == 50.0
