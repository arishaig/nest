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
| 100 | docker | 192.168.1.158 (static) | Decommissioned (start_on_boot=false); cadvisor still in compose for ad-hoc use |
| 101 | musicbrainz | 192.168.1.197 (DHCP) | MusicBrainz server |
| 102 | fileserver | 192.168.1.17 (static) | Samba NAS |
| 103 | scrutiny | 192.168.1.46 (DHCP) | SMART disk monitoring |
| 104 | seedbox | 192.168.1.182 (DHCP) | qBittorrent behind Gluetun VPN |
| 105 | monitoring | 192.168.1.44 (static) | Prometheus · Grafana · Loki · Alertmanager |
| 106 | dns-secondary | 192.168.7.8 (VLAN 7, static) | AdGuard Home + Unbound (secondary DNS) |
| 108 | ci | 192.168.1.18 (static) | GitHub Actions self-hosted runner |
| 109 | mcp | 192.168.1.19 (static) | Nest MCP HTTP server (port 8765) |
| 111 | foundry | 192.168.1.21 (static) | FoundryVTT game server |

### VMs (Proxmox)

| VMID | Name | IP | Purpose |
|---|---|---|---|
| 107 | homeassistant | 192.168.4.50 (VLAN 4) | Home Assistant OS |
| 110 | talos | 192.168.1.110 | Talos Linux — k8s control plane "alpha" (4 vCPU, 24 GB RAM) |
| 113 | talos-beta-vm | 192.168.1.111 | Talos control plane "beta" (2 vCPU, 4 GB) — temporary test VM until RPi5s arrive |
| 115 | talos-delta-vm | 192.168.1.114 | Talos control plane "delta" (4 vCPU, 8 GB) — temporary test VM until RPi5s arrive |
| 500 | backup | 192.168.1.113 | Proxmox Backup Server |

All three control-plane nodes are schedulable (beta/delta were flipped to rehearse the
multi-node topology ahead of the RPi5 swap). Heavy media workloads are pinned to alpha
(~24 GB) via the `nest.arishaig.site/workloads=general` node label / nodeSelector, so the
small beta/delta nodes only carry control-plane and light pods. The earlier "gamma" test VM
(was VM 114 / .112) was removed during a node-swap rehearsal that validated the
add-then-remove etcd flow for the Pi migration; .112 is now free.

Cluster-level IPs (not VMs): `192.168.1.115` Talos API VIP (port 6443 only — kube-proxy
in nftables mode does not serve NodePorts on it), `192.168.1.116` MetalLB metrics LB
(shared by all exporter services), `192.168.1.117` MetalLB ingress LB (k8s Traefik).

---

## Public Ingress Path

```
Client → Cloudflare DNS → VPS :443
  → Traefik (TCP passthrough, HostSNI(*))
  → WireGuard tunnel (10.10.0.1 → 10.10.0.3 on Talos alpha)
  → MetalLB ingress LB 192.168.1.117:443
  → k8s Traefik :443 (TLS termination, PROXY protocol v2)
  → Authelia (forwardAuth) → k8s service / ExternalName
```

### VPS Traefik (66.42.79.175)

Runs as a systemd service (not Docker). Does **TCP passthrough** — it never
terminates TLS. Any hostname on port 443 is forwarded as-is over the WireGuard
tunnel to the MetalLB ingress LB `192.168.1.117:443` (routed via the Talos alpha
WireGuard peer) using PROXY protocol v2 to preserve the real client IP. Note: the
tunnel terminates on alpha's wg0, so external ingress depends on alpha being up
even though k8s Traefik itself is HA across nodes.

Port 80 → 443 redirect is the only HTTP-layer operation.

Exception: `dns3.arishaig.site` is SNI-routed to local AdGuard Home (DoH on `127.0.0.1:8443`) rather than forwarded over WireGuard. All other hostnames fall through to the WireGuard passthrough.

Metrics endpoint listens on `10.10.0.1:8080` (WireGuard interface, not public).

### WireGuard Tunnel

