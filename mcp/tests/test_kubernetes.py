import httpx

from nest_mcp.tools import kubernetes
from helpers import load_tools


def _patch(monkeypatch, routes):
    def handler(req):
        path = req.url.path
        for k, v in routes.items():
            if path == k or (k.endswith("*") and path.startswith(k[:-1])):
                return httpx.Response(200, json=v)
        return httpx.Response(404, json={"items": []})
    monkeypatch.setattr(kubernetes, "_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                  base_url="https://k8s"))


async def test_pods_state_classification(monkeypatch):
    _patch(monkeypatch, {"/api/v1/pods": {"items": [
        {"metadata": {"namespace": "media", "name": "radarr"},
         "spec": {"nodeName": "alpha", "containers": [{}]},
         "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 2}]}},
        {"metadata": {"namespace": "media", "name": "broken"},
         "spec": {"containers": [{}]},
         "status": {"phase": "Running",
                    "containerStatuses": [{"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
    ]}})
    out = await load_tools(kubernetes)["k8s_pods"]()
    by = {p["name"]: p for p in out}
    assert by["radarr"]["state"] == "Running" and by["radarr"]["restarts"] == 2
    assert by["broken"]["state"] == "CrashLoopBackOff"


async def test_pods_namespace_scoped(monkeypatch):
    _patch(monkeypatch, {"/api/v1/namespaces/media/pods": {"items": []}})
    out = await load_tools(kubernetes)["k8s_pods"](namespace="media")
    assert out == []


async def test_events_warning_filter_and_sort(monkeypatch):
    _patch(monkeypatch, {"/api/v1/events": {"items": [
        {"metadata": {"namespace": "media"}, "type": "Warning", "reason": "BackOff",
         "involvedObject": {"kind": "Pod", "name": "radarr"}, "message": "back-off", "lastTimestamp": "2026-06-15T00:00:00Z"},
    ]}})
    out = await load_tools(kubernetes)["k8s_events"]()
    assert out[0]["reason"] == "BackOff" and out[0]["object"] == "Pod/radarr"


async def test_nodes(monkeypatch):
    _patch(monkeypatch, {"/api/v1/nodes": {"items": [
        {"metadata": {"name": "alpha", "labels": {"kubernetes.io/arch": "amd64"}},
         "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
         "status": {"conditions": [{"type": "Ready", "status": "True"},
                                   {"type": "MemoryPressure", "status": "False"}],
                    "capacity": {"cpu": "4"}, "allocatable": {"cpu": "3800m"}}},
    ]}})
    out = await load_tools(kubernetes)["k8s_nodes"]()
    assert out[0]["ready"] is True and out[0]["arch"] == "amd64"
    assert out[0]["taints"] == ["node-role.kubernetes.io/control-plane:NoSchedule"]


def test_age_formatting():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    assert kubernetes._age((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")).endswith("m")
    assert kubernetes._age((now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")).endswith("h")
    assert kubernetes._age((now - timedelta(days=5)).isoformat().replace("+00:00", "Z")).endswith("d")
    assert kubernetes._age("") == ""
    assert kubernetes._age("garbage") == ""
