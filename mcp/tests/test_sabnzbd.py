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


async def test_job_action_delete_batches_and_validates(monkeypatch):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return {"status": True}

    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    t = load_tools(sabnzbd)["sabnzbd_job_action"]
    out = await t(nzo_ids=["SABnzbd_nzo_a", "SABnzbd_nzo_b"], action="delete", del_files=True)
    assert seen[0]["value"] == "SABnzbd_nzo_a,SABnzbd_nzo_b" and seen[0]["del_files"] == "1" and out["deleted_files"]
    await t(nzo_ids=["SABnzbd_nzo_a"], action="priority", priority="Force")
    assert seen[1]["name"] == "priority" and seen[1]["value2"] == "2"
    await t(nzo_ids=["SABnzbd_nzo_a", "SABnzbd_nzo_b"], action="pause")
    assert [c["name"] for c in seen[2:]] == ["pause", "pause"]
    await t(nzo_ids=["93ef1339-f354-4e14-ae53-3f55bfa0f76b"], action="delete")  # newer SABnzbd uses UUID ids
    assert seen[-1]["value"] == "93ef1339-f354-4e14-ae53-3f55bfa0f76b"
    with pytest.raises(ValueError):
        await t(nzo_ids=["not-an-id"], action="delete")
    with pytest.raises(ValueError):
        await t(nzo_ids=[], action="resume")


UUID = "93ef1339-f354-4e14-ae53-3f55bfa0f76b"


def test_check_nzo_ids_accepts_both_id_formats():
    sabnzbd.check_nzo_ids(["SABnzbd_nzo_a1", UUID])
    for bad in (["not-an-id"], [], [UUID.upper()], ["x"] * 21):
        with pytest.raises(ValueError):
            sabnzbd.check_nzo_ids(bad)


async def test_status_flags_jobs_stuck_at_zero_mb_left(monkeypatch):
    # Shape seen live on 2026-11-25: DIG episodes at 100% / 0 MB left but still "Downloading".
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "queue":
            return {"queue": {"status": "Downloading", "paused": False, "mbleft": "0", "slots": [
                {"nzo_id": UUID, "filename": "DIG.S01E01", "cat": "tv", "status": "Downloading", "mb": "4042", "mbleft": "0"},
                {"nzo_id": "b", "filename": "Fresh", "cat": "tv", "status": "Downloading", "mb": "4000", "mbleft": "100"},
                {"nzo_id": "c", "filename": "Paused one", "cat": "tv", "status": "Paused", "mb": "4000", "mbleft": "0"},
            ]}}
        if mode == "warnings":
            return {"warnings": []}
        return {"config": {"misc": {"download_dir": "/i", "complete_dir": "/c"}}}

    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    out = await load_tools(sabnzbd)["sabnzbd_status"]()
    assert [j["name"] for j in out["suspected_stuck"]] == ["DIG.S01E01"]
    assert "0 MB left" in out["stuck_note"] and "restart" in out["stuck_note"]


async def test_status_has_no_stuck_note_when_healthy(monkeypatch):
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "queue":
            return {"queue": {"status": "Idle", "slots": []}}
        return {"warnings": []} if mode == "warnings" else {"config": {"misc": {}}}

    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    out = await load_tools(sabnzbd)["sabnzbd_status"]()
    assert out["suspected_stuck"] == [] and out["stuck_note"] == ""


async def test_history_action_delete_and_retry(monkeypatch):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return {"status": True}

    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    t = load_tools(sabnzbd)["sabnzbd_history_action"]
    out = await t(nzo_ids=[UUID, "SABnzbd_nzo_old"], action="delete", del_files=True)
    assert seen[0]["mode"] == "history" and seen[0]["name"] == "delete"
    assert seen[0]["value"] == f"{UUID},SABnzbd_nzo_old" and seen[0]["del_files"] == "1" and out["deleted_files"]
    await t(nzo_ids=[UUID, "SABnzbd_nzo_old"], action="retry")
    assert [(c["mode"], c["value"]) for c in seen[1:]] == [("retry", UUID), ("retry", "SABnzbd_nzo_old")]
    with pytest.raises(ValueError):
        await t(nzo_ids=["nope"], action="delete")


def _sab_state(queue_slots, history_slots):
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "queue":
            return {"queue": {"slots": queue_slots}}
        return {"history": {"slots": history_slots}}
    return handler


async def test_wait_returns_on_completed_with_storage(monkeypatch):
    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    hist = [{"nzo_id": UUID, "name": "Job", "status": "Completed", "storage": "/data/usenet/complete/Job", "size": "9.7 GB", "fail_message": ""}]
    patch_http(monkeypatch, sabnzbd, {"/api": _sab_state([], hist)})
    out = await load_tools(sabnzbd)["sabnzbd_wait"](nzo_id=UUID, timeout_s=30)
    assert out["done"] and out["state"] == "Completed" and out["storage"] == "/data/usenet/complete/Job"


async def test_wait_returns_failed_with_message(monkeypatch):
    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    hist = [{"nzo_id": UUID, "name": "Job", "status": "Failed", "storage": "", "fail_message": "Repair failed, not enough repair blocks (2877 short)"}]
    patch_http(monkeypatch, sabnzbd, {"/api": _sab_state([], hist)})
    out = await load_tools(sabnzbd)["sabnzbd_wait"](nzo_id=UUID)
    assert out["done"] and out["state"] == "Failed" and "2877 short" in out["fail_message"]


async def test_wait_polls_through_verifying_then_completes(monkeypatch):
    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    monkeypatch.setattr(sabnzbd, "_WAIT_POLL_S", 0)
    states = iter(["Verifying", "Extracting", "Completed"])
    cur = {"s": next(states)}

    def handler(request):
        mode = request.url.params["mode"]
        if mode == "queue":
            return {"queue": {"slots": []}}
        slot = {"nzo_id": UUID, "name": "Job", "status": cur["s"], "storage": "/p", "fail_message": ""}
        if cur["s"] != "Completed":
            cur["s"] = next(states)
        return {"history": {"slots": [slot]}}

    patch_http(monkeypatch, sabnzbd, {"/api": handler})
    out = await load_tools(sabnzbd)["sabnzbd_wait"](nzo_id=UUID, timeout_s=30)
    assert out["done"] and out["state"] == "Completed"


async def test_wait_times_out_and_reports_stuck(monkeypatch):
    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    monkeypatch.setattr(sabnzbd, "_WAIT_POLL_S", 0)
    q = [{"nzo_id": UUID, "filename": "Job", "status": "Downloading", "mb": "100", "mbleft": "0", "percentage": "100"}]
    patch_http(monkeypatch, sabnzbd, {"/api": _sab_state(q, [])})
    out = await load_tools(sabnzbd)["sabnzbd_wait"](nzo_id=UUID, timeout_s=0)
    assert out["done"] is False and out["stuck_suspected"] is True and out["where"] == "queue"


async def test_wait_unknown_job(monkeypatch):
    monkeypatch.setattr(sabnzbd.config.sabnzbd, "key", "k")
    patch_http(monkeypatch, sabnzbd, {"/api": _sab_state([], [])})
    out = await load_tools(sabnzbd)["sabnzbd_wait"](nzo_id=UUID)
    assert out["state"] == "not_found"
