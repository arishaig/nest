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


# ── write tools ──────────────────────────────────────────────────────────────

import datetime

import pytest

PAST, FUTURE = "2001-01-01", "2999-01-01"


async def test_arr_config_lists_profiles_and_folders(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v3/qualityprofile": [{"id": 1, "name": "HD", "extra": 1}],
        "/api/v3/rootfolder": [{"id": 2, "path": "/tv", "freeSpace": 5}],
        "/api/v3/tag": [{"id": 3, "label": "kids"}],
    })
    out = await load_tools(arr)["arr_config"](service="sonarr")
    assert out["quality_profiles"] == [{"id": 1, "name": "HD"}]
    assert out["root_folders"][0]["path"] == "/tv" and out["tags"][0]["label"] == "kids"


async def test_sonarr_lookup_flags_library_hits(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/series/lookup": [
        {"title": "Doug", "year": 1991, "tvdbId": 72210, "id": 7, "seasons": [{}, {}], "overview": "x" * 500},
        {"title": "Doug 2", "tvdbId": 2},
    ]})
    out = await load_tools(arr)["sonarr_lookup"]("doug")
    assert out[0]["in_library"] is True and out[0]["series_id"] == 7 and len(out[0]["overview"]) == 200
    assert out[1]["in_library"] is False


async def test_sonarr_series_filters_and_sorts(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/series": [
        {"id": 2, "title": "Zed", "statistics": {"episodeFileCount": 1, "episodeCount": 2}},
        {"id": 1, "title": "Alpha Doug", "monitored": True},
    ]})
    tool = load_tools(arr)["sonarr_series"]
    assert [r["title"] for r in await tool()] == ["Alpha Doug", "Zed"]
    assert [r["series_id"] for r in await tool("doug")] == [1]


EPISODES = [
    {"id": 11, "seasonNumber": 1, "episodeNumber": 2, "airDate": PAST, "hasFile": False, "title": "B"},
    {"id": 10, "seasonNumber": 1, "episodeNumber": 1, "airDate": PAST, "hasFile": False, "title": "A"},
    {"id": 12, "seasonNumber": 1, "episodeNumber": 3, "airDate": PAST, "hasFile": True},
    {"id": 13, "seasonNumber": 2, "episodeNumber": 1, "airDate": FUTURE, "hasFile": False},
    {"id": 14, "seasonNumber": 2, "episodeNumber": 2, "airDate": PAST, "hasFile": False},
]


