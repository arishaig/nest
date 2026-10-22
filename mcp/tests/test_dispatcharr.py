import json

import pytest

from nest_mcp import config
from nest_mcp.tools import dispatcharr
from helpers import load_tools, patch_http

M3U = (
    '#EXTM3U x-tvg-url="x"\n'
    '#EXTINF:-1 tvg-id="5" tvg-name="Pluto Trending" tvg-chno="5" group-title="Entertainment",Pluto TV Trending Now\n'
    'https://dispatcharr.example/proxy/ts/stream/aaaa-1111\n'
    '#EXTINF:-1 tvg-id="4130" tvg-name="Cine" group-title="En Español",Cine adrenalina\n'
    'https://dispatcharr.example/proxy/ts/stream/bbbb-2222\n'
)


def _frame(rgb, w=96, h=54):
    return bytes(rgb) * (w * h)


BLACK = _frame((5, 5, 5))
GREY = _frame((120, 120, 120))


def _slate_frame():
    """Black frame with a block of yellow 'text' (~3% of pixels)."""
    px = bytearray(BLACK)
    for y in range(20, 26):
        for x in range(20, 76):
            i = (y * 96 + x) * 3
            px[i:i + 3] = bytes((230, 210, 20))
    return bytes(px)


def test_parse_m3u_extracts_fields():
    rows = dispatcharr._parse_m3u(M3U)
    assert rows[0] == {"number": "5", "tvg_id": "5", "name": "Pluto TV Trending Now", "group": "Entertainment",
                       "uuid": "aaaa-1111", "path": "/proxy/ts/stream/aaaa-1111"}
    assert rows[1]["number"] == "4130" and rows[1]["group"] == "En Español"   # no tvg-chno -> tvg-id


def test_select_by_number_name_group_search():
    rows = dispatcharr._parse_m3u(M3U)
    assert [r["uuid"] for r in dispatcharr._select(rows, ["4130"], "", "")] == ["bbbb-2222"]
    assert [r["uuid"] for r in dispatcharr._select(rows, ["Pluto TV Trending Now"], "", "")] == ["aaaa-1111"]
    assert len(dispatcharr._select(rows, None, "en español", "")) == 1
    assert len(dispatcharr._select(rows, None, "", "CINE")) == 1


def test_fingerprint_stable_and_hamming():
    h = dispatcharr._fingerprint(_slate_frame())
    assert h == dispatcharr._fingerprint(_slate_frame()) and len(h) == 36
    assert dispatcharr._hamming(h, h) == 0
    assert dispatcharr._hamming("0" * 36, "f" * 36) == 144
    # text on black must not hash like a flat frame (the weakness of a difference hash)
    assert dispatcharr._hamming(h, dispatcharr._fingerprint(GREY)) > dispatcharr._SLATE_MAX_DISTANCE


def test_analyse_flags_static_text_on_black_but_not_animated_cards():
    slate = _slate_frame()
    static = dispatcharr._analyse([slate, slate, slate], {})
    assert static["possible_slate"] is True and static["frozen"] is True
    moving = dispatcharr._analyse([slate, GREY, slate], {})           # picture changes -> not a slate
    assert moving["possible_slate"] is False and moving["frozen"] is False
    blank = dispatcharr._analyse([BLACK, BLACK], {})
    assert blank["blank_frames"] == 2 and blank["possible_slate"] is False


def test_analyse_matches_known_slate_by_hash():
    slate = _slate_frame()
    out = dispatcharr._analyse([GREY, slate], {"pt-error": dispatcharr._fingerprint(slate)})
    assert out["known_slate"] == "pt-error" and out["slate_distance"] == 0
    assert "known_slate" not in dispatcharr._analyse([GREY], {"pt-error": dispatcharr._fingerprint(slate)})


def test_stream_info_parsed_from_ffmpeg_log():
    log = "Stream #0:0: Video: h264 (High), yuv420p(tv), 1280x720 [SAR 1:1 DAR 16:9], 29.97 fps, 29.97 tbr"
    assert dispatcharr._stream_info(log) == {"video_codec": "h264", "resolution": "1280x720", "fps": 29.97}
    assert dispatcharr._stream_info("nothing") == {}


