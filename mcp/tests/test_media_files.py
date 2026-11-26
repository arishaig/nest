import json

from nest_mcp.tools import media_files
from nest_mcp.tools.media_files import _parse_ncdu
from helpers import load_tools, patch_ssh


def test_parse_ncdu_flattens_with_depth():
    node = [
        {"name": "root"},
        {"name": "a.mkv", "dsize": 2_097_152},               # 2 MB file
        [{"name": "sub", "dsize": 1_048_576},                # 1 MB dir
         {"name": "b.mkv", "dsize": 1_048_576}],
    ]
    out = _parse_ncdu(node, depth=2)
    by_name = {e["name"]: e for e in out}
    assert by_name["a.mkv"]["type"] == "file" and by_name["a.mkv"]["size_mb"] == 2.0
    assert by_name["sub"]["type"] == "dir"
    assert by_name["b.mkv"]["path"] == "sub/b.mkv"


def test_parse_ncdu_respects_depth_limit():
    node = [{"name": "root"}, [{"name": "sub"}, {"name": "deep.mkv", "dsize": 0}]]
    out = _parse_ncdu(node, depth=1)
    assert [e["name"] for e in out] == ["sub"]  # does not descend


async def test_media_ls_rejects_traversal(monkeypatch):
    out = await load_tools(media_files)["media_ls"](path="../etc")
    assert "error" in out


async def test_media_ls_parses_ncdu(monkeypatch):
    ncdu = json.dumps([1, 0, {"progver": "x"}, [{"name": "media"}, {"name": "x.mkv", "dsize": 1_048_576}]])
    patch_ssh(monkeypatch, media_files, ncdu)
    out = await load_tools(media_files)["media_ls"](path="tv", depth=1)
    assert out["total"] == 1 and out["entries"][0]["name"] == "x.mkv"
    assert out["truncated"] is False
    assert out["root"].endswith("/media_root/media/tv")


async def test_media_ls_reports_ncdu_error_text(monkeypatch):
    patch_ssh(monkeypatch, media_files, "Error obtaining full path: No such file or directory.")
    out = await load_tools(media_files)["media_ls"](path="tv")
    assert "No such file" in out["error"]


async def test_media_ls_bad_json(monkeypatch):
    patch_ssh(monkeypatch, media_files, "[not-json")
    out = await load_tools(media_files)["media_ls"](path="tv")
    assert "Failed to parse" in out["error"]


async def test_audio_lang_survey_rejects_traversal(monkeypatch):
    out = await load_tools(media_files)["media_audio_lang_survey"](path="../etc")
    assert "error" in out


async def test_audio_lang_survey_scans_tv_and_movies_by_default(monkeypatch):
    seen_cmd = {}

    def fake(host, cmd):
        seen_cmd["cmd"] = cmd
        return json.dumps({"total_files": 2, "counts": {"ok": 1, "needs_fix": 1}, "examples": {}})

    patch_ssh(monkeypatch, media_files, fake)
    out = await load_tools(media_files)["media_audio_lang_survey"]()
    assert out["counts"]["needs_fix"] == 1
    assert "media_root/media/tv" in seen_cmd["cmd"] and "media_root/media/movies" in seen_cmd["cmd"]


async def test_audio_lang_survey_scopes_to_path(monkeypatch):
    seen_cmd = {}

    def fake(host, cmd):
        seen_cmd["cmd"] = cmd
        return json.dumps({"total_files": 0, "counts": {}, "examples": {}})

    patch_ssh(monkeypatch, media_files, fake)
    await load_tools(media_files)["media_audio_lang_survey"](path="tv/Star Trek")
    assert "media_root/media/tv/Star Trek" in seen_cmd["cmd"]
    assert "movies" not in seen_cmd["cmd"]


async def test_audio_lang_survey_reports_timeout_clearly(monkeypatch):
    def fake(host, cmd):
        raise TimeoutError

    patch_ssh(monkeypatch, media_files, fake)
    out = await load_tools(media_files)["media_audio_lang_survey"](path="tv", timeout=30)
    assert "30s" in out["error"]


async def test_audio_lang_survey_bad_json(monkeypatch):
    patch_ssh(monkeypatch, media_files, "not-json")
    out = await load_tools(media_files)["media_audio_lang_survey"](path="tv")
    assert "error" in out


async def test_audio_lang_fix_rejects_traversal(monkeypatch):
    out = await load_tools(media_files)["media_audio_lang_fix"](path="../etc")
    assert "error" in out


