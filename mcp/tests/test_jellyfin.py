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
    assert out["guide_providers"][0]["mapped_channels"] == 1