async def test_sonarr_missing_episodes_only_aired_and_filed_out(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/episode": EPISODES})
    tool = load_tools(arr)["sonarr_missing_episodes"]
    assert [e["episode_id"] for e in await tool(series_id=1)] == [10, 11, 14]
    assert [e["episode_id"] for e in await tool(series_id=1, season=2)] == [14]


async def test_sonarr_add_series_posts_lookup_with_options(monkeypatch):
    sent = {}

    def post(request):
        import json
        sent.update(json.loads(request.content))
        return {"id": 5, "title": "Doug", "path": "/tv/Doug"}

    patch_http(monkeypatch, arr, {
        "/api/v3/series/lookup": [{"title": "Doug", "tvdbId": 72210}, {"title": "Other", "tvdbId": 1}],
        "/api/v3/series": post,
    })
    out = await load_tools(arr)["sonarr_add_series"](tvdb_id=72210, quality_profile_id=4, root_folder="/tv")
    assert out == {"series_id": 5, "title": "Doug", "path": "/tv/Doug", "searching": True}
    assert sent["tvdbId"] == 72210 and sent["qualityProfileId"] == 4 and sent["rootFolderPath"] == "/tv"
    assert sent["addOptions"]["searchForMissingEpisodes"] is True and sent["monitored"] is True


async def test_sonarr_add_series_unknown_tvdb(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/series/lookup": [{"tvdbId": 1}]})
    with pytest.raises(ValueError, match="no series"):
        await load_tools(arr)["sonarr_add_series"](tvdb_id=2, quality_profile_id=1, root_folder="/tv")


async def test_sonarr_search_episodes_monitors_then_searches(monkeypatch):
    calls = []

    def record(name):
        def h(request):
            import json
            calls.append((name, json.loads(request.content)))
            return {"id": 99}
        return h

    patch_http(monkeypatch, arr, {
        "/api/v3/episode": EPISODES, "/api/v3/episode/monitor": record("monitor"), "/api/v3/command": record("command"),
    })
    out = await load_tools(arr)["sonarr_search_episodes"](series_id=1, missing=True)
    assert out["episode_ids"] == [11, 10, 14] or sorted(out["episode_ids"]) == [10, 11, 14]
    assert calls[0][0] == "monitor" and calls[0][1]["monitored"] is True
    assert calls[1][1]["name"] == "EpisodeSearch" and out["command_id"] == 99


async def test_sonarr_search_episodes_season_and_explicit_dedupe(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/episode": EPISODES, "/api/v3/episode/monitor": {}, "/api/v3/command": {"id": 1}})
    out = await load_tools(arr)["sonarr_search_episodes"](series_id=1, episode_ids=[12], season=2)
    assert out["episode_ids"] == [12, 13, 14]


async def test_sonarr_search_episodes_needs_a_selection_and_caps(monkeypatch):
    many = [{"id": i, "seasonNumber": 1, "episodeNumber": i, "airDate": PAST, "hasFile": False} for i in range(60)]
    patch_http(monkeypatch, arr, {"/api/v3/episode": many})
    tool = load_tools(arr)["sonarr_search_episodes"]
    with pytest.raises(ValueError, match="Nothing to search"):
        await tool(series_id=1)
    with pytest.raises(ValueError, match="at most 50"):
        await tool(series_id=1, missing=True)


RELEASES = [
    {"title": "bad", "indexer": "a", "rejected": True, "rejections": ["Not an upgrade"], "seeders": 99, "size": 1048576},
    {"title": "ok-low", "indexer": "b", "rejected": False, "seeders": 1, "quality": {"quality": {"name": "720p"}}},
    {"title": "ok-high", "indexer": "c", "rejected": False, "seeders": 50},
]


async def test_sonarr_releases_orders_approved_first_with_reasons(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/release": RELEASES})
    out = await load_tools(arr)["sonarr_releases"](episode_id=1, limit=2)
    assert [r["title"] for r in out] == ["ok-high", "ok-low"]
    all_rows = await load_tools(arr)["sonarr_releases"](episode_id=1)
    assert all_rows[-1]["rejections"] == ["Not an upgrade"] and all_rows[-1]["approved"] is False


async def test_radarr_lookup_movies_and_add(monkeypatch):
    sent = {}

    def post(request):
        import json
        sent.update(json.loads(request.content))
        return {"id": 3, "title": "Serenity", "path": "/movies/Serenity"}

    patch_http(monkeypatch, arr, {
        "/api/v3/movie/lookup": [{"title": "Serenity", "year": 2005, "tmdbId": 16320, "id": 0}],
        "/api/v3/movie": post,
    })
    tools = load_tools(arr)
    hit = (await tools["radarr_lookup"]("serenity"))[0]
    assert hit["in_library"] is False and hit["tmdb_id"] == 16320
    out = await tools["radarr_add_movie"](tmdb_id=16320, quality_profile_id=1, root_folder="/movies")
    assert out["movie_id"] == 3 and sent["addOptions"] == {"searchForMovie": True} and sent["monitored"] is True
    with pytest.raises(ValueError, match="no movie"):
        await tools["radarr_add_movie"](tmdb_id=1, quality_profile_id=1, root_folder="/movies")


async def test_radarr_movies_filters(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/movie": [
        {"id": 1, "title": "B", "monitored": True, "hasFile": False},
        {"id": 2, "title": "A", "monitored": True, "hasFile": True},
        {"id": 3, "title": "C", "monitored": False, "hasFile": False},
    ]})
    tool = load_tools(arr)["radarr_movies"]
    assert [m["title"] for m in await tool()] == ["A", "B", "C"]
    assert [m["movie_id"] for m in await tool(missing_only=True)] == [1]
    assert [m["movie_id"] for m in await tool(search="a")] == [2]


async def test_radarr_search_movie_monitors_unmonitored(monkeypatch):
    calls = []

    def put(request):
        import json
        calls.append(("put", json.loads(request.content)))
        return {}

    def get_or_put(request):
        return put(request) if request.method == "PUT" else {"id": 4, "title": "M", "monitored": False}

    def command(request):
        import json
        calls.append(("command", json.loads(request.content)))
        return {"id": 8}

    patch_http(monkeypatch, arr, {"/api/v3/movie/4": get_or_put, "/api/v3/command": command})
    out = await load_tools(arr)["radarr_search_movie"](movie_id=4)
    assert out["command_id"] == 8
    assert calls[0] == ("put", {"id": 4, "title": "M", "monitored": True})
    assert calls[1][1] == {"name": "MoviesSearch", "movieIds": [4]}


async def test_radarr_search_movie_skips_put_when_monitored(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v3/movie/4": {"id": 4, "title": "M", "monitored": True}, "/api/v3/command": {"id": 1},
    })
    assert (await load_tools(arr)["radarr_search_movie"](movie_id=4))["searching"] == "M"


async def test_radarr_releases(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/release": RELEASES})
    out = await load_tools(arr)["radarr_releases"](movie_id=1)
    assert out[0]["title"] == "ok-high"


async def test_prowlarr_health_names_failing_indexers(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v1/health": [{"type": "warning", "source": "IndexerStatusCheck", "message": "Indexers unavailable"}],
        "/api/v1/indexerstatus": [{"indexerId": 2, "disabledTill": "t", "mostRecentFailure": "f"}],
        "/api/v1/indexer": [{"id": 2, "name": "NZBgeek"}],
    })
    out = await load_tools(arr)["prowlarr_health"]()
    assert out["health"][0]["source"] == "IndexerStatusCheck"
    assert out["indexer_failures"][0]["indexer"] == "NZBgeek"


async def test_prowlarr_search_sorts_by_seeders(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v1/search": [
        {"title": "low", "seeders": 1, "size": 1048576, "categories": [{"name": "TV"}]},
        {"title": "high", "seeders": 9, "indexer": "x"},
        {"title": "none"},
    ]})
    out = await load_tools(arr)["prowlarr_search"]("doug", limit=2)
    assert [r["title"] for r in out] == ["high", "low"] and out[1]["categories"] == ["TV"]


