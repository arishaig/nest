#!/usr/bin/env python3
"""Convert the prepper-library sources into reflowable EPUB3 books.

Pipeline per book: docling (PDF -> Markdown + images, cached by the PDF's
sha256) -> normalize (heading hierarchy, printed-TOC removal, placeholder
alt text, old-OCR fixes) -> pandoc (EPUB3 with metadata, cover, TOC) -> QA.

    uv run --extra convert convert.py --pull          # rsync sources from the NAS first
    uv run --extra convert convert.py --only zimgit-water
    uv run --extra convert convert.py --push          # rsync built EPUBs to the NAS

Layout (all under data/, gitignored):
    sources/<category>/<id>/...       inputs (fetch.py / NAS)
    extracted/<sha256>/book.md, img/  docling cache, reused across runs
    ebooks/<category>/<id>/*.epub     output (+ original PDF for medical)
    qa.json                           per-book quality metrics
"""

import argparse
import hashlib
import itertools
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
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
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = ocr
        if ocr:
            opts.ocr_options = TesseractCliOcrOptions(force_full_page_ocr=True)
        opts.do_table_structure = True
        opts.generate_picture_images = True
        opts.images_scale = 1.5
        _converters[ocr] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _converters[ocr]


def extract(pdf, ocr_missing):
    """PDF -> cached Markdown dir. Returns (dir, info) or (None, info)."""
    from docling_core.types.doc import ImageRefMode

    digest = sha256(pdf)
    out = DATA / "extracted" / digest
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
    try:
        doc = converter(ocr).convert(str(pdf)).document
    except Exception as e:  # corrupt/odd PDFs: record and move on
        info.update(status="error", error=str(e)[:300])
        info_path.write_text(json.dumps(info, indent=1))
        return None, info
    # artifacts_dir is resolved relative to the markdown file's directory
    doc.save_as_markdown(out / "book.md", image_mode=ImageRefMode.REFERENCED, artifacts_dir=Path("img"))
    info.update(status="ok", seconds=round(time.time() - t))
    info_path.write_text(json.dumps(info, indent=1))
    return out, info


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
    """Dictionary-checked repair of common old-OCR errors. Returns (md, fixes)."""
    global _spell
    if _spell is None:
        from spellchecker import SpellChecker

        _spell = SpellChecker()
    fixes = 0

    def repl(m):
        nonlocal fixes
        w = m.group(0)
        lw = w.lower()
        if lw in _spell:
            return w
        for bad, good in OCR_SUBS:
            if bad in lw:
                cand = lw.replace(bad, good)
                if cand in _spell:
                    fixes += 1
                    if w.isupper():
                        return cand.upper()
                    return cand.capitalize() if w[0].isupper() else cand
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
    page.render(scale=1200 / page.get_height()).to_pil().convert("RGB").save(dest, quality=85)


