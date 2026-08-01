from nest_mcp.tools import musicbrainz
from helpers import load_tools, patch_ssh


def _status_output(running_line: str, log_lines: list[str], counts: dict) -> str:
    parts = ["===RUNNING===", running_line, "===LOG==="]
    parts.extend(log_lines)
    parts.append("===COUNTS===")
    for name in musicbrainz._COLLECTIONS:
        value = counts.get(name, "")
        parts.append(f"{name}:{value}")
    return "\n".join(parts)


async def test_status_idle_and_fully_populated(monkeypatch):
    counts = {c: 1000 for c in musicbrainz._COLLECTIONS}
    raw = _status_output("", ["2026-08-01 12:00:00: Importing work..."], counts)
    patch_ssh(monkeypatch, musicbrainz, raw)

    out = await load_tools(musicbrainz)["musicbrainz_reindex_status"]()

    assert out["running"] is False
    assert out["current_step"] == "Importing work..."
    assert out["collection_counts"]["recording"] == 1000
    assert out["empty_collections"] == []
    assert out["all_populated"] is True


async def test_status_running_with_empty_collections(monkeypatch):
    counts = {c: 1000 for c in musicbrainz._COLLECTIONS}
    counts["recording"] = 0
    counts["release"] = 0
    raw = _status_output(
        "12345 python -m sir reindex",
        ["2026-08-01 12:00:00: Checking whether the versions of the Solr cores are supported"],
        counts,
    )
    patch_ssh(monkeypatch, musicbrainz, raw)

    out = await load_tools(musicbrainz)["musicbrainz_reindex_status"]()

    assert out["running"] is True
    assert out["current_step"] == "Checking whether the versions of the Solr cores are supported"
    assert sorted(out["empty_collections"]) == ["recording", "release"]
    assert out["all_populated"] is False


async def test_status_tolerates_query_failure(monkeypatch):
    counts = {c: 1000 for c in musicbrainz._COLLECTIONS}
    counts["work"] = ""  # e.g. curl failed, grep found nothing
    raw = _status_output("", [], counts)
    patch_ssh(monkeypatch, musicbrainz, raw)

    out = await load_tools(musicbrainz)["musicbrainz_reindex_status"]()

    assert out["collection_counts"]["work"] is None
    assert "work" not in out["empty_collections"]  # None isn't 0 — distinct failure mode


async def test_status_ssh_error(monkeypatch):
    async def fake_ssh_run(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(musicbrainz, "ssh_run", fake_ssh_run)

    out = await load_tools(musicbrainz)["musicbrainz_reindex_status"]()
    assert "error" in out


async def test_start_refuses_when_already_running(monkeypatch):
    patch_ssh(monkeypatch, musicbrainz, "12345 python -m sir reindex")
    out = await load_tools(musicbrainz)["musicbrainz_reindex_start"]()
    assert out["started"] is False
    assert "already running" in out["error"]


async def test_start_launches_when_idle(monkeypatch):
    calls = []

    def responder(host, cmd):
        calls.append(cmd)
        if "pgrep" in cmd:
            return ""
        return ""

    patch_ssh(monkeypatch, musicbrainz, responder)
    out = await load_tools(musicbrainz)["musicbrainz_reindex_start"]()

    assert out["started"] is True
    assert len(calls) == 2
    assert "sir reindex" in calls[1]
    assert "disown" in calls[1]


async def test_start_ssh_error_on_launch(monkeypatch):
    call_count = 0

    async def fake_ssh_run(host, cmd, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ""
        raise RuntimeError("connection lost")

    monkeypatch.setattr(musicbrainz, "ssh_run", fake_ssh_run)
    out = await load_tools(musicbrainz)["musicbrainz_reindex_start"]()
    assert out["started"] is False
    assert "error" in out
