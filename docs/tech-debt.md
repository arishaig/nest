# Tech debt log

Known shortcuts taken deliberately, with the better path noted. Newest first.

## Low-severity findings from the 2026-07-30 architecture review

**Added:** 2026-07-30 · **Severity:** low · **Area:** repo-wide

Items surfaced by [`architecture-review.md`](architecture-review.md) that fell below the
Medium threshold for inclusion in the review body. None affect blast radius or
reconcilability; all are cleanup.

- **Dead Terraform/scripts:** `terraform/pve-firewall.tf` is 29 lines of fully commented-out
  placeholder with zero resources; `terraform/import.sh` was a one-shot migration;
  `playbooks/upgrade-debian.yml` was the completed Bookworm→Trixie migration;
  `scripts/bulk-subgen.sh` still says "runs on the docker host" while subgen is on k8s.
- **Docker-era config trees superseded by k8s:** `playbooks/provision/files/docker/traefik/`
  and `files/docker/homepage/` are dead (replaced by `k8s/infrastructure/traefik/` and
  `k8s/apps/media/homepage.yaml`). `playbooks/provision/files/docker/docker-compose.yml` is
  down to a single `cadvisor` service.
- **Duplicate Authelia policy:** `templates/docker/authelia-configuration.yml.j2` and
  `templates/k8s/authelia-configuration.yml.j2` are two sources of truth for one policy;
  the docker one is still wired into `provision/docker-host.yml` and deployed to a stopped
  container.
- **Traefik configured three ways:** `k8s/infrastructure/traefik/` (live),
  `playbooks/provision/files/vps/traefik/` (live, systemd), and
  `playbooks/provision/files/docker/traefik/` (dead) — three idioms for one component.
- **~300 hardcoded `192.168.x` literals** across `playbooks/`, `terraform/`, `k8s/`,
  `scripts/` and `.github/workflows/` with no central definition (`.117` ×71, `.110` ×32,
  dead `.158` ×29). `inventory/group_vars/all/vars.yml` carries a comment admitting it must
  be hand-synced with `hosts.yml` after `tofu apply`.
- **VPS nftables ruleset is emitted twice** — every rule appears verbatim in duplicate in
  the `input` chain. Believed cosmetic (behaviour is unaffected by duplicate accepts) but
  worth confirming it is not a symptom of the reload path re-appending.
- **`watchback` HelmRelease is the only unpinned chart** — `version: ">=1.0.0"` against
  `oci://ghcr.io/arishaig/charts/watchback`, contrary to the pinning discipline everywhere
  else in `k8s/`.
- **`volume-snapshot-crds` are inert** — the three CRDs are vendored but no CSI driver,
  snapshot controller, or VolumeSnapshotClass exists to use them.
- **`nest.arishaig.site/workloads: rpi5` label is unused** — set by
  `talos/patches/worker-gamma-rpi5.yaml`, consumed by nothing (its own comment says so).
- **CI toolchain pinned in two places** — `ci/runner/Dockerfile` (Renovate-managed ARGs) and
  `playbooks/provision/runner.yml` (unpinned), kept in sync by comment only.

