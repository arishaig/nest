import pytest

from nest_mcp import config
from nest_mcp.tools import media_pipeline
from helpers import load_tools, patch_http

UUID = "e174d35b-da61-4d10-afc1-722b00c817ed"
REMUX = "The Blues Brothers (1980) - Remux-2160p.mkv"
P1080 = "The Blues Brothers (1980) - 1080p x265.mp4"
FOLDER = "/data/media/movies/The Blues Brothers (1980)"


def _source(name, fname, probed=True, container="mkv"):
    streams = [
        {"Type": "Video", "Codec": "hevc", "Width": 1920, "Height": 1040, "VideoRangeType": "SDR", "BitRate": 2_000_000},
        {"Type": "Audio", "Codec": "aac", "Channels": 6, "DisplayTitle": "English - AAC - 5.1"},
    ] if probed else []
    return {"Id": fname, "Name": name, "Path": f"{FOLDER}/{fname}", "Container": container, "Size": 2 * 1024**3,
            "Bitrate": 2_200_000 if probed else None, "MediaStreams": streams}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config.fileserver, "media_root", "/Tank/media_root")
    state = {"jf_sources": [_source("Remux-2160p", REMUX)], "queue": [], "history": [], "ls": REMUX}

    async def fake_sab(mode, **p):
        if mode == "queue":
            return {"queue": {"slots": state["queue"]}}
        return {"history": {"slots": state["history"]}}

    async def fake_ssh(host, cmd, **kw):
        return state["ls"]

    monkeypatch.setattr(media_pipeline, "sab_call", fake_sab)
    monkeypatch.setattr(media_pipeline, "ssh_run", fake_ssh)
    patch_http(monkeypatch, media_pipeline, {"/Items": lambda r: {"Items": [
        {"Id": "m1", "Name": "The Blues Brothers", "ProductionYear": 1980, "MediaSources": state["jf_sources"]},
        {"Id": "m2", "Name": "Blues Brothers 2000", "ProductionYear": 1998, "MediaSources": []},
    ]}})
    return state


async def test_status_finished_pipeline_two_versions(env):
    env["jf_sources"].append(_source("1080p x265", P1080, container="mp4"))
    env["ls"] = f"{REMUX}\n{P1080}\nposter.jpg\n"
    env["history"] = [{"nzo_id": UUID, "name": "The.Blues.Brothers.1980.EXTENDED.1080p.BluRay.x265-LAMA", "status": "Completed",
                       "category": "prowlarr", "storage": "/data/usenet/complete/x.mp4", "fail_message": ""}]
    out = await load_tools(media_pipeline)["movie_version_status"](title="The Blues Brothers", year=1980)
    assert [m["name"] for m in out["jellyfin"]] == ["The Blues Brothers"]  # year filter dropped the 1998 film
    m = out["jellyfin"][0]
    assert len(m["versions"]) == 2 and m["on_disk_not_in_jellyfin"] == []
    assert m["versions"][1]["video"] == "hevc 1920x1040 SDR" and m["versions"][1]["audio"] == "aac 6ch"
    assert out["downloads"][0]["state"] == "Completed"
    assert any("2 version(s) in Jellyfin" in n for n in out["summary"])


async def test_status_placed_but_not_scanned_and_unprobed(env):
    env["jf_sources"].append(_source("1080p x265", P1080, probed=False, container="mp4"))
    env["ls"] = f"{REMUX}\n{P1080}\nThe Blues Brothers (1980) - 2160p x265.mkv\n"
    out = await load_tools(media_pipeline)["movie_version_status"](title="The Blues Brothers", year=1980)
    m = out["jellyfin"][0]
    assert m["on_disk_not_in_jellyfin"] == ["The Blues Brothers (1980) - 2160p x265.mkv"]
    assert m["versions"][1]["probed"] is False and m["versions"][1]["video"] == "not probed yet"
    assert any("on disk but not in Jellyfin" in n for n in out["summary"])
    assert any("not probed" in n for n in out["summary"])


async def test_status_reports_queue_and_failed_jobs_and_ignores_other_titles(env):
    env["queue"] = [{"nzo_id": UUID, "filename": "The.Blues.Brothers.1980.EXTENDED.2160p.x265-LAMA", "cat": "prowlarr",
                     "status": "Downloading", "percentage": "40", "mbleft": "5887", "timeleft": "0:39:26"},
                    {"nzo_id": "z", "filename": "Spider-Man.2026.2160p", "cat": "movies", "status": "Downloading", "mbleft": "1"}]
    env["history"] = [{"nzo_id": "f", "name": "The.Blues.Brothers.1980.EXTENDED.2160p.DDP5.1-LAMA", "status": "Failed",
                       "category": "prowlarr", "storage": "", "fail_message": "Repair failed, not enough repair blocks (2877 short)"},
                      {"nzo_id": "o", "name": "The.Blues.Brothers.2000.1080p", "status": "Completed", "category": "x", "storage": "/s"}]
    out = await load_tools(media_pipeline)["movie_version_status"](title="The Blues Brothers", year=1980)
    assert [j["where"] for j in out["downloads"]] == ["queue", "history"]  # Spider-Man and the 2000 film excluded
    assert any("still downloading" in n for n in out["summary"])
    assert any("FAILED" in n and "2877 short" in n for n in out["summary"])


async def test_status_survives_sabnzbd_and_ssh_errors(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("SABnzbd unreachable")

    monkeypatch.setattr(media_pipeline, "sab_call", boom)
    monkeypatch.setattr(media_pipeline, "ssh_run", boom)
    out = await load_tools(media_pipeline)["movie_version_status"](title="The Blues Brothers", year=1980)
    assert "unreachable" in out["downloads"][0]["error"]
    assert "could not list folder" in out["jellyfin"][0]["on_disk_not_in_jellyfin"]


async def test_status_requires_a_title(env):
    with pytest.raises(ValueError):
        await load_tools(media_pipeline)["movie_version_status"](title="  ")
