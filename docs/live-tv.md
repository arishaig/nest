# Live TV (Dispatcharr → Jellyfin)

Free ad-supported (FAST) channels in Jellyfin Live TV, with DVR. Jellyfin's
M3U tuner doesn't buffer and reads one flat list, so
[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) sits between the
public feeds and Jellyfin: it merges and filters channels and serves one stable
M3U + XMLTV pair. Jellyfin does the recording.

Manifest: `k8s/apps/media/dispatcharr.yaml`, UI at `dispatcharr.arishaig.site`
(`local-only` + Authelia, which signs admins in through Dispatcharr's
Reverse Proxy Auth on `Remote-User`). Jellyfin reaches it in-cluster at `dispatcharr-main:9191`.

## Layout

| Piece | Where |
|---|---|
| web + celery | two containers, one pod (same image, shared `/data`; celery reaches web on localhost) |
| Postgres | shared `media/postgres`, role and database `dispatcharr` ([postgres.md](postgres.md)) |
| pluto-proxy | `pluto-proxy:8181`, playlist and normalising stream proxy for Pluto (below) |
| Redis | private Valkey `dispatcharr-redis`, no persistence (broker and stream state only) |
| Data | `dispatcharr-config` PVC (`nfs-nvme`) at `/data` |

Redis is deliberately not Authelia's Valkey: a media-app burst must not be able
to touch SSO sessions.

## First deploy: create the role and database

The Secret (`dispatcharr-postgres-secret`) is generated and applied by Flux, but
the role itself is created by hand, like the other apps. Until it exists the web
container crash-loops, which is expected. As the `postgres` superuser, with the
password from `dispatcharr_postgres_password` in `all.sops.yaml`:

```sql
CREATE ROLE dispatcharr LOGIN PASSWORD '<password>';
CREATE DATABASE dispatcharr OWNER dispatcharr;
```

## Feeds

Configured in the Dispatcharr UI (not in git). Two sources:

**Pluto TV** via `pluto-proxy` (`k8s/apps/media/pluto-proxy/pluto_proxy.py`).
Pluto's streams need a short-lived session token from its own boot API, so
nothing publishes a static playlist; the old i.mjh.nz Pluto/Samsung/Plex
playlists are gone (reportedly a takedown, 2024). The helper is a Python script
(stdlib only) in a ConfigMap, run on `ghcr.io/arishaig/pluto-proxy` (built from
`pluto-proxy/Dockerfile`: `python:3.15-alpine` plus Alpine's `ffmpeg`
package). It keeps no state:

- `/playlist.m3u`: ~430 channels; `tvg-id` is Pluto's channel id. The channel
  list and its session refresh every 6h; the last good copy is served if Pluto
  is unreachable.
- `/ch/<id>.m3u8`: a one-variant master playlist (best quality, never the
  audio-description or subtitle tracks) whose media playlist and segments are
  served by the proxy itself under `/s/<key>/`. `MAX_BANDWIDTH` (bits/s,
  default unlimited) caps the quality pick.
- **Separate audio is merged in.** About half of Pluto's channels carry the
  original audio as its own rendition. Served as two playlists, ffmpeg's HLS
  reader can starve one of them (on 2026-11-04 it read no audio for 90 s
  through an ad break, then skipped ahead: a black picture over the show's
  sound, then a freeze). So the proxy always serves one playlist and puts the
  matching audio segment (by `PROGRAM-DATE-TIME`) into each video segment; if
  Pluto's audio segment isn't there yet, that segment is silent.
- **One Pluto session per stream.** Pluto plays one stream per session: a
  second stream on the same session ends the first (its playlist turns empty
  with `EXT-X-ENDLIST`; Dispatcharr then sees "Server closed connection" and
  restarts). So every `/ch` request boots its own session with its own client
  id.
- **Every segment goes through the proxy** (decrypted, copy-remuxed: no
  re-encoding of shows). Pluto's segments don't share one stream layout:
  shows, raw ads and repackaged ads use different PIDs, some carry their own
  audio or an ID3 track, and their timestamps jump at every break (a show at
  94000 s, an ad restarting at 1 s). An ffmpeg remuxer like Dispatcharr's
  fixes its output streams at startup, so on a layout change the mapped
  streams starve and the channel stalls; with separate audio the jump is also
  absorbed differently per track. The proxy outputs one fixed layout (video
  PID 0x100, audio 0x101, nothing else) on one continuous timeline, placed
  by Pluto's `PROGRAM-DATE-TIME`. Ad slates are placed by date alone; show
  segments keep their own timestamps within each discontinuity run.
- `PLUTO_DEBUG=1` (env) logs every segment's placement (run, date, source
  start, output start).
