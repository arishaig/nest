import pytest

from nest_mcp.tools import jellyfin
from helpers import load_tools, patch_http


async def test_livetv_channels_summary_sorted(monkeypatch):
    patch_http(monkeypatch, jellyfin, {"/LiveTv/Channels": {"TotalRecordCount": 4, "Items": [
        {"Number": "5250", "Name": "CBS News", "Id": "a", "ServiceName": "Live TV", "ChannelType": "TV"},
        {"Number": "2", "Name": "After School", "Id": "b", "ServiceName": "Live TV", "ChannelType": "TV"},
        {"Number": "101", "Name": "CBS News", "Id": "c", "ServiceName": "Live TV", "ChannelType": "TV"},
        {"Number": "", "Name": "Odd", "Id": "d", "ServiceName": "Other", "ChannelType": "TV"},
    ]}})
    out = await load_tools(jellyfin)["jellyfin_livetv_channels"](limit=2)
    assert out["total"] == 4
    assert [c["number"] for c in out["channels"]] == ["2", "101"]
    assert out["first_number"] == "2" and out["last_number"] == "5250"
    assert out["by_service"] == {"Live TV": 3, "Other": 1}
    assert out["by_number_band"] == {"5200-5299": 1, "0-99": 1, "100-199": 1, "unnumbered": 1}
    assert out["duplicate_names"] == {"CBS News": 2}


async def test_livetv_tuners_drops_query_strings(monkeypatch):
    patch_http(monkeypatch, jellyfin, {"/System/Configuration/livetv": {
        "TunerHosts": [{"Id": "t1", "Type": "hdhomerun", "FriendlyName": "Tunarr",
                        "Url": "http://tunarr:8000?key=secret", "TunerCount": 2}],
        "ListingProviders": [{"Id": "p1", "Type": "xmltv", "Name": "Dispatcharr",
                              "Path": "http://d/output/epg?token=secret",
                              "ChannelMappings": [{"a": 1}]}],
    }})
    out = await load_tools(jellyfin)["jellyfin_livetv_tuners"]()
    assert out["tuners"][0]["url"] == "http://tunarr:8000"
    assert out["guide_providers"][0]["path"] == "http://d/output/epg"
    assert out["guide_providers"][0]["manual_channel_mappings"] == 1
    assert out["tuners"][0]["label"] == "Tunarr" and out["tuners"][0]["max_streams"] == 2


async def test_livetv_channels_omits_service_when_jellyfin_reports_none(monkeypatch):
    patch_http(monkeypatch, jellyfin, {"/LiveTv/Channels": {"TotalRecordCount": 2, "Items": [
        {"Number": "1", "Name": "A", "Id": "a", "ServiceName": "", "ChannelType": "TV"},
        {"Number": "150", "Name": "B", "Id": "b", "ChannelType": "TV"},
    ]}})
    out = await load_tools(jellyfin)["jellyfin_livetv_channels"]()
    assert "by_service" not in out
    assert out["by_number_band"] == {"0-99": 1, "100-199": 1}


async def test_livetv_tuners_labels_and_unset_values(monkeypatch):
    patch_http(monkeypatch, jellyfin, {"/System/Configuration/livetv": {
        "TunerHosts": [{"Id": "t1", "Type": "m3u", "FriendlyName": None,
                        "Url": "http://dispatcharr-main:9191/output/m3u", "TunerCount": 0}],
        "ListingProviders": [{"Id": "p1", "Type": "xmltv", "Path": "http://tunarr:8000/api/xmltv.xml"}],
    }})
    out = await load_tools(jellyfin)["jellyfin_livetv_tuners"]()
    assert out["tuners"][0]["label"] == "dispatcharr-main:9191"
    assert out["tuners"][0]["max_streams"] == "unlimited"
    assert out["guide_providers"][0]["label"] == "tunarr:8000"
    assert out["guide_providers"][0]["manual_channel_mappings"] == 0


async def test_refresh_all_and_named_series(monkeypatch):
    calls = []

    def post(request):
        calls.append((request.url.path, dict(request.url.params)))
        return (204, "")

    patch_http(monkeypatch, jellyfin, {
        "/Library/Refresh": post,
        "/Items": {"Items": [{"Id": "a", "Name": "The Twilight Zone", "ProductionYear": 2002},
                             {"Id": "b", "Name": "The Twilight Zone Redux"}]},
        "/Items/a/Refresh": post,
    })
    tool = load_tools(jellyfin)["jellyfin_refresh"]
    assert (await tool())["refreshing"] == "all libraries"
    out = await tool(series="the twilight zone", replace_metadata=True)
    assert [i["id"] for i in out["refreshing"]] == ["a"] and out["replace_metadata"] is True
    path, params = calls[-1]
    assert path == "/Items/a/Refresh" and params["ReplaceAllMetadata"] == "true" and params["Recursive"] == "true"
    with pytest.raises(ValueError, match="No Jellyfin series"):
        await tool(series="Nope")


async def test_refresh_notifies_specific_paths(monkeypatch):
    sent = []

    def post(request):
        import json
        sent.append(json.loads(request.content))
        return (204, "")

    patch_http(monkeypatch, jellyfin, {"/Library/Media/Updated": post})
    out = await load_tools(jellyfin)["jellyfin_refresh"](
        created_paths=["/data/media/tv/TZ (1959)"], deleted_paths=["/data/media/tv/TZ (2002)"])
    assert sent[0]["Updates"] == [
        {"Path": "/data/media/tv/TZ (2002)", "UpdateType": "Deleted"},
        {"Path": "/data/media/tv/TZ (1959)", "UpdateType": "Created"},
    ]
    assert out["notified"] == sent[0]["Updates"]
