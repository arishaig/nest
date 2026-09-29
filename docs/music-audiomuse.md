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
- The worker is capped at **3 CPU / 6Gi** so the initial library analysis
  can't starve Jellyfin and the *arrs on alpha's 8 vCPU. Expect the first full
  analysis to take a long time on CPU (hours to days, depending on library
  size); it's a one-time cost, later runs only process new tracks.
- **GPU hook:** a commented block on the `worker` controller shows exactly
  what to add to move analysis to omega's 3080 (`-nvidia` image tag,
  `runtimeClassName: nvidia`, omega affinity, `nvidia.com/gpu`). One GPU
  time-slice is free since Anagnorisis was removed; VRAM is shared, not
  partitioned. Not enabled.

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
We're on Jellyfin 10.11.8 (`jellyfin.pgsql:10.11.8-1`), so Jellyfin will offer
**0.1.55.0** (June 2026), the last 10.11 build. Its compatibility with
AudioMuse 3.6.3 isn't documented upstream. If Instant Mix errors or ignores
AudioMuse, that's the first suspect: check Jellyfin's log for the plugin, and
consider pinning an older AudioMuse release until Jellyfin 12 is adopted.

## Which clients benefit

- **Jellyfin web and the official apps:** yes, via Instant Mix.
- **Finamp:** yes, it uses the server's Instant Mix.
- **Symfonium:** upstream says **yes**. The brief assumed Symfonium builds
  mixes client-side and wouldn't benefit. But Symfonium 13.3.0 added explicit
  support for the AudioMuse Jellyfin plugin ("Smart flows"; needs plugin
  ≥ 0.1.18, which 0.1.55 satisfies). Worth checking in its settings once the
  plugin is installed, rather than writing it off.

## Rollback

1. Jellyfin → Plugins → AudioMuse AI → Uninstall, restart Jellyfin. Instant
   Mix falls back to Jellyfin's built-in genre-based mix.
2. `git revert` the commit. Flux prunes the three Deployments, Services and
   the IngressRoute; `deploy-terraform` removes the DNS rewrite.
3. Left behind, on purpose: the analysis DB at
   `/rpool/data/k8s-configs/media/audiomuse-data` (`Retain`), and the
   `audiomuse-secret` Secret (`k8s.yml` never deletes). Remove both by hand if
   you don't want them. Re-deploying with the DB intact skips re-analysis.
