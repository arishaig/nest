from nest_mcp.tools import scrutiny, loki
from helpers import load_tools, patch_http


async def test_scrutiny_summary_shapes_and_sorts(monkeypatch):
    patch_http(monkeypatch, scrutiny, {
        "/api/summary": {"data": {"summary": {
            "wwn-z": {"device": {"device_name": "sdb", "model_name": "WD", "capacity": 2 * 1024**3},
                      "smart": {"Status": 0, "temp": 35, "power_on_hours": 100}},
            "wwn-a": {"device": {"device_name": "sda", "model_name": "ST", "capacity": 1024**3},
                      "smart": {"Status": 1, "temp": 40, "power_on_hours": 200}},
        }}},
    })
    out = await load_tools(scrutiny)["scrutiny_summary"]()
    assert [d["device_name"] for d in out] == ["sda", "sdb"]
    assert out[0]["capacity_gb"] == 1 and out[1]["capacity_gb"] == 2
    assert out[0]["smart_status"] == 1


async def test_scrutiny_disk_detail_attrs(monkeypatch):
    patch_http(monkeypatch, scrutiny, {
        "/api/device/*": {"data": {
            "device": {"device_name": "sda", "model_name": "ST", "firmware": "v1", "capacity": 1024**3},
            "smart_results": [{"attrs": {"5": {"attribute_name": "Reallocated", "value": 100,
                                               "worst": 100, "thresh": 10, "raw_value": 0, "status": 0}}}],
        }},
    })
    out = await load_tools(scrutiny)["scrutiny_disk_detail"](wwn="wwn-a")
    assert out["firmware"] == "v1"
    assert out["smart_attributes"]["5"]["name"] == "Reallocated"
    assert out["smart_attributes"]["5"]["threshold"] == 10


async def test_loki_builds_logql_and_sorts(monkeypatch):
    patch_http(monkeypatch, loki, {
        "/loki/api/v1/query_range": {"data": {"result": [
            {"stream": {"host": "docker", "container": "jellyfin"},
             "values": [["2000000000", "older"], ["3000000000", "newer"]]},
        ]}},
    })
    out = await load_tools(loki)["loki_logs"](container="jellyfin", host="docker", lines=10)
    assert out["lines_returned"] == 2
    # newest first
    assert out["logs"][0]["line"] == "newer"
    assert out["logs"][0]["container"] == "jellyfin"
    assert '{host="docker", container="jellyfin"}' == out["query"]


async def test_loki_raw_query_overrides_and_clamps(monkeypatch):
    patch_http(monkeypatch, loki, {"/loki/api/v1/query_range": {"data": {"result": []}}})
    out = await load_tools(loki)["loki_logs"](query='{job="x"}', lines=9999, since_minutes=0)
    assert out["query"] == '{job="x"}'
    assert out["lines_returned"] == 0