async def test_sonarr_episode_files_summarises_by_season(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v3/series/58": {"title": "TZ", "year": 1959, "tvdbId": 73587, "path": "/tv/TZ (2002)"},
        "/api/v3/episodefile": [
            {"seasonNumber": 2, "relativePath": "Season 2/b.mkv", "size": 1048576, "releaseGroup": "G",
             "quality": {"quality": {"name": "DVD"}}},
            {"seasonNumber": 1, "relativePath": "Season 1/a.mkv", "size": 2097152},
        ],
    })
    out = await load_tools(arr)["sonarr_episode_files"](series_id=58, limit=1)
    assert out["files"] == 2 and out["files_by_season"] == {1: 1, 2: 1} and out["path"] == "/tv/TZ (2002)"
    assert out["items"][0]["relative_path"] == "Season 1/a.mkv" and len(out["items"]) == 1


async def test_sonarr_set_series_path_moves_files(monkeypatch):
    seen = {}

    def put(request):
        import json
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return {"path": seen["body"]["path"]}

    def route(request):
        return put(request) if request.method == "PUT" else {"id": 58, "title": "TZ", "path": "/data/media/tv/TZ (2002)"}

    patch_http(monkeypatch, arr, {"/api/v3/rootfolder": [{"path": "/data/media/tv"}], "/api/v3/series/58": route})
    tool = load_tools(arr)["sonarr_set_series_path"]
    out = await tool(series_id=58, path="/data/media/tv/TZ (1959)")
    assert out["changed"] is True and out["old_path"].endswith("(2002)") and seen["query"] == {"moveFiles": "true"}
    assert seen["body"]["path"] == "/data/media/tv/TZ (1959)"
    assert (await tool(series_id=58, path="/data/media/tv/TZ (2002)/"))["changed"] is False
    with pytest.raises(ValueError, match="root folder"):
        await tool(series_id=58, path="/elsewhere/TZ")


async def test_refresh_series_and_queue_remove(monkeypatch):
    import json
    seen = []

    def cmd(request):
        seen.append(json.loads(request.content))
        return {"id": 9}

    def rm(request):
        seen.append((request.method, dict(request.url.params), json.loads(request.content)))
        return (200, "")

    patch_http(monkeypatch, arr, {"/api/v3/command": cmd, "/api/v3/queue/bulk": rm})
    tools = load_tools(arr)
    assert (await tools["sonarr_refresh_series"](series_id=5, rescan=True))["command"] == "RescanSeries"
    assert seen[0] == {"name": "RescanSeries", "seriesId": 5}
    out = await tools["arr_queue_remove"](service="sonarr", queue_ids=[1, 2], blocklist=True)
    assert out["blocklisted"] is True and seen[1] == ("DELETE", {"removeFromClient": "true", "blocklist": "true"}, {"ids": [1, 2]})
    with pytest.raises(ValueError, match="empty"):
        await tools["arr_queue_remove"](service="sonarr", queue_ids=[])


