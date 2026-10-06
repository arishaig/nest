#!/usr/bin/env python3
"""Convert the prepper-library sources into reflowable EPUB3 books.

Pipeline per book: docling (PDF -> Markdown + images, cached by the PDF's
sha256) -> normalize (heading hierarchy, printed-TOC removal, placeholder
alt text, old-OCR fixes) -> pandoc (EPUB3 with metadata, cover, TOC) -> QA.

Modes:
    all      (default) extract locally as needed, then package
    extract  queue worker: claim PDFs one at a time and extract them into the
             shared cache. Several workers (k8s Job on omega, --workers N, or
             other machines with the NAS mounted) can run against one --root.
    package  build EPUBs from the cache only; books not yet extracted are
             reported as pending

    uv run --extra convert convert.py --pull          # rsync sources from the NAS first
    uv run --extra convert convert.py --only zimgit-water
    uv run --extra convert convert.py extract --root /mnt/reference --workers 4
    uv run --extra convert convert.py package --pull-cache --push

Layout under --root (default data/, gitignored; on the NAS: media/reference):
    sources/<category>/<id>/...       inputs (fetch.py)
    survivor-library/<category>/...   inputs (survivor.py)
    extracted/<sha256>/               docling cache: book.md, img/, cover.jpg, info.json
    extracted/_bypath/<key>           relative path -> sha256 (saves re-hashing)
    extracted/_claims/<key>           worker claims (stale after CLAIM_TTL)
    extracted/_attempts/<key>         one line per extraction try; delete to retry a skipped PDF
    ebooks/<category>/<id>.epub       output (+ original PDF for medical)
    qa.json                           per-book quality metrics
"""

import argparse
import collections
import hashlib
import itertools
import json
import multiprocessing
import os
import random
import socket
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ROOT = DATA  # overridden by --root
CLAIM_TTL = 3 * 3600  # seconds after which another worker may take over a claim
# Categories whose books ship with the original PDF next to the EPUB: dosage
# and icon tables don't survive conversion (also read by build-devices.py)
KEEP_PDF = {"medical", "reproductive-health", "gender-lgbtq"}
MAX_ATTEMPTS = 3  # extraction tries per PDF (an OOM kill counts) before workers skip it
CHUNK_PAGES = 100  # docling holds a whole document in memory; big PDFs go in chunks
NAS = "root@192.168.1.16:/Tank/media_root/media/reference"
SSH_KEY = Path.home() / ".ssh" / "ansible-on-nest"
MIN_CHARS_PER_PAGE = 100  # below this the PDF has no usable text layer

