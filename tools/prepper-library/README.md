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

## Conversion (in progress)

Tested 2026-10-04 on FM 21-76, *Where There Is No Doctor*, and an 1893 Survivor Library scan, using docling 2.x on CPU then pandoc:

- **Speed:** about 1 s/page on CPU with OCR off.
- **Born-digital PDFs:** good text, tables and figures. docling emits every heading as `##`, so a post-pass rebuilds the hierarchy:
  - `CHAPTER`/`PART`/`APPENDIX`, or a numbered title → h1
  - ALL-CAPS → h2
  - everything else → h3
  - the printed table of contents is dropped
- **Image captions:** docling gives every picture the placeholder alt text `Image`, which pandoc turns into a figure caption. The post-pass empties that alt text so uncaptioned images get no caption; real captions come through as their own text.
- **Multi-part books:** each part becomes one chapter. This is why the 2025 Hesperian per-chapter PDFs are preferred over the older single-file edition.
- **Scans (Survivor Library):** the 1893 test scan already had a text layer of decent quality. Its old-OCR errors (`tlie`→`the`) are mostly in headings, and a dictionary-checked correction pass should fix them. A forced full-page RapidOCR run returned very little text. That run looks misconfigured rather than conclusive, so OCR is still an open question until we know how many Survivor Library PDFs lack a text layer.
- **Medical tables:** table columns drawn as icons are lost (for example, STI protection in the family-planning table). Medical EPUBs therefore always ship with the original PDF alongside, and get a manual table review before going on a device.
- **Device formats:** Calibre `ebook-convert` turns the EPUBs into AZW3 for the Kindle; kepubify produces kepub for the Kobo.

Still to come: `convert.py`, `build-devices.py`, `survivor.py`, and the omega GPU extraction Job.