async def test_lineup_lists_groups(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {"/output/m3u": (200, M3U)})
    out = await load_tools(dispatcharr)["dispatcharr_lineup"](group="En Español")
    assert out["total"] == 1 and out["items"][0]["uuid"] == "bbbb-2222"
    allrows = await load_tools(dispatcharr)["dispatcharr_lineup"]()
    assert allrows["groups"] == {"Entertainment": 1, "En Español": 1}


async def test_probe_requires_ffmpeg(monkeypatch):
    monkeypatch.setattr(dispatcharr.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await load_tools(dispatcharr)["dispatcharr_probe_channels"]()


async def test_probe_reports_slate_error_and_stream_info(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {"/output/m3u": (200, M3U)})
    monkeypatch.setattr(dispatcharr.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    urls = []
    slate = _slate_frame()

    async def fake_capture(url, seconds):
        urls.append(url)
        if url.endswith("aaaa-1111"):
            return {"frames": [slate, slate, slate], "startup_s": 1.5,
                    "stderr": "Stream #0:0: Video: h264, yuv420p, 1280x720, 30 fps"}
        return {"frames": [], "stderr": "x\nServer returned 5XX Server Error reply\n", "startup_s": None}

    monkeypatch.setattr(dispatcharr, "_capture", fake_capture)
    out = await load_tools(dispatcharr)["dispatcharr_probe_channels"](pause=0)
    assert urls[0] == config.dispatcharr.url.rstrip("/") + "/proxy/ts/stream/aaaa-1111"
    assert out[0]["possible_slate"] is True and out[0]["resolution"] == "1280x720" and out[0]["startup_s"] == 1.5
    assert out[1]["error"].endswith("5XX Server Error reply") and "possible_slate" not in out[1]


async def test_probe_caps_limit_and_uses_passed_hashes(monkeypatch):
    big = "".join(
        f'#EXTINF:-1 tvg-id="{i}" group-title="G",Ch{i}\nhttps://d/proxy/ts/stream/u{i}\n' for i in range(40))
    patch_http(monkeypatch, dispatcharr, {"/output/m3u": (200, big)})
    monkeypatch.setattr(dispatcharr.shutil, "which", lambda _: "/x")
    seen = []

    async def fake_capture(url, seconds):
        seen.append(url)
        return {"frames": [GREY], "stderr": "", "startup_s": 0.5}

    monkeypatch.setattr(dispatcharr, "_capture", fake_capture)
    out = await load_tools(dispatcharr)["dispatcharr_probe_channels"](
        limit=1000, pause=0, slate_hashes={"s": dispatcharr._fingerprint(GREY)})
    assert len(out) == 25 == len(seen)
    assert out[0]["known_slate"] == "s"


async def test_capture_reads_frames_from_subprocess(monkeypatch):
    """_capture end to end against a stand-in `ffmpeg` that emits two frames and a log line."""
    import asyncio
    import os
    import stat
    import tempfile
    d = tempfile.mkdtemp()
    script = os.path.join(d, "ffmpeg")
    open(script, "w").write(
        "#!/bin/sh\necho 'Stream #0:0: Video: h264, yuv420p, 640x360, 25 fps' >&2\n"
        "head -c 15552 /dev/zero; head -c 15552 /dev/zero\n")
    os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", d + os.pathsep + os.environ["PATH"])
    out = await asyncio.wait_for(dispatcharr._capture("http://x/stream", 4), 20)
    assert len(out["frames"]) == 2 and out["startup_s"] is not None
    assert dispatcharr._stream_info(out["stderr"])["resolution"] == "640x360"


async def test_capture_with_no_output_returns_empty(monkeypatch):
    import os
    import stat
    import tempfile
    d = tempfile.mkdtemp()
    script = os.path.join(d, "ffmpeg")
    open(script, "w").write("#!/bin/sh\necho 'Server returned 404' >&2\nexit 1\n")
    os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", d + os.pathsep + os.environ["PATH"])
    out = await dispatcharr._capture("http://x", 4)
    assert out["frames"] == [] and out["startup_s"] is None and "404" in out["stderr"]


async def test_missing_key_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(config.dispatcharr, "key", "")
    patch_http(monkeypatch, dispatcharr, {"/api/channels/channels/": (401, {"detail": "no creds"})})
    with pytest.raises(RuntimeError, match="NEST_DISPATCHARR_KEY"):
        await load_tools(dispatcharr)["dispatcharr_channels"]()


async def test_api_error_surfaces_body_when_key_set(monkeypatch):
    monkeypatch.setattr(config.dispatcharr, "key", "k")
    patch_http(monkeypatch, dispatcharr, {"/api/channels/channels/": (500, {"detail": "boom"})})
    with pytest.raises(RuntimeError, match="boom"):
        await load_tools(dispatcharr)["dispatcharr_channels"]()


async def test_client_sends_api_key_header(monkeypatch):
    monkeypatch.setattr(config.dispatcharr, "key", "secret-key")
    captured = {}
    orig = dispatcharr.make_client

    def spy(base_url, **kwargs):
        captured.update(kwargs.get("headers") or {})
        return orig(base_url, **kwargs)

    patch_http(monkeypatch, dispatcharr, {"/api/channels/channels/": {"count": 0, "results": []}})
    patched = dispatcharr.make_client
    monkeypatch.setattr(dispatcharr, "make_client", lambda b, **kw: (captured.update(kw.get("headers") or {}), patched(b, **kw))[1])
    await load_tools(dispatcharr)["dispatcharr_channels"]()
    assert captured == {"X-API-Key": "secret-key"}


async def test_channels_and_streams_rows(monkeypatch):
    seen = []

    def channels(request):
        seen.append(dict(request.url.params))
        return {"count": 1, "next": "n", "results": [{
            "id": 7, "uuid": "u", "name": "raw", "effective_name": "Eff", "channel_number": 1,
            "effective_channel_number": 9, "streams": [3], "hidden_from_output": False}]}

    patch_http(monkeypatch, dispatcharr, {
        "/api/channels/channels/": channels,
        "/api/channels/streams/": {"count": 1, "next": None, "results": [{
            "id": 3, "name": "S", "stream_stats": {"resolution": "1280x720", "video_codec": "h264",
                                                    "source_fps": 30, "ffmpeg_output_bitrate": 2500},
            "is_stale": False, "current_viewers": 2, "stream_stats_updated_at": "t"}]},
    })
    t = load_tools(dispatcharr)
    out = await t["dispatcharr_channels"](search="x", group_id=4, page=2, page_size=10)
    assert seen == [{"page": "2", "page_size": "10", "search": "x", "channel_group": "4"}]
    assert out["has_next"] is True and out["items"][0]["name"] == "Eff" and out["items"][0]["number"] == 9
    s = await t["dispatcharr_streams"](account="Pluto", group="Kids")
    assert s["items"][0]["resolution"] == "1280x720" and s["items"][0]["bitrate_kbps"] == 2500
    assert s["items"][0]["has_stats"] is True


async def test_quality_report_classifies_and_pages(monkeypatch):
    pages = {
        "1": {"next": "x", "results": [
            {"id": 1, "name": "HD", "stream_stats": {"resolution": "1920x1080"}},
            {"id": 2, "name": "SD", "stream_stats": {"resolution": "640x360"}},
        ]},
        "2": {"next": None, "results": [
            {"id": 3, "name": "NoStats", "stream_stats": None},
            {"id": 4, "name": "Old", "stream_stats": {"resolution": "1280x720"}, "is_stale": True},
        ]},
    }
    patch_http(monkeypatch, dispatcharr, {"/api/channels/streams/": lambda r: pages[r.url.params["page"]]})
    out = await load_tools(dispatcharr)["dispatcharr_quality_report"](group="Kids")
    assert out["streams"] == 4
    assert out["resolutions"]["1920x1080"] == 1 and out["resolutions"]["unknown"] == 1
    assert out["problem_counts"] == {"no_stats": 1, "stale": 1, "low_res": 1}
    assert out["problems"]["low_res"] == ["2 SD (640x360)"]


async def test_status_tolerates_failures_and_strips_secrets(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {
        "/api/core/version/": {"version": "0.32.0"},
        "/api/m3u/accounts/": [{"id": 1, "name": "Pluto", "is_active": True, "status": "success",
                                "password": "hunter2", "last_message": "ok"}],
        "/api/epg/sources/": (500, {"detail": "x"}),
        "/proxy/stats/": {"channels": []},
    })
    out = await load_tools(dispatcharr)["dispatcharr_status"]()
    assert out["version"]["version"] == "0.32.0"
    assert "password" not in json.dumps(out["m3u_accounts"])
    assert "500" in out["epg_sources"]["error"] and out["live"] == {"channels": []}


async def test_list_resources_scrubs_credentials(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {"/api/m3u/accounts/": [{"id": 1, "username": "u", "password": "p", "name": "n"}]})
    t = load_tools(dispatcharr)
    assert await t["dispatcharr_list_resources"]("m3u-accounts") == [{"id": 1, "name": "n"}]
    with pytest.raises(ValueError, match="Unknown kind"):
        await t["dispatcharr_list_resources"]("nope")


async def test_channel_includes_stats_even_if_stats_call_fails(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {
        "/api/channels/channels/7/": {"id": 7},
        "/api/channels/channels/7/streams/stats/": (404, {"detail": "nf"}),
    })
    out = await load_tools(dispatcharr)["dispatcharr_channel"](7)
    assert out["id"] == 7 and "404" in out["stream_stats"]["error"]


async def test_write_tools_send_expected_requests(monkeypatch):
    seen = []

    def any_(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return {"ok": True}

    patch_http(monkeypatch, dispatcharr, {"/api/*": any_, "/proxy/*": any_})
    t = load_tools(dispatcharr)
    await t["dispatcharr_create_channel_from_stream"](5, channel_number=12.5, name="N", profile_ids=[1])
    await t["dispatcharr_create_channel_from_stream"](6)
    await t["dispatcharr_update_channel"](7, {"name": "X"})
    await t["dispatcharr_assign_numbers"]([1, 2], 100)
    await t["dispatcharr_set_epg"](7, 33)
    await t["dispatcharr_refresh"]("m3u")
    await t["dispatcharr_refresh"]("m3u", 4)
    await t["dispatcharr_refresh"]("epg", 9)
    await t["dispatcharr_stop_channel"]("uuid-1")
    assert seen == [
        ("POST", "/api/channels/channels/from-stream/",
         {"stream_id": 5, "channel_number": 12.5, "name": "N", "channel_profile_ids": [1]}),
        ("POST", "/api/channels/channels/from-stream/", {"stream_id": 6}),
        ("PATCH", "/api/channels/channels/7/", {"name": "X"}),
        ("POST", "/api/channels/channels/assign/", {"channel_ids": [1, 2], "starting_number": 100}),
        ("POST", "/api/channels/channels/7/set-epg/", {"epg_data_id": 33}),
        ("POST", "/api/m3u/refresh/", None),
        ("POST", "/api/m3u/refresh/4/", None),
        ("POST", "/api/epg/import/", {"id": 9}),
        ("POST", "/proxy/ts/stop/uuid-1", None),
    ]
    with pytest.raises(ValueError):
        await t["dispatcharr_refresh"]("epg")
    with pytest.raises(ValueError):
        await t["dispatcharr_refresh"]("bogus")


async def test_delete_channels_reports_partial_failure(monkeypatch):
    def chan(request):
        return (500, {"detail": "no"}) if request.url.path.endswith("/2/") else (204, "")

    patch_http(monkeypatch, dispatcharr, {"/api/channels/channels/*": chan})
    out = await load_tools(dispatcharr)["dispatcharr_delete_channels"]([1, 2])
    assert out["deleted"] == [1] and "500" in out["failed"][2]


async def test_api_escape_hatch_validates(monkeypatch):
    patch_http(monkeypatch, dispatcharr, {"/api/core/version/": {"version": "x"}, "/proxy/stats/": {}})
    tool = load_tools(dispatcharr)["dispatcharr_api"]
    assert await tool("get", "/api/core/version/") == {"version": "x"}
    assert await tool("GET", "/proxy/stats/") == {}
    with pytest.raises(ValueError, match="method"):
        await tool("TRACE", "/api/x")
    with pytest.raises(ValueError, match="/api/"):
        await tool("GET", "/output/m3u")


def test_every_write_tool_is_marked_destructive():
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("t")
    dispatcharr.register(mcp)
    docs = {t.name: (t.description or "") for t in mcp._tool_manager.list_tools()}
    for name in ("dispatcharr_create_channel_from_stream", "dispatcharr_update_channel",
                 "dispatcharr_delete_channels", "dispatcharr_assign_numbers", "dispatcharr_set_epg",
                 "dispatcharr_stop_channel", "dispatcharr_api"):
        assert "[DESTRUCTIVE" in docs[name], name