# ---------------------------------------------------------------- extraction


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_density(pdf):
    """Average extracted characters per page over a sample of pages."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    n = len(doc)
    idx = sorted({min(n - 1, int(n * k / 6)) for k in range(1, 6)})
    chars = sum(len(doc[i].get_textpage().get_text_range().strip()) for i in idx)
    return n, chars / len(idx)


_converters = {}


def converter(ocr):
    """docling converter; OCR (tesseract) only for PDFs with no text layer."""
    if ocr not in _converters:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = ocr
        if ocr:
            opts.ocr_options = TesseractCliOcrOptions(lang=["eng"], force_full_page_ocr=True)
        opts.do_table_structure = True
        opts.generate_picture_images = True
        opts.images_scale = 1.5
        opts.accelerator_options = AcceleratorOptions(num_threads=int(os.environ.get("DOCLING_THREADS", "4")))
        _converters[ocr] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _converters[ocr]


def extract(pdf, ocr_missing, digest=None):
    """PDF -> cached Markdown dir. Returns (dir, info) or (None, info)."""
    from docling_core.types.doc import ImageRefMode

    digest = digest or resolve(pdf)
    out = ROOT / "extracted" / digest
    info_path = out / "info.json"
    if info_path.exists():
        info = json.loads(info_path.read_text())
        return (out if info["status"] == "ok" else None), info
    pages, density = text_density(pdf)
    ocr = density < MIN_CHARS_PER_PAGE
    info = {"sha256": digest, "pages": pages, "chars_per_page": round(density), "ocr": ocr}
    if ocr and not ocr_missing:
        info["status"] = "no-text"  # not cached: retried when --ocr is given
        return None, info
    out.mkdir(parents=True, exist_ok=True)
    t = time.time()
    chunks = []
    try:
        # Page ranges bound memory: a 700-page book in one go OOM-killed the
        # 3-worker omega pod at 20Gi.
        for n, start in enumerate(range(1, pages + 1, CHUNK_PAGES)):
            end = min(start + CHUNK_PAGES - 1, pages)
            doc = converter(ocr).convert(str(pdf), page_range=(start, end)).document
            part = out / f"chunk{n:03d}.md"
            # artifacts_dir is resolved relative to the markdown file's directory
            doc.save_as_markdown(part, image_mode=ImageRefMode.REFERENCED, artifacts_dir=Path("img"))
            chunks.append(part)
            del doc
    except Exception as e:  # corrupt/odd PDFs: record and move on
        info.update(status="error", error=str(e)[:300])  # not cached: retried next run
        return None, info
    (out / "book.md").write_text("\n\n".join(c.read_text() for c in chunks))
    for c in chunks:
        c.unlink()
    render_cover(pdf, out / "cover.jpg")  # here, so packaging doesn't need the PDF
    info.update(status="ok", seconds=round(time.time() - t), host=socket.gethostname())
    # info.json last: its presence marks the cache entry complete
    tmp = out / "info.json.tmp"
    tmp.write_text(json.dumps(info, indent=1))
    tmp.replace(info_path)
    return out, info


# ------------------------------------------------------------- work queue


def path_key(pdf):
    return hashlib.sha1(pdf.relative_to(ROOT).as_posix().encode()).hexdigest()


def resolve(pdf):
    """sha256 of a PDF via the shared by-path index; hash and record on a miss."""
    marker = ROOT / "extracted" / "_bypath" / path_key(pdf)
    if marker.exists():
        return marker.read_text().strip()
    digest = sha256(pdf)
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    tmp.write_text(digest)
    tmp.replace(marker)
    return digest


def claim(key):
    """Atomically claim a PDF (O_EXCL create is atomic on NFSv4)."""
    path = ROOT / "extracted" / "_claims" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{socket.gethostname()} {os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < CLAIM_TTL:
                    return False
                path.unlink()  # stale: its worker died (e.g. omega rebooted)
            except FileNotFoundError:
                pass
    return False


def release(key):
    try:
        (ROOT / "extracted" / "_claims" / key).unlink()
    except FileNotFoundError:
        pass


def attempt(key, tag):
    """Record a try; False once MAX_ATTEMPTS are used up (e.g. a PDF that OOMs the worker)."""
    path = ROOT / "extracted" / "_attempts" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    tries = len(path.read_text().splitlines()) if path.exists() else 0
    if tries >= MAX_ATTEMPTS:
        return False
    with path.open("a") as f:
        f.write(f"{tag} {time.time():.0f}\n")
    return True


def done(pdf):
    marker = ROOT / "extracted" / "_bypath" / path_key(pdf)
    return marker.exists() and (ROOT / "extracted" / marker.read_text().strip() / "info.json").exists()


def clear_own_claims():
    """Drop claims left by a previous incarnation of this host/pod (a k8s
    container restart keeps the pod name), so they needn't wait CLAIM_TTL."""
    me = socket.gethostname()
    for c in (ROOT / "extracted" / "_claims").glob("*"):
        try:
            if c.read_text().split()[0] == me:
                c.unlink()
        except (FileNotFoundError, IndexError):
            pass


def worker(n, ocr, only):
    """Extract every PDF not yet in the cache; exit when a pass finds nothing."""
    manifest = yaml.safe_load((HERE / "sources.yaml").read_text())
    tag = f"{socket.gethostname()}/{n}"
    while True:
        pdfs = list(dict.fromkeys(
            pdf for book_id, _, parts in all_books(manifest)
            if not only or any(book_id.startswith(o) for o in only) for pdf in parts))
        random.Random(f"{tag}{time.time()}").shuffle(pdfs)  # spread workers apart
        worked = 0
        for pdf in pdfs:
            if pdf.suffix != ".pdf" or done(pdf):  # EPUB sources are passed through
                continue
            key = path_key(pdf)
            if not claim(key):
                continue
            if not attempt(key, tag):
                print(f"[{tag}] skipped  {pdf.relative_to(ROOT)}: failed {MAX_ATTEMPTS} times", flush=True)
                release(key)
                continue
            try:
                out, info = extract(pdf, ocr)
                if out:
                    worked += 1
                print(f"[{tag}] {info['status']:8} {info.get('seconds', 0):5}s "
                      f"{info['pages']:4}pp {pdf.relative_to(ROOT)}", flush=True)
            except Exception as e:
                print(f"[{tag}] FAILED {pdf.relative_to(ROOT)}: {e}", flush=True)
            finally:
                release(key)
        if not worked:
            print(f"[{tag}] nothing left to claim", flush=True)
            return


