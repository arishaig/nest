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
