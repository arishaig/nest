# Anagnorisis removal (2026-09)

Anagnorisis (music rating/recommendation, `ghcr.io/arishaig/anagnorisis`) is
dropped in favour of the music stack in `docs/music-*.md` (AudioMuse-AI for
sonic mixes, ListenBrainz for listen data, Digarr for discovery). It had also
been crash-looping (500+ restarts on omega).

## What this change does

- Deletes the HelmRelease, ConfigMap and PVC manifest
  (`k8s/apps/media/anagnorisis*.yaml`) and its IngressRoute. Flux prunes the
  Deployment, Service, ConfigMap and IngressRoute on reconcile.
- Removes the `anagnorisis` git submodule (and with it the CodeQL submodule
  checkout and paths-ignore entries).
- Drops `anagnorisis-hf-secret` from `playbooks/provision/k8s.yml`. The
  `hugging_face_access_token` vault var stays — subgen still uses it.
- Frees one of the four GPU time-slices on omega.

**Merging is applying**: Flux prunes on the next reconcile, and `deploy-k8s`
reruns `k8s.yml` because it changed.

## What is left behind (manual, optional)

- **PVC data is retained.** `nfs-nvme` is `reclaimPolicy: Retain` /
  `onDelete: retain`. Pruning the PVC object leaves the data at
  `/rpool/data/k8s-configs/media/anagnorisis-config` on the PVE host (and in
  PBS `host/k8s-configs` snapshots). Delete it by hand once you're sure:
  `rm -rf /rpool/data/k8s-configs/media/anagnorisis-config`.
  The released PV object may also linger: `kubectl get pv | grep anagnorisis`.
- **Hand-made secrets** that were never in the repo, so nothing prunes them:
  `kubectl -n media delete secret anagnorisis-secret anagnorisis-hf-secret`.
  (`k8s.yml` creates secrets but never deletes them.)
- The `ghcr.io/arishaig/anagnorisis` image and the
  `github.com/arishaig/Anagnorisis` fork are untouched.

## Rollback

`git revert` the commit. Flux recreates the workload, and the PVC rebinds to
the retained data only if you haven't deleted it (a new PVC with the same
name gets a fresh `media/anagnorisis-config` directory, i.e. the same path —
nfs-subdir `pathPattern` is `${namespace}/${pvc name}`). Re-run the
submodule init: `git submodule update --init`.