# ------------------------------------------------------------- normalizing

CHAPTER = re.compile(r"^(CHAPTER|PART|APPENDIX|BOOK|SECTION)\b[\s.\-–—]*([IVXLC\d]+)?\b", re.I)
NUMBERED = re.compile(r"^\d{1,2}\s+[A-Z][a-z]")  # "1 Home cures ..." (Hesperian)
TOC = re.compile(r"^(TABLE OF CONTENTS|CONTENTS)$", re.I)
# "CHAPTER 4", "PART 2:", "chapter 12" — a label with no title words
BARE_LABEL = re.compile(r"^(CHAPTER|PART|APPENDIX|SECTION)\s*[IVXLC\d]*\s*[:.\-–—]?\s*$", re.I)
HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def heading_level(title):
    t = title.strip()
    if CHAPTER.match(t) or NUMBERED.match(t):
        return 1
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.9:
        return 2
    return 3


def normalize(md, as_part=False):
    """Rebuild the heading hierarchy docling flattens to '##'.

    as_part: this Markdown is one part of a multi-file book; its first
    heading becomes the chapter (h1) and everything else is demoted below it.
    """
    out, lines, i = [], md.splitlines(), 0
    skipping_toc = False
    first = True
    while i < len(lines):
        line = lines[i]
        m = HEADING.match(line)
        stripped = line.strip()
        # Scans: "CHAPTER I." often survives as a plain paragraph with the
        # chapter title on the next non-empty line.
        if not m and CHAPTER.match(stripped) and len(stripped) < 20:
            if as_part:
                # The part's own first heading is already the chapter title.
                i += 1
                continue
            # Scans: "CHAPTER I." often survives as a plain paragraph, with
            # the title on the next line or heading (skip bare page numbers).
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().isdigit()):
                j += 1
            nxt = lines[j].strip() if j < len(lines) else ""
            nm = HEADING.match(nxt)
            label = stripped.rstrip(" .:")
            if nm and re.search(r"[A-Za-z]{3}", nm.group(1)):
                title, i = f"{label} — {nm.group(1).strip()}", j
            elif nxt and re.search(r"[A-Za-z]{3}", nxt) and len(nxt) < 80 and not nxt.startswith(("!", "|")):
                title, i = f"{label} — {nxt.rstrip('.')}", j
            else:
                title = label
            out.append("# " + title)
            first = skipping_toc = False
            i += 1
            continue
        if m:
            title = m.group(1).strip()
            if BARE_LABEL.match(title):
                # Layouts that print "CHAPTER 4" apart from its title. In a
                # multi-part book the part already is the chapter: drop it.
                # Otherwise prefix it onto the next heading if that has words.
                if not as_part:
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    nm = HEADING.match(lines[j]) if j < len(lines) else None
                    if nm and re.search(r"[A-Za-z]{3}", nm.group(1)) and not BARE_LABEL.match(nm.group(1)):
                        lines[j] = "## " + title.rstrip(" :.-–—") + " — " + nm.group(1).strip()
                i += 1
                continue
            # "School Activities ... CHAPTER 4": drop a trailing label
            stripped_label = re.sub(r"\s+CHAPTER\s+[IVXLC\d]+\s*$", "", title, flags=re.I).strip()
            if re.search(r"[A-Za-z]{3}", stripped_label):
                title = stripped_label
            if TOC.match(title):
                skipping_toc = True
                i += 1
                continue
            if as_part:
                lv = 1 if first else min(heading_level(title) + 1, 3)
            else:
                lv = heading_level(title)
            # A printed TOC is entries/dot-leader rows; the first real heading
            # after it ends the skip (whatever its level).
            skipping_toc = False
            first = False
            out.append("#" * lv + " " + title)
        elif not skipping_toc:
            out.append(line)
        i += 1
    md = "\n".join(out)
    md = re.sub(r"^\|.*\.{8,}.*\|\s*$\n?", "", md, flags=re.M)  # dot-leader TOC rows
    # docling's placeholder alt text "Image" becomes a bogus figure caption in
    # pandoc; empty alt -> plain image. Real captions are separate text blocks.
    md = re.sub(r"!\[Image\]\(", "![](", md)
    # docling placeholders (e.g. <!-- formula-not-decoded -->) would render as
    # literal text with raw_html off; drop them (counted in QA beforehand)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    if not as_part:
        # Promote so the book's top heading level is h1 (otherwise a book
        # with only h3s gets an empty TOC at --toc-depth=2).
        levels = [len(h) for h in re.findall(r"^(#{1,6}) ", md, re.M)]
        if levels and min(levels) > 1:
            shift = min(levels) - 1
            md = re.sub(r"^(#{1,6}) ", lambda h: "#" * (len(h.group(1)) - shift) + " ", md, flags=re.M)
    return md