def package(md_chunks, resource_dirs, meta, cover_pdf, dest):
    """Join Markdown chunks and build an EPUB3 with pandoc."""
    import pypandoc

    dest.parent.mkdir(parents=True, exist_ok=True)
    work = DATA / "work" / dest.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    # Copy each chunk's images into one tree so relative paths resolve.
    body = []
    for n, (md, src) in enumerate(zip(md_chunks, resource_dirs)):
        if (src / "img").exists():
            shutil.copytree(src / "img", work / f"img{n}")
        body.append(md.replace("](img/", f"](img{n}/"))
    (work / "book.md").write_text("\n\n".join(body))
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
        str(work / "book.md"), "epub3", format="markdown-raw_html-raw_tex",
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
        elif s["type"] == "zimgit":
            for m in json.loads((d / "meta.json").read_text()):
                if m["mimetype"] != "application/pdf":
                    continue
                meta = dict(base, title=m["title"] or Path(m["file"]).stem,
                            author=m["author"] if m["author"] != "Various" else "",
                            description=m["description"])
                yield f"{s['id']}--{Path(m['file']).stem}", meta, [d / m["file"]]


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
                            "description": f"survivorlibrary.com / {site_cat}"}
                    yield f"survivor--{pdf.stem}", meta, [pdf]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="source ids (prefix match) to convert")
    ap.add_argument("--ocr", action="store_true", help="also OCR PDFs with no text layer (slow)")
    ap.add_argument("--force", action="store_true", help="rebuild EPUBs that already exist")
    ap.add_argument("--pull", action="store_true", help="rsync sources from the NAS first")
    ap.add_argument("--push", action="store_true", help="rsync ebooks to the NAS afterwards")
    args = ap.parse_args()

    rsync = ["rsync", "-a", "-e", f"ssh -i {SSH_KEY}"]
    if args.pull:
        subprocess.run(rsync + [f"{NAS}/sources/", f"{DATA / 'sources'}/"], check=True)

    manifest = yaml.safe_load((HERE / "sources.yaml").read_text())
    qa_path = DATA / "qa.json"
    qa = json.loads(qa_path.read_text()) if qa_path.exists() else {}
    seen = {}  # sha256 of first part -> book id, to skip duplicate PDFs across bundles

    every = itertools.chain(books(manifest, DATA / "sources"), survivor_books(manifest, DATA / "survivor"))
    for book_id, meta, pdfs in every:
        if args.only and not any(book_id.startswith(o) for o in args.only):
            continue
        key = sha256(pdfs[0])
        if key in seen:
            qa[book_id] = {"status": "duplicate", "of": seen[key]}
            continue
        seen[key] = book_id
        dest = DATA / "ebooks" / meta["category"] / f"{book_id}.epub"
        if dest.exists() and not args.force:
            continue
        print(f"{book_id}: {meta['title']} ({len(pdfs)} file(s))", flush=True)
        chunks, dirs, infos = [], [], []
        for pdf in pdfs:
            out, info = extract(pdf, args.ocr)
            infos.append(info)
            if out:
                chunks.append(normalize((out / "book.md").read_text(), as_part=len(pdfs) > 1))
                dirs.append(out)
        if not chunks:
            qa[book_id] = {"status": infos[0]["status"], "title": meta["title"], **infos[0]}
            print(f"  skipped: {infos[0]['status']}", flush=True)
            continue
        fixes = 0
        if any(i.get("ocr") or i["chars_per_page"] < 3000 for i in infos):
            fixed = [fix_ocr(c) for c in chunks]
            chunks = [c for c, _ in fixed]
            fixes = sum(f for _, f in fixed)
        try:
            package(chunks, dirs, meta, pdfs[0], dest)
        except Exception as e:  # one bad book shouldn't stop the run
            qa[book_id] = {"status": "package-error", "title": meta["title"], "error": str(e)[:300]}
            print(f"  package error: {str(e)[:200]}", flush=True)
            continue
        if meta["category"] == "medical":
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
            "pages": pages,
            "missing_parts": sum(i["status"] != "ok" for i in infos),
            "chars_per_page": round(len(text) / max(pages, 1)),
            # converted text vs the PDF's own text layer; a low ratio means
            # the normalizer (or docling) dropped content
            "text_retained": round(len(text) / max(1, sum(i["chars_per_page"] * i["pages"] for i in infos)), 2),
            "suspicious_words": suspicious_ratio(text),
            "ocr_fixes": fixes,
            "ocr": any(i.get("ocr") for i in infos),
            "headings": len(re.findall(r"^#{1,2} ", text, re.M)),
            "images": text.count("]("),
            "epub_bytes": dest.stat().st_size,
        }
        qa_path.write_text(json.dumps(qa, indent=1, sort_keys=True))
        print(f"  ok: {qa[book_id]['chars_per_page']} chars/pg, "
              f"{qa[book_id]['suspicious_words']:.1%} suspicious, {fixes} OCR fixes", flush=True)

    qa_path.write_text(json.dumps(qa, indent=1, sort_keys=True))
    if args.push:
        subprocess.run(rsync + ["--chown=1000:1000", f"{DATA / 'ebooks'}/", f"{NAS}/ebooks/"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
