import json

import pytest

from nest_mcp.tools import tunarr
from helpers import load_tools, patch_http

CH_A = {"id": "aaa", "number": 6001, "name": "Classic TV", "programCount": 3, "duration": 7_200_000,
        "streamMode": "hls", "offline": {}, "stealth": False, "onDemand": {"enabled": True},
        "transcodeConfigId": "tc", "sessions": []}
CH_B = {"id": "bbb", "number": 6005, "name": "Cat TV", "programCount": 1, "duration": 3_600_000}


def _prog(uuid, title, show, state="ok", jf="jf1", season=1, episode=1, minutes=30):
    return {
        "uuid": uuid, "title": title, "type": "episode", "year": 2000, "state": state,
        "duration": minutes * 60000, "episodeNumber": episode,
        "show": {"title": show}, "season": {"index": season},
        "identifiers": [{"type": "jellyfin", "id": jf}, {"type": "imdb", "id": "tt1"}, {"type": "tvdb", "id": "9"}],
    }


PROGS = [
    _prog("p1", "Pilot", "Lucy", "missing", "JF-1"),
    _prog("p2", "Second", "Lucy", "ok", "jf2"),
    _prog("p3", "Moon", "Other", "missing", "jf3"),
]


def _lineup_payload():
    return {
        "lineup": [
            {"type": "content", "id": "p1", "duration": 1800000},
            {"type": "flex", "id": "flex1", "duration": 60000},
            {"type": "content", "id": "p2", "duration": 1800000, "durationMs": 1800000},
            {"type": "content", "id": "p3", "duration": 1800000},
        ],
        "programs": {
            "p1": {"program": PROGS[0]}, "p2": {"program": PROGS[1]}, "p3": {"program": PROGS[2]},
        },
    }


def _base_routes(extra=None):
    routes = {"/api/channels": [CH_A, CH_B]}
    routes.update(extra or {})
    return routes