- **Ads are replaced** (segments whose URL contains `/creative/` or the
  repackaged-ad marker `id3(p=clik`, plus Pluto's "ad break" bumper
  animations; channel idents such as "you're watching" bumpers are kept) by a clip from the breaks folder (below), or
  black and silence when there is none.
- **Ad-break clips** live in `/data/media/live-tv-breaks` on the NAS
  (mounted read-only at `/breaks`, `BREAKS_DIR`). Each break picks one clip
  at random, plays it from its start and loops it until the show returns; the
  pick holds for the whole break. Every 5 s break segment is cut from the clip
  in the show's own size, frame rate and audio format. The folder is rescanned
  every 5 minutes, so adding or removing a clip needs no deploy; files changed
  in the last 2 minutes, partial downloads and non-video files are ignored. It
  is deliberately not under `filler/` (subgen transcribes that) or the MeTube
  folder (every download would become break filler).
  Convert a new clip first so the proxy only ever decodes cheap 720p H.264
  (keep long ones to their first 10 minutes, since no break is that long):

  ```sh
  ffmpeg -t 600 -i IN -map 0:v:0 -map 0:a:0 \
    -vf "fps=30,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p" \
    -c:v libx264 -preset veryfast -crf 22 -g 30 -keyint_min 30 -sc_threshold 0 \
    -af "loudnorm=I=-24:TP=-2:LRA=11,aresample=48000" -ac 2 -c:a aac -b:a 128k \
    -movflags +faststart OUT.mp4
  ```

  (loudness normalised to -24 LUFS, so a break is never louder than the
  show). Break clips and black slates are encoded with the show's own H.264
  profile, reference frames and B-frames (`x264_args`): the player's decoder
  gets show and break as one stream, and Baseline slates inside a High show
  flashed green on Android TV at break boundaries. A clip segment taking over 12 s, or failing, falls back to black.
  The pod prefers an amd64 node (never omega): a 5 s clip segment takes about
  1.3 s on a Pi 5, two at once up to 4.4 s; break segments are built one
  ahead in the background.
- **Closed captions** (`CC_CHANNELS`: Pluto channel ids, comma-separated,
  or `*`; NBC News NOW only for now). Pluto's subtitles are a separate WebVTT
  track, which can't reach players through Dispatcharr (one MPEG-TS, video
  and audio only). For listed channels each show segment's new words are put
  back into its H.264 video as roll-up CEA-608 captions (GStreamer:
  `tttocea608` → `cccombiner` → `h264ccinserter`), without re-encoding the
  picture; players show them under their CC/subtitle setting. About 0.1–0.4 s
  of extra work per segment; ads get none; any failure sends the segment
  uncaptioned. Pluto's live cues are the broadcast's own roll-up captions
  redrawn after every word, already timed on the video's clock. Details that
  matter:
  - `caption-meta-order=display` on `h264ccinserter`: with B-frames the
    letters come out scrambled otherwise.
  - Every segment's captions come from a fresh encoder that starts on the
    bottom row without rolling it up, so the row the previous segment left
    half full is re-sent first (`bottom_row`, which mirrors tttocea608's
    31-column word wrap); otherwise the words at each boundary vanish.
  - Words due in a segment's last 0.5 s go in the next one, so they aren't cut
    off mid-send (608 carries about 2 characters a frame).
  - To check a stream by hand, `ffmpeg -real_time 1 -f lavfi -i
    "movie=X.ts[out0+subcc]" -map 0:s -f srt -`: without `-real_time` ffmpeg
    only prints a roll-up line once it scrolls, so short tests look empty.
- **Captions drawn into the picture** (`CC_BURN_CHANNELS`, same form; NBC
  News NOW only for now). Jellyfin never offers a subtitle track for live TV
  (its live-stream probe keeps one video and one audio stream and ignores
  CEA-608), so Wholphin can't switch the CEA-608 captions on. Each listed
  channel gets a twin, `<name> (CC)` at `/cc/<id>.m3u8`, whose show segments
  are re-encoded with Pluto's cues drawn in via libass (`ass_doc`: each cue
  is the whole caption display for its time; white DejaVu Sans Mono on black
  boxes, bottom left). Every show segment is re-encoded, captioned or not,
  with the show's H.264 settings and exact source timestamps
  (`-enc_time_base demux`). About 0.5 s of CPU per 5 s segment on amd64. In
  Dispatcharr the twin is a separate stream: make a channel from it and give
  it the original's EPG.
- Cost: the proxy is in the video path, about 3 Mbit/s and one ffprobe plus
  one short ffmpeg run per 5 s segment per stream. If it is down, Pluto
  channels stop.
- A script change reloads by itself (the process re-execs when its ConfigMap
  file changes). A change to `pluto-proxy/Dockerfile` is built by the
  `build-pluto-proxy` deploy job. The GHCR package must be public.
