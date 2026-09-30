import csv
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from lidarr_triage import apply as apply_mod
from lidarr_triage.cli import main
from lidarr_triage.lidarr import Lidarr

from .conftest import lidarr_album, lidarr_file, lidarr_track

URL = "http://lidarr.test"


class FakeLidarr:
    """Stateful respx-backed Lidarr: records every call in order."""

    def __init__(self, *, recycle_bin="/data/lidarr-recycle", backup_age=timedelta(minutes=5),
                 monitor_sticks=True, fail_delete_id=None, lists=None):
        self.calls: list[str] = []
        self.album = lidarr_album(1, "Band", "LP", files=3)
        self.tracks = [lidarr_track(11, 1, "Hit", 101), lidarr_track(12, 1, "B", 102),
                       lidarr_track(13, 1, "C", 103)]
        self.files = {i: lidarr_file(i, 1) for i in (101, 102, 103)}
        self.recycle_bin = recycle_bin
        self.backup_time = datetime.now(timezone.utc) - backup_age
        self.monitor_sticks = monitor_sticks
        self.fail_delete_id = fail_delete_id
        self.lists = lists or []

    def install(self, router: respx.MockRouter) -> None:
        def rec(name, response):
            def handler(request):
                self.calls.append(name if "{" not in name else name.format(path=request.url.path))
                return response(request) if callable(response) else response
            return handler

        router.get(f"{URL}/api/v1/system/status").mock(side_effect=rec("status", httpx.Response(200, json={"version": "3.1.2.4913"})))
        router.get(f"{URL}/api/v1/config/mediamanagement").mock(side_effect=rec("mm", httpx.Response(200, json={"recycleBin": self.recycle_bin, "recycleBinCleanupDays": 30})))
        router.get(f"{URL}/api/v1/system/backup").mock(side_effect=rec("backup", lambda r: httpx.Response(200, json=[{"id": 1, "type": "manual", "time": self.backup_time.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")}])))
        router.get(f"{URL}/api/v1/importlist").mock(side_effect=rec("importlist", lambda r: httpx.Response(200, json=self.lists)))
        router.get(f"{URL}/api/v1/album/1").mock(side_effect=rec("get-album", lambda r: httpx.Response(200, json=self.album)))
        router.get(f"{URL}/api/v1/trackfile").mock(side_effect=rec("get-files", lambda r: httpx.Response(200, json=list(self.files.values()))))
        router.get(f"{URL}/api/v1/track").mock(side_effect=rec("get-tracks", lambda r: httpx.Response(200, json=self.tracks)))

        def monitor(request):
            body = json.loads(request.content)
            if self.monitor_sticks:
                self.album = {**self.album, "monitored": body["monitored"]}
            return httpx.Response(202, json=[self.album])
        router.put(f"{URL}/api/v1/album/monitor").mock(side_effect=rec("unmonitor", monitor))

        def delete(request):
            fid = int(request.url.path.rsplit("/", 1)[1])
            if fid == self.fail_delete_id:
                return httpx.Response(500, text="disk error")
            self.files.pop(fid)
            return httpx.Response(200)
        router.delete(url__regex=rf"{URL}/api/v1/trackfile/\d+").mock(side_effect=rec("delete {path}", delete))

    def writes(self):
        return [c for c in self.calls if c == "unmonitor" or c.startswith("delete")]


def _row(keep=(11,), snapshot=(101, 102, 103)):
    return apply_mod.Row(1, "Band", "LP", set(keep), set(snapshot))


@pytest.fixture
def fake():
    return FakeLidarr()


def _client():
    return Lidarr(URL, "test-key")


def _log(tmp_path):
    return apply_mod.RunLog(tmp_path / "run.jsonl")


def test_order_is_unmonitor_verify_then_delete(fake, tmp_path):
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row()], execute=True, log=_log(tmp_path))
    assert out.done == [1] and not out.aborted
    seq = [c for c in fake.calls if c in ("unmonitor", "get-album") or c.startswith("delete")]
    first_delete = next(i for i, c in enumerate(seq) if c.startswith("delete"))
    assert seq.index("unmonitor") < first_delete
    assert "get-album" in seq[seq.index("unmonitor"):first_delete]  # verify step between
    assert fake.writes() == ["unmonitor", "delete /api/v1/trackfile/102", "delete /api/v1/trackfile/103"]
    assert set(fake.files) == {101}


def test_no_delete_when_unmonitor_does_not_stick(tmp_path):
    fake = FakeLidarr(monitor_sticks=False)
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row()], execute=True, log=_log(tmp_path))
    assert 1 in out.aborted and "still monitored" in out.aborted[1]
    assert not any(c.startswith("delete") for c in fake.calls)
    assert len(fake.files) == 3


def test_stale_snapshot_aborts_before_any_write(fake, tmp_path):
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row(snapshot=(101, 102))], execute=True, log=_log(tmp_path))
    assert "changed since report" in out.aborted[1]
    assert fake.writes() == []


