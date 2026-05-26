# ansible-on-nest — Technical Design

Private home lab managed as code. Terraform provisions the infrastructure;
Ansible converges configuration. Both are triggered together via Terraform
`local-exec` provisioners on first create, then re-run independently for day-to-day changes.

See [docs/dependencies.md](dependencies.md) for a full dependency inventory with licenses and GitHub links.

## Diagrams

Full infrastructure: `docs/architecture.png`
Client ingress flow: `docs/architecture-flow.png`

Regenerate with: `python3 scripts/generate_diagram.py`

---

## Infrastructure Overview

### Physical / Cloud Hosts

| Host | IP | Role | Managed by |
|---|---|---|---|
| Proxmox VE | 192.168.1.16 | Hypervisor | Terraform (bpg/proxmox-ve) |
| Vultr VPS | 66.42.79.175 | Public ingress proxy | Terraform (vultr/vultr) |
| Raspberry Pi | 192.168.7.7 | Primary DNS | Ansible (config only) |

The Pi lives on VLAN 7. PVE and all LXCs are on the main LAN (192.168.1.x). Home Assistant is isolated on VLAN 4.

### LXC Containers (Proxmox)

| VMID | Name | IP | Purpose |
|---|---|---|---|
| 100 | docker | 192.168.1.158 (static) | All application Docker services |
| 101 | musicbrainz | 192.168.1.197 (DHCP) | MusicBrainz server |
| 102 | fileserver | 192.168.1.17 (static) | Samba NAS |
| 103 | scrutiny | 192.168.1.46 (DHCP) | SMART disk monitoring |
| 104 | seedbox | 192.168.1.182 (DHCP) | qBittorrent behind Gluetun VPN |
| 105 | monitoring | 192.168.1.44 (static) | Prometheus · Grafana · Loki · Alertmanager |
| 106 | dns-secondary | 192.168.1.53 | AdGuard Home + Unbound (secondary DNS) |

### VMs (Proxmox)

| VMID | Name | IP | Purpose |
|---|---|---|---|
| 107 | homeassistant | 192.168.4.50 (VLAN 4) | Home Assistant OS |
| 500 | backup | 192.168.1.113 | Proxmox Backup Server |

---

## Public Ingress Path

```
Client → Cloudflare DNS → VPS :443
  → Traefik (TCP passthrough, HostSNI(*))
  → WireGuard tunnel (10.10.0.1 → 10.10.0.2)
  → Nest Traefik :443 (TLS termination, PROXY protocol v2)
  → Authelia (forwardAuth) → Docker service
```

### VPS Traefik (66.42.79.175)

Runs as a systemd service (not Docker). Does **TCP passthrough** — it never
terminates TLS. Any hostname on port 443 is forwarded as-is over the WireGuard
tunnel to `10.10.0.2:443` using PROXY protocol v2 to preserve the real client IP.

Port 80 → 443 redirect is the only HTTP-layer operation.

Metrics endpoint listens on `10.10.0.1:8080` (WireGuard interface, not public).

### WireGuard Tunnel

VPS side: `10.10.0.1/24`, ListenPort 51820, MTU 1420
Nest side: `10.10.0.2/24`, MTU 1420

Keys: public keys stored in `inventory/group_vars/all/vars.yml`; private keys in `vault.yml`.

### Nest Traefik (Docker LXC, port 443)

Terminates TLS using Cloudflare ACME DNS challenge (wildcard cert: `*.arishaig.site`).
API token in Docker env as `CF_DNS_API_TOKEN`. Certificate stored at `/mnt/app_config/traefik/acme.json`.

Trusts PROXY protocol from `10.10.0.1/32`. Rate-limit middleware applied globally.

Docker socket access is via `tecnativa/docker-socket-proxy` (CONTAINERS=1 read-only;
no SERVICES, TASKS, NETWORKS, VOLUMES, NODES).

Two Docker networks:
- `internal-net` — service-to-service (no external access)
- `proxy-net` — Traefik-facing; only services that need routing attach to this

