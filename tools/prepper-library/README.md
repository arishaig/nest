# prepper-library

Offline prepper/emergency reference library, built from existing free and public-domain collections. The goal is reflowable EPUBs for a Kobo (16 GB, kepub) and a Kindle (32 GB, AZW3), plus the raw Kiwix ZIMs served on the LAN at `https://kiwix.arishaig.site`.

**This repo is public.** Only tooling and manifests live here. Content (PDFs, EPUBs, ZIMs) lives only on the NAS under `/Tank/media_root/media/reference/`:

| Path | What |
|---|---|
| `zim/` | Kiwix ZIMs plus `library.xml`. Kept current by the `kiwix-sync` CronJob (`k8s/apps/media/kiwix.yaml`). Created by hand, owned by 1000:1000; kiwix-serve won't start without a `library.xml` (an empty `<library version="20110515"></library>` is enough). Seeded 2026-10-04 with the five zimgit ZIMs. |
| `sources/<category>/<id>/` | Source documents fetched by `fetch.py` and copied to the NAS with `--push`. |
| `to_import/` | The earlier ad-hoc attempt. Its PDFs were copied into `sources/` as `legacy-*` entries. |

## Fetching sources

```sh
uv run fetch.py                 # everything in sources.yaml -> ./data/sources (gitignored)
uv run fetch.py --only zimgit-water fema-are-you-ready
uv run fetch.py --push          # then rsync to the NAS (uid/gid 1000)
```

- Idempotent: files already present are skipped.
- Each source is recorded in `sources.lock.json` with a sha256 for every file. If an upstream file changes, the run prints a warning and the lockfile diff shows it.
- `zimgit` sources: the latest ZIM is downloaded, then every embedded PDF is unpacked, named by its real title. The titles and authors come from the ZIM's own `database.js`, and the metadata goes to `meta.json`.
- `legacy` sources already exist on the NAS and are never fetched.

Adding a source: add an entry to `sources.yaml`. If a born-digital EPUB or HTML edition exists, use it instead of a PDF. Keep to free or public-domain material.

## Survivor Library

```sh
uv run survivor.py --push              # categories mapped in sources.yaml -> NAS reference/survivor-library/
uv run survivor.py --all --push        # every category (~200 GB)
```

The mirror goes one file at a time with a 2 s delay. It can be resumed, and downloads are atomic. Each category's book list is saved as `index.json`. `sources.yaml` → `survivor_library` maps site categories onto our categories and priorities. These are historical books: for example, 1890s canning advice predates modern food-safety guidance.

## Converting to EPUB

```sh
uv sync --extra convert
uv run --extra convert convert.py --pull        # pull sources from the NAS, convert everything
uv run --extra convert convert.py --only fema   # source-id prefix
uv run --extra convert convert.py --ocr         # also OCR text-less PDFs (tesseract, slow)
uv run --extra convert convert.py --push        # push data/ebooks to NAS reference/ebooks/
```

How each book is built:

1. **Extract.** docling converts the PDF to Markdown plus images. The result is cached under `data/extracted/<sha256>/`, so a rerun or a tweak to the post-pass costs seconds.
2. **Normalize.** docling emits every heading as `##`, so the post-pass rebuilds a hierarchy:
   - `CHAPTER`/`PART`/numbered titles become h1, ALL-CAPS becomes h2, everything else h3.
   - Levels are shifted so each book's top heading level becomes h1.
   - Bare "CHAPTER 4" labels are merged into the next title, or dropped in multi-part books, where each part becomes one chapter.
   - The printed table of contents is removed. Removal stops at the next heading of any level.
   - docling's placeholder alt text "Image" is emptied, so uncaptioned images get no caption.
   - Common old-OCR errors are fixed (`tlie`→`the`, `witli`→`with`), but only when the fix produces a dictionary word.
3. **Package.** pandoc builds an EPUB3 with title, author, publisher and rights, the category as the series, a cover rendered from page 1, and a two-level table of contents. Duplicate PDFs across bundles are skipped by hash.
4. **Medical books** ship with the original PDF alongside, concatenated into one file for multi-part books. Table columns drawn as icons don't survive conversion.
5. **QA.** Each book's metrics go into `data/qa.json`:
   - `text_retained`: converted text ÷ the PDF's own text layer
   - `suspicious_words`: share of lowercase words that aren't in the dictionary
   - `chars_per_page`, `headings`, `images`, `ocr_fixes`

PDFs with no text layer (fewer than 100 characters per page) are skipped unless `--ocr` is given.

Speed is about 1 s/page on CPU. Survivor Library scans almost all have a text layer (16/16 sampled), so no GPU or OCR is needed for them.

## Triage

`triage.yaml` gives every book a tier: 1 Survive, 2 Sustain, 3 Rebuild, 4 Archive (NAS/Kiwix only). Each source or Survivor category has a default tier, and individual books are listed where they differ. Books are judged by subject, not age. An 1880s camp-sanitation manual stays; municipal sewer tables, periodical runs, memoirs and scout novels go to tier 4. Files that differ only in spaces versus underscores are near-duplicates and also drop to tier 4.

Three hazard flags mark books worth keeping but not to follow blindly: `old-medicine`, `old-food-safety` (pre-USDA canning) and `id-caution` (wild plant and mushroom identification). Flagged books carry a tag in their title on the device.

```sh
uv run triage.py --root /mnt/fileserver/media/reference                       # check names, size per tier
uv run triage.py --root /mnt/fileserver/media/reference --list data/triage-list.tsv
```

## Building device libraries

```sh
uv run build-devices.py --labels                          # what each device holds, for its label
uv run build-devices.py                                   # data/devices/{kobo,kindle}/prepper/<category>/
uv run build-devices.py --device kobo --copy-to /run/media/$USER/KOBOeReader
uv run build-devices.py --device kindle --copy-to /run/media/$USER/Kindle
```

Each device takes whole tiers of a fixed list of topics (`DEVICES` in `build-devices.py`). That way its label is accurate. Kobo gets tiers 1–2 as KEPUB and Kindle gets tiers 1–3 as AZW3, both converted with Calibre's `ebook-convert`. If a selection exceeds the budget (13 / 28 GB), the build fails for that device rather than dropping part of a topic.

A book is flagged and left out unless `--include-flagged` is given if either:
- `suspicious_words` > 8%, or
- `text_retained` < 50%.

`--copy-to` rsyncs into a `prepper/` folder on the device (on the Kindle, `documents/prepper/`), and `--delete` only applies within that folder.