async def test_channel_resolves_by_number_name_and_id(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/bbb": {"id": "bbb", "name": "Cat TV"}}))
    t = load_tools(tunarr)["tunarr_channel"]
    assert (await t("6005"))["id"] == "bbb"
    assert (await t("cat tv"))["id"] == "bbb"
    assert (await t("bbb"))["id"] == "bbb"


async def test_unknown_channel_raises(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes())
    with pytest.raises(ValueError, match="No Tunarr channel"):
        await load_tools(tunarr)["tunarr_channel"]("9999")


async def test_http_error_surfaces_tunarr_body(monkeypatch):
    patch_http(monkeypatch, tunarr, {"/api/channels": (500, {"message": "db locked"})})
    with pytest.raises(RuntimeError, match="db locked"):
        await load_tools(tunarr)["tunarr_channels"]()


async def test_channels_summary(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes())
    out = await load_tools(tunarr)["tunarr_channels"]()
    assert out[0]["number"] == 6001 and out[0]["hours"] == 2.0
    assert out[0]["lineup_entries"] == 3 and out[0]["on_demand"] is True


async def test_channel_programs_rows_filter_and_paging(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({
        "/api/channels/aaa/programs": {"total": 3, "result": PROGS},
    }))
    tool = load_tools(tunarr)["tunarr_channel_programs"]
    everything = await tool("6001")
    assert everything["total"] == 3
    assert everything["items"][0] == {
        "id": "p1", "title": "Pilot", "show": "Lucy", "season": 1, "episode": 1, "type": "episode",
        "year": 2000, "minutes": 30.0, "state": "missing", "jellyfin_id": "JF-1", "imdb": "tt1", "tvdb": "9",
    }
    missing = await tool("6001", state="missing")
    assert [r["id"] for r in missing["items"]] == ["p1", "p3"]
    assert [r["id"] for r in (await tool("6001", search="LUCY"))["items"]] == ["p1", "p2"]
    paged = await tool("6001", limit=1, offset=1)
    assert paged["total"] == 3 and [r["id"] for r in paged["items"]] == ["p2"]


async def test_all_programs_pages_until_total(monkeypatch):
    calls = []

    def page(request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        return {"total": 250, "result": [{"uuid": f"u{offset + i}"} for i in range(200 if offset == 0 else 50)]}

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programs": page}))
    out = await load_tools(tunarr)["tunarr_channel_programs"]("6001", limit=1000)
    assert out["total"] == 250 and calls == [0, 200]


async def test_channel_lineup_maps_titles(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programming": _lineup_payload()}))
    out = await load_tools(tunarr)["tunarr_channel_lineup"]("6001")
    assert out["channel"] == "Classic TV" and out["total"] == 4
    assert out["items"][0]["title"] == "Pilot" and out["items"][0]["state"] == "missing"
    assert out["items"][1]["type"] == "flex" and out["items"][1]["title"] is None


async def test_remove_programs_requires_selector(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes())
    with pytest.raises(ValueError, match="program_ids"):
        await load_tools(tunarr)["tunarr_remove_programs"]("6001")


async def test_remove_programs_dry_run_does_not_post(monkeypatch):
    posts = []

    def programming(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return {}
        return _lineup_payload()

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programming": programming}))
    out = await load_tools(tunarr)["tunarr_remove_programs"]("6001", only_missing=True)
    assert out == {"channel": "6001 Classic TV", "before": 4, "after": 2, "removed": 2, "applied": False}
    assert posts == []


async def test_remove_programs_apply_keeps_order_and_non_content(monkeypatch):
    posts = []

    def programming(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return {}
        return _lineup_payload()

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programming": programming}))
    out = await load_tools(tunarr)["tunarr_remove_programs"]("6001", only_missing=True, dry_run=False)
    assert out["applied"] is True and out["removed"] == 2
    assert posts == [{"type": "manual", "append": False, "lineup": [
        {"type": "flex", "id": "flex1", "duration": 60000},
        {"type": "content", "id": "p2", "duration": 1800000},
    ]}]
    assert [e["id"] for e in out["removed_entries"]] == ["p1", "p3"]


async def test_remove_programs_by_id_only_touches_listed(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programming": _lineup_payload()}))
    out = await load_tools(tunarr)["tunarr_remove_programs"]("6001", program_ids=["p2"])
    assert out["removed"] == 1 and out["after"] == 3


async def test_remove_programs_nothing_to_remove_skips_post(monkeypatch):
    def programming(request):
        assert request.method == "GET"
        return {"lineup": [{"type": "content", "id": "p2", "duration": 1}],
                "programs": {"p2": {"program": PROGS[1]}}}

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/aaa/programming": programming}))
    out = await load_tools(tunarr)["tunarr_remove_programs"]("6001", only_missing=True, dry_run=False)
    assert out["removed"] == 0 and out["applied"] is False


async def test_audit_counts_states_and_groups_missing(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({
        "/api/channels/aaa/programs": {"total": 3, "result": PROGS},
    }))
    out = await load_tools(tunarr)["tunarr_audit_programs"]("6001")
    assert out == [{
        "channel": "6001 Classic TV", "programs": 3,
        "by_state": {"missing": 2, "ok": 1},
        "missing_by_show": {"Lucy": 1, "Other": 1},
    }]


async def test_audit_check_jellyfin_reports_absent_ids(monkeypatch):
    def items(request):
        assert request.url.params["Ids"].split(",") == ["jf1", "jf2", "jf3"]
        return {"Items": [{"Id": "jf2"}, {"Id": "JF3"}]}

    patch_http(monkeypatch, tunarr, _base_routes({
        "/api/channels/aaa/programs": {"total": 3, "result": PROGS},
        "/Items": items,
    }))
    out = await load_tools(tunarr)["tunarr_audit_programs"]("6001", check_jellyfin=True)
    assert out[0]["jellyfin_absent"] == 1
    assert out[0]["jellyfin_absent_by_show"] == {"Lucy": 1}


async def test_audit_all_channels_when_none_given(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({
        "/api/channels/aaa/programs": {"total": 0, "result": []},
        "/api/channels/bbb/programs": {"total": 0, "result": []},
    }))
    out = await load_tools(tunarr)["tunarr_audit_programs"]()
    assert [r["channel"] for r in out] == ["6001 Classic TV", "6005 Cat TV"]


async def test_status_tolerates_failing_subcall(monkeypatch):
    patch_http(monkeypatch, tunarr, {
        "/api/version": {"tunarr": "1.3.15"},
        "/api/system/health": (500, {"message": "boom"}),
        "/api/sessions": {}, "/api/xmltv-last-refresh": {}, "/api/guide/status": {},
        "/api/tasks": [{"id": "ScanLibrariesTask", "scheduledTasks": [
            {"nextExecution": "n", "lastExecution": "l", "running": False}]}],
    })
    out = await load_tools(tunarr)["tunarr_status"]()
    assert out["version"]["tunarr"] == "1.3.15"
    assert "boom" in out["health"]["error"]
    assert out["tasks"][0]["scheduled"][0]["next"] == "n"


async def test_update_channel_merges_over_current(monkeypatch):
    puts = []

    def channel(request):
        if request.method == "PUT":
            puts.append(json.loads(request.content))
            return {"ok": True}
        return {"id": "bbb", "name": "Cat TV", "number": 6005, "stealth": False}

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/bbb": channel}))
    await load_tools(tunarr)["tunarr_update_channel"]("6005", {"name": "Cats", "stealth": True})
    assert puts == [{"id": "bbb", "name": "Cats", "number": 6005, "stealth": True}]


async def test_delete_channel_reports_what_was_deleted(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/bbb": (204, "")}))
    out = await load_tools(tunarr)["tunarr_delete_channel"]("6005")
    assert out == {"deleted": "6005 Cat TV", "id": "bbb"}


async def test_create_channel_posts_body(monkeypatch):
    seen = []

    def create(request):
        seen.append((request.method, json.loads(request.content)))
        return {"id": "new"}

    patch_http(monkeypatch, tunarr, {"/api/channels": create})
    out = await load_tools(tunarr)["tunarr_create_channel"]({"name": "X", "number": 7000})
    assert out == {"id": "new"} and seen == [("POST", {"name": "X", "number": 7000})]


async def test_set_programming_posts_to_resolved_channel(monkeypatch):
    seen = []

    def prog(request):
        seen.append(json.loads(request.content))
        return {"ok": 1}

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/bbb/programming": prog}))
    body = {"type": "manual", "lineup": [], "append": True}
    await load_tools(tunarr)["tunarr_set_programming"]("Cat TV", body)
    assert seen == [body]


async def test_generate_schedule_modes(monkeypatch):
    seen = []

    def slots(request):
        seen.append((request.url.path, json.loads(request.content)))
        return {}

    patch_http(monkeypatch, tunarr, _base_routes({"/api/channels/bbb/*": slots}))
    tool = load_tools(tunarr)["tunarr_generate_schedule"]
    await tool("6005", "slots", {"a": 1})
    await tool("6005", "time-slots", {"b": 2})
    assert seen == [("/api/channels/bbb/schedule-slots", {"schedule": {"a": 1}}),
                    ("/api/channels/bbb/schedule-time-slots", {"schedule": {"b": 2}})]
    with pytest.raises(ValueError, match="mode"):
        await tool("6005", "nope", {})


async def test_resource_crud_and_unknown_kind(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return {"id": "r1"}

    patch_http(monkeypatch, tunarr, {"/api/filler-lists*": handler, "/api/smart_collections*": handler})
    tools = load_tools(tunarr)
    await tools["tunarr_list_resources"]("filler-lists")
    await tools["tunarr_get_resource"]("filler-lists", "r1")
    await tools["tunarr_save_resource"]("filler-lists", {"name": "n"})
    await tools["tunarr_save_resource"]("smart-collections", {"name": "n"}, resource_id="r1")
    await tools["tunarr_delete_resource"]("smart-collections", "r1")
    assert seen == [
        ("GET", "/api/filler-lists"), ("GET", "/api/filler-lists/r1"), ("POST", "/api/filler-lists"),
        ("PUT", "/api/smart_collections/r1"), ("DELETE", "/api/smart_collections/r1"),
    ]
    with pytest.raises(ValueError, match="Unknown kind"):
        await tools["tunarr_list_resources"]("nope")


async def test_get_resource_include_programs(monkeypatch):
    patch_http(monkeypatch, tunarr, {
        "/api/custom-shows/c1": {"id": "c1"},
        "/api/custom-shows/c1/programs": [{"uuid": "p"}],
    })
    out = await load_tools(tunarr)["tunarr_get_resource"]("custom-shows", "c1", include_programs=True)
    assert out == {"resource": {"id": "c1"}, "programs": [{"uuid": "p"}]}


async def test_settings_get_and_set(monkeypatch):
    seen = []

    def ffmpeg(request):
        seen.append(request.method)
        return {"ok": True}

    patch_http(monkeypatch, tunarr, {"/api/ffmpeg-settings": ffmpeg})
    tools = load_tools(tunarr)
    await tools["tunarr_get_settings"]("ffmpeg")
    await tools["tunarr_set_settings"]("ffmpeg", {"x": 1})
    assert seen == ["GET", "PUT"]
    with pytest.raises(ValueError, match="Unknown kind"):
        await tools["tunarr_get_settings"]("bogus")


async def test_scan_library_sets_force_param(monkeypatch):
    seen = []

    def scan(request):
        seen.append(dict(request.url.params))
        return (202, "")

    patch_http(monkeypatch, tunarr, {"/api/media-sources/ms/libraries/lib/scan": scan})
    out = await load_tools(tunarr)["tunarr_scan_library"]("ms", "lib", force=False)
    assert out == {"status": 202} and seen == [{"forceScan": "false"}]


async def test_scan_status_source_and_library(monkeypatch):
    patch_http(monkeypatch, tunarr, {
        "/api/media-sources/ms/status": {"healthy": True},
        "/api/media-sources/ms/lib/status": {"state": "idle"},
    })
    tool = load_tools(tunarr)["tunarr_scan_status"]
    assert await tool("ms") == {"healthy": True}
    assert await tool("ms", "lib") == {"state": "idle"}


async def test_media_sources_strips_tokens(monkeypatch):
    patch_http(monkeypatch, tunarr, {"/api/media-sources": [{"id": "m", "accessToken": "secret", "name": "n"}]})
    out = await load_tools(tunarr)["tunarr_media_sources"]()
    assert out == [{"id": "m", "name": "n"}]


async def test_search_body_shape(monkeypatch):
    seen = []

    def search(request):
        seen.append(json.loads(request.content))
        return {"results": []}

    patch_http(monkeypatch, tunarr, {"/api/programs/search": search})
    await load_tools(tunarr)["tunarr_search_programs"](
        query="earth", media_source="ms", library="lib", restrict_to=["title"],
        sort_by="title", descending=True, limit=5, page=2,
    )
    assert seen == [{
        "query": {"query": "earth", "restrictSearchTo": ["title"], "sort": [{"field": "title", "direction": "desc"}]},
        "limit": 5, "page": 2, "mediaSourceId": "ms", "libraryId": "lib",
    }]


async def test_program_includes_stream_details_even_if_missing(monkeypatch):
    patch_http(monkeypatch, tunarr, {
        "/api/programs/p1": {"uuid": "p1"},
        "/api/programs/p1/stream_details": (404, {"message": "none"}),
    })
    out = await load_tools(tunarr)["tunarr_program"]("p1")
    assert out["uuid"] == "p1" and "none" in out["stream_details"]["error"]


async def test_guide_all_and_single_channel(monkeypatch):
    seen = []

    def guide(request):
        seen.append((request.url.path, dict(request.url.params)))
        return []

    patch_http(monkeypatch, tunarr, _base_routes({"/api/guide/channels*": guide}))
    tool = load_tools(tunarr)["tunarr_guide"]
    await tool("2026-10-22T00:00:00Z", "2026-10-23T00:00:00Z")
    await tool("2026-10-22T00:00:00Z", "2026-10-23T00:00:00Z", channel="6005")
    assert [p for p, _ in seen] == ["/api/guide/channels", "/api/guide/channels/bbb"]
    assert seen[0][1] == {"dateFrom": "2026-10-22T00:00:00Z", "dateTo": "2026-10-23T00:00:00Z"}


async def test_simple_passthrough_tools(monkeypatch):
    seen = []

    def any_(request):
        seen.append((request.method, request.url.path))
        return {"ok": True}

    patch_http(monkeypatch, tunarr, _base_routes({
        "/api/sessions": any_, "/api/trash": any_, "/api/tasks/*": any_, "/api/system/fixers/*": any_,
        "/api/xmltv/refresh": any_, "/api/transcode_configs/*": any_, "/api/custom-shows/*": any_,
        "/api/media-sources/*": any_, "/api/channels/bbb/*": any_,
    }))
    t = load_tools(tunarr)
    await t["tunarr_sessions"]()
    await t["tunarr_trash"]()
    await t["tunarr_empty_trash"]()
    await t["tunarr_run_task"]("BackupTask")
    await t["tunarr_run_fixer"]("f1")
    await t["tunarr_refresh_xmltv"]()
    await t["tunarr_copy_transcode_config"]("tc")
    await t["tunarr_sync_custom_show"]("c1")
    await t["tunarr_refresh_libraries"]("ms")
    await t["tunarr_set_library_enabled"]("ms", "lib", True)
    await t["tunarr_stop_sessions"]("6005")
    await t["tunarr_now_playing"]("6005")
    assert ("DELETE", "/api/trash") in seen and ("POST", "/api/tasks/BackupTask/run") in seen
    assert ("PUT", "/api/media-sources/ms/libraries/lib") in seen
    assert ("DELETE", "/api/channels/bbb/sessions") in seen


async def test_api_escape_hatch_validates(monkeypatch):
    patch_http(monkeypatch, tunarr, {"/api/version": {"tunarr": "1"}, "/api/x": (204, "")})
    tool = load_tools(tunarr)["tunarr_api"]
    assert await tool("get", "/api/version") == {"tunarr": "1"}
    assert await tool("POST", "/api/x", body={"a": 1}) == {"status": 204}
    with pytest.raises(ValueError, match="method"):
        await tool("TRACE", "/api/version")
    with pytest.raises(ValueError, match="/api/"):
        await tool("GET", "/stream/channels/1.ts")


async def test_non_json_success_body_is_wrapped(monkeypatch):
    patch_http(monkeypatch, tunarr, {"/api/xmltv/refresh": (200, "done")})
    out = await load_tools(tunarr)["tunarr_refresh_xmltv"]()
    assert out == {"status": 200, "body": "done"}


def test_every_write_tool_is_marked_destructive():
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("t")
    tunarr.register(mcp)
    docs = {t.name: (t.description or "") for t in mcp._tool_manager.list_tools()}
    writes = ["tunarr_create_channel", "tunarr_update_channel", "tunarr_delete_channel",
              "tunarr_set_programming", "tunarr_remove_programs", "tunarr_rematch_missing", "tunarr_generate_schedule",
              "tunarr_stop_sessions", "tunarr_save_resource", "tunarr_delete_resource",
              "tunarr_set_settings", "tunarr_set_library_enabled", "tunarr_empty_trash", "tunarr_api"]
    missing = [w for w in writes if "[DESTRUCTIVE" not in docs[w]]
    assert not missing


# ── rematch ──────────────────────────────────────────────────────────────────

def _ep(uuid, state, jf, title="Pilot", ep=1, year=2000, dur=1_800_000, tvdb="9", imdb="tt1", typ="episode"):
    ids = [{"type": "jellyfin", "id": jf}]
    if tvdb:
        ids.append({"type": "tvdb", "id": tvdb})
    if imdb:
        ids.append({"type": "imdb", "id": imdb})
    return {"uuid": uuid, "title": title, "type": typ, "year": year, "state": state, "duration": dur,
            "episodeNumber": ep, "identifiers": ids, "mediaSourceId": "ms", "libraryId": "lib",
            "show": {"title": "Lucy"}}


def test_same_content_rules():
    old = _ep("o", "missing", "j0")
    assert tunarr._same_content(old, _ep("n", "ok", "j1"))
    assert not tunarr._same_content(old, _ep("n", "missing", "j1"))          # twin must be ok
    assert not tunarr._same_content(old, _ep("o", "ok", "j1"))               # not itself
    assert not tunarr._same_content(old, _ep("n", "ok", "j1", title="Other"))
    assert not tunarr._same_content(old, _ep("n", "ok", "j1", ep=2))
    assert not tunarr._same_content(old, _ep("n", "ok", "j1", year=2001))
    assert not tunarr._same_content(old, _ep("n", "ok", "j1", tvdb="8"))     # conflicting ids
    assert not tunarr._same_content(old, _ep("n", "ok", "j1", typ="movie"))
    # shared external id outranks a changed duration (re-encoded / re-cut file)...
    assert tunarr._same_content(old, _ep("n", "ok", "j1", dur=700_000))
    # ...but without one, duration must match
    bare = _ep("o", "missing", "j0", tvdb="", imdb="")
    assert tunarr._same_content(bare, _ep("n", "ok", "j1", tvdb="", imdb="", dur=1_800_500))
    assert not tunarr._same_content(bare, _ep("n", "ok", "j1", tvdb="", imdb="", dur=700_000))


def test_best_twin_prefers_closest_duration():
    old = _ep("o", "missing", "j0", dur=1_000_000)
    far, near = _ep("a", "ok", "j1", dur=500_000), _ep("b", "ok", "j2", dur=990_000)
    assert tunarr._best_twin(old, [far, near])["uuid"] == "b"
    assert tunarr._best_twin(old, [_ep("c", "missing", "j3")]) is None


def _rematch_routes(apply_to=None):
    old1, old2, ok = _ep("o1", "missing", "j0"), _ep("o2", "missing", "j9", title="Gone", tvdb="", imdb=""), \
        _ep("k1", "ok", "j5", title="Keep", tvdb="", imdb="")
    twin = _ep("n1", "ok", "j1", dur=900_000)

    def search(request):
        q = json.loads(request.content)["query"]["query"]
        return {"results": [twin] if q == "Pilot" else [], "totalPages": 1}

    def programming(request):
        if request.method == "POST":
            apply_to.append(json.loads(request.content))
            return {}
        return {"lineup": [
            {"type": "content", "id": "o1", "duration": 1_800_000},
            {"type": "flex", "id": "f", "duration": 60_000},
            {"type": "content", "id": "o2", "duration": 1_800_000},
            {"type": "content", "id": "k1", "duration": 1_800_000},
        ]}

    return _base_routes({
        "/api/channels/aaa/programs": {"total": 3, "result": [old1, old2, ok]},
        "/api/channels/aaa/programming": programming,
        "/api/programs/search": search,
    })


async def test_rematch_dry_run_reports_without_writing(monkeypatch):
    posts = []
    routes = _rematch_routes(posts)
    patch_http(monkeypatch, tunarr, routes)
    out = await load_tools(tunarr)["tunarr_rematch_missing"]("6001")
    assert out[0]["missing_programs"] == 2 and out[0]["matched"] == 1 and out[0]["unmatched"] == 1
    assert out[0]["duration_changed"] == 1 and out[0]["applied"] is False
    assert "Lucy - Gone" in out[0]["unmatched_examples"][0]
    assert posts == []


async def test_rematch_apply_swaps_ids_and_durations_keeps_rest(monkeypatch):
    posts = []
    patch_http(monkeypatch, tunarr, _rematch_routes(posts))
    out = await load_tools(tunarr)["tunarr_rematch_missing"]("6001", dry_run=False)
    assert out[0]["applied"] is True and out[0]["lineup_entries_after"] == 4
    assert posts == [{"type": "manual", "append": False, "lineup": [
        {"type": "content", "id": "n1", "duration": 900_000},
        {"type": "flex", "id": "f", "duration": 60_000},
        {"type": "content", "id": "o2", "duration": 1_800_000},   # unmatched stays
        {"type": "content", "id": "k1", "duration": 1_800_000},
    ]}]


async def test_rematch_drop_unmatched_and_all_channels(monkeypatch):
    posts = []
    routes = _rematch_routes(posts)
    routes["/api/channels/bbb/programs"] = {"total": 0, "result": []}
    routes["/api/channels/bbb/programming"] = {"lineup": []}
    patch_http(monkeypatch, tunarr, routes)
    out = await load_tools(tunarr)["tunarr_rematch_missing"](dry_run=False, drop_unmatched=True)
    assert [r["applied"] for r in out] == [True, False]       # nothing to do on the empty channel
    assert [e["id"] for e in posts[0]["lineup"]] == ["n1", "f", "k1"]


# ── tunarr_add_shows ─────────────────────────────────────────────────────────

MIN = 60000


def _desc(uid, season, episode, state="ok", minutes=22):
    return {"id": uid, "duration": minutes * MIN, "type": "content", "program": {
        "type": "episode", "state": state, "episodeNumber": episode, "season": {"index": season},
        "duration": minutes * MIN, "show": {"title": "Doug"}}}


def _show_search(request):
    body = json.loads(request.content)
    assert body["query"]["filter"]["fieldSpec"]["value"] == ["show"]
    return {"results": [
        {"uuid": "show-a", "title": "Doug"}, {"uuid": "show-b", "title": "doug"}, {"uuid": "x", "title": "Doug Funnie"},
    ], "totalPages": 1}


def _add_routes(captured, lineup=None):
    def programming(request):
        if request.method == "POST":
            captured.append(json.loads(request.content))
            return {}
        return lineup if lineup is not None else {"lineup": [], "programs": {}}

    return _base_routes({
        "/api/programs/search": _show_search,
        # show-a is the live library row; show-b the stale duplicate carrying one extra dead episode and a twin
        "/api/programs/show-a/descendants": [_desc("a2", 1, 2), _desc("a1", 1, 1), _desc("a3", 2, 1), _desc("a4", 2, 2, minutes=30)],
        "/api/programs/show-b/descendants": [_desc("b1", 1, 1), _desc("b9", 3, 1, state="missing")],
        "/api/channels/aaa/programming": programming,
    })


async def test_add_shows_dry_run_reports_without_writing(monkeypatch):
    captured = []
    patch_http(monkeypatch, tunarr, _add_routes(captured))
    out = await load_tools(tunarr)["tunarr_add_shows"]("6001", ["Doug"])
    assert out["applied"] is False and captured == []
    row = out["shows"][0]
    assert row["library_rows"] == 2 and row["to_add"] == 4  # a1,a2,a3,a4: b1 duplicates a1, b9 is dead
    assert row["skipped_not_playable"] == 1 and out["entries_after"] == 4


async def test_add_shows_appends_in_season_order_with_padding(monkeypatch):
    captured = []
    patch_http(monkeypatch, tunarr, _add_routes(captured))
    out = await load_tools(tunarr)["tunarr_add_shows"]("6001", ["doug"], pad_to_minutes=30, dry_run=False)
    assert out["applied"] is True
    lineup = captured[0]["lineup"]
    assert captured[0]["append"] is False
    assert [e["id"] for e in lineup if e["type"] == "content"] == ["a1", "a2", "a3", "a4"]
    flex = [e["duration"] for e in lineup if e["type"] == "flex"]
    assert flex == [8 * MIN, 8 * MIN, 8 * MIN]  # 22 -> 30 three times; the 30-minute episode needs none


async def test_add_shows_skips_episodes_already_on_channel(monkeypatch):
    captured = []
    existing = {
        "lineup": [{"type": "content", "id": "a1", "duration": 22 * MIN}, {"type": "flex", "duration": 8 * MIN}],
        "programs": {"a1": {"program": {"show": {"title": "Doug"}}}},
    }
    patch_http(monkeypatch, tunarr, _add_routes(captured, existing))
    out = await load_tools(tunarr)["tunarr_add_shows"]("6001", ["Doug"], dry_run=False)
    assert out["shows"][0]["already_on_channel"] >= 1 and out["shows"][0]["to_add"] == 3
    ids = [e.get("id") for e in captured[0]["lineup"] if e["type"] == "content"]
    assert ids == ["a1", "a2", "a3", "a4"]  # b1 (twin of a1) is not added a second time


async def test_add_shows_round_robin_alternates_shows(monkeypatch):
    captured = []
    existing = {
        "lineup": [{"type": "content", "id": f"t{i}", "duration": 40 * MIN} for i in range(3)],
        "programs": {f"t{i}": {"program": {"show": {"title": "TNG"}}} for i in range(3)},
    }
    patch_http(monkeypatch, tunarr, _add_routes(captured, existing))
    await load_tools(tunarr)["tunarr_add_shows"]("6001", ["Doug"], placement="round_robin", dry_run=False)
    ids = [e["id"] for e in captured[0]["lineup"]]
    assert ids == ["t0", "a1", "t1", "a2", "t2", "a3", "a4"]


async def test_add_shows_shuffle_in_is_repeatable_and_keeps_existing_order(monkeypatch):
    results = []
    for _ in range(2):
        captured = []
        existing = {
            "lineup": [{"type": "content", "id": f"t{i}", "duration": 40 * MIN} for i in range(3)],
            "programs": {f"t{i}": {"program": {"show": {"title": "TNG"}}} for i in range(3)},
        }
        patch_http(monkeypatch, tunarr, _add_routes(captured, existing))
        await load_tools(tunarr)["tunarr_add_shows"]("6001", ["Doug"], placement="shuffle_in", seed=7, dry_run=False)
        results.append([e["id"] for e in captured[0]["lineup"]])
    assert results[0] == results[1]
    assert [i for i in results[0] if i.startswith("t")] == ["t0", "t1", "t2"]


async def test_add_shows_keeps_fillers_and_unknown_show_is_reported(monkeypatch):
    captured = []
    existing = {
        "lineup": [{"type": "content", "id": "t0", "duration": MIN}, {"type": "redirect", "channel": "x", "duration": MIN}],
        "programs": {"t0": {"program": {"show": {"title": "TNG"}}}},
    }
    routes = _add_routes(captured, existing)
    routes["/api/programs/search"] = lambda r: {"results": [], "totalPages": 1} if b"Nope" in r.content else _show_search(r)
    patch_http(monkeypatch, tunarr, routes)
    out = await load_tools(tunarr)["tunarr_add_shows"]("6001", ["Nope", "Doug"], placement="round_robin", dry_run=False)
    assert "no show" in out["shows"][0]["error"]
    types = [e["type"] for e in captured[0]["lineup"]]
    assert "redirect" in types and types[-1] == "redirect"


async def test_add_shows_validates_arguments(monkeypatch):
    patch_http(monkeypatch, tunarr, _base_routes())
    tool = load_tools(tunarr)["tunarr_add_shows"]
    with pytest.raises(ValueError, match="placement"):
        await tool("6001", ["Doug"], placement="sideways")
    with pytest.raises(ValueError, match="at least one"):
        await tool("6001", [])