---

## DNS

### External (Cloudflare)

Managed by Terraform (`terraform/cloudflare.tf`, `cloudflare/cloudflare ~> 5.0`).

| Record | Target | Notes |
|---|---|---|
| `*.arishaig.site` | VPS IP | Auto-populated from `vultr_instance.vps_proxy.main_ip` |
| `dns.arishaig.site` | Home IP (50.47.227.169) | Direct to Pi, bypasses VPS |
| `vpn.arishaig.site` | Home IP (50.47.227.169) | Direct to UDM WireGuard |

`cloudflare-ddns` container keeps `vpn.arishaig.site` current when the home IP changes.
All records are unproxied (Cloudflare orange cloud off).

### Internal (AdGuard Home)

Primary: Raspberry Pi at 192.168.7.7, Unbound upstream.
Secondary: LXC 106 at 192.168.1.53, its own Unbound upstream, failover from primary.

Internal rewrites (`.arishaig.site` → LAN IPs) managed by Terraform (`gmichels/adguard ~> 1.7`).
The Traefik `certresolver=cloudflare` handles TLS for `.local.arishaig.site` names using
the same wildcard cert — no separate cert infrastructure needed for internal access.

---

## Authentication

Authelia runs as a Docker container on the Docker LXC, attached only to `proxy-net`.
Nest Traefik uses it as a `forwardAuth` middleware (`authelia@file` defined in `dynamic/middlewares.yml`).

**Authelia required (most services):** bazarr, copyparty, glances, homarr, lidarr, medialyze,
mealie, prowlarr, radarr, recommendarr, sabnzbd, sonarr, storyteller, uptime-kuma, watchback.

**Authelia bypassed (intentional):**

| Service | Reason |
|---|---|
| jellyfin | Media clients (AppleTV, Kodi etc.) cannot handle auth redirects |
| seerr | Intended for external users to submit requests |
| tunarr | Jellyfin communicates with it directly |
| watcharr | Login flow breaks with forwardAuth enabled |

**`.local.arishaig.site` routes** also bypass Authelia — these are direct internal-access
routes for services that also have an Authelia-protected public route (torrent, scrutiny, musicbrainz, backlight).

---

## Docker Services (LXC 100)

### Media Stack

| Service | Image | Port | Auth |
|---|---|---|---|
| sonarr | linuxserver/sonarr | 8989 | ✓ |
| radarr | linuxserver/radarr | 7878 | ✓ |
| lidarr | linuxserver-labs/prarr:lidarr-plugins | 8686 | ✓ |
| bazarr | linuxserver/bazarr | 6767 | ✓ |
| prowlarr | linuxserver/prowlarr | 9696 | ✓ |
| sabnzbd | linuxserver/sabnzbd | 8080 | ✓ |
| jellyfin | linuxserver/jellyfin | 8096 | — |
| seerr | ghcr.io/seerr-team/seerr | 5055 | — |
| tunarr | chrisbenincasa/tunarr | 8000 | — |
| recyclarr | ghcr.io/recyclarr/recyclarr | — | — |
| subgen | mccloud/subgen:cpu | 9000 | — |
| medialyze | ghcr.io/frederikemmer/medialyze | 8080 | ✓ |
| tdarr | ghcr.io/haveagitgat/tdarr | 8265, 8266 | bridge network |
| tdarr-node | ghcr.io/haveagitgat/tdarr_node | — | network_mode: service:tdarr |

Media files and service configs are on NFS/Samba mounts from the fileserver LXC at `/mnt/media_root`.
App configs are at `/mnt/app_config/<service>`.

### Other Apps