_spell = None
# Classic 19th-century OCR confusions: 'h' read as 'li', 'm' as 'rn'.
OCR_SUBS = [("tli", "th"), ("li", "h"), ("rn", "m"), ("ii", "n"), ("vv", "w")]
WORD = re.compile(r"\b[A-Za-z]{3,}\b")


def fix_ocr(md):
    """Dictionary-checked repair of common old-OCR errors.

    Only for scanned sources (Survivor Library, OCR'd PDFs): on born-digital
    text the substitutions do more harm than good ('Iid' -> 'nd').
    Returns (md, Counter of 'old->new' pairs)."""
    global _spell
    if _spell is None:
        from spellchecker import SpellChecker

        _spell = SpellChecker()
    fixes = collections.Counter()

    def repl(m):
        w = m.group(0)
        lw = w.lower()
        if lw in _spell:
            return w
        for bad, good in OCR_SUBS:
            if bad in lw:
                cand = lw.replace(bad, good)
                if cand in _spell:
                    new = cand.upper() if w.isupper() else cand.capitalize() if w[0].isupper() else cand
                    fixes[f"{w}->{new}"] += 1
                    return new
        return w

    # leave image paths and table separators alone
    parts = re.split(r"(\]\([^)]*\))", md)
    parts = [p if p.startswith("](") else WORD.sub(repl, p) for p in parts]
    return "".join(parts), fixes


def suspicious_ratio(md):
    """Share of words that are neither dictionary words nor plausibly names."""
    global _spell
    if _spell is None:
        fix_ocr("")
    words = [w for w in WORD.findall(re.sub(r"\]\([^)]*\)", "", md)) if not w[0].isupper()]
    if not words:
        return 1.0
    unknown = _spell.unknown([w.lower() for w in words])
    return round(sum(w.lower() in unknown for w in words) / len(words), 4)


# ---------------------------------------------------------------- packaging


def merge_pdfs(pdfs, dest):
    """Copy (or, for multi-part books, concatenate) the original PDFs."""
    if len(pdfs) == 1:
        shutil.copy2(pdfs[0], dest)
        return
    import pypdfium2 as pdfium

    merged = pdfium.PdfDocument.new()
    for pdf in pdfs:
        merged.import_pages(pdfium.PdfDocument(str(pdf)))
    merged.save(str(dest))


def render_cover(pdf, dest):
    import pypdfium2 as pdfium

    page = pdfium.PdfDocument(str(pdf))[0]
    page.render(scale=1200 / page.get_height()).to_pil().convert("L").save(dest, quality=85)


EINK_MAX_PX = 1400  # long side; both readers are ~1264x1680 grayscale panels


def eink_image(src, dest_dir):
    """Grayscale, downscaled copy of an image for e-ink; keeps whichever of
    JPEG (photos, scans) or PNG (line art) is smaller. Returns the new name."""
    import io

    from PIL import Image

    im = Image.open(src).convert("L")
    if max(im.size) > EINK_MAX_PX:
        im.thumbnail((EINK_MAX_PX, EINK_MAX_PX))
    jpg, png = io.BytesIO(), io.BytesIO()
    im.save(jpg, "JPEG", quality=75, optimize=True)
    im.save(png, "PNG", optimize=True)
    ext, data = min((".jpg", jpg), (".png", png), key=lambda c: c[1].tell())
    name = src.stem + ext
    (dest_dir / name).write_bytes(data.getvalue())
    return name