VPS side: `10.10.0.1/24`, ListenPort 51820, MTU 1420. Two peers:
- **Talos k8s node** (`10.10.0.3/32, 192.168.1.117/32`) — receives all public HTTPS ingress for the MetalLB ingress LB
- **Docker LXC** (`10.10.0.2/32, 192.168.1.44/32`) — retained so the monitoring LXC (192.168.1.44) can reach VPS metrics over WireGuard; does **not** handle public ingress

Talos k8s side: `10.10.0.3/32`, MTU 1420 (managed by Talos config in `talos/` directory)
Docker LXC side: `10.10.0.2/24`, MTU 1420

Keys: public keys stored in `inventory/group_vars/all/vars.yml`; private keys in `vault.yml`.

### k8s Traefik (MetalLB LoadBalancer 192.168.1.117)

Deployed as a k8s Deployment (2 replicas, pod anti-affinity) in the `traefik`
namespace, exposed via a MetalLB L2 LoadBalancer Service at `192.168.1.117`
(ports 80/443/8080). The LB IP floats to whichever node MetalLB elects, so
ingress survives the loss of any single node.

Terminates TLS using Cloudflare ACME DNS challenge (wildcard cert: `*.arishaig.site`),
managed by cert-manager and stored as a k8s Secret.

Trusts PROXY protocol from `10.10.0.1/32`. Rate-limit middleware applied globally.

Uses the `kubernetesCRD` provider — routes are defined as `IngressRoute` CRDs; no Docker socket.

Routes to k8s services within the cluster or to `ExternalName` services for non-k8s targets
(Proxmox, PBS, monitoring, scrutiny, torrent, foundry, glances, musicbrainz, backlight, mcp).

Access logs written as JSON to stdout; shipped to Loki by the k8s Alloy DaemonSet
with `job="traefik-access"` and parsed `router`/`status` labels.

---

## DNS

### External (Cloudflare)

Managed by Terraform (`terraform/cloudflare.tf`, `cloudflare/cloudflare = 5.19.1`).

| Record | Target | Notes |
|---|---|---|
| `*.arishaig.site` | VPS IP | Auto-populated from `vultr_instance.vps_proxy.main_ip` |
| `dns.arishaig.site` | Home IP (50.47.227.169) | Direct to Pi, bypasses VPS |
| `vpn.arishaig.site` | Home IP (50.47.227.169) | Direct to UDM WireGuard |

`cloudflare-ddns` (k8s pod in `cloudflare-ddns` namespace) keeps `vpn.arishaig.site` current when the home IP changes.
All records are unproxied (Cloudflare orange cloud off).

### Internal (AdGuard Home)

Primary: Raspberry Pi at 192.168.7.7, Unbound upstream.
Secondary: LXC 106 at 192.168.7.8 (VLAN 7), its own Unbound upstream, failover from primary.
Tertiary: VPS at 66.42.79.175 (`dns3.arishaig.site`), DoH/DoT only (no plain UDP/53), no local rewrites. For phone/laptop use outside the house — no VPN required.

Internal rewrites (`.arishaig.site` → LAN IPs) managed by Terraform (`gmichels/adguard ~> 1.7`).
Tertiary has no rewrites — public DNS via Unbound resolves `*.arishaig.site` correctly via the Cloudflare wildcard.
cert-manager's wildcard cert covers `.local.arishaig.site` names too — no separate cert
infrastructure needed for internal access.

---

## Authentication

Authelia runs in k8s (`authelia` namespace), exposed at `auth.arishaig.site` via an IngressRoute.
k8s Traefik uses it as a `forwardAuth` middleware (CRD name `authelia` in the `traefik` namespace;
`forwardAuth` address: `http://authelia.authelia.svc.cluster.local:9091/api/authz/forward-auth`).
Valkey runs in k8s (`authelia` namespace) as Authelia's session store.

**Authelia required (most services):** bazarr, copyparty, lidarr, medialyze,
mealie, prowlarr, radarr, recommendarr, sabnzbd, sonarr, storyteller, watchback.

**Authelia bypassed (intentional):**