def test_keep_track_without_file_aborts(fake, tmp_path):
    fake.tracks[0] = lidarr_track(11, 1, "Hit", None)
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row()], execute=True, log=_log(tmp_path))
    assert "have no file" in out.aborted[1] and fake.writes() == []


def test_delete_failure_aborts_rest_of_album_and_logs(tmp_path):
    fake = FakeLidarr(fail_delete_id=102)
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row()], execute=True, log=_log(tmp_path))
    assert out.aborted[1].startswith("delete:")
    assert "delete /api/v1/trackfile/103" not in fake.calls
    events = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[:4] == ["start", "album", "unmonitor", "verified-unmonitored"]
    assert "abort" in kinds and "delete" not in kinds
    album = next(e for e in events if e["event"] == "album")
    assert {f["path"] for f in album["trackfiles"]} == {f"/data/media/music/a/{i}.flac" for i in (101, 102, 103)}


@pytest.mark.parametrize("kwargs,match", [
    ({"recycle_bin": ""}, "Recycling Bin is not set"),
    ({"backup_age": timedelta(hours=3)}, "newest Lidarr backup"),
    ({"lists": [{"name": "Spotify", "enableAutomaticAdd": True, "shouldMonitorExisting": True}]}, "Monitor Existing"),
])
def test_preconditions_block_all_writes(kwargs, match, tmp_path):
    fake = FakeLidarr(**kwargs)
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        with pytest.raises(apply_mod.PreconditionError, match=match):
            apply_mod.run(_client(), [_row()], execute=True, log=_log(tmp_path))
    assert fake.writes() == []


def test_dry_run_makes_no_writes(fake):
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        out = apply_mod.run(_client(), [_row()], execute=False, log=None)
    assert out.planned == {1: [102, 103]}
    assert fake.writes() == [] and "mm" not in fake.calls


def _csv(tmp_path, approved):
    path = tmp_path / "prune.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["approved", "album_id", "artist", "album", "keep_track_ids", "trackfile_snapshot"])
        w.writeheader()
        w.writerow({"approved": approved, "album_id": 1, "artist": "Band", "album": "LP",
                    "keep_track_ids": "11", "trackfile_snapshot": "101;102;103"})
    return path


def test_unapproved_rows_are_ignored(tmp_path):
    assert apply_mod.read_approved(_csv(tmp_path, "")) == []
    assert [r.album_id for r in apply_mod.read_approved(_csv(tmp_path, "Y"))] == [1]


def test_cli_apply_requires_flag_to_write(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")
    reviewed = _csv(tmp_path, "yes")
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        assert main(["--url", URL, "apply", str(reviewed), "--runs", str(tmp_path / "runs")]) == 0
        assert fake.writes() == []
        assert "dry run" in capsys.readouterr().out
        assert main(["--url", URL, "apply", str(reviewed), "--apply", "--runs", str(tmp_path / "runs")]) == 0
    assert fake.writes()[0] == "unmonitor"
    assert len(list((tmp_path / "runs").glob("apply-*.jsonl"))) == 1


def test_missing_api_key_is_an_error(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LIDARR_API_KEY", raising=False)
    assert main(["--url", URL, "apply", str(_csv(tmp_path, "y"))]) == 2
    assert "LIDARR_API_KEY" in capsys.readouterr().err
