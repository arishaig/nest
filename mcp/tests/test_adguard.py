from nest_mcp.tools import adguard
from helpers import load_tools, patch_http


def _routes():
    return {
        "/control/stats": {"num_dns_queries": 1000, "num_blocked_filtering": 100,
                           "avg_processing_time": 0.005,
                           "top_queried_domains": [{"d": 1}] * 20},
        "/control/rewrite/list": [{"domain": "z.test", "answer": "1.1.1.1"},
                                  {"domain": "a.test", "answer": "2.2.2.2"}],
        "/control/querylog": {"data": [
            {"time": "t", "question": {"name": "x.test", "type": "A"},
             "answer": [{"value": "1.2.3.4"}], "client": "c", "reason": "FilteredBlackList", "elapsedMs": 1.234},
        ]},
        "/control/rewrite/add": {},
        "/control/rewrite/delete": {},
    }


async def test_stats_both_instances(monkeypatch):
    patch_http(monkeypatch, adguard, _routes())
    out = await load_tools(adguard)["adguard_stats"]()
    assert out["primary"]["num_dns_queries"] == 1000
    assert out["primary"]["avg_processing_time_ms"] == 5.0
    assert len(out["primary"]["top_queried_domains"]) == 10  # capped
    assert out["secondary"]["num_blocked_filtering"] == 100


async def test_list_rewrites_sorted(monkeypatch):
    patch_http(monkeypatch, adguard, _routes())
    out = await load_tools(adguard)["adguard_list_rewrites"]()
    assert [r["domain"] for r in out] == ["a.test", "z.test"]


async def test_query_log_blocked_flag(monkeypatch):
    patch_http(monkeypatch, adguard, _routes())
    out = await load_tools(adguard)["adguard_query_log"](search="x")
    assert out[0]["question"] == "x.test"
    assert out[0]["blocked"] is True
    assert out[0]["answer"] == ["1.2.3.4"]


async def test_add_and_delete_rewrite(monkeypatch):
    patch_http(monkeypatch, adguard, _routes())
    tools = load_tools(adguard)
    assert (await tools["adguard_add_rewrite"](domain="a.test", answer="1.1.1.1"))["added"] is True
    assert (await tools["adguard_delete_rewrite"](domain="a.test", answer="1.1.1.1"))["deleted"] is True
