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
| flaresolverr | media | none (stateless) | ClusterIP only; prowlarr resolves via cluster DNS |
| metube | media | none (media NFS only) | NodePort 30808 |

Traefik on the Docker LXC routes public domains to k8s NodePorts via `external-services.yml` (temporary bridge until Traefik itself moves).

---

## Remaining Manual Steps (Post-Migration Debt)

These are one-off data/config operations that must happen after the k8s manifests are deployed but before the Docker version is decommissioned. They are inherently imperative — not IaC — but are documented here so nothing gets skipped.

### Sonarr / Radarr (migrated, but DB issue occurred)
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

### Pending decommission (k8s manifests exist; Docker still active)

Manifests for all services below are deployed. Each service's Docker container is still running — decommission each one by copying data, then uncommenting its router + service in `external-services.yml` and commenting out its Docker entry in the same PR.

**Config-only services** (copy, no WAL checkpoint needed):

| Service | NFS copy source | NFS copy dest | NodePort |
|---|---|---|---|
| recyclarr | `/mnt/app_config/recyclarr/` | `/rpool/data/k8s-configs/media/recyclarr-config/` | CronJob (no port) |
| homepage | `/mnt/app_config/homepage/` | `/rpool/data/k8s-configs/media/homepage-config/` | 30805 |
| watchback | `/mnt/app_config/watchback/config/` | `/rpool/data/k8s-configs/media/watchback-data/config/` | 30804 |
| watchback | `/mnt/app_config/watchback/static/` | `/rpool/data/k8s-configs/media/watchback-data/static/` | — |
| copyparty | `/mnt/app_config/copyparty/` | `/rpool/data/k8s-configs/media/copyparty-config/` | 30806 |
| medialyze | `/mnt/app_config/medialyze/` | `/rpool/data/k8s-configs/media/medialyze-config/` | 30807 |

**SQLite services** (WAL checkpoint first — see WAL copy rule below):

| Service | WAL source | NFS copy dest | NodePort |
|---|---|---|---|
| recommendarr | `/mnt/app_config/recommendarr/` | `/rpool/data/k8s-configs/media/recommendarr-data/` | 30809 |
| sabnzbd | `/mnt/app_config/sabnzbd/` | `/rpool/data/k8s-configs/media/sabnzbd-config/` | 30800 |
| seerr | `/mnt/app_config/seerr/` | `/rpool/data/k8s-configs/media/seerr-config/` | 30801 |
| tunarr | `/mnt/app_config/tunarr/data/` | `/rpool/data/k8s-configs/media/tunarr-config/` | 30802 |
| watcharr | `/mnt/app_config/watcharr/` | `/rpool/data/k8s-configs/media/watcharr-data/` | 30803 |
| storyteller | `/mnt/app_config/storyteller/` | `/rpool/data/k8s-configs/media/storyteller-config/` | 30810 |

Note: storyteller's `/data` and `/books` mounts come from the media NFS (`storyteller/` and `media/books/` subpaths) — no copy needed for those.

**Postgres service** (mealie):

```bash
# 1. Dump from running Docker postgres
docker exec postgres pg_dump -U $POSTGRES_USER mealie > /tmp/mealie_backup.sql

# 2. Copy /app/data (uploads, images — not in postgres)
cp -r /mnt/app_config/mealie-data/ /rpool/data/k8s-configs/media/mealie-data/

# 3. After postgres k8s pod is Running:
kubectl exec -n media deploy/postgres -- psql -U $POSTGRES_USER -c "CREATE DATABASE mealie;"
kubectl exec -n media -i deploy/postgres -- psql -U $POSTGRES_USER mealie < /tmp/mealie_backup.sql

# 4. Scale down Docker mealie + postgres, then uncomment external-services.yml mealie entry
```

Required secrets (create once, manually):
```bash
kubectl create secret generic postgres-secret -n media \
  --from-literal=POSTGRES_USER=<user> \
  --from-literal=POSTGRES_PASSWORD=<password>

kubectl create secret generic mealie-secret -n media \
  --from-literal=OIDC_CLIENT_SECRET=<secret>

kubectl create secret generic storyteller-secret -n media \
  --from-literal=SECRET_KEY=<key>
```

---

### Jellyfin

**Current state:** Docker LXC (linuxserver/jellyfin:10.11.10), `/mnt/app_config/jellyfin:/config`, `/mnt/media_root:/data`. Active on port 8096 / jellyfin.arishaig.site.

**PostgreSQL approach:** Jellyfin 10.11 added EF Core + experimental plugin API for external DB providers. Community plugin `JPVenson/Jellyfin.Pgsql` (`ghcr.io/jpvenson/jellyfin.pgsql:10.11.8-1`) wraps the official Jellyfin image and replaces SQLite with postgres entirely via `POSTGRES_HOST/PORT/DB/USER/PASSWORD` env vars.

**Test instance running:** `jellyfin-pgsql` deployment in `media` namespace, NodePort 30814. Fresh config PVC (not a data migration). Docker Jellyfin remains live on port 8096. Media NFS is read-only to avoid two instances writing trickplay/metadata.

**Test goals:**
- Confirm startup connects to postgres (not SQLite)
- Go through setup wizard — verify data persists across pod restarts in postgres
- Verify media browsing works with read-only media mount

**After test passes — full migration steps:**
1. `CREATE DATABASE jellyfin;` in k8s postgres (done for test; already exists)
2. Scale down Docker Jellyfin
3. WAL checkpoint all SQLite DBs: `sqlite3 <db> "PRAGMA wal_checkpoint(TRUNCATE);"` (library.db, jellyfin.db, etc.)
4. Copy `/mnt/app_config/jellyfin/` → NFS PVC (config, plugins, metadata — NOT SQLite DBs if using postgres)
5. Scale up k8s Jellyfin; verify library scan/watch history
6. Add Traefik external-services.yml entry pointing at NodePort 30814
7. Comment out `jellyfin` in `docker-compose.yml`

**Note:** plugin image is on 10.11.8 (Docker is 10.11.10); minor gap, acceptable for test.

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

### Services staying on Docker LXC permanently (or until Docker LXC decommission)

| Service | Why |
|---|---|
| Tdarr + Tdarr Node | CPU-intensive transcoding; no k8s scheduling benefit; dedicated LXC preferred when Docker LXC decommissions |
| Glances | Needs docker.sock + rootfs — moot once Docker LXC is gone |

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
| 30800 | sabnzbd http | → 8080 |
| 30801 | seerr http | → 5055 |
| 30802 | tunarr http | → 8000 |
| 30803 | watcharr http | → 3080 |
| 30804 | watchback http | → 8484 |
| 30805 | homepage http | → 3000 |
| 30806 | copyparty http | → 3923 |
| 30807 | medialyze http | → 8080 |
| 30808 | metube http | → 8081 |
| 30809 | recommendarr http | → 3000 |
| 30810 | storyteller http | → 8001 |
| 30813 | mealie http | → 9000 |
| 30814 | jellyfin-pgsql (test instance) | → 8096 |
| 30878 | radarr http | → 7878 |
| 30989 | sonarr http | → 8989 |

\* bazarr internal port is 6767; 30767 is the NodePort by convention.

Next available block: **30811–30812**, **30814–30877**, **30900+** (avoid collisions with existing assignments above).
