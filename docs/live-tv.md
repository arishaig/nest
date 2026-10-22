# Live TV (Dispatcharr → Jellyfin)

Free ad-supported (FAST) channels in Jellyfin Live TV, with DVR. Jellyfin's
M3U tuner doesn't buffer and reads one flat list, so
[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) sits between the
public feeds and Jellyfin: it merges and filters channels and serves one stable
M3U + XMLTV pair. Jellyfin does the recording.

Manifest: `k8s/apps/media/dispatcharr.yaml`, UI at `dispatcharr.arishaig.site`
(`local-only`). Jellyfin reaches it in-cluster at `dispatcharr-main:9191`.

## Layout

| Piece | Where |
|---|---|
| web + celery | two containers, one pod (same image, shared `/data`; celery reaches web on localhost) |
| Postgres | shared `media/postgres`, role and database `dispatcharr` ([postgres.md](postgres.md)) |
| pluto-proxy | `pluto-proxy:8181`, playlist/redirect helper for Pluto (below) |
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
playlists are gone (reportedly a takedown, 2024). The helper is a stdlib-only
Python script in a ConfigMap on `python:3.13-alpine`, no state:

- `/playlist.m3u`: ~430 channels; `tvg-id` is Pluto's channel id.
- `/ch/<id>.m3u8`: 302 to the best single quality level of Pluto's stitched
  stream, with a fresh token (tokens last 24h;
  the channel list and session refresh every 6h, and the last good copy is
  served if Pluto is unreachable). Pluto's master playlist lists five qualities
  plus audio-description and subtitle tracks; Dispatcharr's default ffmpeg
  profile maps all of them (`-map 0`), which made channels slow to probe and
  heavy to stream. `MAX_BANDWIDTH` (bits/s, default unlimited) caps the pick.
  A script change needs `kubectl -n media rollout restart deploy/pluto-proxy`
  (the ConfigMap has no name hash).
- Dispatcharr M3U account (type **Standard**): `http://pluto-proxy:8181/playlist.m3u`.
- Dispatcharr EPG (XMLTV): `https://i.mjh.nz/PlutoTV/us.xml`. Its channel ids
  match Pluto's (426 of 429 when checked), so EPG matching is by id.
- Pluto's API is unofficial and against the spirit of its terms; it can change
  or block at any time. Streams carry Pluto's ads (server-side stitched).
- Tests: `python3 -m unittest discover -s k8s/apps/media/pluto-proxy/test`
  (offline, Pluto stubbed).

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

- Pluto streams have ads stitched in, so recordings contain them.
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
  menu, API keys) and put it in `k8s/apps/nest-mcp/nest-mcp-secrets.sops.yaml`
  as `NEST_DISPATCHARR_KEY`. Write tools are marked `[DESTRUCTIVE]`.
- **Lineup and probing** need no key: they use the open `/output/m3u`.
  `dispatcharr_probe_channels` decodes a few frames per channel through the
  proxy and reports time to first frame, codec/resolution, blank or frozen
  pictures and error slates. It probes one channel at a time (at most 25 per
  call): concurrent probes overload the proxy and show healthy channels as
  5xx. ffmpeg is in the nest-mcp image for this.
- **Stored stats**: Dispatcharr records resolution/bitrate per stream only once
  it has been played, so `dispatcharr_quality_report` shows many `no_stats`
  until streams are watched or probed. Pluto's normal tier is 1216x684.
- **Error slates**: some streams show a yellow-on-black error message instead
  of video. `dispatcharr_probe_channels` flags a static yellow-on-black screen
  as a guess, and matches known slates exactly by image fingerprint
  (`KNOWN_SLATES` in the module, or the `slate_hashes` argument). Every probe
  result carries the `fingerprint` of its last frame, so a slate seen once can
  be added.
