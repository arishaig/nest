# k8s Migration Roadmap

Migration from Docker Compose on LXCs to Flux-managed Talos k8s.  
Goal: git as the live source of truth with a continuous reconciliation loop (Flux), not push-on-demand Ansible.

Cluster: single-node Talos VM (VMID 110, `192.168.1.110`), Flux watching `k8s/` in this repo.  
Storage: `nfs-nvme` StorageClass (nfs-subdir-external-provisioner → `rpool/data/k8s-configs` on PVE) for app configs; `media-nfs` PVC (NFS to fileserver LXC `192.168.1.17`) for `/Tank/media_root`.

---

## What's Already on k8s

| Service | Namespace | Config storage | Notes |
|---|---|---|---|
| cloudflare-ddns | cloudflare-ddns | none (env only) | Stateless; done early as proof-of-concept |
| lidarr + exportarr | media | nfs-nvme PVC | NodePort 30686 (http) / 30709 (metrics) |
| bazarr + exportarr | media | nfs-nvme PVC | NodePort 30767 (http) / 30711 (metrics) |
| subgen (Whisper) | media | nfs-nvme PVC (models) | GPU-less CPU inference |
| prowlarr + exportarr | media | nfs-nvme PVC | NodePort 30696 (http) / 30710 (metrics) |
| sonarr + exportarr | media | nfs-nvme PVC | NodePort 30989 (http) / 30707 (metrics) |
| radarr + exportarr | media | nfs-nvme PVC | NodePort 30878 (http) / 30708 (metrics) |

Traefik on the Docker LXC routes public domains to k8s NodePorts via `external-services.yml` (temporary bridge until Traefik itself moves).

---

## Remaining Manual Steps (Post-Migration Debt)

These are one-off data/config operations that must happen after the k8s manifests are deployed but before the Docker version is decommissioned. They are inherently imperative — not IaC — but are documented here so nothing gets skipped.

### Open PRs to merge first
- **PR #52** — decommission Docker prowlarr/exportarr-prowlarr
- **PR #53** — fix monitoring deploy pipeline (deploy-monitoring job)

### Per-service checklist

#### Prowlarr (PR #52 not yet merged)
- [ ] Merge PR #52; pipeline removes Docker prowlarr container via `remove_orphans`
- [ ] Verify `prowlarr.arishaig.site` still resolves to k8s (already wired in external-services.yml)
- [ ] FlareSolverr: `flaresolverr:8191` doesn't resolve from k8s pod — see FlareSolverr section below

#### Sonarr / Radarr (migrated, but DB issue occurred)
The NFS sonarr.db was overwritten with an empty DB when the pod restarted after migration.  
Fixed manually on 2026-06-08 by copying directly on PVE: `cp /rpool/data/docker-apps/sonarr/sonarr.db /rpool/data/k8s-configs/media/sonarr-config/sonarr.db`

**Lesson:** after copying a DB, immediately verify the file size on NFS matches the source before scaling up.

**WAL copy rule (must follow every time):**
1. On the source host: `sqlite3 <db> "PRAGMA wal_checkpoint(TRUNCATE);"` — confirm result is `0|0|0`
2. Copy only `*.db` — exclude `*.db-shm` and `*.db-wal`
3. Remove any stale WAL/SHM on destination before scaling up the pod
4. After scale-up, check logs for `CorruptDatabaseException` before declaring success

#### Prometheus scrape targets (PR #53 not yet merged)
Currently still pointing at Docker LXC addresses for sonarr/radarr:
- `docker.arishaig.site:9707` → should be `192.168.1.110:30707` (sonarr)
- `docker.arishaig.site:9708` → should be `192.168.1.110:30708` (radarr)

Will be fixed automatically when PR #53 merges and `deploy-monitoring` runs.

---

## What Stays on LXC Permanently

| Service | Why |
|---|---|
| qBittorrent + Gluetun (seedbox LXC 104) | `network_mode: service:gluetun` is hard to replicate in k8s; VPN coupling is simpler as a dedicated LXC |
| Scrutiny (LXC 103) | Requires raw disk device passthrough; privileged DaemonSet is possible but adds operational risk |
| MusicBrainz (LXC 101) | 350 GB database; used only by Lidarr; not worth the storage complexity |
| Home Assistant (VM 107) | HAOS; not a Docker workload |
| PBS (VM 500) | Backup infrastructure; not appropriate to put inside what it's backing up |

---

## Remaining Migration Roadmap

### Next: FlareSolverr

**Current state:** Running on Docker LXC; `flaresolverr:8191` resolves within the Docker bridge network but not from k8s pods.

**Options:**
1. **Migrate to k8s** — straightforward stateless deployment; add ExternalName Service or inline manifest; k8s DNS resolves `flaresolverr.media.svc.cluster.local` from prowlarr
2. **ExternalName bridge** — add a k8s Service `kind: ExternalName` pointing at `192.168.1.158:8191` so prowlarr can still use `http://flaresolverr:8191` without Docker being gone; remove when flaresolverr moves

