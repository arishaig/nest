#!/usr/bin/env python3
"""Assemble per-device libraries from the converted EPUBs.

The two readers split the library by role: the Kobo is the practical
reference (medicine, water, food, farming, trades), the Kindle the learning
library (teaching children, history, science, contested topics). Each holds
every topic's tier 1, so whichever reader is grabbed covers the first days.
Each device takes whole tiers per topic (triage.yaml), so its contents can be
written on a label; the storage budget is a check, not a selector. Books go
in numbered topic folders, plus a generated "00 START HERE" book for whoever
is handed the reader. Medical/health books carry their original PDF alongside.
Hazard-flagged books get a short tag in their title (set with Calibre's
ebook-meta). Books whose QA flags them (too many unrecognised words, e.g. bad
OCR) are left out unless --include-flagged.

    uv run build-devices.py                        # build both device trees
    uv run build-devices.py --labels               # print label text, build nothing
    uv run build-devices.py --device kobo --copy-to /run/media/$USER/KOBOeReader
    uv run build-devices.py --device kindle --ssh root@192.168.15.244   # USBNetLite

Output: data/devices/<device>/prepper/<NN topic>/<title>.epub
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
from convert import KEEP_PDF

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GB = 1000**3
# working the problem (Kobo) vs teaching and keeping the record (Kindle)
PRACTICAL = ["medical", "reproductive-health", "water", "food", "foraging", "shelter-survival", "preparedness",
             "electricity-radio", "agriculture", "tools-building"]
LEARNING = ["education", "civics-history", "science", "gender-lgbtq", "books-religion"]


def depth(deep, shallow):
    """Topic -> max tier, in label order: the device's own role in full (tiers
    1-3), the other role's essentials (tier 1) after it."""
    return {**{c: 3 for c in deep}, **{c: 1 for c in shallow}}


DEVICES = {
    # budget = usable space we allow ourselves, leaving headroom for the OS
    # topics: sources.yaml category -> max triage.yaml tier, in label order
    # role/other: the start-here book's description of this reader and the other
    # format: "epub" is copied as-is (KOReader on both readers); "kepub"/"azw3"
    # go through ebook-convert, for a reader on its stock software
    # subdir: under the USB mount (--copy-to); ssh_dir: absolute, for --ssh
    # reader: which "open a book" text the start-here book gets (START_OPEN)
    # title_prefix: start titles with the topic number, for a reader that shows
    # no folders (stock Kindle): sorting by title then groups books by topic
    "kobo": {"format": "epub", "ext": ".epub", "budget": 13 * GB, "subdir": "prepper",
             "ssh_dir": "/mnt/onboard/prepper", "reader": "koreader",
             "topics": depth(PRACTICAL, LEARNING), "role": "PRACTICAL REFERENCE",
             "about": "medicine, water, food, wild plants, shelter, radio, farming and trades, in depth",
             "other": "The **Kindle** is the learning library: teaching children to read and count, "
                      "maths and science textbooks, history, civics and banned books. "
                      "It has the first-days basics of this reader too."},
    # PW5 on firmware 5.19.x: no jailbreak, so stock software and AZW3
    "kindle": {"format": "azw3", "ext": ".azw3", "budget": 28 * GB, "subdir": "documents/prepper",
               "ssh_dir": "/mnt/us/documents/prepper", "reader": "kindle", "title_prefix": True,
               "topics": depth(LEARNING, PRACTICAL), "role": "LEARNING & HISTORY",
               "about": "teaching children to read and count, maths and science textbooks, history, "
                        "civics, gender and banned books, in depth",
               "other": "The **Kobo** is the practical reference: medicine, childbirth, water, food, "
                        "farming, livestock and trades in depth. "
                        "The first-days basics of those are on this reader too."},
}
START_OPEN = {
    "koreader": """1. Open **KOReader** (on a Kobo: the KOReader entry in the menu; on a Kindle: KUAL, then KOReader).
2. Tap the **folder icon** at the top left and go to the **prepper** folder.
3. Pick a topic folder, then tap a book.

Tap the **top** of the page for menus, the **bottom** for page and font settings.""",
    "kindle": """1. Go to the **Library** (home screen, then *Library*).
2. Tap the sort menu and choose **Title**. Every book's title starts with its topic number
   from the list below, so books are grouped by topic.
3. Tap a book. Use the search box on the home screen to find a book by title.

Tap the **top** of the page for menus and **Aa** for text size.""",
}
TOPICS = {  # label wording per sources.yaml category
    "medical": "Medicine & dental", "water": "Water & sanitation", "food": "Food & preserving",
    "foraging": "Wild plants, mushrooms & shellfish (W. Washington)", "electricity-radio": "Electricity, radio & comms",
    "education": "Teaching & learning: reading, maths, science basics", "civics-history": "Civics, law & history",
    "science": "Science: chemistry, physics, evolution, climate, vaccines", "reproductive-health": "Reproductive & sexual health",
    "gender-lgbtq": "Gender & LGBTQ+", "books-religion": "Banned books, philosophy & religious texts",
    "shelter-survival": "Survival, shelter & navigation", "preparedness": "Fallout & preparedness",
    "agriculture": "Gardening, livestock & vet", "tools-building": "Trades & building", "military": "Military",
}
HAZARD_TAGS = {"old-medicine": "[old medicine]", "old-food-safety": "[old canning]", "id-caution": "[verify ID]",
               "dated-views": "[dated views]"}
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