- Dispatcharr M3U account (type **Standard**): `http://pluto-proxy:8181/playlist.m3u`.
- Dispatcharr EPG (XMLTV): `https://i.mjh.nz/PlutoTV/us.xml`. Its channel ids
  match Pluto's (426 of 429 when checked), so EPG matching is by id.
- Pluto's API is unofficial and against the spirit of its terms (more so now
  that ads are blanked); it can change or block at any time.
- Tests: `python3 -m unittest discover -s k8s/apps/media/pluto-proxy/test`
  (offline, Pluto stubbed; the segment pipeline cases need ffmpeg on PATH,
  the caption pipeline case GStreamer as in the image).
- Known: a few `Non-monotonic DTS` warnings per hour in Dispatcharr's ffmpeg
  (audio, a few ms at segment edges); ffmpeg corrects them.

**Free-TV/IPTV** (`playlists/playlist_usa.m3u8` on raw.githubusercontent.com):
28 US channels (news, NASA, Newsmax, etc.). Its published guide files don't
cover them (epgshare01 renamed or dropped the US ids), so these have no guide.

Pluto has ~430 channels; in Dispatcharr only create the groups you want, or
the Jellyfin guide gets unwieldy.

## Not available
PBS: all 143 station streams in i.mjh.nz's data are Widevine DRM, which neither
Dispatcharr nor Jellyfin can play or record. Over-the-air PBS and the local
networks need an antenna and an HDHomeRun tuner, which Jellyfin supports
natively, DVR included.

## Jellyfin

Dashboard → Live TV:

1. Add an **M3U Tuner** with the Dispatcharr M3U URL.
2. Add an **XMLTV** guide provider with the Dispatcharr XMLTV URL.
3. Set the recording path to `/data/media/livetv-recordings` on the media NFS
   mount. Never the NVMe rpool (see the MediaDash recycle note: it starved etcd).
4. Channels map on `tvg-id`.

## Limits

- Pluto's ads are blanked by pluto-proxy, so recordings contain black,
  silent stretches where the breaks were.
- The guide is thin and episodes often look identical, so "record new episodes"
  series rules are unreliable. Use timed or one-off recordings.
- Dispatcharr has its own DVR; it's unused here so Jellyfin stays the single
  recorder.

## Metrics

The `dispatcharr_exporter` plugin (sethwv, installed from the Dispatcharr UI, so
it lives in `/data/plugins` on the PVC, not in git) serves Prometheus metrics on
`:9192`. The `dispatcharr-metrics` Service puts that on the shared MetalLB
metrics IP, and Prometheus scrapes it as job `dispatcharr`
(`192.168.1.116:9192`). `MediaAppExporterDown` covers the scrape path. If the
plugin is reinstalled or disabled, the target goes unscrapeable but the app is
unaffected.

## Managing and probing from nest-mcp

The `dispatcharr_*` tools (`mcp/nest_mcp/tools/dispatcharr.py`) cover this setup:

- **Management** (channels, streams, profiles, M3U/EPG refresh, a raw-API
  escape hatch) uses an `X-API-Key`. Generate it in the Dispatcharr UI (user
  menu, API keys) and add it to `inventory/group_vars/all.sops.yaml` as
  `dispatcharr_api_key`, then run `scripts/render-k8s-secrets.sh`: it generates
  `nest-mcp-secrets.sops.yaml` (as `NEST_DISPATCHARR_KEY`) from
  `playbooks/provision/k8s-secrets.yml`, so a key added only to the generated
  file is dropped on the next render. Write tools are marked `[DESTRUCTIVE]`.
- **Lineup and probing** need no key: they use the open `/output/m3u`.
  `dispatcharr_probe_channels` decodes a few frames per channel through the
  proxy and reports time to first frame, codec/resolution, blank or frozen
  pictures and error slates. It probes one channel at a time (at most 25 per
  call): concurrent probes overload the proxy and show healthy channels as
  5xx. Each channel is cut off after the capture window plus 30 s, and the call
  stops starting new channels after `budget_s` (90 s) and lists the ones it
  skipped, so it stays inside the MCP call timeout. ffmpeg is in the nest-mcp
  image for this.
- **Stored stats**: Dispatcharr records resolution/bitrate per stream only once
  it has been played, so `dispatcharr_quality_report` shows many `no_stats`
  until streams are watched or probed. Pluto's normal tier is 1216x684.
- **Error slates**: some streams show a yellow-on-black error message instead
  of video. `dispatcharr_probe_channels` flags a static yellow-on-black screen
  as a guess, and matches known slates exactly by image fingerprint
  (`KNOWN_SLATES` in the module, or the `slate_hashes` argument). Every probe
  result carries the `fingerprint` of its last frame, so a slate seen once can
  be added.
