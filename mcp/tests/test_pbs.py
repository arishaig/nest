import httpx
from nest_mcp.tools import pbs
from helpers import load_tools


def _pbs_handler(request):
    p = request.url.path
    if p.endswith("/access/ticket"):
        return httpx.Response(200, json={"data": {"ticket": "T", "CSRFPreventionToken": "C"}})
    if p.endswith("/tasks"):
        return httpx.Response(200, json={"data": [
            {"id": "vm/100", "upid": "UPID:backup:" + "x" * 80, "status": "OK",
             "starttime": 1, "endtime": 2, "worker_id": "w"},
        ]})
    if p.endswith("/admin/datastore"):
        return httpx.Response(200, json={"data": [{"store": "ds1"}]})
    if p.endswith("/status"):
        return httpx.Response(200, json={"data": {"total": 2 * 1024**3, "used": 1024**3, "avail": 1024**3}})
    if p.endswith("/gc"):
        return httpx.Response(200, json={"data": {"last-run-state": "OK"}})
    return httpx.Response(404, json={"data": {}})


async def test_backup_status_aggregates(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(pbs.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(_pbs_handler)))
    out = await load_tools(pbs)["proxmox_backup_status"]()
    assert out["recent_backup_tasks"][0]["status"] == "OK"
    assert len(out["recent_backup_tasks"][0]["upid"]) <= 60  # truncated
    ds = out["datastores"][0]
    assert ds["name"] == "ds1"
    assert ds["total_gb"] == 2.0 and ds["used_gb"] == 1.0
    assert ds["gc_status"] == "OK"
