#!/usr/bin/env python3
"""Assemble per-device libraries from the converted EPUBs.

Each device takes whole tiers of whole topics (triage.yaml), so its contents
can be written on a label; the storage budget is a check, not a selector.
Kobo gets KEPUB, Kindle gets AZW3 (both via Calibre's ebook-convert). Medical
books carry their original PDF alongside. Hazard-flagged books get a short tag
in their title. Books whose QA flags them (too many unrecognised words, e.g.
bad OCR) are left out unless --include-flagged.

    uv run build-devices.py                        # build both device trees
    uv run build-devices.py --labels               # print label text, build nothing
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

import triage

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GB = 1000**3
CORE = ["medical", "water", "food", "shelter-survival", "preparedness", "agriculture", "tools-building"]
DEVICES = {
    # budget = usable space we allow ourselves, leaving headroom for the OS
    # max_tier/topics: what goes on it (triage.yaml tiers, sources.yaml categories)
    "kobo": {"format": "kepub", "ext": ".kepub.epub", "budget": 13 * GB, "subdir": "prepper",
             "max_tier": 2, "topics": CORE},
    "kindle": {"format": "azw3", "ext": ".azw3", "budget": 28 * GB, "subdir": "documents/prepper",
               "max_tier": 3, "topics": CORE},
}
TOPICS = {  # label wording per sources.yaml category
    "medical": "Medicine & dental", "water": "Water & sanitation", "food": "Food & preserving",
    "shelter-survival": "Survival, shelter & navigation", "preparedness": "Fallout & preparedness",
    "agriculture": "Gardening, livestock & vet", "tools-building": "Trades & building", "military": "Military",
}
HAZARD_TAGS = {"old-medicine": "[old medicine]", "old-food-safety": "[old canning]", "id-caution": "[verify ID]"}
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


def convert(epub, dest, fmt, title=None):
    """ebook-convert, cached: skipped when dest is newer than the EPUB."""
    if dest.exists() and dest.stat().st_mtime >= epub.stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp" + (".kepub" if fmt == "kepub" else ".azw3"))
    cmd = ["ebook-convert", str(epub), str(tmp)] + (["--title", title] if title else [])
    r = subprocess.run(cmd, capture_output=True, text=True, env=system_env())
    if r.returncode != 0:
        raise RuntimeError(f"ebook-convert failed for {epub.name}: {(r.stdout + r.stderr)[-800:]}")
    tmp.rename(dest)


def label(name, dev):
    topics = dev["topics"]
    names = {1: "Survive", 2: "Sustain", 3: "Rebuild", 4: "Archive"}
    span = " + ".join(names[t] for t in range(1, dev["max_tier"] + 1))
    lines = [f"{name.upper()}: PREPPER LIBRARY", f"Tiers 1-{dev['max_tier']}: {span}"]
    lines += [f"  - {TOPICS.get(c, c)}" for c in topics]
    lines.append("Tagged " + " ".join(HAZARD_TAGS.values()) + " = historical, double-check")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=DEVICES, nargs="*", default=list(DEVICES))
    ap.add_argument("--include-flagged", action="store_true")
    ap.add_argument("--labels", action="store_true", help="print each device's label text and exit")
    ap.add_argument("--copy-to", type=Path, help="device mount point; syncs the prepper folder onto it")
    args = ap.parse_args()

    categories = yaml.safe_load((HERE / "sources.yaml").read_text())["categories"]
    tiers = triage.load()
    if args.labels:
        print("\n\n".join(label(n, DEVICES[n]) for n in args.device))
        return 0
    qa = json.loads((DATA / "qa.json").read_text())
    books = [(bid, q) for bid, q in qa.items() if q.get("status") == "ok"]
    flagged = [bid for bid, q in books
               if q["suspicious_words"] > MAX_SUSPICIOUS or q.get("text_retained", 1) < MIN_RETAINED]
    if not args.include_flagged:
        books = [(bid, q) for bid, q in books if bid not in flagged]
    for bid, q in books:
        q["tier"], q["hazards"] = triage.classify(tiers, bid, q["priority"], q.get("site_category"))
    books.sort(key=lambda b: (b[1]["tier"], categories.index(b[1]["category"]), b[1]["epub_bytes"]))
    # near-duplicate Survivor files ('the book 1886' vs 'the_book_1886', or a copy of a
    # curated book): keyed by stem like triage.py, curated sources seen first
    seen = set()
    for bid, q in sorted(books, key=lambda b: b[0].startswith("survivor--")):
        key = triage.norm(triage.split_id(bid, q.get("site_category"))[2])
        q["dup"] = bid.startswith("survivor--") and key in seen
        seen.add(key)

    over = False
    for name in args.device:
        dev = DEVICES[name]
        topics = dev["topics"]
        chosen = [(bid, q) for bid, q in books
                  if q["tier"] <= dev["max_tier"] and q["category"] in topics and not q["dup"]]
        root = DATA / "devices" / name / "prepper"
        used, kept = 0, set()
        for bid, q in chosen:
            epub = DATA / "ebooks" / q["category"] / f"{bid}.epub"
            title = " ".join([q["title"]] + [HAZARD_TAGS[h] for h in q["hazards"]])
            dest = root / q["category"] / (safe_name(title) + dev["ext"])
            if dest in kept:  # two books with the same title: disambiguate
                dest = dest.with_name(safe_name(f"{title} ({bid})") + dev["ext"])
            original = epub.with_suffix(".pdf")
            extras = [original] if q["category"] == "medical" and original.exists() else []
            convert(epub, dest, dev["format"], title if q["hazards"] else None)
            kept.add(dest)
            for pdf in extras:
                target = dest.parent / (safe_name(q["title"]) + " (original PDF).pdf")
                if not target.exists():
                    shutil.copy2(pdf, target)
                kept.add(target)
            used += dest.stat().st_size + sum(p.stat().st_size for p in extras)
        # drop anything no longer selected (e.g. a book that was re-tiered)
        for stale in [p for p in root.rglob("*") if p.is_file() and p not in kept]:
            stale.unlink()
        print(f"{name}: tiers 1-{dev['max_tier']}, {len(chosen)} books, "
              f"{used / GB:.2f} GB of {dev['budget'] / GB:.0f} GB budget")
        if used > dev["budget"]:
            # never silently drop part of a topic: the label would lie
            print(f"  OVER BUDGET: lower max_tier or drop a topic for {name}", file=sys.stderr)
            over = True
            continue
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
    return 1 if over else 0

if __name__ == "__main__":
    sys.exit(main())