def eink_epub(src, dest):
    """Copy a ready-made EPUB with its raster images made grayscale and e-ink
    sized; names and formats are kept so the manifest stays valid."""
    import io
    import zipfile

    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(Path(item.filename).suffix.lower())
            if fmt:
                try:
                    im = Image.open(io.BytesIO(data)).convert("L")
                    if max(im.size) > EINK_MAX_PX:
                        im.thumbnail((EINK_MAX_PX, EINK_MAX_PX))
                    out = io.BytesIO()
                    im.save(out, fmt, **({"quality": 75, "optimize": True} if fmt == "JPEG" else {"optimize": True}))
                    if out.tell() < len(data):
                        data = out.getvalue()
                except OSError:
                    pass  # leave anything PIL can't read untouched
            # mimetype must stay first and uncompressed (EPUB OCF)
            ztype = zipfile.ZIP_STORED if item.filename == "mimetype" else zipfile.ZIP_DEFLATED
            zout.writestr(item, data, compress_type=ztype)
    tmp.replace(dest)


def package(md_chunks, resource_dirs, meta, cover_pdf, dest):
    """Join Markdown chunks and build an EPUB3 with pandoc."""
    import pypandoc

    dest.parent.mkdir(parents=True, exist_ok=True)
    work = DATA / "work" / dest.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    # Each chunk's images, made e-ink sized, go into one tree so relative
    # paths resolve. Both target readers are grayscale: docling's full-colour
    # PNGs are ~85% of an untouched book's size.
    body = []
    for n, (md, src) in enumerate(zip(md_chunks, resource_dirs)):
        if (src / "img").exists():
            (work / f"img{n}").mkdir()
            for img in sorted((src / "img").iterdir()):
                new = eink_image(img, work / f"img{n}")
                if new != img.name:
                    md = md.replace(f"](img/{img.name})", f"](img/{new})")
        body.append(md.replace("](img/", f"](img{n}/"))
    (work / "book.md").write_text("\n\n".join(body))
    if (resource_dirs[0] / "cover.jpg").exists():
        cover = work / eink_image(resource_dirs[0] / "cover.jpg", work)
    else:
        cover = work / "cover.jpg"
        render_cover(cover_pdf, cover)
    md_meta = {
        "title": meta["title"],
        "creator": [{"role": "author", "text": meta["author"]}] if meta.get("author") else [],
        "publisher": meta.get("publisher", ""),
        "lang": "en",
        "subject": meta["category"],
        "description": meta.get("description", ""),
        "rights": meta.get("license", ""),
        "belongs-to-collection": meta["category"],
        "collection-type": "series",
    }
    (work / "meta.yaml").write_text(yaml.safe_dump(md_meta, allow_unicode=True))
    pypandoc.convert_file(
        str(work / "book.md"), "epub3", format="markdown-raw_html-raw_tex-superscript-subscript-tex_math_dollars-tex_math_single_backslash",
        outputfile=str(dest),
        extra_args=["--toc", "--toc-depth=2", "--split-level=1", f"--resource-path={work}",
                    f"--metadata-file={work / 'meta.yaml'}",
                    # CLI, not metadata: pandoc's smart typography would turn
                    # a "--" in the path into an en-dash.
                    f"--epub-cover-image={cover}"],
    )
    shutil.rmtree(work)


# --------------------------------------------------------------------- books


def books(manifest, src_root):
    """Yield (book_id, meta, [pdf parts]) for every source in the manifest."""
    for s in manifest["sources"]:
        d = src_root / s["category"] / s["id"]
        if not d.exists():
            continue
        base = {k: s.get(k, "") for k in ("title", "author", "publisher", "license")}
        base["category"] = s["category"]
        base["priority"] = s.get("priority", 9)
        if s["type"] == "parts":
            pdfs = [d / Path(s["url_template"].format(part=p)).name for p in s["parts"]]
            yield s["id"], base, [p for p in pdfs if p.exists()]
        elif s["type"] in ("url", "legacy"):
            pdfs = sorted(d.glob("*.pdf"))
            if pdfs:
                yield s["id"], base, pdfs[:1]
        elif s["type"] == "gutenberg":
            for n, title in s["ebooks"].items():
                epub = d / f"pg{n}.epub"
                if epub.exists():
                    yield f"{s['id']}--pg{n}", dict(base, title=title), [epub]
        elif s["type"] == "zimgit":
            for m in json.loads((d / "meta.json").read_text()):
                if m["mimetype"] != "application/pdf":
                    continue
                meta = dict(base, title=m["title"] or Path(m["file"]).stem,
                            author=m["author"] if m["author"] != "Various" else "",
                            description=m["description"])
                yield f"{s['id']}--{Path(m['file']).stem}", meta, [d / m["file"]]


