from nest_mcp.tools import arr
from helpers import load_tools, patch_http


async def test_status(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/system/status": {"version": "4.0", "osName": "linux"}})
    out = await load_tools(arr)["arr_status"](service="sonarr")
    assert out["service"] == "sonarr" and out["version"] == "4.0"


async def test_queue_shapes_items(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/queue": {"totalRecords": 1, "records": [
        {"title": "Show.S01E01", "status": "downloading", "trackedDownloadStatus": "ok",
         "size": 1024 * 1024 * 500, "sizeleft": 1024 * 1024 * 100, "timeleft": "00:10:00",
         "protocol": "usenet", "indexer": "nzbgeek"},
    ]}})
    out = await load_tools(arr)["arr_queue"](service="radarr")
    assert out["total"] == 1
    assert out["items"][0]["size_mb"] == 500 and out["items"][0]["sizeleft_mb"] == 100


async def test_history(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/history": {"records": [
        {"date": "t", "eventType": "grabbed", "sourceTitle": "X",
         "quality": {"quality": {"name": "1080p"}}, "data": {"indexer": "nzbgeek"}},
    ]}})
    out = await load_tools(arr)["arr_history"](service="sonarr", limit=5)
    assert out[0]["event_type"] == "grabbed" and out[0]["quality"] == "1080p"


async def test_prowlarr_indexers_sorted(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v1/indexer": [
        {"id": 2, "name": "Zeta", "enable": True, "protocol": "usenet"},
        {"id": 1, "name": "Alpha", "enable": False, "protocol": "torrent",
         "status": {"lastRssSyncMessage": "ok"}},
    ]})
    out = await load_tools(arr)["prowlarr_indexers"]()
    assert [i["name"] for i in out] == ["Alpha", "Zeta"]
    assert out[0]["status"] == "ok" and out[0]["enabled"] is False
