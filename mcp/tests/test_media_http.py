from nest_mcp.tools import jellyfin, jellyseerr
from helpers import load_tools, patch_http


async def test_jellyfin_sessions_only_active(monkeypatch):
    patch_http(monkeypatch, jellyfin, {
        "/Sessions": [
            {"UserName": "alice", "Client": "web", "DeviceName": "pc",
             "NowPlayingItem": {"Name": "Dune", "Type": "Movie", "RunTimeTicks": 1000},
             "PlayState": {"PositionTicks": 500, "IsPaused": False}, "TranscodingInfo": {"x": 1}},
            {"UserName": "bob"},  # idle, no NowPlayingItem -> filtered out
        ],
    })
    out = await load_tools(jellyfin)["jellyfin_sessions"]()
    assert len(out) == 1
    assert out[0]["user"] == "alice"
    assert out[0]["playing"] == "Dune"
    assert out[0]["position_pct"] == 50.0
    assert out[0]["transcoding"] is True


async def test_jellyfin_library_stats_merges_counts(monkeypatch):
    patch_http(monkeypatch, jellyfin, {
        "/Library/VirtualFolders": [{"Name": "Movies", "CollectionType": "movies",
                                     "ItemId": "1", "Locations": ["/m"]}],
        "/Items/Counts": {"MovieCount": 42},
    })
    out = await load_tools(jellyfin)["jellyfin_library_stats"]()
    assert out[0]["name"] == "Movies"
    assert out[0]["movie_count"] == 42


async def test_jellyseerr_requests_maps_filter(monkeypatch):
    patch_http(monkeypatch, jellyseerr, {
        "/api/v1/request": {"results": [
            {"id": 7, "status": 1, "type": "movie",
             "media": {"tmdbId": 603, "status": 3},
             "requestedBy": {"displayName": "alice"}, "createdAt": "t"},
        ]},
    })
    out = await load_tools(jellyseerr)["jellyseerr_requests"](status="pending")
    assert out[0]["id"] == 7 and out[0]["requested_by"] == "alice"


async def test_jellyseerr_stats_passthrough(monkeypatch):
    patch_http(monkeypatch, jellyseerr, {"/api/v1/request/count": {"pending": 2, "available": 5}})
    out = await load_tools(jellyseerr)["jellyseerr_stats"]()
    assert out == {"pending": 2, "available": 5}
