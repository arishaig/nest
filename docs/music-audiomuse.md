# AudioMuse-AI (music stack, phase 1)

AudioMuse-AI does local sonic analysis of the music library and serves
"more like this" mixes. Its Jellyfin plugin swaps Jellyfin's Instant Mix for
AudioMuse's, so any client that asks Jellyfin for an Instant Mix gets sonic
results.

- Manifest: `k8s/apps/media/audiomuse.yaml` (bjw-s app-template),
  `k8s/apps/media/audiomuse-data-pvc.yaml`, route in `ingress-routes.yaml`,
  DNS rewrite in `terraform/adguard.tf`, secret in `playbooks/provision/k8s.yml`.
- UI: <https://audiomuse.arishaig.site> (Authelia + `local-only`).
- In-cluster API: `http://audiomuse.media.svc.cluster.local:8000`.
- Upstream: <https://github.com/NeptuneHub/AudioMuse-AI> (image
  `ghcr.io/neptunehub/audiomuse-ai:3.6.3`, multi-arch).

## Why app-template, not the official chart

The upstream chart (`NeptuneHub/AudioMuse-AI-helm` 1.1.6):
- mints a Helm-owned Postgres PVC with no `existingClaim` option, which
  `scripts/check-helm-pvc-safety.sh` rejects by design;
- defaults the image tag to `latest` (`pullPolicy: Always`);
- still deploys Redis, which AudioMuse 3.x no longer uses (no Redis in the
  3.6.3 `config.py` or compose file).

So this mirrors upstream's `deployment/docker-compose.yaml` instead: three
controllers (`flask`, `worker`, `postgres:15`), Postgres data on the raw
`audiomuse-data` PVC (`nfs-nvme`, PBS-backed via `host/k8s-configs`).

## Resources and placement

- Soft preference for alpha (`workloads=general`), like the *arrs.
- The web UI and Postgres prefer alpha. The first full analysis is a
  one-time cost; later runs only process new tracks.
- **The worker runs on omega's GPU** (since 2026-09-29). On alpha's 3
  capped vCPUs the first full scan ran about 28 s per track (~45 h for the
  library), mostly model inference. It now uses the `-nvidia` image,
  `runtimeClassName: nvidia`, a required omega affinity and one of the four
  GPU time-slices (VRAM is shared with subgen, tdarr-node and ollama, not
  partitioned). While omega is booted into Windows the worker is Pending;
  finished tracks are kept and interrupted ones are re-queued. The web UI and
  Postgres stay on alpha. To go back to CPU, revert that change.

## Before merging: add the vault vars

`deploy-k8s` reruns `playbooks/provision/k8s.yml` when it changes. If these
vars are missing it fails on an undefined variable, **and no other secret
gets pushed either**. Add them first (`ansible-vault edit
inventory/group_vars/all/vault.yml`) and commit the vault change on this
branch:

```yaml
audiomuse_postgres_password: "<random, e.g. openssl rand -base64 32>"
audiomuse_jellyfin_user_id: "<your Jellyfin user id>"
audiomuse_jellyfin_token: "<new Jellyfin API key>"
```

- **API key:** Jellyfin → Dashboard → API Keys → `+` → name it `audiomuse`.
  Use a dedicated key rather than `jellyfin_api_key` (nest-mcp's) so it can be
  revoked on its own.
- **User id:** Jellyfin → Dashboard → Users → your user; the id is the
  `userId=` value in the page URL.

**Merging is applying**: Flux deploys the workload, `deploy-k8s` pushes
`audiomuse-secret`, and `deploy-terraform` adds the DNS rewrite.

## First run

1. Open <https://audiomuse.arishaig.site>. If the setup wizard appears, confirm
   the media server is Jellyfin at `http://jellyfin.media.svc.cluster.local:8096`
   (it's pre-filled from env) and pick the music library. If the wizard offers
   to set up AudioMuse's own login, it's optional — the route is already behind
   Authelia.
2. Leave the AI provider as **NONE** for now. It's only used for naming
   clustered playlists. Pointing it at Ollama is possible later, with the omega
   caveats in `docs/music-digarr.md`.
3. Start **Analysis**. Watch the worker:
   `kubectl -n media logs deploy/audiomuse-worker -f` and nest-mcp
   `k8s_node_pod_stats` for alpha's CPU and memory.

## Jellyfin plugin (manual, stored on Jellyfin's config PVC)

1. Jellyfin → Dashboard → Plugins → Repositories → `+`:
   `https://raw.githubusercontent.com/NeptuneHub/audiomuse-ai-plugin/master/manifest.json`
2. Catalog → **AudioMuse AI** → Install, restart Jellyfin
   (`kubectl -n media rollout restart deploy/jellyfin`, or the Dashboard button).
3. Plugin settings → **AudioMuse-AI Endpoint URL:**
   `http://audiomuse.media.svc.cluster.local:8000` — the in-cluster Service,
   **not** `audiomuse.arishaig.site` (Authelia would block the plugin's calls).
4. Dashboard → Scheduled Tasks now lists the AudioMuse tasks (analysis,
   clustering, sonic fingerprint). Run them once manually.

**Version caveat.** Plugin releases 0.2.0 and later target Jellyfin 12.0.
We're on Jellyfin 10.11 (`jellyfin.pgsql:10.11.11-1`), so Jellyfin will offer
**0.1.55.0** (June 2026), the last 10.11 build. It's compiled against
Jellyfin **10.11.10**: on 10.11.8 Jellyfin disabled it at startup
(`Failed to load assembly … MediaBrowser.Controller, Version=10.11.10.0`),
which is why Jellyfin is on 10.11.11. Keep Jellyfin at ≥ 10.11.10 while
this plugin is installed. If Instant Mix ignores AudioMuse, grep Jellyfin's
startup log for `AudioMuse` first.

On 10.11, the plugin only replaces Instant Mix. It does **not** feed
`/Items/{id}/Similar` ("More Like This"). Music Assistant's radio mode uses
that endpoint, so it stays on Jellyfin's metadata-based similarity. The
plugin's similar-items provider (0.2+) needs Jellyfin 12.

## Which clients benefit

- **Jellyfin web and the official apps:** yes, via Instant Mix.
- **Finamp:** yes, it uses the server's Instant Mix.
- **Symfonium:** upstream says **yes**. The brief assumed Symfonium builds
  mixes client-side and wouldn't benefit. But Symfonium 13.3.0 added explicit
  support for the AudioMuse Jellyfin plugin ("Smart flows"; needs plugin
  ≥ 0.1.18, which 0.1.55 satisfies). Worth checking in its settings once the
  plugin is installed, rather than writing it off.
- **Music Assistant:** no, not on Jellyfin 10.11 (see the version caveat).

## Rollback

1. Jellyfin → Plugins → AudioMuse AI → Uninstall, restart Jellyfin. Instant
   Mix falls back to Jellyfin's built-in genre-based mix.
2. `git revert` the commit. Flux prunes the three Deployments, Services and
   the IngressRoute; `deploy-terraform` removes the DNS rewrite.
3. Left behind, on purpose: the analysis DB at
   `/rpool/data/k8s-configs/media/audiomuse-data` (`Retain`), and the
   `audiomuse-secret` Secret (`k8s.yml` never deletes). Remove both by hand if
   you don't want them. Re-deploying with the DB intact skips re-analysis.