async def test_audio_lang_fix_defaults_to_dry_run(monkeypatch):
    seen_cmd = {}

    def fake(host, cmd):
        seen_cmd["cmd"] = cmd
        return json.dumps({"total_files": 1, "dry_run": True, "counts": {"would_fix": 1}, "examples": {}})

    patch_ssh(monkeypatch, media_files, fake)
    out = await load_tools(media_files)["media_audio_lang_fix"](path="tv")
    assert out["dry_run"] is True
    assert "--apply" not in seen_cmd["cmd"]


async def test_audio_lang_fix_apply_passes_flag(monkeypatch):
    seen_cmd = {}

    def fake(host, cmd):
        seen_cmd["cmd"] = cmd
        return json.dumps({"total_files": 1, "dry_run": False, "counts": {"fixed": 1}, "examples": {}})

    patch_ssh(monkeypatch, media_files, fake)
    out = await load_tools(media_files)["media_audio_lang_fix"](path="tv", apply=True)
    assert out["dry_run"] is False
    assert "--apply" in seen_cmd["cmd"]


async def test_audio_lang_fix_scans_tv_and_movies_by_default(monkeypatch):
    seen_cmd = {}

    def fake(host, cmd):
        seen_cmd["cmd"] = cmd
        return json.dumps({"total_files": 0, "counts": {}, "examples": {}})

    patch_ssh(monkeypatch, media_files, fake)
    await load_tools(media_files)["media_audio_lang_fix"]()
    assert "media_root/media/tv" in seen_cmd["cmd"] and "media_root/media/movies" in seen_cmd["cmd"]


async def test_audio_lang_fix_reports_timeout_clearly(monkeypatch):
    def fake(host, cmd):
        raise TimeoutError

    patch_ssh(monkeypatch, media_files, fake)
    out = await load_tools(media_files)["media_audio_lang_fix"](path="tv", timeout=30)
    assert "30s" in out["error"]


async def test_audio_lang_fix_bad_json(monkeypatch):
    patch_ssh(monkeypatch, media_files, "not-json")
    out = await load_tools(media_files)["media_audio_lang_fix"](path="tv")
    assert "error" in out


# ── media_place_version: the remote script really runs here, against a temp tree ──

import asyncio
import json as _json
import os

import pytest

from nest_mcp import config
from nest_mcp.tools import media_files


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "media_root"
    (root / "usenet/complete/Job.1080p/Sample").mkdir(parents=True)
    (root / "media/movies/Blues (1980)").mkdir(parents=True)
    monkeypatch.setattr(config.fileserver, "media_root", str(root))

    async def fake_ssh(host, cmd, **kw):
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        o, e = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(e.decode())
        return o.decode().strip()

    async def fake_sab(mode, **p):
        return {"config": {"misc": {"complete_dir": "/data/usenet/complete"}}}

    monkeypatch.setattr(media_files, "ssh_run", fake_ssh)
    monkeypatch.setattr(media_files, "sab_call", fake_sab)
    job = root / "usenet/complete/Job.1080p"
    (job / "movie.mkv").write_bytes(b"x" * 2000)
    (job / "movie.en.srt").write_text("sub")
    (job / "Sample/sample.mkv").write_bytes(b"y" * 5000)  # bigger, but a sample: must be ignored
    (job / "movie.nfo").write_text("nfo")
    return root



async def test_place_dry_run_then_apply_same_fs(tree):
    folder = tree / "media/movies/Blues (1980)"
    (folder / "Blues (1980) - Remux.mkv").write_bytes(b"r" * 10)
    os.chmod(folder / "Blues (1980) - Remux.mkv", 0o640)
    t = load_tools(media_files)["media_place_version"]
    kw = dict(source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="1080p x265", include_subtitles=True)
    plan = await t(**kw)
    assert plan["status"] == "dry_run" and plan["method"] == "rename" and plan["mode"] == "0o640"
    assert [os.path.basename(f["to"]) for f in plan["files"]] == ["Blues (1980) - 1080p x265.mkv", "Blues (1980) - 1080p x265.en.srt"]
    assert (tree / "usenet/complete/Job.1080p/movie.mkv").exists()  # dry run changed nothing
    done = await t(**kw, dry_run=False)
    assert done["status"] == "placed"
    assert (folder / "Blues (1980) - 1080p x265.mkv").stat().st_size == 2000
    assert oct(os.stat(folder / "Blues (1980) - 1080p x265.mkv").st_mode & 0o777) == "0o640"
    assert (tree / "usenet/complete/Job.1080p/movie.nfo").exists() and (tree / "usenet/complete/Job.1080p/Sample/sample.mkv").exists()
    again = await t(**kw, dry_run=False)  # source gone now
    assert "error" in again or again.get("status") == "blocked"


