from nest_mcp.tools import prometheus
from helpers import load_tools, patch_http


async def test_query_unwraps_result(monkeypatch):
    patch_http(monkeypatch, prometheus, {
        "/api/v1/query": {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "42"]}]}},
    })
    fn = load_tools(prometheus)["prometheus_query"]
    out = await fn(query="up")
    assert out == {"status": "success", "result_type": "vector", "result": [{"value": [1, "42"]}]}


async def test_targets_sorted_and_shaped(monkeypatch):
    patch_http(monkeypatch, prometheus, {
        "/api/v1/targets": {"data": {"activeTargets": [
            {"labels": {"job": "node", "instance": "b"}, "health": "up", "lastScrape": "t2",
             "lastError": "", "lastScrapeDuration": 0.0123},
            {"labels": {"job": "node", "instance": "a"}, "health": "down", "lastScrape": "t1",
             "lastError": "boom", "lastScrapeDuration": 0.2},
        ]}},
    })
    fn = load_tools(prometheus)["prometheus_targets"]
    out = await fn()
    # sorted by (job, instance) -> a before b
    assert [t["instance"] for t in out] == ["a", "b"]
    assert out[0]["health"] == "down" and out[0]["last_error"] == "boom"
    assert out[0]["scrape_duration_ms"] == 200.0
    assert out[1]["scrape_duration_ms"] == 12.3


async def test_alerts_filters_to_firing(monkeypatch):
    patch_http(monkeypatch, prometheus, {
        "/api/v1/alerts": {"data": {"alerts": [
            {"state": "firing", "labels": {"alertname": "DiskFull", "severity": "critical", "instance": "x"},
             "annotations": {"summary": "disk full"}, "activeAt": "t"},
            {"state": "pending", "labels": {"alertname": "Warming"}, "annotations": {}},
        ]}},
    })
    fn = load_tools(prometheus)["prometheus_alerts"]
    out = await fn()
    assert len(out) == 1
    assert out[0]["name"] == "DiskFull"
    assert out[0]["severity"] == "critical"
    assert out[0]["summary"] == "disk full"
