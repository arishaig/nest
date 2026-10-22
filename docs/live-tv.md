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

Add the sources in the Dispatcharr UI (not in git):

- **i.mjh.nz**: per-service M3U and XMLTV for Pluto, Samsung TV Plus, Plex and
  Stirr, region `us`. Volunteer-run; expect occasional outages and stale guides.
  Check the site for the current URLs.
- Filter to the groups you want, map EPG, and use the generated M3U + XMLTV
  endpoints below.

## Jellyfin

Dashboard → Live TV:

1. Add an **M3U Tuner** with the Dispatcharr M3U URL.
2. Add an **XMLTV** guide provider with the Dispatcharr XMLTV URL.
3. Set the recording path to `/data/media/livetv-recordings` on the media NFS
   mount. Never the NVMe rpool (see the MediaDash recycle note: it starved etcd).
4. Channels map on `tvg-id`.

## Limits

- Ads are stitched into the streams, so recordings contain them.
- The guide is thin and episodes often look identical, so "record new episodes"
  series rules are unreliable. Use timed or one-off recordings.
- Dispatcharr has its own DVR; it's unused here so Jellyfin stays the single
  recorder.