def save_qa(path, qa):
    """Merge this run's entries over what's on disk (another run may be
    writing too) and write atomically."""
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(qa)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=1, sort_keys=True))
    tmp.replace(path)


def survivor_title(raw):
    """'A_Treatise_On_Canning-1917' -> 'A Treatise on Canning (1917)'."""
    m = re.match(r"^(.*?)[-_](1[6-9]\d\d|20[0-2]\d)$", raw)
    name, year = (m.group(1), m.group(2)) if m else (raw, "")
    small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
    words = re.sub(r"[_\-]+", " ", name).split()
    words = [w.lower() if i and w.lower() in small else w for i, w in enumerate(words)]
    return " ".join(words) + (f" ({year})" if year else "")


def survivor_books(manifest, root):
    """Yield books for the mirrored Survivor Library categories (survivor.py)."""
    for category, cfg in manifest.get("survivor_library", {}).items():
        for site_cat in cfg["site"]:
            index = root / site_cat / "index.json"
            if not index.exists():
                continue
            for b in json.loads(index.read_text()):
                pdf = root / site_cat / Path(b["url"]).name
                if pdf.exists():
                    meta = {"title": survivor_title(b["title"]), "author": "", "category": category,
                            "publisher": "Survivor Library", "priority": cfg["priority"],
                            "license": "Public domain (historical)",
                            "description": f"survivorlibrary.com / {site_cat}", "scan": True,
                            "site_category": site_cat}
                    yield f"survivor--{pdf.stem}", meta, [pdf]


def all_books(manifest):
    return itertools.chain(books(manifest, ROOT / "sources"), survivor_books(manifest, ROOT / "survivor-library"))


def cached(pdf):
    """(dir, info) from the cache without extracting, or (None, info)."""
    digest = resolve(pdf)
    info_path = ROOT / "extracted" / digest / "info.json"
    if not info_path.exists():
        return None, {"status": "pending-extraction", "pages": 0, "chars_per_page": 0}
    info = json.loads(info_path.read_text())
    return (info_path.parent if info["status"] == "ok" else None), info


