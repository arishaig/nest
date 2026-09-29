# Digarr trial (music stack, phase 5)

[Digarr](https://github.com/iuliandita/digarr) builds a taste profile from
your listening sources and uses an LLM pipeline to recommend new artists and
albums. It runs here as a **discovery-only trial**: no Lidarr URL or API key,
so it can recommend but can't add, monitor or download anything.

- Manifest: `k8s/apps/media/digarr.yaml` (app-template, mirrors upstream's
  `docker-compose.pglite.yml`), `digarr-data-pvc.yaml` (PGlite DB, with
  Digarr's own backups under `/app/data/backups`), route in `ingress-routes.yaml`, DNS in `terraform/adguard.tf`,
  `digarr-secret` in `playbooks/provision/k8s.yml`.
- UI: <https://digarr.arishaig.site> (Authelia, then Digarr's own login).
- Image `ghcr.io/iuliandita/digarr:1.18.0` (multi-arch), pinned to the exact
  patch.

## Vetting note

Your dependency rule asks for a flag here:
- **Single maintainer.** About 1,100 of the commits are one person's; the
  others are dependabot and CI.
- **Young.** The repo was created 2026-03, has ~300 stars and is MIT-licensed.
- **Active.** Several releases a month.
- **Big surface.** It connects to many third-party services (Spotify, Deezer,
  TIDAL, Plex, …), all of them optional.

Hence discovery-only, no Lidarr credentials, no Jellyfin write paths, and a
pinned patch version.

## Before merging: add the vault var

`deploy-k8s` reruns `k8s.yml` on change and fails on an undefined variable,
which also blocks every other secret. Add this first and commit the vault
change on this branch:

```yaml
digarr_encryption_key: "<openssl rand -base64 32>"
```

It encrypts the connector tokens Digarr stores (ListenBrainz, Jellyfin) with
AES-256-GCM. **Keep it:** lose it and those stored tokens are unreadable.

**Merging is applying**: Flux deploys the app, `deploy-k8s` pushes the secret,
and `deploy-terraform` adds the DNS rewrite.

## Model (manual pull)

Settings: `AI_PROVIDER=ollama`, `AI_MODEL=qwen3:14b`,
`AI_BASE_URL=http://ollama.media.svc.cluster.local:11434`. That's Digarr's
native Ollama provider, in-cluster.

Ollama's models were all pulled by hand. There is no GitOps for models; the
same gap exists for the three already there. Pull it from the LAN
(`ollama.arishaig.site` is `local-only`):

```bash
curl -N https://ollama.arishaig.site/api/pull -d '{"model":"qwen3:14b"}'
curl -s https://ollama.arishaig.site/api/tags | python3 -m json.tool | grep '"name"'
```

The pull goes to the `ollama-data` PVC (50Gi; about 9.3 GB needed).

Caveats:
- **VRAM.** qwen3:14b is 9.3 GB of weights plus its context cache, on a
  10 GB 3080. GPU time-slicing shares compute, not memory, with subgen,
  tdarr-node (and, if enabled, AudioMuse's GPU hook). Expect Ollama to
  offload part of the model to CPU, which is slower but works (the ollama pod
  has 12Gi). With `OLLAMA_MAX_LOADED_MODELS=1`, a Digarr run also evicts
  whatever model CutScript last loaded.
- **Omega availability.** Omega is the dual-boot gaming PC. While it's in
  Windows, Ollama is Pending and every Digarr AI step fails. Run discovery
  when omega is up. The timeout is 600 s to survive cold loads.
- **Thinking output.** qwen3 is a reasoning model. If Digarr's pipeline chokes
  on its thinking output (malformed JSON in the Digarr logs), switch
  `AI_MODEL` to `qwen3:8b` (5.2 GB, fits entirely in VRAM) or the existing
  `qwen2.5:7b-instruct-ctx16k`.

## First run

1. Open the UI. The first account becomes admin, and registration closes
   automatically after it (`DIGARR_DISABLE_REGISTRATION` defaults to true).
   Use a 12+ character password.
2. Setup wizard: choose **discovery-only**. Don't connect Lidarr.
3. Settings → Your Connections:
   - **ListenBrainz:** your username and the user token (vault
     `listenbrainz_user_token`, see `docs/music-listenbrainz.md`). Do this
     after the Spotify backfill, so the profile has your history.
   - **Jellyfin:** `http://jellyfin.media.svc.cluster.local:8096` with a
     dedicated Jellyfin API key named `digarr`. Use it for library sync
     (what you already own) and listening history only.
     **Leave playlist export off** during the trial; it writes playlists into
     Jellyfin.
4. Use the Settings test button on the AI provider; it checks the model
   exists.
5. Run a discovery scan.

If the pod crashloops on first boot with permission errors, check that
`/rpool/data/k8s-configs/media/digarr-data` is writable by UID 1000.
nfs-subdir normally creates it world-writable, which is how recyclarr runs
as 1000 on the same storage class.

Don't connect slskd, Spotify, Deezer or TIDAL; they're out of scope for this
trial.

## What to evaluate (suggest ~4 weeks)

Keep a short running note per scan:
- **Hit rate:** out of N recommendations, how many would you actually want?
  Sample 20 per scan and rate them.
- **Novelty:** how many are artists already in Lidarr or Jellyfin, or obvious
  "most popular artist in your top genre" picks, versus genuinely new?
- **Feedback learning:** after approving and rejecting a batch, does the next
  scan shift? Digarr claims to learn from feedback.
- **Album-level precision:** does it recommend a specific album, or just an
  artist? This matters for the Lidarr decision below.
- **Operational:** scan duration, failures while omega was down, pod memory
  (1Gi limit; upstream says 768M is the floor for PGlite), and GPU contention
  with subgen and tdarr jobs (Grafana's dcgm panels).
- **Model:** compare one scan on qwen3:14b with one on qwen3:8b. If they
  aren't clearly different, the smaller model is kinder to the shared GPU.

## If you later add Lidarr access (don't do this yet)

It would involve:
- Another consumer of the single `lidarr_api_key`. Lidarr has one key, so
  Digarr would get full API rights (the same as nest-mcp, lidarr-ui and
  exportarr), and rotating it touches all of them.
- `LIDARR_URL=http://lidarr.media.svc.cluster.local:8686` plus the key via
  `digarr-secret` from vault.
- **Auto-approve off.** Every recommendation must be approved by hand.
- **Album-level adds only.** Approve a single album, not "add artist, monitor
  all". Pick a Lidarr monitor option of *none/specific album*, not *all
  albums*. Otherwise one approval can queue a whole discography.
- Choose the quality and metadata profiles deliberately. See
  `docs/music-lidarr-config.md`.
- Keep an eye on overlap with the triage tool: Digarr-added albums will show
  up as HOLD (no plays) until you listen.

## Rollback

`git revert` the commit. Flux prunes the Deployment, Service and
IngressRoute, and `deploy-terraform` removes the DNS rewrite. Left behind:
- the PGlite DB and Digarr's own backups at
  `/rpool/data/k8s-configs/media/digarr-data` (`Retain`);
- the `digarr-secret` Secret;
- the Jellyfin `digarr` API key (revoke it in Jellyfin);
- the pulled model (`curl -X DELETE https://ollama.arishaig.site/api/delete
  -d '{"model":"qwen3:14b"}'`).