# ── arr_queue paging / arr_queue_remove batching ─────────────────────────────

async def test_queue_paging_and_download_id(monkeypatch):
    seen = {}

    def queue(req):
        seen.update(dict(req.url.params))
        return {"totalRecords": 120, "records": [
            {"id": 7, "downloadId": "abc123", "title": "T", "size": 0, "sizeleft": 0},
        ]}

    patch_http(monkeypatch, arr, {"/api/v3/queue": queue})
    out = await load_tools(arr)["arr_queue"](service="sonarr", page=2, page_size=50)
    assert seen["page"] == "2" and seen["pageSize"] == "50"
    assert out["page"] == 2 and out["page_size"] == 50 and out["total"] == 120
    assert out["has_more"] is True            # 2 * 50 < 120
    assert out["items"][0]["download_id"] == "abc123"

    last = await load_tools(arr)["arr_queue"](service="sonarr", page=3, page_size=50)
    assert last["has_more"] is False          # 3 * 50 >= 120


async def test_queue_page_bounds_are_clamped(monkeypatch):
    seen = {}

    def queue(req):
        seen.update(dict(req.url.params))
        return {"totalRecords": 0, "records": []}

    patch_http(monkeypatch, arr, {"/api/v3/queue": queue})
    await load_tools(arr)["arr_queue"](service="sonarr", page=0, page_size=5000)
    assert seen["page"] == "1" and seen["pageSize"] == "200"


async def test_queue_remove_sends_small_batches(monkeypatch):
    batches = []

    def bulk(req):
        import json
        batches.append(json.loads(req.content)["ids"])
        return (200, {})

    patch_http(monkeypatch, arr, {"/api/v3/queue/bulk": bulk})
    out = await load_tools(arr)["arr_queue_remove"](service="sonarr", queue_ids=list(range(1, 11)))
    assert batches == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]
    assert out["removed"] == list(range(1, 11)) and out["already_gone"] == []


async def test_queue_remove_treats_404_as_already_removed(monkeypatch):
    singles = []

    def single(req):
        qid = int(req.url.path.rsplit("/", 1)[1])
        singles.append(qid)
        return (404, {}) if qid == 2 else (200, {})

    patch_http(monkeypatch, arr, {
        "/api/v3/queue/bulk": (404, {"message": "NotFound"}),
        "/api/v3/queue/*": single,
    })
    out = await load_tools(arr)["arr_queue_remove"](service="sonarr", queue_ids=[1, 2, 3], blocklist=True)
    assert singles == [1, 2, 3]
    assert out["removed"] == [1, 3] and out["already_gone"] == [2]
    assert out["blocklisted"] is True


async def test_queue_remove_failure_reports_progress(monkeypatch):
    calls = {"n": 0}

    def bulk(req):
        calls["n"] += 1
        return (200, {}) if calls["n"] == 1 else (502, {"error": "bad gateway"})

    patch_http(monkeypatch, arr, {"/api/v3/queue/bulk": bulk})
    with pytest.raises(RuntimeError) as exc:
        await load_tools(arr)["arr_queue_remove"](service="sonarr", queue_ids=list(range(1, 11)))
    msg = str(exc.value)
    assert "[5, 6, 7, 8]" in msg                       # the batch that failed
    assert "Removed so far: [1, 2, 3, 4]" in msg
    assert "not attempted: [9, 10]" in msg


# ── second-version workflow (read side) ──────────────────────────────────────
# Fixtures are hand-built from the Radarr v3 / Prowlarr v1 API schemas, not recorded from the live services.

import httpx


async def test_radarr_releases_has_grab_handles_and_mbps(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v3/movie/7": {"id": 7, "runtime": 133},
        "/api/v3/release": [
            {"title": "Blues.Brothers.1980.2160p", "indexer": "geek", "indexerId": 3, "guid": "g1",
             "size": 20 * 1024**3, "quality": {"quality": {"name": "Bluray-2160p"}}, "qualityWeight": 19,
             "customFormatScore": 50, "customFormats": [{"name": "HDR"}], "rejected": False},
            {"title": "Other", "guid": "g2", "indexerId": 4, "size": 0, "rejected": True, "rejections": ["Not an upgrade"]},
        ],
    })
    out = await load_tools(arr)["radarr_releases"](movie_id=7)
    top = out[0]
    assert top["guid"] == "g1" and top["indexer_id"] == 3 and top["custom_formats"] == ["HDR"]
    assert top["custom_format_score"] == 50 and top["quality_weight"] == 19
    assert top["est_mbps"] == round(20 * 1024**3 * 8 / (133 * 60) / 1e6, 1)
    assert out[1]["rejections"] == ["Not an upgrade"] and out[1]["est_mbps"] is None


