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

## Model

The trial starts local, then compares against Claude.

**Local (default):** `AI_PROVIDER=ollama`, `AI_MODEL=qwen3.5:9b`,
`AI_BASE_URL=http://ollama.media.svc.cluster.local:11434` (Digarr's native
Ollama provider, in-cluster). `qwen3.5:9b` is already pulled (6.6 GB); it fits
entirely on the 3080 alongside subgen and tdarr. Ollama models are pulled by
hand; there's no GitOps for them.

Caveats:
- **Omega availability.** Omega is the dual-boot gaming PC. While it's in
  Windows, Ollama is Pending and every Digarr AI step fails. Run discovery
  when omega is up. The timeout is 600 s to survive cold loads.
- **One model at a time.** `OLLAMA_MAX_LOADED_MODELS=1`, so a Digarr scan
  evicts whatever model was loaded last, and vice versa.
- **Thinking output.** qwen3.5 is a reasoning model. If Digarr's pipeline
  chokes on it (malformed JSON in the Digarr logs), try
  `qwen2.5:7b-instruct-ctx16k`.

**Claude comparison:** Settings → AI provider → **Anthropic**, model
`claude-sonnet-4-6`, API key = the AudioMuse key (vault
`anthropic_audiomuse_api_key`; stored encrypted by Digarr, and spend shows up
under that key). Run the same scan and compare.
- **Why Sonnet 4.6:** Digarr's Anthropic provider forces a specific tool call
  (`tool_choice: {type: "tool"}`), which the 5.5-generation models reject.
  Digarr's own default, Haiku 4.5, also works but knows less about music.
- **Cost:** not yet measured; estimated at roughly $0.10–0.50 per scan. Check
  the Claude Console after the first scan.
- **Data:** your taste profile (built from listening history) is sent to
  Anthropic. The listens are public on ListenBrainz already.
- The `AI_*` env vars in `digarr.yaml` only seed settings on first boot, so
  switching providers is a UI change, not a commit.

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
- **Model:** run the same scan on `qwen3.5:9b` and on `claude-sonnet-4-6`.
  Compare hit rate and novelty against the per-scan cost of Claude.

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

## Rollback

`git revert` the commit. Flux prunes the Deployment, Service and
IngressRoute, and `deploy-terraform` removes the DNS rewrite. Left behind:
- the PGlite DB and Digarr's own backups at
  `/rpool/data/k8s-configs/media/digarr-data` (`Retain`);
- the `digarr-secret` Secret;
- the Jellyfin `digarr` API key (revoke it in Jellyfin);
- the pulled model, if nothing else uses it (`curl -X DELETE
  https://ollama.arishaig.site/api/delete -d '{"model":"qwen3.5:9b"}'`).
