#!/usr/bin/env python3
"""Fetch the prepper-library sources listed in sources.yaml.

Downloads into a local staging tree (<dest>/<category>/<id>/...), unpacks the
PDFs out of Kiwix zimgit ZIMs (with titles/authors from the ZIM's own
database.js), records sha256s in sources.lock.json, and optionally pushes the
tree to the NAS with rsync. Idempotent: files already present are skipped.

    uv run fetch.py                     # fetch everything into ./data/sources
    uv run fetch.py --only zimgit-water # one source
    uv run fetch.py --push              # then rsync to the NAS
"""

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
KIWIX = "https://download.kiwix.org/zim"
NAS = "root@192.168.1.16:/Tank/media_root/media/reference/sources/"
SSH_KEY = Path.home() / ".ssh" / "ansible-on-nest"
UA = {"User-Agent": "prepper-library-fetch/1.0 (personal offline archive)"}
DELAY = 1.0  # seconds between requests to the same host — be polite


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest):
    """Download url to dest atomically. Returns True if a download happened."""
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"  get {url}")
    with requests.get(url, headers=UA, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    time.sleep(DELAY)
    return True


def latest_zim(zim):
    """'other/zimgit-water_en' -> URL of the newest dated build."""
    subdir, name = zim.split("/", 1)
    listing = requests.get(f"{KIWIX}/{subdir}/", headers=UA, timeout=60).text
    builds = sorted(set(re.findall(rf'href="({re.escape(name)}_\d{{4}}-\d{{2}}\.zim)"', listing)))
    if not builds:
        raise RuntimeError(f"no build found for {zim}")
    return f"{KIWIX}/{subdir}/{builds[-1]}"


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:90] or "untitled"


def unpack_zimgit(zim_path, outdir):
    """Write each PDF in a nautilus ZIM to outdir, named by its real title.

    The nautilus scraper stores per-document metadata in database.js as a
    Python-literal list of dicts: ti=title, dsc=description, aut=author,
    fp=[file paths]. Returns the metadata written to outdir/meta.json.
    """
    from libzim.reader import Archive

    archive = Archive(str(zim_path))
    raw = bytes(archive.get_entry_by_path("database.js").get_item().content).decode()
    records = ast.literal_eval(raw.split("=", 1)[1].strip().rstrip(";"))
    meta = []
    outdir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        for i, fp in enumerate(rec.get("fp", [])):
            entry = archive.get_entry_by_path(f"files/{fp}")
            item = entry.get_item()
            suffix = Path(fp).suffix.lower() or ".bin"
            name = slug(rec.get("ti", fp)) + (f"_{i + 1}" if len(rec["fp"]) > 1 else "") + suffix
            target = outdir / name
            if not target.exists():
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(bytes(item.content))
                tmp.rename(target)
            meta.append({
                "file": name,
                "title": rec.get("ti", "").strip(" -"),
                "author": rec.get("aut", ""),
                "description": rec.get("dsc", ""),
                "mimetype": item.mimetype,
            })
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def fetch(src, dest, zim_cache):
    d = dest / src["category"] / src["id"]
    kind = src["type"]
    if kind == "url":
        download(src["url"], d / Path(src["url"]).name)
    elif kind == "parts":
        for part in src["parts"]:
            url = src["url_template"].format(part=part)
            download(url, d / Path(url).name)
    elif kind == "zimgit":
        url = latest_zim(src["zim"])
        zim = zim_cache / Path(url).name
        download(url, zim)
        unpack_zimgit(zim, d)
    elif kind == "gutenberg":
        # Project Gutenberg's own EPUB3 (with images): no PDF extraction needed
        for n in src["ebooks"]:
            download(f"https://www.gutenberg.org/cache/epub/{n}/pg{n}-images-3.epub", d / f"pg{n}.epub")
            time.sleep(DELAY)
    elif kind == "legacy":
        return  # lives on the NAS already; see README
    else:
        raise ValueError(f"{src['id']}: unknown type {kind}")


def update_lock(dest, ids):
    """Record sha256 per file; report files whose hash changed upstream."""
    lock_path = HERE / "sources.lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    changed = []
    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.suffix == ".part" or path.name == "meta.json":
            continue
        rel = path.relative_to(dest).as_posix()
        if rel.split("/")[1] not in ids:
            continue
        digest = sha256(path)
        if lock.get(rel) not in (None, digest):
            changed.append(rel)
        lock[rel] = digest
    lock_path.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n")
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=HERE / "data" / "sources")
    ap.add_argument("--zim-cache", type=Path, default=HERE / "data" / "zim")
    ap.add_argument("--only", nargs="*", help="source ids to fetch (default: all)")
    ap.add_argument("--push", action="store_true", help="rsync dest to the NAS afterwards")
    args = ap.parse_args()

    manifest = yaml.safe_load((HERE / "sources.yaml").read_text())
    sources = [s for s in manifest["sources"] if not args.only or s["id"] in args.only]
    failed = []
    for src in sources:
        assert src["category"] in manifest["categories"], src["id"]
        print(f"{src['id']} ({src['type']})")
        try:
            fetch(src, args.dest, args.zim_cache)
        except Exception as e:  # keep going; report at the end
            print(f"  FAILED: {e}", file=sys.stderr)
            failed.append(src["id"])

    changed = update_lock(args.dest, {s["id"] for s in sources})
    for rel in changed:
        print(f"WARNING: upstream content changed: {rel}", file=sys.stderr)

    if args.push and not failed:
        subprocess.run(
            ["rsync", "-a", "--chown=1000:1000", "--exclude=*.part",
             "-e", f"ssh -i {SSH_KEY}", f"{args.dest}/", NAS],
            check=True,
        )
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
