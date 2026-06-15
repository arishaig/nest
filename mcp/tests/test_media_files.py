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


async def test_media_ls_bad_json(monkeypatch):
    patch_ssh(monkeypatch, media_files, "not-json")
    out = await load_tools(media_files)["media_ls"](path="tv")
    assert "error" in out
