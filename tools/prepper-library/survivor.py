#!/usr/bin/env python3
"""Mirror Survivor Library (survivorlibrary.com) categories.

Polite and resumable: one request at a time with a delay, files already
present (same size) are skipped, downloads are atomic. Each category's book
list (title, date added, size) is saved as index.json next to the PDFs.

    uv run survivor.py                       # the categories mapped in sources.yaml
    uv run survivor.py --categories canning sanitation
    uv run survivor.py --all                 # every category (~200 GB)
    uv run survivor.py --push                # rsync to the NAS afterwards

Output: data/survivor/<site category>/<file>.pdf
"""

import argparse
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
DEST = HERE / "data" / "survivor"
SITE = "https://www.survivorlibrary.com"
NAS = "root@192.168.1.16:/Tank/media_root/media/reference/survivor-library/"
SSH_KEY = Path.home() / ".ssh" / "ansible-on-nest"
UA = {"User-Agent": "prepper-library-mirror/1.0 (personal offline archive)"}
DELAY = 2.0  # seconds between requests

ROW = re.compile(r"<tr data-row_id.*?</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
HREF = re.compile(r'href="([^"]+\.pdf)"', re.I)


def all_categories(session):
    page = session.get(f"{SITE}/index.php/main-category-index/", timeout=60).text
    return sorted(set(re.findall(r'href="https://www\.survivorlibrary\.com/index\.php/library-([^"/]+)/?"', page)))


def category_index(session, cat):
    """[{title, added, size, url}] for one category page."""
    page = session.get(f"{SITE}/index.php/library-{cat}/", timeout=60).text
    books = []
    for row in ROW.findall(page):
        link = HREF.search(row)
        if not link:
            continue  # the category .ZIP row has no plain link
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in CELL.findall(row)]
        cells = [c for c in cells if c]
        books.append({
            "title": cells[1] if len(cells) > 1 else Path(link.group(1)).stem,
            "added": cells[0] if cells else "",
            "size": cells[2] if len(cells) > 2 else "",
            "url": html.unescape(link.group(1)),
        })
    return books


def download(session, url, dest):
    tmp = dest.with_name(dest.name + ".part")
    with session.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        expected = int(r.headers.get("Content-Length", 0))
        if dest.exists() and expected and dest.stat().st_size == expected:
            return False
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if expected and tmp.stat().st_size != expected:
        tmp.unlink()
        raise IOError(f"short read: {url}")
    tmp.rename(dest)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--categories", nargs="*", help="site category slugs (default: those in sources.yaml)")
    ap.add_argument("--all", action="store_true", help="mirror every category")
    ap.add_argument("--push", action="store_true", help="rsync to the NAS afterwards")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(UA)
    if args.all:
        cats = all_categories(session)
    elif args.categories:
        cats = args.categories
    else:
        mapping = yaml.safe_load((HERE / "sources.yaml").read_text())["survivor_library"]
        cats = [c for m in mapping.values() for c in m["site"]]

    failed = []
    for cat in cats:
        books = category_index(session, cat)
        time.sleep(DELAY)
        d = DEST / cat
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.json").write_text(json.dumps(books, indent=1) + "\n")
        got = 0
        for b in books:
            dest = d / Path(b["url"]).name
            if dest.exists():
                continue  # resumable without a HEAD per file; --verify could be added later
            try:
                got += download(session, b["url"], dest)
            except Exception as e:
                print(f"  FAILED {b['url']}: {e}", file=sys.stderr)
                failed.append(b["url"])
            time.sleep(DELAY)
        print(f"{cat}: {len(books)} books, {got} downloaded", flush=True)

    if args.push:
        subprocess.run(["rsync", "-a", "--chown=1000:1000", "--exclude=*.part",
                        "-e", f"ssh -i {SSH_KEY}", f"{DEST}/", NAS], check=True)
    if failed:
        print(f"{len(failed)} downloads failed; rerun to retry", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