def calibre(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, env=system_env())
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(r.stdout + r.stderr)[-800:]}")


def convert(epub, dest, fmt, title=None):
    """Copy (epub) or ebook-convert into dest, setting a tagged title if given;
    cached: skipped when dest is newer than the EPUB."""
    if dest.exists() and dest.stat().st_mtime >= epub.stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp" + {"kepub": ".kepub", "azw3": ".azw3"}.get(fmt, ".epub"))
    if fmt == "epub":
        shutil.copyfile(epub, tmp)
        if title:
            calibre("ebook-meta", str(tmp), "--title", title)
    else:
        calibre("ebook-convert", str(epub), str(tmp), *(["--title", title] if title else []))
    tmp.rename(dest)


def sync_ssh(root, host, remote_dir):
    """A Kindle on recent firmware mounts over MTP, which rsync can't write;
    over USBNetLite (or KOReader's SSH server) it's a plain path. Use rsync
    when the device has it, else replace the folder with a busybox tar stream."""
    has_rsync = subprocess.run(["ssh", host, "command -v rsync"], capture_output=True).returncode == 0
    if has_rsync:
        subprocess.run(["rsync", "-rt", "--delete", "--modify-window=2", f"{root}/", f"{host}:{remote_dir}/"],
                       check=True)
        return
    subprocess.run(["ssh", host, f"rm -rf '{remote_dir}' && mkdir -p '{remote_dir}'"], check=True)
    tar = subprocess.Popen(["tar", "-C", str(root), "-cf", "-", "."], stdout=subprocess.PIPE)
    subprocess.run(["ssh", host, f"tar -C '{remote_dir}' -xf -"], stdin=tar.stdout, check=True)
    if tar.wait():
        raise RuntimeError("tar failed")


def shown(dev):
    """Topics actually on the device, in label order: a topic with no book at
    its tier depth (e.g. no tier-1 history yet) gets no folder, label line or number."""
    return dev.get("present", list(dev["topics"]))


def topic_num(dev, category):
    return f"{shown(dev).index(category) + 1:02d}"


def topic_dir(dev, category):
    """'03 Food & preserving': numbered like the label so folders list in label order."""
    return f"{topic_num(dev, category)} {safe_name(TOPICS.get(category, category))}"


def start_here(name, dev, dest):
    """The quick-start book: start-here.md plus this device's label."""
    if dest.exists() and dest.stat().st_mtime >= max((HERE / "start-here.md").stat().st_mtime,
                                                     Path(__file__).stat().st_mtime):
        return
    topics = "\n".join(f"- **{topic_num(dev, c)} {TOPICS.get(c, c)}**"
                       f"{'' if dev['topics'][c] > 1 else ': first-days basics'}" for c in shown(dev))
    text = (HERE / "start-here.md").read_text().format(device=name.capitalize(), topics=topics,
                                                       about=dev["about"], other=dev["other"],
                                                       open=START_OPEN[dev["reader"]])
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = dest.with_suffix(".md.txt")
    src.write_text(text)
    try:
        # paragraph-type off: otherwise Calibre joins lines and Markdown lists collapse
        calibre("ebook-convert", str(src), str(dest), "--formatting-type", "markdown",
                "--paragraph-type", "off", "--title", "00 Start here", "--authors", "Prepper library")
    finally:
        src.unlink()