async def test_place_blocked_when_existing_name_does_not_group(tree):
    folder = tree / "media/movies/Blues (1980)"
    (folder / "The.Blues.Brothers.1980.Extended.mkv").write_bytes(b"r")
    out = await load_tools(media_files)["media_place_version"](
        source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="x265", dry_run=False)
    assert out["status"] == "blocked" and out["not_groupable"] == ["The.Blues.Brothers.1980.Extended.mkv"]
    assert (tree / "usenet/complete/Job.1080p/movie.mkv").exists()


async def test_place_blocks_radarr_default_name_that_jellyfin_will_not_group(tree):
    folder = tree / "media/movies/Blues (1980)"
    (folder / "Blues (1980) Remux-2160p.mkv").write_bytes(b"r")
    out = await load_tools(media_files)["media_place_version"](
        source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="x265")
    assert out["status"] == "blocked" and out["not_groupable"] == ["Blues (1980) Remux-2160p.mkv"]


async def test_place_returns_container_paths_for_jellyfin(tree):
    out = await load_tools(media_files)["media_place_version"](
        source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="x265", dry_run=False)
    assert out["movie_folder_container_path"] == "/data/media/movies/Blues (1980)"
    assert 'created_paths=["/data/media/movies/Blues (1980)"]' in out["next"]


async def test_place_copy_branch_across_filesystems(tree, tmp_path):
    shm = "/dev/shm"
    if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat(tree).st_dev:
        pytest.skip("no second filesystem available")
    import shutil as _sh, tempfile
    other = tempfile.mkdtemp(dir=shm)
    try:
        movies = tree / "media/movies"
        link_target = os.path.join(other, "movies")
        _sh.move(str(movies), link_target)
        movies.symlink_to(link_target)
        os.makedirs(os.path.join(link_target, "Blues (1980)"), exist_ok=True)
        t = load_tools(media_files)["media_place_version"]
        kw = dict(source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="x265")
        plan = await t(**kw)
        assert plan["status"] == "dry_run" and plan["method"] == "copy+verify+delete"
        done = await t(**kw, dry_run=False)
        assert done["status"] == "placed"
        dest = os.path.join(link_target, "Blues (1980)", "Blues (1980) - x265.mkv")
        assert os.path.getsize(dest) == 2000 and not os.path.exists(dest + ".partial")
        assert not (tree / "usenet/complete/Job.1080p/movie.mkv").exists()  # source removed only after verify
    finally:
        _sh.rmtree(other, ignore_errors=True)


async def test_place_never_overwrites(tree):
    folder = tree / "media/movies/Blues (1980)"
    (folder / "Blues (1980) - x265.mkv").write_bytes(b"old")
    out = await load_tools(media_files)["media_place_version"](
        source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="x265", dry_run=False)
    assert out["status"] == "blocked" and "never overwritten" in out["reason"]
    assert (folder / "Blues (1980) - x265.mkv").read_bytes() == b"old"


async def test_place_path_and_label_guards(tree):
    t = load_tools(media_files)["media_place_version"]
    with pytest.raises(ValueError):
        await t(source_path="/data/usenet/complete/../media", movie_folder="Blues (1980)", version_label="x")
    with pytest.raises(ValueError):
        await t(source_path="/etc/passwd", movie_folder="Blues (1980)", version_label="x")
    with pytest.raises(ValueError):
        await t(source_path="/data/usenet/complete/Job.1080p", movie_folder="Blues (1980)", version_label="a/b")
    # a source outside the complete dir (but still on the share) is refused by the remote script's own check
    out = await t(source_path="/data/media/movies/Blues (1980)", movie_folder="Blues (1980)", version_label="x")
    assert "not inside the SABnzbd complete dir" in out["error"]
    # symlinked source escaping the complete dir
    (tree / "usenet/complete/evil").symlink_to("/etc")
    out = await t(source_path="/data/usenet/complete/evil", movie_folder="Blues (1980)", version_label="x")
    assert "error" in out


async def test_place_folder_must_be_direct_child_of_movies(tree):
    out = await load_tools(media_files)["media_place_version"](
        source_path="/data/usenet/complete/Job.1080p", movie_folder="/data/usenet/complete/Job.1080p", version_label="x")
    assert "direct child of the movies directory" in out["error"]


