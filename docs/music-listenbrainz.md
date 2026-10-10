# ListenBrainz (music stack, phase 2)

ListenBrainz stores listen history. Jellyfin scrobbles to it going forward,
and the Spotify Extended Streaming History export backfills everything before
that. Digarr reads it as a taste source (`docs/music-digarr.md`).

Nothing here is deployed to the cluster. It's an account, a Jellyfin plugin
(configured in Jellyfin's UI, stored on its config PVC) and a one-off upload.

## 0. Privacy gate: read before creating the account

**ListenBrainz listens are public.** The project exists to build an open listen
dataset:
- Every listen shows on your public profile page.
- Listens are released under **CC0** in full dumps every two weeks, with daily
  incrementals. Profile details (email etc.) are excluded; the listens and your
  username are not.
- There is no private mode. Deleting listens or the account removes them from
  the site and future dumps, **not** from dumps already published.

Uploading the Spotify export therefore publishes your full Spotify history
under your ListenBrainz username. If that isn't acceptable, stop here. The
Lidarr triage tool (`tools/lidarr-triage`) reads the export locally and
doesn't need ListenBrainz at all. Digarr can run on Jellyfin as its only
listening source.

Sources: [About](https://listenbrainz.org/about/),
[Data dumps](https://listenbrainz.readthedocs.io/en/latest/users/listenbrainz-dumps.html).

## 1. Account and token

1. Create an account at <https://listenbrainz.org> (MetaBrainz login). The
   username is public and ends up in the dumps; pick accordingly.
2. Settings → **User token** → copy it.
3. Record it in vault as the canonical copy:
   `sops edit inventory/group_vars/all.sops.yaml` →
   `listenbrainz_user_token: "<token>"`. No k8s Secret consumes it: the Jellyfin
   plugin and Digarr keep their own copies in their own config stores. The vault
   entry is where you look it up or rotate it from.

## 2. Spotify backfill: native importer, no script

The brief called for a one-shot `tools/` script, **unless ListenBrainz had
gained a native importer. It has** (ListenBrainz release v-2025-08-30.0), so
there is no script.

Do this **before** turning on Jellyfin scrobbling, so the backfill is the only
source for the pre-Jellyfin years. The account is new, so nothing overlaps.

1. Request the export from Spotify: Account → Privacy → Download your data →
   tick **Extended streaming history** (not just "Account data"). It arrives as
   `my_spotify_data.zip` after a few days.
2. Keep it out of the repo: `spotify-history/` at the repo root is gitignored,
   or keep it outside the repo entirely.
3. ListenBrainz → Settings → **Import listens** → Spotify → upload the zip.
   It's processed in the background; the page shows progress.

What the importer does, from its source
([`spotify.py`](https://github.com/metabrainz/listenbrainz-server/blob/master/listenbrainz/background/listens_importer/spotify.py)):
- Reads files whose names contain `audio` or `endsong` (i.e.
  `Streaming_History_Audio_*.json`).
- **Drops** incognito-mode plays and anything that isn't a
  `spotify:track:` URI (podcasts, audiobooks, video).
- **Drops a play only if it is under 30 s *and*** was skipped, or ended for a
  skip-like reason (`fwdbtn`, `backbtn`, `endplay`, …). That's looser than the
  brief's rule (drop if skipped **or** under 30 s):
  - A track skipped after two minutes is kept as a listen.
  - A 20-second play that ended naturally is kept.

  ListenBrainz treats both as listens. The Lidarr triage tool applies the
  stricter rule itself, locally, so this only affects what ListenBrainz and
  Digarr see.
- Uses Spotify's `ts` (the time playback ended) as the listen timestamp.
- Has no date-range option and no deduplication against existing listens.
  Irrelevant for an empty account, but **don't upload the same zip twice**.

The zip itself is uploaded to MetaBrainz's servers for processing.

## 3. Jellyfin → ListenBrainz scrobbling

Plugin: [lyarenei/jellyfin-plugin-listenbrainz](https://github.com/lyarenei/jellyfin-plugin-listenbrainz)
(6.x supports Jellyfin 10.11 and 12).

1. Jellyfin → Dashboard → Plugins → Repositories → `+`:
   `https://repo.xkrivo.net/jellyfin/manifest.json`. This is the author's own
   repo host, not Jellyfin's official catalog. It's the documented install path
   for this well-established plugin, but note that it's self-hosted.
2. Catalog → **ListenBrainz** → Install → restart Jellyfin.
3. Plugin settings → **User Config** tab → pick your Jellyfin user → paste the
   token → Verify → enable listen submission. Optionally enable favorites ↔
   loved-recordings sync.
4. Play a track to completion, then check your ListenBrainz profile for it.

The token now also lives in Jellyfin's plugin XML on the `jellyfin-pgsql-config`
PVC. That PVC is covered by the PBS `host/k8s-configs` job, so treat those
backups as containing it.

## Rollback

- **Scrobbling:** uninstall the plugin (Dashboard → Plugins), restart
  Jellyfin, and optionally remove the repository entry.
- **Imported listens:** delete them on ListenBrainz (Settings → delete listens),
  or delete the account. Already-published CC0 dumps can't be recalled.
- **Token:** regenerate it in ListenBrainz settings (this invalidates the old
  one), update vault and the plugin.
- **This commit** is docs only. Revert freely.