| Service | Image | Auth | Notes |
|---|---|---|---|
| mealie | ghcr.io/mealie-recipes/mealie | ✓ | Postgres backend |
| postgres | postgres:15 | — | Mealie only |
| storyteller | registry.gitlab.com/storyteller-platform/storyteller | ✓ | Ebook/audiobook readalongs |
| recommendarr | tannermiddleton/recommendarr | ✓ | AI content suggestions |
| watcharr | ghcr.io/sbondco/watcharr | — | Watch history |
| watchback | ghcr.io/arishaig/watchback | ✓ | Custom app (Isaac's own image) |
| copyparty | copyparty/ac | ✓ | File browser |
| homarr | ghcr.io/homarr-labs/homarr | ✓ | Dashboard (Docker socket removed for security) |
| uptime-kuma | louislam/uptime-kuma:1 | ✓ | Uptime monitoring |
| glances | nicolargo/glances | ✓ | System stats |
| flaresolverr | ghcr.io/flaresolverr/flaresolverr | — | Cloudflare bypass for Prowlarr |

### Infrastructure Containers

| Service | Purpose |
|---|---|
| traefik | Reverse proxy, TLS termination, ACME |
| authelia | SSO / 2FA forwardAuth |
| socket-proxy | Restricted Docker socket (CONTAINERS=1 read-only) |
| cloudflare-ddns | Keeps `vpn.arishaig.site` pointed at home IP |

### Monitoring Exporters (Docker LXC)

| Service | Port | Scrape target |
|---|---|---|
| cadvisor | 8081 | Container resource metrics |
| exportarr-sonarr | 9707 | Sonarr queue/health |
| exportarr-radarr | 9708 | Radarr queue/health |
| exportarr-lidarr | 9709 | Lidarr queue/health |
| exportarr-prowlarr | 9710 | Prowlarr indexer stats |
| exportarr-bazarr | 9711 | Bazarr subtitle stats |

---

## Monitoring Stack (LXC 105)

All services run via Docker Compose in `/opt/monitoring/`.

### Prometheus

Scrapes every 30s. Jobs:

| Job | Targets |
|---|---|
| `node` | All LXCs, PVE, PBS, Pi (`:9100`) via internal DNS names |
| `vps` | `10.10.0.1:9100` (over WireGuard), labelled `vps-proxy` |
| `traefik-vps` | `10.10.0.1:8080` (VPS Traefik metrics, over WireGuard) |
| `cadvisor` | Docker/seedbox/scrutiny/monitoring LXCs |
| `proxmox` | Via pve-exporter at `monitoring.local:9221` |
| `adguard` | Primary + secondary AdGuard exporters (`:9618`) |
| `unbound` | Primary + secondary Unbound exporters (`:9167`) |
| `unpoller` | UniFi metrics via unpoller container |
| `sonarr/radarr/lidarr/prowlarr/bazarr` | exportarr sidecars on docker LXC |
| `qbittorrent` | Seedbox qBittorrent exporter |
| `postgres` | Postgres exporter on docker LXC |
| `scrutiny` | SMART metrics API |
| `homeassistant` | HA Prometheus integration (bearer token in vault) |
| `wled` | WLED LED controller at `backlight.arishaig.site` |
| `speedtest` | speedtest-exporter, 1h interval, 90s timeout |

Alert rules in `playbooks/provision/files/monitoring/prometheus/rules/homelab.yml`.
Includes `PBSBackupStale` — fires when any backup group's newest snapshot is too old.

### Grafana

Provisioned dashboards include a dedicated VPS Proxy dashboard (`uid: vps-proxy`) with
CPU, memory, network, Traefik request rate, open connections, access log panel (Loki),
and system journal panel (SSH / fail2ban / WireGuard events).

### Loki

Receives logs from Grafana Alloy on the VPS only. Two log streams:
- `job=traefik-access, host=vps-proxy` — Traefik access logs (JSON, parsed for `router` and `status` labels)
- `job=vps-journal, host=vps-proxy` — systemd journal (unit label from `__journal__systemd_unit`)

### PBS Backup Freshness

`playbooks/provision/pbs.yml` installs a cron script (`/usr/local/bin/pbs-backup-freshness.sh`)
that runs every 15 minutes, writes per-group newest-snapshot timestamps to the node_exporter
textfile collector, and exposes them to Prometheus. The `PBSBackupStale` Prometheus alert
fires when any backup group has not received a new snapshot within the expected window.

---

## VPS (Vultr, 66.42.79.175)

Plan: `vc2-1c-1gb` (1 vCPU, 1 GB RAM), Seattle region, Debian 13 (trixie).

Services (all systemd, no Docker):
- `traefik` — TCP passthrough proxy
- `wg-quick@wg0` — WireGuard tunnel to Docker LXC
- `node_exporter` — scraped by Prometheus over WireGuard
- `alloy` — ships logs to Loki over WireGuard
- `fail2ban` — SSH and Traefik jails

Grafana Alloy config at `/etc/alloy/config.alloy`. Logs ship to `192.168.1.44:3100` (Loki).

---

## IaC Tooling

### Terraform Providers

| Provider | Version | Purpose |
|---|---|---|
| bpg/proxmox-ve | ~> 0.107 | LXC/VM resources, PVE user management |
| gmichels/adguard | ~> 1.7 | AdGuard DNS rewrites (primary only) |
| vultr/vultr | ~> 2.31 | VPS instance, SSH keys |
| cloudflare/cloudflare | ~> 5.0 | External DNS A records |
| hashicorp/null | ~> 3.3 | VPS Ansible provisioning trigger |

State: local (`terraform/terraform.tfstate`), backed up to NAS via rclone (encrypted).
Secrets in `terraform/secrets.tfvars` (gitignored).

### Ansible

Inventory: `inventory/hosts.yml`. All hosts use `~/.ssh/ansible-on-nest` key, root user.
Secrets: `inventory/group_vars/all/vault.yml` (ansible-vault, password in `~/.config/ansible-on-nest/vault-pass`).

`playbooks/site.yml` runs the full converge:
1. `provision/common.yml` — node_exporter, BBR sysctl (all LXCs + VPS)
2. Per-host provision playbooks (adguard, docker-host, vps, fileserver, monitoring, musicbrainz, scrutiny, seedbox, pbs, nftables)
3. `update_apt.yml`, `update_docker.yml`, `update_proxmox.yml`

Terraform triggers Ansible via `local-exec` on resource creation. Subsequent converges run `site.yml` manually.

### Deployment Workflow

**First-time provisioning:**
```bash
cd terraform
terraform apply -var-file=secrets.tfvars   # creates infra + triggers Ansible
```

**Day-to-day:**
```bash
# Config changes
ansible-playbook playbooks/site.yml --ask-vault-pass

# Infrastructure changes
terraform -chdir=terraform apply -var-file=secrets.tfvars

# Diagrams
python3 scripts/generate_diagram.py
```

**Vault operations:**
```bash
ansible-vault edit inventory/group_vars/all/vault.yml
```

---

## Performance Tuning

BBR congestion control is applied to all LXCs and the VPS via `/etc/sysctl.d/99-bbr.conf`:

```
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 262144
net.core.wmem_default = 262144
```

WireGuard MTU is explicitly set to 1420 on both sides of the tunnel to avoid fragmentation.

---

## What Is Not Managed by IaC

| Component | Why | Notes |
|---|---|---|
| Raspberry Pi OS | Hardware, provisioned manually | Ansible manages AdGuard/Unbound config only |
| PBS → PVE storage link | bpg/proxmox-ve has no storage_pbs resource type | Documented in `terraform/pve-storage.tf` |
| UniFi firewall / VLANs | UDM controller UI, no API IaC | Documented in audit summary |
| Home Assistant integrations | HAOS, not config-file driven | |
| rclone Google Drive OAuth | Interactive auth, can't be automated | Must re-authorize on rebuild |
| ProtonVPN WireGuard key | Generated per-device by ProtonVPN | Must regenerate on rebuild |
| Cloudflare zone settings | Only DNS records are managed | |
| TLS certificates | Issued by Traefik ACME, stored in acme.json | Lost on Docker LXC rebuild; re-issued automatically |
| `casa.arishaig.site` | Managed by Nabu Casa | Not in Terraform |
