import csv

import httpx
import respx

from lidarr_triage.cli import main

from .conftest import lidarr_album, lidarr_file, lidarr_track, rec

URL = "http://lidarr.test"


def _install(router):
    albums = [lidarr_album(1, "Band", "Big LP", artist_id=7, files=3),
              lidarr_album(2, "Band", "Other LP", artist_id=7, files=2, monitored=False),
              lidarr_album(3, "Band", "Wanted Only", artist_id=7, files=0)]
    tracks = [lidarr_track(11, 1, "Hit", 101), lidarr_track(12, 1, "B", 102), lidarr_track(13, 1, "C", 103),
              lidarr_track(21, 2, "D", 201), lidarr_track(22, 2, "E", 202)]
    files = [lidarr_file(101, 1, quality="MP3-320"), lidarr_file(102, 1), lidarr_file(103, 1),
             lidarr_file(201, 2, quality="AAC-256"), lidarr_file(202, 2)]

    def by_album(items):
        def handler(request):
            p = request.url.params
            if "albumId" in p:
                return httpx.Response(200, json=[i for i in items if i["albumId"] == int(p["albumId"])])
            return httpx.Response(200, json=items)
        return handler

    router.get(f"{URL}/api/v1/system/status").mock(return_value=httpx.Response(200, json={"version": "3.1.2.4913"}))
    router.get(f"{URL}/api/v1/album").mock(return_value=httpx.Response(200, json=albums))
    router.get(f"{URL}/api/v1/track").mock(side_effect=by_album(tracks))
    router.get(f"{URL}/api/v1/trackfile").mock(side_effect=by_album(files))


def test_report_and_quality_report(history_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")
    hist = history_dir([rec("Band", "Big LP (Remastered)", "Hit"), rec("Band", "Big LP", "Hit", days_ago=40)])
    out = tmp_path / "reports"
    with respx.mock(assert_all_called=False) as router:
        _install(router)
        assert main(["--url", URL, "report", "--history", str(hist), "--out", str(out)]) == 0
        prune = list(csv.DictReader((out / "prune.csv").open()))
        assert [r["album_id"] for r in prune] == ["1"]
        assert prune[0]["keep_track_ids"] == "11"
        assert prune[0]["trackfile_snapshot"] == "101;102;103"
        assert prune[0]["keep_non_flac"] == "Hit [MP3-320]"
        assert prune[0]["approved"] == ""
        hold = list(csv.DictReader((out / "hold.csv").open()))
        assert [r["album_id"] for r in hold] == ["2"]  # album 3 has no files: not considered
        assert "PRUNE" in (out / "summary.md").read_text()

        assert main(["--url", URL, "quality-report", "--out", str(out)]) == 0
        rows = list(csv.DictReader((out / "non-flac-kept.csv").open()))
        assert [(r["album_id"], r["quality"]) for r in rows] == [("2", "AAC-256")]

        assert main(["--url", URL, "quality-report", "--from", str(out / "prune.csv"), "--out", str(out)]) == 0
        rows = list(csv.DictReader((out / "non-flac-kept.csv").open()))
        assert [(r["track"], r["quality"]) for r in rows] == [("Hit", "MP3-320")]


def test_report_fails_loudly_on_unexpected_album_shape(history_dir, monkeypatch, capsys):
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")
    hist = history_dir([rec("Band", "Big LP", "Hit")])
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{URL}/api/v1/system/status").mock(return_value=httpx.Response(200, json={"version": "3.1.2.4913"}))
        router.get(f"{URL}/api/v1/album").mock(return_value=httpx.Response(200, json=[{"id": 1, "artistId": 7, "title": "X"}]))
        assert main(["--url", URL, "report", "--history", str(hist)]) == 2
    assert "statistics" in capsys.readouterr().err
