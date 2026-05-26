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
| **Terraform** | BSL 1.1 | 🟡 Source-available | [hashicorp/terraform](https://github.com/hashicorp/terraform) |
| **Ansible** | GPL-3.0 | ⚠️ FOSS† (Red Hat / IBM) | [ansible/ansible](https://github.com/ansible/ansible) |
| **diagrams** (py) | MIT | ✅ FOSS | [mingrammer/diagrams](https://github.com/mingrammer/diagrams) |
| **PyYAML** | MIT | ✅ FOSS | [yaml/pyyaml](https://github.com/yaml/pyyaml) |
| **Graphviz** | EPL-2.0 | ✅ FOSS | [graphviz/graphviz](https://gitlab.com/graphviz/graphviz) |

> **Terraform note:** HashiCorp switched from MPL-2.0 to BSL 1.1 in August 2023 (v1.6+). BSL prohibits using Terraform to build a competing hosted service — for personal use this is irrelevant, but it is not OSI-approved open source. [OpenTofu](https://github.com/opentofu/opentofu) is the community MPL-2.0 fork maintained by the Linux Foundation.

---

## Terraform Providers

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
| **Homarr** | MIT | ✅ FOSS | [homarr-labs/homarr](https://github.com/homarr-labs/homarr) |
| **MediaLyze** | MIT | ✅ FOSS | [FrederikEmmer/medialyze](https://github.com/FrederikEmmer/medialyze) |
| **Watchback** | — | Private (Isaac's own) | [arishaig/watchback](https://github.com/arishaig/watchback) |

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

## Dependency Graph

```
Public Internet
└── Cloudflare DNS (external DNS, ACME for *.arishaig.site)
    └── Vultr VPS
        ├── Traefik (TCP passthrough)
        ├── WireGuard (tunnel to Nest)
        ├── Grafana Alloy → Loki (logs over WireGuard)
        ├── node_exporter → Prometheus (metrics over WireGuard)
        └── fail2ban

Proxmox VE (192.168.1.16)
├── LXC 100: docker
│   ├── Traefik (TLS termination, ACME via Cloudflare DNS)
│   │   └── authelia (forwardAuth)
│   ├── Media: sonarr · radarr · lidarr · bazarr · prowlarr
│   │   └── sabnzbd (usenet) · qBittorrent via seedbox
│   ├── Streaming: jellyfin · tunarr · seerr
│   ├── Apps: mealie/postgres · copyparty · storyteller · recommendarr
│   │        watcharr · watchback · homarr · uptime-kuma · glances
│   ├── Processing: tdarr · tdarr-node · subgen · recyclarr · flaresolverr
│   ├── Exporters: cadvisor · exportarr (×5) · cloudflare-ddns
│   └── Infra: socket-proxy · medialyze
│
├── LXC 101: musicbrainz
│   └── musicbrainz-docker (metabrainz/musicbrainz-server + solr + postgres + redis)
│
├── LXC 102: fileserver
│   └── Samba (NAS, mounts to docker LXC as /mnt/media_root)
│
├── LXC 103: scrutiny
│   └── Scrutiny (SMART monitoring, omnibus image)
│
├── LXC 104: seedbox
│   └── qBittorrent + Gluetun (ProtonVPN exit) + cadvisor + exportarr
│
├── LXC 105: monitoring
│   └── Prometheus · Grafana · Loki · Alertmanager
│       cadvisor · pve-exporter · speedtest-exporter · unpoller
│
├── LXC 106: dns-secondary
│   └── AdGuard Home + Unbound
│
├── VM 107: homeassistant (VLAN 4)
│   └── Home Assistant OS (Nabu Casa remote access)
│
└── VM 500: backup (PBS)
    └── Proxmox Backup Server → backs up all LXCs + VMs

Raspberry Pi (192.168.7.7, VLAN 7)
└── AdGuard Home (primary DNS) + Unbound
    └── Terraform (gmichels/adguard) manages rewrites
```