def label(name, dev):
    lines = [f"{name.upper()}: PREPPER LIBRARY, {dev['role']}"]
    for c in shown(dev):
        span = "essentials only" if dev["topics"][c] == 1 else "in depth"
        lines.append(f"  {topic_num(dev, c)} {TOPICS.get(c, c)} ({span})")
    lines.append("[old medicine] [old canning] = historical, check modern guidance")
    lines.append("[verify ID] = never eat a wild plant or mushroom on one book's ID")
    lines.append("[dated views] = old history/readers, views of their time")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=DEVICES, nargs="*", default=list(DEVICES))
    ap.add_argument("--include-flagged", action="store_true")
    ap.add_argument("--labels", action="store_true", help="print each device's label text and exit")
    ap.add_argument("--copy-to", type=Path, help="device mount point; syncs the prepper folder onto it")
    ap.add_argument("--ssh", metavar="USER@HOST",
                    help="sync over ssh instead (Kindle via USBNetLite, or KOReader's SSH server)")
    args = ap.parse_args()

    categories = yaml.safe_load((HERE / "sources.yaml").read_text())["categories"]
    tiers = triage.load()
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

    selection = {}
    for name in args.device:
        dev = DEVICES[name]
        selection[name] = [(bid, q) for bid, q in books
                           if q["tier"] <= dev["topics"].get(q["category"], 0) and not q["dup"]]
        dev["present"] = [c for c in dev["topics"] if any(q["category"] == c for _, q in selection[name])]
    if args.labels:
        print("\n\n".join(label(n, DEVICES[n]) for n in args.device))
        return 0

    over = False
    for name in args.device:
        dev = DEVICES[name]
        chosen = selection[name]
        root = DATA / "devices" / name / "prepper"
        used, kept = 0, set()
        guide = root / ("00 START HERE" + dev["ext"])
        start_here(name, dev, guide)
        kept.add(guide)
        for bid, q in chosen:
            epub = DATA / "ebooks" / q["category"] / f"{bid}.epub"
            title = " ".join([q["title"]] + [HAZARD_TAGS[h] for h in q["hazards"]])
            if dev.get("title_prefix"):
                title = f"{topic_num(dev, q['category'])} {title}"
            dest = root / topic_dir(dev, q["category"]) / (safe_name(title) + dev["ext"])
            if dest in kept:  # two books with the same title: disambiguate
                dest = dest.with_name(safe_name(f"{title} ({bid})") + dev["ext"])
            original = epub.with_suffix(".pdf")
            extras = [original] if q["category"] in KEEP_PDF and original.exists() else []
            convert(epub, dest, dev["format"], title if q["hazards"] or dev.get("title_prefix") else None)
            kept.add(dest)
            for pdf in extras:
                prefix = f"{topic_num(dev, q['category'])} " if dev.get("title_prefix") else ""
                target = dest.parent / (safe_name(prefix + q["title"]) + " (original PDF).pdf")
                if not target.exists():
                    shutil.copy2(pdf, target)
                kept.add(target)
            used += dest.stat().st_size + sum(p.stat().st_size for p in extras)
        # drop anything no longer selected (e.g. a book that was re-tiered)
        for stale in [p for p in root.rglob("*") if p.is_file() and p not in kept]:
            stale.unlink()
        for d in sorted((d for d in root.rglob("*") if d.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()  # e.g. old per-category folders from before the numbered names
        print(f"{name}: {dev['role'].lower()}, {len(chosen)} books, "
              f"{used / GB:.2f} GB of {dev['budget'] / GB:.0f} GB budget")
        if used > dev["budget"]:
            # never silently drop part of a topic: the label would lie
            print(f"  OVER BUDGET: lower a topic's tier or drop a topic for {name}", file=sys.stderr)
            over = True
            continue
        if args.copy_to:
            target = args.copy_to / dev["subdir"]
            target.mkdir(parents=True, exist_ok=True)
            # --delete is scoped to our own prepper folder on the device
            subprocess.run(["rsync", "-rt", "--delete", "--modify-window=2", f"{root}/", f"{target}/"], check=True)
            print(f"  synced to {target}")
        if args.ssh:
            sync_ssh(root, args.ssh, dev["ssh_dir"])
            print(f"  synced to {args.ssh}:{dev['ssh_dir']}")

    if flagged:
        print(f"flagged (excluded unless --include-flagged): {len(flagged)}")
        for bid in flagged:
            print(f"  {qa[bid]['suspicious_words']:.1%} suspicious, "
                  f"{qa[bid].get('text_retained', 1):.0%} retained  {bid}")
    return 1 if over else 0

if __name__ == "__main__":
    sys.exit(main())