# ── media_verify_file: the remote script runs here against real temp files ────

MKV_MAGIC = bytes.fromhex("1a45dfa3")


@pytest.fixture
def vtree(tree, tmp_path, monkeypatch):
    """`tree` plus a PATH without ffprobe, so results do not depend on the dev machine."""
    import sys
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{bindir}:/bin:/usr/bin" if not os.path.exists("/usr/bin/ffprobe") and not os.path.exists("/bin/ffprobe") else str(bindir))
    return tree


def _mkv(path, size=2_000_000, hole=None):
    data = bytearray(os.urandom(size))
    data[:4] = MKV_MAGIC
    if hole:
        data[hole[0]:hole[0] + hole[1]] = bytes(hole[1])
    path.write_bytes(bytes(data))


async def test_verify_ok_file_and_container_path(vtree):
    f = vtree / "usenet/complete/Job.1080p/good.mkv"
    _mkv(f)
    out = await load_tools(media_files)["media_verify_file"](path="/data/usenet/complete/Job.1080p/good.mkv")
    assert out["verdict"] == "ok" and out["header_ok"] and out["format"] == "matroska" and out["zero_blocks"] == 0
    assert "unavailable" in out["ffprobe"]


async def test_verify_flags_zeroed_head_as_bad(vtree):
    f = vtree / "usenet/complete/Job.1080p/zero.mkv"
    f.write_bytes(bytes(1_000_000))
    out = await load_tools(media_files)["media_verify_file"](path="/data/usenet/complete/Job.1080p/zero.mkv")
    assert out["verdict"] == "bad" and not out["header_ok"] and "zeros" in out["problems"][0]


async def test_verify_deep_finds_a_hole_the_sample_misses(vtree):
    f = vtree / "usenet/complete/Job.1080p/holey.mkv"
    # 8 MiB with one 1 MiB hole at an aligned offset away from the 64 sample points' reach.
    _mkv(f, size=8 * 1024 * 1024, hole=(3 * 1024 * 1024, 1024 * 1024))
    t = load_tools(media_files)["media_verify_file"]
    deep = await t(path="/data/usenet/complete/Job.1080p/holey.mkv", deep=True)
    assert deep["zero_blocks"] == 4 and deep["longest_zero_run_blocks"] == 4 and deep["verdict"] == "suspect"
    assert "first at byte 3145728" in deep["problems"][0]


async def test_verify_mp4_header_and_wrong_header(vtree):
    ok = vtree / "usenet/complete/Job.1080p/a.mp4"
    ok.write_bytes(b"\x00\x00\x00\x20ftypisom" + os.urandom(5000))
    bad = vtree / "usenet/complete/Job.1080p/b.mp4"
    bad.write_bytes(os.urandom(5000))
    t = load_tools(media_files)["media_verify_file"]
    assert (await t(path="/data/usenet/complete/Job.1080p/a.mp4"))["verdict"] == "ok"
    out = await t(path="/data/usenet/complete/Job.1080p/b.mp4")
    assert out["verdict"] == "bad" and "header does not look like mp4" in out["problems"][0]


async def test_verify_folder_uses_largest_video_and_expected_size(vtree):
    _mkv(vtree / "usenet/complete/Job.1080p/movie.mkv", size=3_000_000)
    out = await load_tools(media_files)["media_verify_file"](path="/data/usenet/complete/Job.1080p", expected_gb=1.0)
    assert out["file"].endswith("Job.1080p/movie.mkv")  # the 5 KB sample is not larger, the nfo is not video
    assert out["verdict"] == "suspect" and "below the expected" in out["problems"][-1]


async def test_verify_rejects_paths_outside_the_share(vtree):
    t = load_tools(media_files)["media_verify_file"]
    with pytest.raises(ValueError):
        await t(path="/etc/passwd")
    with pytest.raises(ValueError):
        await t(path="/data/../etc/passwd")
    out = await t(path="/data/usenet/complete/missing.mkv")
    assert "does not exist" in out["error"]


async def test_media_ls_usenet_area_and_empty_exception_message(monkeypatch):
    seen = {}

    async def fake(host, cmd, **kw):
        seen["cmd"] = cmd
        raise TimeoutError()  # str() is empty: the old code returned {"error": ""}

    monkeypatch.setattr(media_files, "ssh_run", fake)
    out = await load_tools(media_files)["media_ls"](path="complete/Job", area="usenet")
    assert "/media_root/usenet/complete/Job" in seen["cmd"]
    assert "TimeoutError" in out["error"]
