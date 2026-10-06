#!/usr/bin/env python3
"""Tier and hazard lookup for the prepper library (curation lives in triage.yaml).

As a script: check triage.yaml against the books that actually exist and
report what each tier holds, with estimated device sizes.

    uv run triage.py --root /mnt/fileserver/media/reference
    uv run triage.py --root ... --list data/triage-list.tsv   # every book, for review

Sizes are estimates: the extraction cache with images recompressed for e-ink
(IMAGE_RATIO), or the PDF size scaled by the category's ratio where a book
isn't extracted yet.
"""

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GB = 1000**3
IMAGE_RATIO = 0.15  # grayscale JPEG q75 <=1400px vs docling's PNGs, measured on 532 images


def load():
    return yaml.safe_load((HERE / "triage.yaml").read_text())


def norm(stem):
    """Key for near-duplicate files ('the book 1886' vs 'the_book_1886')."""
    return re.sub(r"[^a-z0-9]", "", stem.lower().removesuffix(".pdf"))


def split_id(book_id, site_category=None):
    """book id -> (section, group, stem): ('survivor', 'canning', 'x') or ('sources', 'zimgit-water', 'y')."""
    if book_id.startswith("survivor--"):
        return "survivor", site_category, book_id[len("survivor--"):]
    group, _, stem = book_id.partition("--")
    return "sources", group, stem


def classify(triage, book_id, priority, site_category=None):
    """(tier, [hazards]) for one book."""
    section, group, stem = split_id(book_id, site_category)
    cfg = triage.get(section, {}).get(group) or {}
    tier = min(priority, 4)
    if "tier" in cfg:
        tier = cfg["tier"]
    for t, stems in (cfg.get("books") or {}).items():
        if stem in stems:
            tier = int(t)
    hazards = []
    if cfg.get("hazard") and stem not in (cfg.get("safe") or []):
        hazards.append(cfg["hazard"])
    for h, stems in (cfg.get("flag") or {}).items():
        if stem in stems and h not in hazards:
            hazards.append(h)
    return tier, hazards


def check(triage, books):
    """Stems named in triage.yaml that match no book (typos, renamed files)."""
    present = collections.defaultdict(set)
    for book_id, meta, _ in books:
        section, group, stem = split_id(book_id, meta.get("site_category"))
        present[(section, group)].add(stem)
    problems = []
    for section in ("sources", "survivor"):
        for group, cfg in (triage.get(section) or {}).items():
            have = present.get((section, group))
            if have is None:
                problems.append(f"{section}/{group}: not found")
                continue
            named = [s for stems in (cfg.get("books") or {}).values() for s in stems]
            named += cfg.get("safe") or []
            named += [s for stems in (cfg.get("flag") or {}).values() for s in stems]
            problems += [f"{section}/{group}: no book '{s}'" for s in named if s not in have]
    return problems


def dir_size(d, image_ratio=IMAGE_RATIO):
    total = 0
    for r, _, files in os.walk(d):
        scale = image_ratio if os.path.basename(r) == "img" else 1
        total += sum(os.path.getsize(os.path.join(r, f)) for f in files) * scale
    return total


def main():
    import convert  # heavy imports in convert are lazy

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=convert.DATA, help="library root with sources/, survivor-library/, extracted/")
    ap.add_argument("--list", type=Path, help="write every book with its tier/hazard/size as TSV")
    args = ap.parse_args()
    convert.ROOT = args.root

    triage = load()
    manifest = yaml.safe_load((HERE / "sources.yaml").read_text())
    books = list(convert.all_books(manifest))
    problems = check(triage, books)
    for p in problems:
        print(f"triage.yaml: {p}", file=sys.stderr)

    # pass 1: measured sizes, and per-category ratios to estimate the rest
    rows, seen, measured = [], {}, collections.defaultdict(lambda: [0, 0])
    for book_id, meta, pdfs in books:
        site = meta.get("site_category")
        tier, hazards = classify(triage, book_id, meta["priority"], site)
        key = norm(split_id(book_id, site)[2])
        dup = seen.get(key) if book_id.startswith("survivor--") else None
        seen.setdefault(key, book_id)
        pdf_bytes = sum(p.stat().st_size for p in pdfs)
        size, extracted = 0, True
        for pdf in pdfs:
            if pdf.suffix == ".epub":  # passed through as-is
                size += pdf.stat().st_size
                continue
            marker = args.root / "extracted" / "_bypath" / convert.path_key(pdf)
            d = args.root / "extracted" / marker.read_text().strip() if marker.exists() else None
            if d and (d / "info.json").exists():
                size += dir_size(d)
            else:
                extracted = False
        group = site or split_id(book_id)[1]
        if extracted:
            measured[group][0] += size
            measured[group][1] += pdf_bytes
        rows.append(dict(id=book_id, category=meta["category"], group=group, tier=4 if dup else tier,
                         hazards=hazards, dup=dup, pdf=pdf_bytes, size=size if extracted else None,
                         medical_pdf=pdf_bytes if meta["category"] == "medical" else 0, title=meta["title"]))
    for r in rows:
        if r["size"] is None:
            done, pdf = measured[r["group"]]
            r["size"] = r["pdf"] * (done / pdf if pdf else 0.25)

    table = collections.defaultdict(lambda: [0, 0.0])
    for r in rows:
        t = table[(r["category"], r["tier"])]
        t[0] += 1
        t[1] += r["size"] + r["medical_pdf"]
    cats = manifest["categories"]
    print(f"{'category':18}" + "".join(f"{'tier ' + str(t):>16}" for t in (1, 2, 3, 4)))
    for c in cats:
        cells = [table[(c, t)] for t in (1, 2, 3, 4)]
        print(f"{c:18}" + "".join(f"{n:5} {gb / GB:6.2f} GB  " for n, gb in cells))
    cum = 0
    for t in (1, 2, 3, 4):
        n = sum(table[(c, t)][0] for c in cats)
        gb = sum(table[(c, t)][1] for c in cats) / GB
        cum += gb
        print(f"tier {t}: {n:5} books {gb:6.2f} GB   (tiers 1-{t}: {cum:6.2f} GB)")
    hz = collections.Counter(h for r in rows if r["tier"] < 4 for h in r["hazards"])
    print("hazard-flagged (tiers 1-3): " + ", ".join(f"{h} {n}" for h, n in hz.items()))
    print(f"near-duplicates moved to tier 4: {sum(1 for r in rows if r['dup'])}")

    if args.list:
        with args.list.open("w") as f:
            f.write("tier\tcategory\tgroup\thazards\test_MB\tid\ttitle\n")
            for r in sorted(rows, key=lambda r: (r["tier"], cats.index(r["category"]), r["group"], r["id"])):
                f.write(f"{r['tier']}\t{r['category']}\t{r['group']}\t{','.join(r['hazards'])}\t"
                        f"{(r['size'] + r['medical_pdf']) / 1e6:.0f}\t{r['id']}\t{r['title']}\n")
        print(f"wrote {args.list}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