async def test_radarr_releases_timeout_is_a_clear_error(monkeypatch):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    patch_http(monkeypatch, arr, {"/api/v3/movie/7": {"runtime": 100}, "/api/v3/release": boom})
    out = await load_tools(arr)["radarr_releases"](movie_id=7, timeout_s=15)
    assert "didn't respond within 15s" in out["error"] and "arr_health" in out["error"]


async def test_arr_health(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/health": [
        {"type": "warning", "source": "MetadataCheck", "message": "radarr.servarr.com 521", "wikiUrl": "u"}]})
    out = await load_tools(arr)["arr_health"](service="radarr")
    assert out[0]["source"] == "MetadataCheck" and "521" in out[0]["message"]


async def test_queue_flags_duplicates_and_zero_left(monkeypatch):
    mb = 1024 * 1024
    patch_http(monkeypatch, arr, {"/api/v3/queue": {"totalRecords": 3, "records": [
        {"id": 1, "movieId": 9, "title": "A", "status": "downloading", "size": 100 * mb, "sizeleft": 0},
        {"id": 2, "movieId": 9, "title": "A again", "status": "downloading", "size": 100 * mb, "sizeleft": 50 * mb},
        {"id": 3, "movieId": 10, "title": "B", "status": "completed", "size": 100 * mb, "sizeleft": 0},
    ]}})
    out = await load_tools(arr)["arr_queue"](service="radarr")
    assert out["duplicates"] == [{"target": "movieId=9", "queue_ids": [1, 2], "titles": ["A", "A again"]}]
    assert "0 MB left" in out["items"][0]["warning"]
    assert out["items"][1]["warning"] == "" and out["items"][2]["warning"] == ""


async def test_prowlarr_search_returns_handles(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v1/search": [
        {"title": "T", "indexer": "geek", "indexerId": 3, "guid": "https://x/details/1",
         "size": 10 * 1024**3, "downloadUrl": "http://prowlarr/3/download?apikey=SECRET"},
    ]})
    out = await load_tools(arr)["prowlarr_search"](query="blues", kind="movie", runtime_min=133)
    assert out[0]["guid"] == "https://x/details/1" and out[0]["indexer_id"] == 3
    assert out[0]["est_mbps"] == round(10 * 1024**3 * 8 / (133 * 60) / 1e6, 1)
    assert "SECRET" not in str(out)


# ── destructive half ─────────────────────────────────────────────────────────

