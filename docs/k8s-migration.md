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
| lidarr + exportarr | media | nfs-nvme PVC | NodePort 30686 (http) / hostPort 9709 (metrics) |
| bazarr + exportarr | media | nfs-nvme PVC | NodePort 30767 (http) / hostPort 9711 (metrics) |
| subgen (Whisper) | media | nfs-nvme PVC (models) | GPU-less CPU inference |
| prowlarr + exportarr | media | nfs-nvme PVC | NodePort 30696 (http) / hostPort 9710 (metrics) |
| sonarr + exportarr | media | nfs-nvme PVC | NodePort 30989 (http) / hostPort 9707 (metrics) |
| radarr + exportarr | media | nfs-nvme PVC | NodePort 30878 (http) / hostPort 9708 (metrics) |
| flaresolverr | media | none (stateless) | ClusterIP only; prowlarr resolves via cluster DNS |
| metube | media | none (media NFS only) | NodePort 30808 |
| tdarr server + node | media | nfs-nvme PVC (server state) | NodePort 30815 (web) / 30816 (server); node connects via cluster DNS |
| sabnzbd | media | nfs-nvme PVC | NodePort 30800 |
| seerr | media | nfs-nvme PVC | NodePort 30801 |
| tunarr | media | nfs-nvme PVC | NodePort 30802 |
| watcharr | media | nfs-nvme PVC | NodePort 30803 |
| watchback | media | nfs-nvme PVC | NodePort 30804 |
| homepage | media | nfs-nvme PVC | NodePort 30805 |
| copyparty | media | nfs-nvme PVC | NodePort 30806 |
| medialyze | media | nfs-nvme PVC | NodePort 30807 |
| recommendarr | media | nfs-nvme PVC | NodePort 30809 |
| storyteller | media | nfs-nvme PVC | NodePort 30810 |
| mealie + postgres | media | nfs-nvme PVC | NodePort 30813 |
| recyclarr | media | nfs-nvme PVC | CronJob (no http port) |
| jellyfin-pgsql | media | nfs-nvme PVC + external postgres | NodePort 30814; routed at `jellyfin.arishaig.site`; Docker Jellyfin decommissioned |

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

---

## What Stays on LXC Permanently

| Service | Why |
|---|---|
| qBittorrent + Gluetun + qbittorrent-exporter (seedbox LXC 104) | `network_mode: service:gluetun` is hard to replicate in k8s; VPN coupling is simpler as a dedicated LXC |
| Scrutiny (LXC 103) | Requires raw disk device passthrough; privileged DaemonSet is possible but adds operational risk |
| MusicBrainz (LXC 101) | 350 GB database; used only by Lidarr; not worth the storage complexity |
| Home Assistant (VM 107) | HAOS; not a Docker workload |
| PBS (VM 500) | Backup infrastructure; not appropriate to put inside what it's backing up |

---

## Remaining Migration Roadmap

### Jellyfin cutover ✅ DONE

`jellyfin-pgsql` is the primary instance at `jellyfin.arishaig.site`. Docker Jellyfin removed from docker-compose.yml; config archived at `/mnt/app_config/jellyfin.bak` on the fileserver NFS mount.

---

### Traefik + Authelia + Redis ✅ DONE

All three running in k8s (`traefik`, `authelia` namespaces). Metrics scraping via hostPort (9959 authelia, 9121 redis-exporter). All Prometheus targets healthy as of 2026-06-09.

---

### Services staying on Docker LXC permanently (or until Docker LXC decommission)

| Service | Why |
|---|---|
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
| 30707 | sonarr metrics (exportarr) NodePort | → 9707 (also exposed via hostPort 9707) |
| 30708 | radarr metrics (exportarr) NodePort | → 9708 (also exposed via hostPort 9708) |
| 30709 | lidarr metrics (exportarr) NodePort | → 9709 (also exposed via hostPort 9709) |
| 30710 | prowlarr metrics (exportarr) NodePort | → 9710 (also exposed via hostPort 9710) |
| 30711 | bazarr metrics (exportarr) NodePort | → 9711 (also exposed via hostPort 9711) |
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
| 30814 | jellyfin-pgsql http | → 8096 |
| 30815 | tdarr web UI | → 8265 |
| 30816 | tdarr server (node comms) | → 8266 |
| 30878 | radarr http | → 7878 |
| 30989 | sonarr http | → 8989 |

\* bazarr internal port is 6767; 30767 is the NodePort by convention.

Next available block: **30811–30812**, **30814–30877**, **30900+** (avoid collisions with existing assignments above).
