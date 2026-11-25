import httpx
import pytest

from nest_mcp import config
from nest_mcp.tools import sabnzbd
from helpers import load_tools, patch_http


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(config.sabnzbd, "key", "SECRETKEY")


def _by_mode(**modes):
    """Route /api by ?mode= (SAB has one endpoint)."""
    def handler(request: httpx.Request):
        assert request.url.params["output"] == "json"
        return modes[request.url.params["mode"]]
    return {"/api": handler}


# Shapes follow SABnzbd's documented JSON API (queue / history / warnings / get_config).
QUEUE = {"queue": {
    "status": "Downloading", "paused": False, "speed": "12.3 M", "speedlimit": "", "noofslots_total": 2,
    "mbleft": "100.5", "timeleft": "0:10:00", "diskspace1": "250.00", "diskspace2": "1800.50",
    "slots": [
        {"nzo_id": "SABnzbd_nzo_a", "filename": "Movie.2160p", "cat": "manual", "status": "Downloading",
         "mb": "40000", "mbleft": "100.5", "percentage": "99", "timeleft": "0:10:00", "priority": "Normal"},
        {"nzo_id": "SABnzbd_nzo_b", "filename": "Other", "cat": "movies", "status": "Paused",
         "mb": "10", "mbleft": "0", "percentage": "100", "timeleft": "0:00:00", "priority": "Low"},
    ],
}}


async def test_queue_rows(monkeypatch):
    patch_http(monkeypatch, sabnzbd, _by_mode(queue=QUEUE))
    out = await load_tools(sabnzbd)["sabnzbd_queue"]()
    assert out["total"] == 2 and not out["paused"]
    assert out["jobs"][0]["nzo_id"] == "SABnzbd_nzo_a"
    assert out["jobs"][0]["category"] == "manual" and out["jobs"][0]["mb_left"] == 100 and out["jobs"][1]["mb_left"] == 0


async def test_status_combines_queue_warnings_and_dirs(monkeypatch):
    patch_http(monkeypatch, sabnzbd, _by_mode(
        queue=QUEUE,
        warnings={"warnings": ["WARNING: disk almost full"]},
        get_config={"config": {"misc": {"download_dir": "/data/usenet/incomplete", "complete_dir": "/data/usenet/complete",
                                         "api_key": "MUST-NOT-LEAK"}}},
    ))
    out = await load_tools(sabnzbd)["sabnzbd_status"]()
    assert out["free_gb_incomplete"] == 250.0 and out["free_gb_complete"] == 1800.5
    assert out["complete_dir"] == "/data/usenet/complete"
    assert out["warnings"] == ["WARNING: disk almost full"]
    assert "MUST-NOT-LEAK" not in str(out)


async def test_history_filters_and_shapes(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return {"history": {"slots": [{"nzo_id": "n1", "name": "X", "category": "manual", "status": "Failed",
                                        "fail_message": "aborted", "storage": "/data/usenet/complete/X", "completed": 0}]}}
    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    out = await load_tools(sabnzbd)["sabnzbd_history"](limit=5, category="manual")
    assert seen["category"] == "manual" and seen["limit"] == "5"
    assert out[0]["fail_message"] == "aborted" and out[0]["storage"].endswith("/X")


async def test_http_error_never_contains_api_key(monkeypatch):
    patch_http(monkeypatch, sabnzbd, {"/api": (500, {"error": "boom"})})
    with pytest.raises(RuntimeError) as e:
        await load_tools(sabnzbd)["sabnzbd_queue"]()
    assert "SECRETKEY" not in str(e.value) and "HTTP 500" in str(e.value)


async def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(config.sabnzbd, "key", "")
    with pytest.raises(RuntimeError, match="NEST_SABNZBD_KEY"):
        await load_tools(sabnzbd)["sabnzbd_queue"]()


async def test_sab_api_error_body(monkeypatch):
    patch_http(monkeypatch, sabnzbd, {"/api": {"status": False, "error": "API Key Incorrect"}})
    with pytest.raises(RuntimeError, match="API Key Incorrect"):
        await load_tools(sabnzbd)["sabnzbd_queue"]()
