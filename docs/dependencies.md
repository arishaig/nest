# Dependency Inventory

All software this lab depends on, with license, FOSS status, and upstream links.

**Legend:**
- ✅ FOSS — OSI-approved open source license, community-governed
- ⚠️ FOSS† — open source license but primary development is by a corporation that also sells a paid product or cloud service built on it
- 🟡 Source-available — code is readable but license is not OSI-approved
- ❌ Proprietary — closed source or commercial-only

---

## IaC & Automation

| Project | License | Status | Repo |
|---|---|---|---|
| **OpenTofu** | MPL-2.0 | ✅ FOSS (Linux Foundation) | [opentofu/opentofu](https://github.com/opentofu/opentofu) |
| **Ansible** | GPL-3.0 | ⚠️ FOSS† (Red Hat / IBM) | [ansible/ansible](https://github.com/ansible/ansible) |
| **Flux** | Apache-2.0 | ✅ FOSS (CNCF) | [fluxcd/flux2](https://github.com/fluxcd/flux2) |
| **Helm** | Apache-2.0 | ✅ FOSS (CNCF) | [helm/helm](https://github.com/helm/helm) |
| **flux-local** (CI render/test) | Apache-2.0 | ✅ FOSS (single-maintainer) | [allenporter/flux-local](https://github.com/allenporter/flux-local) |
| **diagrams** (py) | MIT | ✅ FOSS | [mingrammer/diagrams](https://github.com/mingrammer/diagrams) |
| **PyYAML** | MIT | ✅ FOSS | [yaml/pyyaml](https://github.com/yaml/pyyaml) |
| **Graphviz** | EPL-2.0 | ✅ FOSS | [graphviz/graphviz](https://gitlab.com/graphviz/graphviz) |

> **OpenTofu note:** the lab migrated from Terraform to [OpenTofu](https://github.com/opentofu/opentofu)
> (the community MPL-2.0 fork maintained by the Linux Foundation). HashiCorp had switched Terraform
> from MPL-2.0 to BSL 1.1 in August 2023 (v1.6+) — source-available but not OSI-approved. The state
> file name is unchanged (`terraform.tfstate`); the `tofu` CLI is a drop-in replacement.

---

## OpenTofu Providers

| Provider | License | Status | Repo |
|---|---|---|---|
| **bpg/proxmox-ve** | MPL-2.0 | ✅ FOSS | [bpg/terraform-provider-proxmox](https://github.com/bpg/terraform-provider-proxmox) |
| **gmichels/adguard** | MPL-2.0 | ✅ FOSS | [gmichels/terraform-provider-adguard](https://github.com/gmichels/terraform-provider-adguard) |
| **vultr/vultr** | Apache-2.0 | ⚠️ FOSS† (Vultr official) | [vultr/terraform-provider-vultr](https://github.com/vultr/terraform-provider-vultr) |
| **cloudflare/cloudflare** | MPL-2.0 | ⚠️ FOSS† (Cloudflare official) | [cloudflare/terraform-provider-cloudflare](https://github.com/cloudflare/terraform-provider-cloudflare) |
| **hashicorp/null** | MPL-2.0 | ⚠️ FOSS† (HashiCorp / IBM) | [hashicorp/terraform-provider-null](https://github.com/hashicorp/terraform-provider-null) |

---

## Proxmox Platform

| Project | License | Status | Repo |
|---|---|---|---|
| **Proxmox VE** | AGPL-3.0 (community) | ⚠️ FOSS† (Proxmox GmbH) | [proxmox/proxmox](https://github.com/proxmox) |
| **Proxmox Backup Server** | AGPL-3.0 (community) | ⚠️ FOSS† (Proxmox GmbH) | [proxmox/proxmox-backup](https://github.com/proxmox/proxmox-backup) |

> Proxmox VE and PBS are fully open source (AGPL). Proxmox GmbH sells enterprise subscriptions for tested repositories and support, but the software itself is identical to the community version.

---

## Kubernetes Platform

The application layer runs on a Talos Kubernetes cluster, GitOps-reconciled by Flux. Workloads
use the bjw-s `app-template` chart; cluster primitives use their official upstream charts.

| Project | License | Status | Repo |
|---|---|---|---|
| **Talos Linux** | MPL-2.0 | ✅ FOSS (Sidero Labs) | [siderolabs/talos](https://github.com/siderolabs/talos) |
| **Kubernetes** | Apache-2.0 | ✅ FOSS (CNCF) | [kubernetes/kubernetes](https://github.com/kubernetes/kubernetes) |
| **Flux** (flux2 / GitOps) | Apache-2.0 | ✅ FOSS (CNCF) | [fluxcd/flux2](https://github.com/fluxcd/flux2) |
| **cert-manager** | Apache-2.0 | ✅ FOSS (CNCF) | [cert-manager/cert-manager](https://github.com/cert-manager/cert-manager) |
| **MetalLB** | Apache-2.0 | ✅ FOSS (CNCF) | [metallb/metallb](https://github.com/metallb/metallb) |
| **nfs-subdir-external-provisioner** | Apache-2.0 | ✅ FOSS (k8s-sigs) | [kubernetes-sigs/nfs-subdir-external-provisioner](https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner) |
| **bjw-s app-template** (Helm chart) | MIT | ✅ FOSS (community) | [bjw-s-labs/helm-charts](https://github.com/bjw-s-labs/helm-charts) |
| **Actions Runner Controller** (ARC) | Apache-2.0 | ⚠️ FOSS† (GitHub) | [actions/actions-runner-controller](https://github.com/actions/actions-runner-controller) |
| **kube-state-metrics** | Apache-2.0 | ✅ FOSS (CNCF) | [kubernetes/kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) |
| **Headlamp** (k8s UI) | Apache-2.0 | ✅ FOSS (CNCF / Kinvolk) | [kubernetes-sigs/headlamp](https://github.com/kubernetes-sigs/headlamp) |
| **kubeconform** (CI) | Apache-2.0 | ✅ FOSS | [yannh/kubeconform](https://github.com/yannh/kubeconform) |

> Traefik (k8s ingress controller) and Grafana Alloy (log DaemonSet) also run in the cluster —
> they are listed under [Ingress & Networking](#ingress--networking) and
> [Monitoring & Observability](#monitoring--observability) respectively.

---

## Ingress & Networking

| Project | License | Status | Repo |
|---|---|---|---|
| **Traefik** | MIT | ⚠️ FOSS† (Traefik Labs) | [traefik/traefik](https://github.com/traefik/traefik) |
| **WireGuard** | GPL-2.0 (kernel module) | ✅ FOSS | [WireGuard/WireGuard](https://github.com/WireGuard/WireGuard) |
| **fail2ban** | GPL-2.0+ | ✅ FOSS | [fail2ban/fail2ban](https://github.com/fail2ban/fail2ban) |
| **docker-socket-proxy** | Apache-2.0 | ✅ FOSS | [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) |
| **Docker Engine** | Apache-2.0 | ⚠️ FOSS† (Docker Inc) | [moby/moby](https://github.com/moby/moby) |
| **Gluetun** (seedbox VPN) | MIT | ✅ FOSS | [qdm12/gluetun](https://github.com/qdm12/gluetun) |

> Traefik Labs sells Traefik Enterprise (rate limiting, access control, plugins) as a commercial product layered on top of the MIT-licensed Traefik proxy.

---

## DNS

| Project | License | Status | Repo |
|---|---|---|---|
| **AdGuard Home** | GPL-3.0 | ⚠️ FOSS† (AdGuard) | [AdguardTeam/AdGuardHome](https://github.com/AdguardTeam/AdGuardHome) |
| **Unbound** | BSD-3-Clause | ✅ FOSS | [NLnetLabs/unbound](https://github.com/NLnetLabs/unbound) |
| **cloudflare-ddns** | Apache-2.0 | ✅ FOSS | [favonia/cloudflare-ddns](https://github.com/favonia/cloudflare-ddns) |

> AdGuard sells commercial AdGuard products (browser extension, Android/iOS app, DNS service). AdGuard Home is a genuinely open source self-hosted project developed alongside the commercial line.

---

## Authentication

| Project | License | Status | Repo |
|---|---|---|---|
| **Authelia** | Apache-2.0 | ✅ FOSS | [authelia/authelia](https://github.com/authelia/authelia) |

---

## Monitoring & Observability

| Project | License | Status | Repo |
|---|---|---|---|
| **Prometheus** | Apache-2.0 | ✅ FOSS (CNCF) | [prometheus/prometheus](https://github.com/prometheus/prometheus) |
| **Alertmanager** | Apache-2.0 | ✅ FOSS (CNCF) | [prometheus/alertmanager](https://github.com/prometheus/alertmanager) |
| **node_exporter** | Apache-2.0 | ✅ FOSS (CNCF) | [prometheus/node_exporter](https://github.com/prometheus/node_exporter) |
| **Grafana** | AGPL-3.0 | ⚠️ FOSS† (Grafana Labs) | [grafana/grafana](https://github.com/grafana/grafana) |
| **Loki** | AGPL-3.0 | ⚠️ FOSS† (Grafana Labs) | [grafana/loki](https://github.com/grafana/loki) |
| **Grafana Alloy** | Apache-2.0 | ⚠️ FOSS† (Grafana Labs) | [grafana/alloy](https://github.com/grafana/alloy) |
| **cAdvisor** | Apache-2.0 | ⚠️ FOSS† (Google) | [google/cadvisor](https://github.com/google/cadvisor) |
| **exportarr** | MIT | ✅ FOSS | [onedr0p/exportarr](https://github.com/onedr0p/exportarr) |
| **prometheus-pve-exporter** | Apache-2.0 | ✅ FOSS | [prometheus-pve/prometheus-pve-exporter](https://github.com/prometheus-pve/prometheus-pve-exporter) |
| **unpoller** (UniFi) | MIT | ✅ FOSS | [unpoller/unpoller](https://github.com/unpoller/unpoller) |
| **speedtest-exporter** | MIT | ✅ FOSS | [MiguelNdeCarvalho/speedtest-exporter](https://github.com/MiguelNdeCarvalho/speedtest-exporter) |
| **Glances** | LGPL-3.0 | ✅ FOSS | [nicolargo/glances](https://github.com/nicolargo/glances) |
| **Uptime Kuma** | MIT | ✅ FOSS | [louislam/uptime-kuma](https://github.com/louislam/uptime-kuma) |
| **Scrutiny** | MIT | ✅ FOSS | [AnalogJ/scrutiny](https://github.com/AnalogJ/scrutiny) |

> Grafana Labs sells Grafana Cloud and enterprise plugins. Grafana, Loki, and Alloy are all open source but the AGPL means any networked modification must be published. Alloy is Apache-2.0, making it more permissive.

---

## Media Stack

| Project | License | Status | Repo |
|---|---|---|---|
| **Jellyfin** | GPL-2.0 | ✅ FOSS | [jellyfin/jellyfin](https://github.com/jellyfin/jellyfin) |
| **Sonarr** | GPL-3.0 | ✅ FOSS | [Sonarr/Sonarr](https://github.com/Sonarr/Sonarr) |
| **Radarr** | GPL-3.0 | ✅ FOSS | [Radarr/Radarr](https://github.com/Radarr/Radarr) |
| **Lidarr** | GPL-3.0 | ✅ FOSS | [Lidarr/Lidarr](https://github.com/Lidarr/Lidarr) |
| **Bazarr** | GPL-3.0 | ✅ FOSS | [morpheus65535/bazarr](https://github.com/morpheus65535/bazarr) |
| **Prowlarr** | GPL-3.0 | ✅ FOSS | [Prowlarr/Prowlarr](https://github.com/Prowlarr/Prowlarr) |
| **SABnzbd** | GPL-2.0 | ✅ FOSS | [sabnzbd/sabnzbd](https://github.com/sabnzbd/sabnzbd) |
| **qBittorrent** | GPL-2.0+ | ✅ FOSS | [qbittorrent/qBittorrent](https://github.com/qbittorrent/qBittorrent) |
| **Recyclarr** | MIT | ✅ FOSS | [recyclarr/recyclarr](https://github.com/recyclarr/recyclarr) |
| **subgen** | MIT | ✅ FOSS | [McCloudS/subgen](https://github.com/McCloudS/subgen) |
| **Tdarr** | MIT | ✅ FOSS | [HaveAGitGat/Tdarr](https://github.com/HaveAGitGat/Tdarr) |
| **Tunarr** | MIT | ✅ FOSS | [chrisbenincasa/tunarr](https://github.com/chrisbenincasa/tunarr) |
| **Seerr** | MIT | ✅ FOSS | [seerr-team/seerr](https://github.com/seerr-team/seerr) |
| **FlareSolverr** | MIT | ✅ FOSS | [FlareSolverr/FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) |
| **MusicBrainz** | GPL-2.0 | ✅ FOSS (MetaBrainz Foundation) | [metabrainz/musicbrainz-server](https://github.com/metabrainz/musicbrainz-server) |
| **musicbrainz-docker** | MIT | ✅ FOSS | [metabrainz/musicbrainz-docker](https://github.com/metabrainz/musicbrainz-docker) |

> Jellyfin is a community fork of the formerly-open-source Emby. It has no commercial entity behind it.
>
> Tdarr has a free community edition with a commercial cloud-processing add-on. The core is MIT-licensed.

---

## Other Apps

| Project | License | Status | Repo |
|---|---|---|---|
| **Mealie** | AGPL-3.0 | ✅ FOSS | [mealie-recipes/mealie](https://github.com/mealie-recipes/mealie) |
| **PostgreSQL** | PostgreSQL License | ✅ FOSS | [postgres/postgres](https://github.com/postgres/postgres) |
| **Copyparty** | MIT | ✅ FOSS | [9001/copyparty](https://github.com/9001/copyparty) |
| **Storyteller** | MIT | ✅ FOSS | [storyteller-platform/storyteller](https://gitlab.com/storyteller-platform/storyteller) |
| **Recommendarr** | MIT | ✅ FOSS | [tannermiddleton/recommendarr](https://github.com/tannermiddleton/recommendarr) |
| **Watcharr** | MIT | ✅ FOSS | [sbondCo/Watcharr](https://github.com/sbondCo/Watcharr) |
| **Homepage** | GPL-3.0 | ✅ FOSS | [gethomepage/homepage](https://github.com/gethomepage/homepage) |
| **MediaLyze** | MIT | ✅ FOSS | [FrederikEmmer/medialyze](https://github.com/FrederikEmmer/medialyze) |
| **Watchback** | — | Private (Isaac's own) | [arishaig/watchback](https://github.com/arishaig/watchback) |
| **nest-mcp** | — | Private (Isaac's own) | this repo (`mcp/`) — homelab MCP server |

> **nest-mcp** (LXC 109) is built on the [FastMCP / `mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)
> (MIT, ✅ FOSS) and `httpx` (BSD-3); it exposes live homelab state to Claude Code. Its test suite
> uses `pytest` + `pytest-asyncio` + `pytest-cov`, and `genbadge` for the committed coverage badge.

---

## Docker Image Wrappers

Many services run via [LinuxServer.io](https://github.com/linuxserver) images (`linuxserver/sonarr`, `linuxserver/radarr`, etc.). These are Apache-2.0-licensed community wrappers that add s6-overlay process supervision, consistent PUID/PGID mapping, and stable tagging on top of the upstream application.

---

## Commercial Services (not software)

| Service | Purpose | Notes |
|---|---|---|
| **Vultr** | VPS hosting | ~$6/mo, vc2-1c-1gb Seattle |
| **Cloudflare** | External DNS, ACME DNS challenge | Free tier |
| **ProtonVPN** | Seedbox WireGuard exit node | Paid subscription |
| **Nabu Casa** | Home Assistant remote access | Manages `casa.arishaig.site` externally |

---

## Operating Systems & Host Components

### LXC Containers — all Debian 13 (Trixie)

Template: `debian-13-standard_13.1-2_amd64.tar.zst` (downloaded by Terraform via `proxmox_download_file`).

All LXCs receive the following via `playbooks/provision/common.yml`:
- Base packages: `curl wget vim htop git`
- **node_exporter v1.11.1** (Prometheus metrics, systemd service)
- **BBR congestion control** (`/etc/sysctl.d/99-bbr.conf`) + `fq` qdisc + tuned socket buffers

| VMID | Host | Additional components |
|---|---|---|
| 100 | docker | **Decommissioned** (`start_on_boot=false`) post-k8s migration; compose file kept with cadvisor for ad-hoc scratch use. Retains a WireGuard peer so monitoring can reach VPS metrics |
| 101 | musicbrainz | Docker CE, musicbrainz-docker stack (MusicBrainz Server, Solr, PostgreSQL 16, Redis) |
| 102 | fileserver | `samba` `samba-common-bin` (NFS export `/Tank/media_root` to the k8s `media-nfs` StorageClass) |
| 103 | scrutiny | Docker CE, Scrutiny omnibus (web + collector + InfluxDB), cAdvisor |
| 104 | seedbox | Docker CE, qBittorrent, Gluetun (ProtonVPN), cAdvisor, qbittorrent-exporter |
| 105 | monitoring | Docker CE, Prometheus, Grafana, Loki, Alertmanager, cAdvisor, pve-exporter, speedtest-exporter, unpoller |
| 106 | dns-secondary | AdGuard Home (install script), Unbound, unbound_exporter v0.6.0, Docker CE |
| 108 | ci | GitHub Actions self-hosted runner (recovery-critical deploy jobs); OpenTofu, ansible venv, kustomize/kubeconform/kubectl/shellcheck |
| 109 | mcp | nest-mcp HTTP server (FastMCP, port 8765), systemd unit |
| 111 | foundry | FoundryVTT game server |

### Talos VMs (Kubernetes control plane)

Talos Linux is provisioned from the Image Factory `metal-amd64` ISO (qemu-guest-agent
extension) — not Debian, no Ansible. alpha VM 110 (`192.168.1.110`, 24 GB), beta VM 113
(`192.168.1.111`, 4 GB), delta VM 115 (`192.168.1.114`, 8 GB). The beta/delta test VMs are
temporary and will be replaced by Raspberry Pi 5 (8 GB) nodes booting Talos from the
`rpi_5` overlay — see [rpi5-talos.md](rpi5-talos.md).

### VPS — Debian 13 (Trixie)

Vultr `vc2-1c-1gb`, Seattle. All services run as systemd units (no Docker).

| Component | Version | Notes |
|---|---|---|
| Traefik | 3.7.1 | TCP passthrough proxy, SNI routes dns3.arishaig.site to local AdGuard |
| WireGuard | kernel | `wg-quick@wg0`, MTU 1420 |
| AdGuard Home | latest (install script) | Tertiary DNS, DoH (:8443) + DoT (:853), no plain DNS |
| Unbound | distro | Recursive upstream for AdGuard on 127.0.0.1:5335 |
| node_exporter | 1.11.1 | Scraped by Prometheus over WireGuard |
| unbound_exporter | 0.6.0 | Scraped by Prometheus over WireGuard |
| Grafana Alloy | 1.16.1 | Ships logs to Loki over WireGuard |
| fail2ban | distro | SSH + Traefik jails |
| logrotate | distro | Traefik access log rotation |
| BBR + fq | kernel | Same sysctl tuning as LXCs |

### Raspberry Pi — Raspberry Pi OS (Trixie)

Primary DNS host, on VLAN 7. Not provisioned by Terraform — Ansible manages config only.

| Component | Version | Notes |
|---|---|---|
| AdGuard Home | latest (install script) | Primary DNS, HTTPS admin |
| Unbound | distro | Recursive upstream resolver |
| unbound_exporter | 0.6.0 | Prometheus metrics |
| node_exporter | 1.11.1 | Prometheus metrics |
| Docker CE | latest stable | Runs adguard-exporter |

### VM 107 — Home Assistant OS (HAOS)

HAOS is self-contained; no Ansible provisioner. Restored from PBS backup after VM creation.

- UEFI boot (OVMF), 2 vCPU, 16 GB RAM, 32 GB NVMe (ZFS), VLAN 4
- USB passthrough: Zigbee controller (host `1-5`), Z-Wave controller (host `3-4`), USB WiFi adapter (`0bda:a728`)
- Remote access via Nabu Casa (`casa.arishaig.site` managed externally, not in Terraform)

### VM 500 — Proxmox Backup Server 4.2.0

ISO: `proxmox-backup-server_3.4-1.iso` (original install). 4 vCPU, 32 GB RAM, 500 GB NVMe (ZFS). Upgraded in-place to PBS 4.2.0 / Debian 13 Trixie.

Ansible (`pbs.yml`) adds:
- **node_exporter** with textfile collector (`--collector.textfile.directory`)
- **pbs-backup-freshness.sh** cron (every 15 min) — writes per-group newest-snapshot timestamps for the `PBSBackupStale` Prometheus alert

PBS → PVE storage connection is in `/etc/pve/storage.cfg` (managed manually; provider has no `storage_pbs` resource type — see `terraform/pve-storage.tf`).

---

## Dependency Graph

```
Public Internet
└── Cloudflare DNS (external DNS, ACME for *.arishaig.site)
    └── Vultr VPS
        ├── Traefik (TCP passthrough, :443 → WireGuard)
        ├── WireGuard (tunnel to Talos alpha 10.10.0.3 + ingress LB 192.168.1.117)
        ├── AdGuard Home + Unbound (tertiary DNS, dns3.arishaig.site)
        ├── Grafana Alloy → Loki (logs over WireGuard)
        ├── node_exporter → Prometheus (metrics over WireGuard)
        └── fail2ban

Proxmox VE (192.168.1.16)
├── Talos k8s cluster (control plane: alpha VM 110, beta VM 113, delta VM 115)
│   │   API VIP 192.168.1.115 · Flux reconciles k8s/ from git
│   ├── infrastructure (official charts):
│   │   ├── Traefik (TLS termination, MetalLB LB 192.168.1.117) → authelia forwardAuth
│   │   ├── cert-manager (wildcard cert, Cloudflare DNS-01)
│   │   ├── MetalLB (traefik-pool .117, metrics-pool .116)
│   │   ├── Authelia + Valkey (SSO/2FA)
│   │   ├── Alloy DaemonSet → Loki · nfs-subdir-provisioner (nfs-nvme)
│   │   └── ARC (arc-lint ephemeral CI runners) · kube-state-metrics · Headlamp
│   └── apps (bjw-s app-template HelmReleases):
│       ├── Media: sonarr · radarr · lidarr · bazarr · prowlarr (+ exportarr ×5)
│       ├── Streaming: jellyfin · tunarr · seerr · tdarr(+node) · subgen
│       ├── Acquisition: sabnzbd · flaresolverr · recyclarr · metube
│       ├── Apps: mealie/postgres · copyparty · storyteller · recommendarr
│       │        watcharr · watchback · homepage · medialyze
│       └── cloudflare-ddns
│
├── LXC 100: docker — decommissioned (scratch host; WireGuard peer for monitoring reach)
│
├── LXC 101: musicbrainz
│   └── musicbrainz-docker (metabrainz/musicbrainz-server + solr + postgres + redis)
│
├── LXC 102: fileserver
│   └── Samba/NFS (NAS, /Tank/media_root → k8s media-nfs StorageClass)
│
├── LXC 103: scrutiny
│   └── Scrutiny (SMART monitoring, omnibus image)
│
├── LXC 104: seedbox
│   └── qBittorrent + Gluetun (ProtonVPN exit) + cadvisor + qbittorrent-exporter
│
├── LXC 105: monitoring
│   └── Prometheus · Grafana · Loki + Ruler · Alertmanager
│       cadvisor · pve-exporter · speedtest-exporter · unpoller
│
├── LXC 106: dns-secondary
│   └── AdGuard Home + Unbound
│
├── LXC 108: ci   └── GitHub Actions self-hosted runner (deploy jobs)
├── LXC 109: mcp  └── nest-mcp (FastMCP HTTP server :8765)
├── LXC 111: foundry └── FoundryVTT
│
├── VM 107: homeassistant (VLAN 4)
│   └── Home Assistant OS (Nabu Casa remote access)
│
└── VM 500: backup (PBS)
    └── Proxmox Backup Server → backs up all LXCs + VMs

Raspberry Pi (192.168.7.7, VLAN 7)
└── AdGuard Home (primary DNS) + Unbound
    └── Terraform (gmichels/adguard) manages rewrites

Vultr VPS — dns3.arishaig.site (public)
└── AdGuard Home (tertiary DNS, DoH :8443 + DoT :853, no local rewrites)
    └── Unbound (recursive resolver)
```
