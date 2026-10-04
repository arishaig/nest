#!/usr/bin/env python3
"""Assemble per-device libraries from the converted EPUBs.

Books are taken in priority order (sources.yaml), then category order, until
the device's storage budget is used up. Kobo gets KEPUB, Kindle gets AZW3
(both via Calibre's ebook-convert). Medical books carry their original PDF
alongside. Books whose QA flags them (too many unrecognised words, e.g. bad
OCR) are left out unless --include-flagged.

    uv run build-devices.py                        # build both device trees
    uv run build-devices.py --device kobo --copy-to /run/media/$USER/KOBOeReader

Output: data/devices/<device>/prepper/<category>/<title>.<ext>
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GB = 1000**3
DEVICES = {
    # budget = usable space we allow ourselves, leaving headroom for the OS
    "kobo": {"format": "kepub", "ext": ".kepub.epub", "budget": 13 * GB, "subdir": "prepper"},
    "kindle": {"format": "azw3", "ext": ".azw3", "budget": 28 * GB, "subdir": "documents/prepper"},
}
MAX_SUSPICIOUS = 0.08  # share of unknown lowercase words above which a book is flagged
MIN_RETAINED = 0.5  # converted text / PDF text layer below which content was likely lost


def safe_name(title):
    return re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:120] or "untitled"


def system_env():
    """Environment without this venv: Calibre's launcher runs `python3` from
    PATH and needs the system interpreter, not the venv's."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not p.startswith(venv))
    return env


def convert(epub, dest, fmt):
    """ebook-convert, cached: skipped when dest is newer than the EPUB."""
    if dest.exists() and dest.stat().st_mtime >= epub.stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp" + (".kepub" if fmt == "kepub" else ".azw3"))
    r = subprocess.run(["ebook-convert", str(epub), str(tmp)], capture_output=True, text=True, env=system_env())
    if r.returncode != 0:
        raise RuntimeError(f"ebook-convert failed for {epub.name}: {(r.stdout + r.stderr)[-800:]}")
    tmp.rename(dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=DEVICES, nargs="*", default=list(DEVICES))
    ap.add_argument("--include-flagged", action="store_true")
    ap.add_argument("--copy-to", type=Path, help="device mount point; syncs the prepper folder onto it")
    args = ap.parse_args()

    categories = yaml.safe_load((HERE / "sources.yaml").read_text())["categories"]
    qa = json.loads((DATA / "qa.json").read_text())
    books = [(bid, q) for bid, q in qa.items() if q.get("status") == "ok"]
    flagged = [bid for bid, q in books
               if q["suspicious_words"] > MAX_SUSPICIOUS or q.get("text_retained", 1) < MIN_RETAINED]
    if not args.include_flagged:
        books = [(bid, q) for bid, q in books if bid not in flagged]
    books.sort(key=lambda b: (b[1]["priority"], categories.index(b[1]["category"]), b[1]["epub_bytes"]))

    for name in args.device:
        dev = DEVICES[name]
        root = DATA / "devices" / name / "prepper"
        used, kept, cut = 0, set(), []
        for bid, q in books:
            epub = DATA / "ebooks" / q["category"] / f"{bid}.epub"
            dest = root / q["category"] / (safe_name(q["title"]) + dev["ext"])
            if dest in kept:  # two books with the same title: disambiguate
                dest = dest.with_name(safe_name(f"{q['title']} ({bid})") + dev["ext"])
            original = epub.with_suffix(".pdf")
            extras = [original] if q["category"] == "medical" and original.exists() else []
            size = q["epub_bytes"] + sum(p.stat().st_size for p in extras)
            if used + size > dev["budget"]:
                cut.append(bid)
                continue
            convert(epub, dest, dev["format"])
            kept.add(dest)
            for pdf in extras:
                target = dest.parent / (safe_name(q["title"]) + " (original PDF).pdf")
                if not target.exists():
                    shutil.copy2(pdf, target)
                kept.add(target)
            used += dest.stat().st_size + sum(p.stat().st_size for p in extras)
        # drop anything no longer selected (e.g. a book that was re-flagged)
        for stale in [p for p in root.rglob("*") if p.is_file() and p not in kept]:
            stale.unlink()
        print(f"{name}: {len([k for k in kept if k.suffix != '.pdf'])} books, "
              f"{used / GB:.2f} GB of {dev['budget'] / GB:.0f} GB budget"
              + (f", {len(cut)} cut for space" if cut else ""))
        if args.copy_to:
            target = args.copy_to / dev["subdir"]
            target.mkdir(parents=True, exist_ok=True)
            # --delete is scoped to our own prepper folder on the device
            subprocess.run(["rsync", "-rt", "--delete", "--modify-window=2", f"{root}/", f"{target}/"], check=True)
            print(f"  synced to {target}")

    if flagged:
        print(f"flagged (excluded unless --include-flagged): {len(flagged)}")
        for bid in flagged:
            print(f"  {qa[bid]['suspicious_words']:.1%} suspicious, "
                  f"{qa[bid].get('text_retained', 1):.0%} retained  {bid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