| Service | Middleware | Reason |
|---|---|---|
| jellyfin | — | Media clients (AppleTV, Kodi etc.) cannot handle auth redirects |
| foundry | — | FoundryVTT clients cannot handle auth redirects |
| seerr | — | Intended for external users to submit requests |
| tunarr | `local-only` (LAN only) | Jellyfin communicates with it directly; no external access |
| watcharr | `local-only` (LAN only) | Login flow breaks with forwardAuth enabled |
| mcp | — | Validates Bearer JWT itself (OIDC); no Authelia layer needed |

**`.local.arishaig.site` routes** also bypass Authelia — these are direct internal-access
routes for services that also have an Authelia-protected public route (torrent, scrutiny, musicbrainz, backlight).

---

## Docker Services (LXC 100)

The vast majority of application services have migrated to Kubernetes (see [Kubernetes](#kubernetes-talos-vm-110) below).
What remains on the Docker LXC:

LXC 100 is decommissioned (`start_on_boot = false`). The compose file is left intact with cadvisor
so the LXC can be started manually and used as a scratch Docker host when needed.

### Compose File (preserved, not auto-running)

| Service | Purpose |
|---|---|
| cadvisor | Container resource metrics (port 8081) — only useful when LXC is manually started |

---

## Kubernetes (Talos VM 110)

Three-node Talos Linux control plane: alpha VM `192.168.1.110` plus temporary test
VMs beta `192.168.1.111` / delta `192.168.1.114` (to be replaced by Raspberry Pi 5s).
All three are schedulable; heavy media pods are pinned to alpha via the
`nest.arishaig.site/workloads=general` node label. API VIP `192.168.1.115` (port 6443
only). Managed by **Flux** watching `k8s/` in this repo — git is the live source of
truth with continuous reconciliation.

Workloads are deployed as Flux `HelmRelease`s using the [bjw-s `app-template`](https://github.com/bjw-s-labs/helm-charts)
chart (one app = one HelmRelease values block, the k8s analogue of a single compose
service); cluster primitives (Traefik, cert-manager, MetalLB, ARC, kube-state-metrics)
use their official upstream charts. See [k8s-helm-migration.md](k8s-helm-migration.md).

Storage classes:
- `nfs-nvme` — nfs-subdir-external-provisioner pointing at PVE `rpool/data/k8s-configs`; used for app configs
- `media-nfs` — NFS mount to fileserver LXC (`192.168.1.17`), the shared `/Tank/media_root`

### Infrastructure (`k8s/infrastructure/`)

| Namespace | Components |
|---|---|
| `traefik` | Traefik v3 ingress controller (MetalLB LB 192.168.1.117:80/443/8080), middlewares, IngressRoute CRDs, ExternalName services |
| `authelia` | Authelia SSO, Valkey session store, redis-exporter and Authelia metrics (shared metrics LB 192.168.1.116:9121/:9959) |
| `metallb-system` | MetalLB L2: `traefik-pool` (192.168.1.117/32) and `metrics-pool` (192.168.1.116/32) |
| `cert-manager` | Wildcard cert from Cloudflare DNS-01 challenge; cert stored as k8s Secret |
| `alloy` | Grafana Alloy DaemonSet — ships all pod logs to Loki; parses Traefik access logs as JSON |
| `local-path-provisioner` | Local path storage class |
| `nfs-provisioner` | `nfs-nvme` StorageClass |

### Apps (`k8s/apps/`)

| Namespace | Services |
|---|---|
| `media` | sonarr+exportarr, radarr+exportarr, lidarr+exportarr, bazarr+exportarr, prowlarr+exportarr, flaresolverr, sabnzbd, seerr, tunarr, watcharr, watchback, homepage, copyparty, medialyze, metube, recommendarr, storyteller, subgen, tdarr+node, mealie+postgres (postgres-exporter hostPort 9187), recyclarr (CronJob), jellyfin |
| `cloudflare-ddns` | cloudflare-ddns — keeps `vpn.arishaig.site` current |
| `headlamp` | Headlamp — Kubernetes UI |

All media app configs use `nfs-nvme` PVCs; media files via `media-nfs` PVC.

### Exportarr metrics (hostPort on Talos)

Prometheus scrapes exportarr sidecars directly via hostPort on the Talos node:

| Exporter | hostPort | Prometheus job |
|---|---|---|
| exportarr-sonarr | 9707 | `sonarr` |
| exportarr-radarr | 9708 | `radarr` |
| exportarr-lidarr | 9709 | `lidarr` |
| exportarr-prowlarr | 9710 | `prowlarr` |
| exportarr-bazarr | 9711 | `bazarr` |
| redis-exporter | 9121 | `redis` |
| postgres-exporter | 9187 | `postgres` |
| Authelia | 9959 | `authelia` |
| Traefik | 8080 | `traefik-k8s` |

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
| `adguard` | Primary + secondary + tertiary AdGuard exporters (`:9618`) |
| `unbound` | Primary + secondary + tertiary Unbound exporters (`:9167`) |
| `unpoller` | UniFi metrics via unpoller container |
| `sonarr/radarr/lidarr/prowlarr/bazarr` | exportarr sidecars via shared metrics LB (`192.168.1.116:9707-9711`) |
| `traefik-k8s` | k8s Traefik metrics via ingress LB (`192.168.1.117:8080`) |
| `authelia` | Authelia metrics via metrics LB (`192.168.1.116:9959`) |
| `redis` | redis-exporter via metrics LB (`192.168.1.116:9121`) |
| `postgres` | postgres-exporter via metrics LB (`192.168.1.116:9187`) |
| `qbittorrent` | qbittorrent-exporter on seedbox LXC (`192.168.1.182:9022`) |
| `scrutiny` | SMART metrics API |
| `homeassistant` | HA Prometheus integration (bearer token in vault) |
| `wled` | WLED LED controller at `backlight.arishaig.site` |
| `speedtest` | speedtest-exporter, 1h interval, 90s timeout |
| `blackbox` | HTTP probes for all public-facing services (60s interval) |

Alert rules in `playbooks/provision/files/monitoring/prometheus/rules/nest.yml`.
Includes `PBSBackupStale` — fires when any backup group's newest snapshot is too old.

### Grafana

Provisioned dashboards: infrastructure health, network/DNS, services overview, and a dedicated
VPS Proxy dashboard (`uid: vps-proxy`) with CPU, memory, network, Traefik request rate,
open connections, access log panel (Loki), and system journal panel (SSH / fail2ban / WireGuard events).

### Loki + Ruler

Loki runs on the monitoring LXC and receives logs shipped by Grafana Alloy from every host.
Retention is 30 days. The Loki Ruler evaluates log-based alert rules and sends to Alertmanager.

**Log streams by host:**

| Host | Streams |
|---|---|
| vps-proxy | `traefik-access` (JSON, parsed for `router`/`status` labels), `traefik-app` (logfmt), `vps-journal` (systemd) |
| talos | k8s pod logs (all namespaces); `traefik` container parsed as `job=traefik-access` with `router`/`status` labels |
| docker | `docker` (all container stdout), `journal` |
| monitoring | `docker` (all container stdout), `journal` |
| seedbox | `docker` (gluetun, qbittorrent, cadvisor), `journal` |
| scrutiny | `docker`, `journal` |
| musicbrainz | `docker`, `journal` |
| dns-secondary | `docker` (adguard-exporter), `journal` |
| fileserver | `journal` |
| mcp | `journal` |
| pbs | `journal` |
| pve | `journal` |
| ci | `journal` |
| adguard (Pi) | `journal` |

Alloy is deployed via `playbooks/provision/alloy.yml` for LXC/VPS hosts and via `k8s/infrastructure/alloy/` for Talos. Four Alloy configs:
- `monitoring.alloy.j2` — monitoring LXC (Docker discovery + journal)
- `docker-host.alloy.j2` — Docker hosts (Docker discovery + journal + optional file tails)
- `journal-only.alloy.j2` — non-Docker hosts (journal only)
- k8s DaemonSet (`k8s/infrastructure/alloy/config-map.yaml`) — Talos: reads pod log files from `/var/log/pods/`, parses CRI format, ships with namespace/pod/container labels; Traefik access logs tagged `job=traefik-access`

**Ruler alert groups:** `log_ingestion`, `systemd`, `traefik`, `loki`, `security`, `hardware`, `media`.
Alert rules live in `playbooks/provision/files/monitoring/loki/ruler-rules/fake/homelab.yml`.

### PBS Backup Freshness

`playbooks/provision/pbs.yml` installs a cron script (`/usr/local/bin/pbs-backup-freshness.sh`)
that runs every 15 minutes, writes per-group newest-snapshot timestamps to the node_exporter
textfile collector, and exposes them to Prometheus. The `PBSBackupStale` Prometheus alert
fires when any backup group has not received a new snapshot within the expected window.

---

## VPS (Vultr, 66.42.79.175)

Plan: `vc2-1c-1gb` (1 vCPU, 1 GB RAM), Seattle region, Debian 13 (trixie).

Services (all systemd, no Docker):
- `traefik` — TCP passthrough proxy (SNI routes `dns3.arishaig.site` to local AdGuard)
- `wg-quick@wg0` — WireGuard tunnel; two peers: Talos k8s node (10.10.0.3 + ingress LB 192.168.1.117, HTTPS ingress) and monitoring LXC (10.10.0.2, monitoring reach)
- `AdGuardHome` — tertiary DNS, DoH (:8443) + DoT (:853), Unbound upstream
- `unbound` — recursive resolver on `127.0.0.1:5335`
- `node_exporter` — scraped by Prometheus over WireGuard (`10.10.0.1:9100`)
- `unbound_exporter` — scraped by Prometheus over WireGuard (`10.10.0.1:9167`)
- `alloy` — ships Traefik access logs + app log + systemd journal to Loki over WireGuard
- `fail2ban` — SSH and Traefik jails

Docker (single container):
- `adguard-exporter` — AdGuard metrics, scraped by Prometheus over WireGuard (`10.10.0.1:9618`)

Grafana Alloy config at `/etc/alloy/config.alloy`. Logs ship to `192.168.1.44:3100` (Loki).

---

## MCP Server (LXC 109)

`nest-mcp` is an HTTP MCP server exposing live homelab state to AI assistants (Claude Code).
Runs on port 8765, exposed externally at `https://mcp.arishaig.site` behind Authelia OIDC.

Tools cover: Proxmox (VMs/LXCs/tasks/snapshots), PBS backups, Docker (containers/logs),
Home Assistant entities, UniFi (clients/devices/firewall), AdGuard (rewrites/query log/stats),
Prometheus (queries/alerts/targets), Loki (log queries), Jellyfin, *arr stack, Mealie,
Jellyseerr, Scrutiny, seedbox, VPS (nftables/fail2ban/WireGuard/Vultr), and a `lab_health_summary`
tool that gives a full live snapshot of the homelab in one call.

---

## CI / CD

GitHub Actions runs on two runner pools, split by blast radius:

- **`arc-lint`** — ephemeral Kubernetes pods via Actions Runner Controller (ARC) in the
  `arc-runners` namespace (image `ghcr.io/arishaig/nest-ci-runner:latest`, `minRunners: 0`).
  Stateless PR lint/validate jobs run here. See [arc-runners.md](arc-runners.md).
- **LXC 108 `ci` (`192.168.1.18`), `self-hosted`** — the recovery-critical deploy jobs that
  hold OpenTofu state, secrets, and the kubeconfig deliberately stay on the dedicated LXC, so
  the cluster's deploys never depend on the cluster being up. Has no Docker daemon; ansible
  vault password is on the runner for `--ask-vault-pass`-free runs.

### Workflows

| Workflow | Trigger | Runs on | What it does |
|---|---|---|---|
| `lint.yml` | every push / PR | `arc-lint` | `tofu validate`, `ansible-lint`, `yamllint`, `shellcheck` 0.10.0, `promtool`/`amtool` rule checks, `kubeconform` (k8s-validate), **helm-render** (flux-local renders every HelmRelease → kubeconform + `check-helm-pvc-safety.sh`), **talos-config-validate** (`talosctl gen config`/`validate -m metal` over `talos/patches/`), deploy-coverage + RPi5 overlay checks |
| `integration.yml` | PR/push touching `talos/**`, `k8s/**`, tfvars | `ubuntu-latest` | Boots a **Talos-in-Docker** cluster (`talosctl cluster create docker`), waits for nodes `Ready`, then server-side dry-run applies the rendered manifests against the ephemeral API |
| `mcp-tests.yml` | PR touching `mcp/**` | `ubuntu-latest` | `pytest` for `nest_mcp` (86 tests, ~92% coverage, `--cov-fail-under=85`) + verifies the committed `assets/coverage.svg` badge is current |
| `deploy.yml` | push to `main` | `self-hosted` (+ `ubuntu-latest` image builds) | `deploy-tofu`, `deploy-k8s` (Flux reconcile), per-host ansible deploys, and image builds (`build-mcp`, `build-lidarr-ui`, `build-ci-runner`) |
| `docs.yml` | push to `main` touching inventory/`lxc-*.tf`/diagram script | `self-hosted` | Regenerates `docs/architecture*.png` from `scripts/generate_diagram.py` and commits if changed |

The unit tier (helm-render + talos-config-validate) proves the manifests/machine configs are
well-formed; the Docker integration tier proves a cluster actually forms and a node joins. The
QEMU-only multi-control-plane etcd quorum rehearsal is out of scope for CI (the talosctl docker
provisioner is single-control-plane) and stays a manual runbook step.

> New tooling baked into the `arc-lint` image (`ci/runner/Dockerfile`) requires a merge-first
> image-rebuild PR, because `build-ci-runner` only runs on push to `main`. Keep
> `ci/runner/Dockerfile` and `playbooks/provision/runner.yml` in sync (LXC-runner parity).

---

## IaC Tooling

### OpenTofu Providers

| Provider | Version | Purpose |
|---|---|---|
| bpg/proxmox-ve | = 0.108.0 | LXC/VM resources, PVE user management |
| gmichels/adguard | = 1.7.0 | AdGuard DNS rewrites (primary only) |
| vultr/vultr | = 2.31.2 | VPS instance, SSH keys |
| cloudflare/cloudflare | = 5.19.1 | External DNS A records |
| hashicorp/null | = 3.3.0 | VPS Ansible provisioning trigger |

State: local (`terraform/terraform.tfstate`), backed up to NAS via rclone (encrypted).
The state filename is unchanged under OpenTofu (`tofu` defaults to `terraform.tfstate`).
Secrets in `terraform/secrets.tfvars` (gitignored).

### Ansible

Inventory: `inventory/hosts.yml`. All hosts use `~/.ssh/ansible-on-nest` key, root user.
Secrets: `inventory/group_vars/all/vault.yml` (ansible-vault, password in `~/.config/ansible-on-nest/vault-pass`).

`playbooks/site.yml` runs the full converge:
1. `provision/common.yml` — node_exporter, BBR sysctl (all LXCs + VPS)
2. Per-host provision playbooks (adguard, docker-host, vps, fileserver, monitoring, musicbrainz, scrutiny, seedbox, pbs, nftables, mcp, foundry)
3. `alloy.yml` — Grafana Alloy on all hosts
4. `update_apt.yml`, `update_docker.yml`, `update_proxmox.yml`

OpenTofu triggers Ansible via `local-exec` on resource creation. Subsequent converges run `site.yml` manually.

### Flux GitOps (k8s)

`k8s/` is watched by Flux running in the `flux-system` namespace on Talos. Any commit to `main` that changes `k8s/` is automatically reconciled into the cluster — no manual `kubectl apply` needed.

Talos cluster config lives in `talos/`. Bootstrap: `scripts/bootstrap-talos.sh` after initial `tofu apply`.

### Deployment Workflow

**First-time provisioning:**
```bash
cd terraform
tofu apply -var-file=secrets.tfvars   # creates infra + triggers Ansible
```

**Day-to-day:**
```bash
# Config changes
ansible-playbook playbooks/site.yml --ask-vault-pass

# Infrastructure changes
tofu -chdir=terraform apply -var-file=secrets.tfvars

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
| TLS certificates | Issued by cert-manager (Cloudflare DNS-01), stored as k8s Secret | Lost on Talos cluster rebuild; re-issued automatically |
| `casa.arishaig.site` | Managed by Nabu Casa | Not in Terraform |
| Tertiary AdGuard TLS config | Provider bug: switches to https mid-apply via tunnel | Configured once via web UI; cert managed by certbot |
