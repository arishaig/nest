# Lidarr configuration review (music stack, phase 4)

These are **proposals only**. Nothing here is applied: quality and metadata
profiles live in Lidarr's database, not in git, so you change them in the UI
(Settings → Profiles).

Snapshot read from the live Lidarr (3.1.2.4913, `plugins` branch) on
2026-09-28 via read-only GETs:
- 363 artists, 8,622 albums
- 439 albums with files, across 137 artists
- 33 artists with *Monitor New Items: all*

## Quality profiles

| Profile (id) | Used by | Allowed | Cutoff | Upgrades |
|---|---|---|---|---|
| Lossless (2) | 362 artists | High Quality Lossy group (MP3-320, MP3-VBR-V0, AAC-VBR, AAC-320, OGG Q9/Q10) + Lossless group (FLAC, FLAC 24bit, ALAC, APE, WavPack) | Lossless group | yes |
| Any (1) | 1 artist | everything except WAV | — | **no** |
| Standard (3) | nobody | Low/Mid/High lossy, no lossless | Low Quality Lossy | no |

**Target:** FLAC as the cutoff, with MP3 320 and AAC 256 allowed below it, so
manual imports are accepted but still upgradable.

**Lossless (2) is already almost exactly this.** It accepts MP3-320 and
upgrades until it reaches lossless. The one gap is **AAC-256**: it sits in the
*Mid Quality Lossy* group, which is off, so a manual AAC-256 import is
rejected.

Proposed changes to **Lossless (2)**:
1. Enable the **Mid Quality Lossy** group, but inside it untick everything
   except **AAC-256** (untick MP3-256, MP3-VBR-V2, OGG Q7, OGG Q8). This lets
   AAC-256 in without opening the door to MP3-256/V2.
   - Side effect: Lidarr can now *grab* an AAC-256 release from an indexer
     when nothing better is available, then keep upgrading it. That's the
     behaviour you want for manual imports; it just also applies to automatic
     grabs.
2. Optional: narrow *High Quality Lossy* to MP3-320 (and AAC-320 if you like),
   unticking MP3-VBR-V0, AAC-VBR and OGG Q9/Q10, if you only want the formats
   you named.
3. Leave the **cutoff at the Lossless group**. It's effectively "FLAC as
   cutoff", except ALAC/APE/WavPack also satisfy it. Setting the cutoff to
   plain FLAC would treat an ALAC file as below cutoff and keep searching.
   Related: `lidarr-triage quality-report` flags anything whose quality name
   isn't FLAC, so ALAC files show up there too. That's intended, since they're
   just as frozen once unmonitored.

Also:
- **Any (1)** has upgrades off, so its one artist never improves. Move that
  artist to Lossless unless it's deliberate.
- **Standard (3)** is unused. Delete it or leave it; it's harmless.

**Unmonitored albums never upgrade**, whatever the profile. Anything the
triage tool prunes stays at its current quality; run
`lidarr-triage quality-report` to see which kept tracks are lossy.

## Metadata profiles and Singles

| Profile (id) | Used by | Primary types | Secondary | Status |
|---|---|---|---|---|
| Standard (1) | all 363 artists | Album only | Studio only | Official |
| None (2) | nobody | nothing | nothing | nothing |

With Singles and EPs excluded, Lidarr doesn't know those releases exist, so
the only way to own a one-song artist's song is to monitor a whole album.

**Proposal: don't turn Singles on in Standard.** Create a new profile and
assign it per artist:
- **Standard + Singles**
  - Primary types: Album, EP, Single
  - Secondary: Studio
  - Status: Official
- Assign it only to artists you want a single from (Artist → Edit →
  Metadata Profile). Keep their *Monitor New Items* at **none**, then
  monitor just the single you want.

Why not flip Standard:
- **Mass monitoring risk.** 33 artists have *Monitor New Items: all*. On the
  next refresh, releases that newly become visible (every back-catalog single
  and EP) would be added. Whether Lidarr treats those as "new items" and
  monitors and searches them isn't something I've verified; test it on one
  artist before relying on either behaviour.
- **Database bloat.** Singles and EPs add many release rows across all 363
  artists (8,622 albums today).
- **Radio edits.** MusicBrainz singles are often the radio edit, or bundle
  remixes and instrumentals. A single can be a *different recording* from
  the album track you actually know. Check the tracklist and durations
  before monitoring it. The triage tool treats "Song (Radio Edit)" as a
  different title from "Song", on purpose.

## Other settings seen

- **Recycling Bin:** empty, so deletes are permanent. Set it before any
  cleanup (`docs/music-lidarr-safety.md`). **Cleanup days** is 7; raise it to
  about 30 so a mistake noticed weeks later is still recoverable.
- **Import list "Spotify Playlists":** automatic add is on. *Monitor* is
  `none`, *Monitor Existing* is **off**, and *Search* is off. It adds artists
  unmonitored and won't re-monitor pruned albums, so `lidarr-triage apply`'s
  precondition passes. If you ever turn Monitor Existing on, the tool will
  refuse to run.
- **Watch library for changes:** on. Fine; deletions made through the API
  are already reflected.

## Deferred design note: slskd + Soularr as a second acquisition tier

Not built. Recorded so the shape is known if torrents and usenet keep
missing albums.

- **What:** [slskd](https://github.com/slskd/slskd) is a headless Soulseek
  client with an API. [Soularr](https://github.com/mrusse/soularr) polls
  Lidarr's *wanted/missing* (or *cutoff unmet*) list, searches slskd with a
  filetype preference (e.g. `flac 24/192, flac 16/44.1, mp3 320`), downloads,
  and tells Lidarr to import. It's actively maintained (~1k stars); slskd is
  the de-facto Soulseek daemon. Vet both at the time.
- **Where:** alongside qBittorrent on the **seedbox LXC (104)**, behind the
  existing Gluetun container (`playbooks/provision/files/seedbox/docker-compose.yml`,
  `network_mode: "service:gluetun"`). That's the repo's one VPN pattern;
  `docs/k8s-migration.md` explains why VPN-coupled containers stayed out of
  k8s.
- **Ports:** Soulseek needs an inbound listen port for good results. Gluetun
  already does ProtonVPN port forwarding for qBittorrent (`VPN_PORT_FORWARDING=on`
  plus an up-command that pushes the port). Only one forwarded port exists,
  so slskd either shares the scheme (a second up-command that sets slskd's
  listen port via its API) or runs passive-only with worse results.
- **Paths:** Soularr needs Lidarr and slskd to see the *same* download
  directory at the same path and permissions. Use something like
  `/Tank/media_root/downloads/slskd` (the LXC mounts `/Tank/media_root` at
  `/mnt/data`; Lidarr sees `/data`), so configure Lidarr's remote path
  mapping or keep the paths aligned.
- **Credentials:** Soularr needs the Lidarr API key. That's another consumer
  of the single `lidarr_api_key`, which already goes to exportarr, lidarr-ui
  and nest-mcp. slskd's API key and Soulseek login go in vault and the
  seedbox `env.j2`.
- **Etiquette:** Soulseek expects you to share. slskd needs a shared
  directory; sharing `/Tank/media_root/media/music` read-only is the norm.
  That's an upload-bandwidth and legal-exposure decision, not just a config
  line.
- **Interaction with triage:** Soularr only fetches *monitored* wanted
  albums. Pruned albums are unmonitored, so it won't re-fetch them.

## Rollback

This commit is docs only. The profile changes above are UI edits; undo them
the same way, or restore Lidarr's native backup
(`docs/music-lidarr-safety.md`).