def main():
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["all", "extract", "package"], default="all")
    ap.add_argument("--root", type=Path, default=DATA, help="library root (see Layout)")
    ap.add_argument("--workers", type=int, default=1, help="extract: parallel worker processes")
    ap.add_argument("--pull-cache", action="store_true", help="package: rsync the NAS extraction cache first")
    ap.add_argument("--only", nargs="*", help="source ids (prefix match) to convert")
    ap.add_argument("--ocr", action="store_true", help="also OCR PDFs with no text layer (slow)")
    ap.add_argument("--force", action="store_true", help="rebuild EPUBs that already exist")
    ap.add_argument("--pull", action="store_true", help="rsync sources from the NAS first")
    ap.add_argument("--push", action="store_true", help="rsync ebooks to the NAS afterwards")
    args = ap.parse_args()
    ROOT = args.root

    if args.mode == "extract":
        clear_own_claims()  # before any worker of this run claims anything
        if args.workers == 1:
            worker(0, args.ocr, args.only)
            return 0
        # spawn: each worker loads its own docling models
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_worker_main, args=(str(ROOT), n, args.ocr, args.only))
                 for n in range(args.workers)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join()
        return 0

    rsync = ["rsync", "-a", "-e", f"ssh -i {SSH_KEY}"]
    if args.pull:
        subprocess.run(rsync + [f"{NAS}/sources/", f"{ROOT / 'sources'}/"], check=True)
    if args.pull_cache:
        subprocess.run(rsync + ["--exclude=_claims", f"{NAS}/extracted/", f"{ROOT / 'extracted'}/"], check=True)

    manifest = yaml.safe_load((HERE / "sources.yaml").read_text())
    qa_path = ROOT / "qa.json"
    qa = {}  # this run's entries; save_qa merges them over the file on disk
    seen = {}  # sha256 of first part -> book id, to skip duplicate PDFs across bundles

    for book_id, meta, pdfs in all_books(manifest):
        if args.only and not any(book_id.startswith(o) for o in args.only):
            continue
        if pdfs[0].suffix == ".epub":
            dest = ROOT / "ebooks" / meta["category"] / f"{book_id}.epub"
            if not dest.exists() or args.force:
                eink_epub(pdfs[0], dest)
            qa[book_id] = {"status": "ok", "title": meta["title"], "category": meta["category"],
                           "priority": meta["priority"], "site_category": None, "pages": 0,
                           "suspicious_words": 0.0, "text_retained": 1.0, "epub_bytes": dest.stat().st_size}
            continue
        key = resolve(pdfs[0])
        if seen.get(key) == book_id:
            continue  # the same Survivor file listed under two site categories
        if key in seen:
            qa[book_id] = {"status": "duplicate", "of": seen[key]}
            continue
        seen[key] = book_id
        dest = ROOT / "ebooks" / meta["category"] / f"{book_id}.epub"
        if dest.exists() and not args.force:
            continue
        print(f"{book_id}: {meta['title']} ({len(pdfs)} file(s))", flush=True)
        chunks, dirs, infos = [], [], []
        for pdf in pdfs:
            out, info = extract(pdf, args.ocr) if args.mode == "all" else cached(pdf)
            infos.append(info)
            if out:
                chunks.append(normalize((out / "book.md").read_text(), as_part=len(pdfs) > 1))
                dirs.append(out)
        pending = [i for i in infos if i["status"] == "pending-extraction"]
        if pending:  # don't package a multi-part book with parts missing
            qa[book_id] = {"status": "pending-extraction", "title": meta["title"]}
            continue
        if not chunks:
            qa[book_id] = {"status": infos[0]["status"], "title": meta["title"], **infos[0]}
            print(f"  skipped: {infos[0]['status']}", flush=True)
            continue
        formulas_lost = sum((d / "book.md").read_text().count("formula-not-decoded") for d in dirs)
        fixes = collections.Counter()
        if meta.get("scan") or any(i.get("ocr") for i in infos):
            fixed = [fix_ocr(c) for c in chunks]
            chunks = [c for c, _ in fixed]
            for _, f in fixed:
                fixes.update(f)
        try:
            package(chunks, dirs, meta, pdfs[0], dest)
        except Exception as e:  # one bad book shouldn't stop the run
            qa[book_id] = {"status": "package-error", "title": meta["title"], "error": str(e)[:300]}
            print(f"  package error: {str(e)[:200]}", flush=True)
            continue
        if meta["category"] in KEEP_PDF:
            # Icon-drawn table columns don't survive conversion; ship the
            # original alongside so dosage tables can be checked.
            merge_pdfs(pdfs, dest.with_suffix(".pdf"))
        text = "\n".join(chunks)
        pages = sum(i["pages"] for i in infos)
        qa[book_id] = {
            "status": "ok",
            "title": meta["title"],
            "category": meta["category"],
            "priority": meta["priority"],
            "site_category": meta.get("site_category"),  # triage.yaml key for survivor books
            "pages": pages,
            "missing_parts": sum(i["status"] != "ok" for i in infos),
            "chars_per_page": round(len(text) / max(pages, 1)),
            # converted text vs the PDF's own text layer; a low ratio means
            # the normalizer (or docling) dropped content
            # (meaningless for OCR'd books, whose source text layer is empty)
            "text_retained": 1.0 if any(i.get("ocr") for i in infos) else
            round(len(text) / max(1, sum(i["chars_per_page"] * i["pages"] for i in infos)), 2),
            "suspicious_words": suspicious_ratio(text),
            "ocr_fixes": sum(fixes.values()),
            "ocr_fix_pairs": dict(fixes.most_common(40)),
            "formulas_lost": formulas_lost,
            "ocr": any(i.get("ocr") for i in infos),
            "headings": len(re.findall(r"^#{1,2} ", text, re.M)),
            "images": text.count("]("),
            "epub_bytes": dest.stat().st_size,
        }
        save_qa(qa_path, qa)
        print(f"  ok: {qa[book_id]['chars_per_page']} chars/pg, {qa[book_id]['text_retained']:.0%} retained, "
              f"{qa[book_id]['suspicious_words']:.1%} suspicious, {sum(fixes.values())} OCR fixes", flush=True)

    save_qa(qa_path, qa)
    if args.push:
        subprocess.run(rsync + ["--chown=1000:1000", f"{ROOT / 'ebooks'}/", f"{NAS}/ebooks/"], check=True)
    return 0


def _worker_main(root, n, ocr, only):
    global ROOT
    ROOT = Path(root)
    worker(n, ocr, only)


if __name__ == "__main__":
    sys.exit(main())