async def test_radarr_grab_ok_and_expired_cache(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v3/release": {"title": "T", "indexer": "geek", "size": 1024**3}})
    out = await load_tools(arr)["radarr_grab"](guid="g", indexer_id=3)
    assert out["grabbed"] == "T" and out["replaces_current_file"] is True
    patch_http(monkeypatch, arr, {"/api/v3/release": (404, {"message": "Unable to find matching release"})})
    out = await load_tools(arr)["radarr_grab"](guid="g", indexer_id=3)
    assert "search cache" in out["error"]


def _prowlarr_routes(sab_category, radarr_category="movies", sab_enabled=True):
    sab = [{"name": "SABnzbd", "implementation": "Sabnzbd", "enable": sab_enabled,
            "fields": [{"name": "category", "value": sab_category}]}]
    return {
        "/api/v1/downloadclient": sab,
        "/api/v3/downloadclient": [{"fields": [{"name": "movieCategory", "value": radarr_category}]}],
        "/api/v1/search": {"ok": True},
    }


async def test_prowlarr_grab_requires_dedicated_category(monkeypatch):
    sent = {}

    def search(request):
        sent["body"] = request.content
        return {}

    routes = _prowlarr_routes("manual") | {"/api/v1/search": search}
    patch_http(monkeypatch, arr, routes)
    out = await load_tools(arr)["prowlarr_grab"](guid="g", indexer_id=3)
    assert out["category"] == "manual" and b'"indexerId"' in sent["body"]

    patch_http(monkeypatch, arr, _prowlarr_routes("movies"))
    assert "also watch" in (await load_tools(arr)["prowlarr_grab"](guid="g", indexer_id=3))["error"]
    patch_http(monkeypatch, arr, _prowlarr_routes("manual", sab_enabled=False))
    assert "no enabled SABnzbd" in (await load_tools(arr)["prowlarr_grab"](guid="g", indexer_id=3))["error"]


async def test_rename_preview_and_apply(monkeypatch):
    posted = {}

    def command(request):
        posted["body"] = request.content
        return {"id": 5}

    patch_http(monkeypatch, arr, {
        "/api/v3/config/naming": {"standardMovieFormat": "{Movie CleanTitle} ({Release Year}) - {Quality Full}",
                                   "movieFolderFormat": "{Movie CleanTitle} ({Release Year})", "renameMovies": False},
        "/api/v3/rename": [{"movieFileId": 11, "existingPath": "a.mkv", "newPath": "b.mkv"}],
        "/api/v3/command": command,
    })
    t = load_tools(arr)["radarr_rename_files"]
    prev = await t(movie_id=7)
    assert prev["applied"] is False and prev["format_groups_in_jellyfin"] and "body" not in posted
    assert "OFF" in prev["warning"]
    done = await t(movie_id=7, apply=True)
    assert done["applied"] and b'"RenameFiles"' in posted["body"] and b"11" in posted["body"]


async def test_manual_import_preview_apply_and_path_guard(monkeypatch):
    posted = {}

    def command(request):
        posted["body"] = request.content
        return {"id": 9}

    patch_http(monkeypatch, arr, {
        "/api/v3/manualimport": [
            {"path": "/data/usenet/complete/X/x.mkv", "size": 5 * 1024**3, "quality": {"quality": {"name": "x"}}, "rejections": []},
            {"path": "/data/usenet/complete/X/bad.mkv", "rejections": [{"reason": "Sample"}]},
        ],
        "/api/v3/command": command,
    })
    t = load_tools(arr)["radarr_manual_import"]
    prev = await t(movie_id=7, folder="/data/usenet/complete/X")
    assert prev["applied"] is False and prev["candidates"][1]["rejections"] == ["Sample"]
    done = await t(movie_id=7, folder="/data/usenet/complete/X", apply=True)
    assert done["importing"] == ["/data/usenet/complete/X/x.mkv"] and b'"ManualImport"' in posted["body"]
    with pytest.raises(ValueError):
        await t(movie_id=7, folder="/data/media/movies/X")


async def test_rename_flags_default_format_that_will_not_group(monkeypatch):
    patch_http(monkeypatch, arr, {
        "/api/v3/config/naming": {"standardMovieFormat": "{Movie Title} ({Release Year}) {Quality Full}",
                                  "movieFolderFormat": "{Movie Title} ({Release Year})", "renameMovies": True},
        "/api/v3/rename": [],
    })
    out = await load_tools(arr)["radarr_rename_files"](movie_id=1)
    assert out["format_groups_in_jellyfin"] is False and "NOT group" in out["warning2"] and "warning" not in out


async def test_manual_import_force_only_overrides_upgrade_rejections(monkeypatch):
    posted = {}

    def command(request):
        posted["body"] = request.content
        return {"id": 1}

    patch_http(monkeypatch, arr, {
        "/api/v3/manualimport": [
            {"path": "/data/usenet/complete/X/down.mkv", "rejections": [{"reason": "Not an upgrade for existing movie file"}]},
            {"path": "/data/usenet/complete/X/sample.mkv", "rejections": [{"reason": "Sample"}]},
        ],
        "/api/v3/command": command,
    })
    t = load_tools(arr)["radarr_manual_import"]
    blocked = await t(movie_id=1, folder="/data/usenet/complete/X", apply=True)
    assert "force=True" in blocked["error"] and "body" not in posted
    forced = await t(movie_id=1, folder="/data/usenet/complete/X", apply=True, force=True)
    assert forced["importing"] == ["/data/usenet/complete/X/down.mkv"]


async def test_prowlarr_search_protocol_filter_and_grab_refuses_torrents(monkeypatch):
    patch_http(monkeypatch, arr, {"/api/v1/search": [
        {"title": "T", "protocol": "torrent", "guid": "magnet:?xt=1", "indexerId": 7},
        {"title": "N", "protocol": "usenet", "guid": "https://x/d/1", "indexerId": 2},
    ]})
    out = await load_tools(arr)["prowlarr_search"](query="x", protocol="usenet")
    assert [r["title"] for r in out] == ["N"]
    grab = load_tools(arr)["prowlarr_grab"]
    assert "usenet-only" in (await grab(guid="magnet:?xt=1", indexer_id=7))["error"]
    assert "usenet-only" in (await grab(guid="https://y.pw/t/1", indexer_id=6, protocol="torrent"))["error"]