Recommended: migrate directly (it's stateless, no DB, no config worth migrating).

**Steps:**
1. Add `k8s/apps/media/flaresolverr-deployment.yaml` and `flaresolverr-service.yaml` (ClusterIP, port 8191)
2. Add to `k8s/apps/media/kustomization.yaml`
3. Update prowlarr FlareSolverr setting (already uses `http://flaresolverr:8191` in DB — k8s ClusterIP service named `flaresolverr` resolves identically within the `media` namespace)
4. Comment out `flaresolverr` in `docker-compose.yml`

---

### Jellyfin

**Current state:** Docker LXC, `/mnt/app_config/jellyfin:/config`, `/mnt/media_root:/data`.

**Blocker:** SQLite DB; no concurrent-writer support → no blue/green; upgrade = brief downtime.  
**Investigate first:** Jellyfin external PostgreSQL support (community plugin / in-progress upstream). If viable, enables rolling updates. The Mealie postgres instance is already running — adding a Jellyfin DB is cheap.

**Steps (once external DB is confirmed or accepted as deferred):**
1. Create `k8s/apps/media/jellyfin-config-pvc.yaml` (nfs-nvme, ~10 Gi)
2. Create deployment + service (NodePort; expose 8096)
3. Scale down Docker Jellyfin
4. WAL checkpoint + copy `/mnt/app_config/jellyfin/` → NFS PVC (follow WAL copy rule above)
5. Scale up k8s Jellyfin; verify library, watch history, metadata intact
6. Add Traefik external-services.yml entry (NodePort)
7. Comment out `jellyfin` in `docker-compose.yml`

---

### Traefik + Authelia + Redis (infrastructure layer)

These three move together — Authelia depends on Redis and Traefik's forwardAuth; Traefik's config changes when it moves to k8s (IngressRoute CRDs replace `external-services.yml`).

**This is the most impactful migration.** Once done, `external-services.yml` goes away and all routing is k8s-native.

**Traefik:**
- ACME cert (`acme.json`) must be migrated to a PVC or switched to cert-manager (preferred — cert-manager is the k8s-idiomatic choice; handles Cloudflare DNS challenge natively)
- Traefik Docker LXC stays running in parallel until k8s Traefik is validated end-to-end

**Authelia:**
- Config YAML is a Jinja2 template — becomes a k8s Secret/ConfigMap
- Session state is Redis (see below)
- Users/ACL DB is file-based in `/mnt/app_config/authelia/` — copy to nfs-nvme PVC

**Redis:**
- Session state only; loss = users need to re-login; acceptable
- local-path or nfs-nvme PVC (small)

**Steps (high-level):**
1. Bootstrap cert-manager in `k8s/infrastructure/` with Cloudflare DNS solver
2. Deploy Traefik to k8s (`k8s/infrastructure/traefik/`) with IngressRoute CRDs
3. Migrate TLS cert handling to cert-manager (delete `acme.json` dependency)
4. Deploy Redis to k8s
5. Deploy Authelia to k8s; update Traefik middleware to point at k8s Authelia
6. Cut DNS or use parallel routing to validate k8s Traefik serves traffic
7. Decommission Docker Traefik → `external-services.yml` is no longer needed
8. Replace all external-services.yml router entries with IngressRoute manifests

---

### Remaining Media / Utility Services

These are lower-complexity moves that can be done in any order once Traefik is on k8s (IngressRoute is simpler than NodePort + external-services entries).

| Service | Config state | Notes |
|---|---|---|
| SABnzbd | SQLite + config dir | Follow WAL copy rule; expose on IngressRoute |
| Recyclarr | Config YAML only | Stateless-ish; config in git as ConfigMap |
| Recommendarr | SQLite in `/app/server/data` | Small DB; straightforward copy |
| Mealie + Postgres | Postgres data dir | Back up to PBS first; Postgres PVC on nfs-nvme |
| Homepage | Config YAML | Pure config → ConfigMap; no DB |
| Tunarr | Config + SQLite | Follow WAL copy rule |
| Watcharr | Config + SQLite | Follow WAL copy rule |
| Watchback | Config only | Stateless-ish |
| Tdarr + Tdarr Node | Config + SQLite | Media transcoding; GPU passthrough if needed |
| Glances | Config only | System stats; may be redundant with cAdvisor |
| Copyparty | Config only | File server frontend |
| Medialyze | Config only | Read-only media analysis |
| MeTube | Download dir only | Downloads go to `/Tank/media_root` anyway |
| Storyteller | Config + library data | Check data volume before migrating |

---

### Monitoring Stack (long-term)

Prometheus, Grafana, Loki, Alertmanager are on monitoring LXC 105.

Low urgency — the LXC is working well and the `deploy-monitoring` pipeline job (PR #53) now keeps it in sync. Move after the app stack is settled.

When moving: all dashboards are already in git (`playbooks/provision/files/monitoring/grafana/dashboards/`); Prometheus rules and Alertmanager config are templated. The main complexity is Loki's chunk storage and Grafana's SQLite DB.

---

## NodePort Allocation Reference

Ports 30000–32767 are the k8s NodePort range. Allocated so far:

| Port | Service | Direction |
|---|---|---|
| 30686 | lidarr http | → 8686 |
| 30696 | prowlarr http | → 9696 |
| 30707 | sonarr metrics (exportarr) | → 9707 |
| 30708 | radarr metrics (exportarr) | → 9708 |
| 30709 | lidarr metrics (exportarr) | → 9709 |
| 30710 | prowlarr metrics (exportarr) | → 9710 |
| 30711 | bazarr metrics (exportarr) | → 9711 |
| 30767 | bazarr http | → 7878\* |
| 30878 | radarr http | → 7878 |
| 30989 | sonarr http | → 8989 |

\* bazarr internal port is 6767; 30767 is the NodePort by convention.

Next available block: **30800–30877** and **30900+** (avoid collisions with existing assignments above).
