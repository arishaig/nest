# Architecture Review — 2026-07-30

A point-in-time architectural assessment of the lab, based on live state (Nest MCP)
cross-checked against the repository. Scope is **infrastructure topology and the CI /
supply-chain layer**; application-internal code quality in `mcp/`, `lidarr-ui/` and the
anagnorisis submodule is out of scope, though how they are built and shipped is not.

This review supersedes the current-state sections of [`design.md`](design.md) and the
status header of [`k8s-migration.md`](k8s-migration.md), both corrected in the same
change. Findings below Medium severity are logged in [`tech-debt.md`](tech-debt.md)
rather than discussed here.

Findings are organised on two axes rather than by directory, because a per-directory
list produces a lint report rather than an architecture review:

- **Spine A — reconcilability:** is this state declared in git and reconciled, or does
  it exist only on a disk somewhere?
- **Spine B — blast radius:** what fails, and for how long, when one component dies?

---

## Executive summary

The lab is **healthy but not resilient**. Everything is running, both ZFS pools are
clean and recently scrubbed, external exposure is tight, and the alerting rules are
noticeably better-considered than most homelabs. Nothing here is on fire.

The two structural problems are that **the lab cannot be rebuilt from git alone**, and
that **losing the Proxmox host is data loss rather than an outage**. Both are invisible
day-to-day, which is precisely why they warrant a review.

Three overlapping migrations — Compose → Kubernetes, multi-node control plane → single
dedicated CP VM, raw Kustomize → bjw-s Helm — are each partly done, and their residue
accounts for most of the Medium findings.

**Answering the two framing questions:**

| Question | Today's answer |
|---|---|
| Can the lab be rebuilt from git + vault? | **No.** WireGuard ingress, all k8s Secrets, and OpenTofu state live outside git. |
| What survives losing PVE? | **Two Raspberry Pis with no API server, no storage, and no backups.** |

### Remediation status

> Added 2026-07-30 during remediation. The findings below are preserved as
> written, because a dated review that gets edited to match later knowledge
> stops being evidence of anything. This block records what changed.

**Two findings did not survive verification and should not be "fixed":**

| # | Status | Evidence |
|---|---|---|
| D2 | **Retracted** | The finding assumed two kube-state-metrics replicas, one able to sit `Failed` while the other served scrapes. Live: `kube_deployment_spec_replicas` is **1** and available is **1**. At a single replica there is no half-dead state — `up` goes to 0 and `KubeStateMetricsDown` fires correctly. The `Failed` KSM pod was an evicted orphan, covered by D1's fix. `KubeDeploymentReplicasMismatch` already covers shortfall for every deployment if KSM is ever scaled up. |
| D4 | **Retracted** | The check already used `/ready` — the change the finding recommended. It currently reports `"loki": "ok"` and `/ready` returns 200. The observed 503 was transient (Loki returns 503 while its ingester starts). |

**Corrections to findings that do stand:**

- **D1** is real but was *not* the "one-word fix" described. Verified
  `max_over_time` and `min_over_time` of the `Failed` count are both 4 over 7
  days before making the change.
- **A2** overstated live memory pressure. The 18 kubelet evictions were a single
  past event — `min_over_time` over 7d is also 18, and no node currently reports
  `MemoryPressure`. The *mechanism* is real: memory requests on alpha total
  41.6% of allocatable against ~90% actual usage, so the scheduler overpacks it.
- **F1** was worse than recorded. Both the Renovate manager and the CI overlay
  gate matched `controlplane-*-rpi5.yaml` — files no node uses — so the version
  gate validated dead config and stayed green while the live worker patches
  froze a full release behind.
- **F4** was half-done: `redis-pvc.yaml` already requested `local-path`
  explicitly, so only the default annotation needed moving.
- **B4**'s open question is answered: `Tank/media_root` is deliberately
  unprotected. See [`disaster-recovery.md`](disaster-recovery.md).

**Findings the review missed**, surfaced while verifying it:

- 18 pods carried `reason="TerminationByKubelet"` with no alerting at all.
- `pbs.yml` had no path filter and no deploy job — the only playbook outside the
  deploy pipeline, so no PBS change could ship under the GitOps rule.
- `lab_health_summary` reported every PBS datastore as 0.0 GB, having queried
  `/admin/datastore` (config, no usage fields) instead of
  `/status/datastore-usage`.
- `rpi5-talos.md` cited the bare `rpi_5` schematic while the patches pin the one
  bundling `nfs-utils` — following its upgrade command would have stripped NFS
  support from a Pi.
- `PBSBackupStale` cannot detect a backup that has *never* run, since
  `time() - <absent series>` yields nothing.

### Findings by severity

| # | Finding | Spine | Severity |
|---|---|---|---|
| B1 | k8s PVC data is not backed up by anything | B | **Critical** |
| B2 | PBS datastore is a zvol on the pool it backs up | B | **Critical** |
| C1 | WireGuard ingress on alpha exists only outside git | A | **Critical** |
| A1 | PVE is a single point of failure for control plane, storage, and backups | B | **Critical** |
| A2 | VM 110 is memory-saturated and it is the only node showing probe churn | B | High |
| B3 | `cold-backup` / `cold-storage` are neither cold nor separate | B | High |
| C2 | All k8s Secrets are pushed out-of-band by Ansible | A | High |
| C3 | OpenTofu state is unlocked and exists in three uncoordinated copies | A | High |
| D1 | Pods in `Failed` phase match no alert rule | B | High |
| D2 | `KubeStateMetricsDown` cannot see a half-dead kube-state-metrics | B | High |
| E1 | 15 NodePort services + zero NetworkPolicies bypass Traefik and Authelia on the LAN | B | High |
| G1 | No `tofu plan` in CI; infra changes are first executed by apply-on-main | A | High |
| G2 | Talos upgrades touch only one of four nodes | A | High |
| A3 | Workload placement is gravitational rather than deliberate | B | Medium |
| B4 | No evidence a restore has ever been tested | B | Medium |
| C4 | `nest-mcp` is deployed twice; the LXC copy is unmanaged | A | Medium |
| C5 | No bootstrap-order documentation | A | Medium |
| D3 | Observability plane runs outside the cluster it watches | B | Medium |
| D4 | `lab_health_summary` reports a false Loki failure | B | Medium |
| E2 | Single wildcard certificate is the sole TLS dependency for all 35 routes | B | Medium |
| E3 | Authentication posture is not uniform across ingress routes | B | Medium |
| F1 | Contradictory Talos machine configs with no declared authority | A | Medium |
| F2 | Every BestEffort pod in the cluster is a cluster primitive | B | Medium |
| F3 | No `dependsOn` between infrastructure and apps | A | Medium |
| F4 | `local-path` is the default StorageClass but has one consumer | A | Medium |
| F5 | cert-manager and MetalLB are vendored by remote URL | A | Medium |
| F6 | Decommissioned LXC 100 is still referenced across four layers | A | Medium |
| G3 | `lidarr-ui` is built and shipped with no tests and no workflow | A | Medium |

---

## 1. Compute topology and single-host blast radius

### A1 — PVE is a single point of failure for control plane, storage, and backups · **Critical** · Spine B

Verified against `proxmox_list_vms`, `k8s_pods` and `proxmox_disk_topology`:

| On PVE (192.168.1.16) | Consequence of host loss |
|---|---|
| VM 112 `talos-alpha-control` | Sole control plane, sole etcd member — cluster API gone |
| VM 110 `talos-alpha` (57 pods) | Every heavy workload gone |
| `rpool/data/k8s-configs` | 23 of 25 PVCs unreachable |
| `Tank/media_root` (17.8 TB) | `media-nfs` PV unreachable; Samba shares too |
| VM 500 `backup` (PBS) | Backups gone — see B2 |
| VM 107 `homeassistant` | Home automation gone |

Only `talos-beta-rpi5` and `talos-gamma-rpi5` are physically independent, and both are
useless in isolation: no API server, no storage, no ingress.

Note that **PVE itself is the NFS server for both datasets**, not the fileserver LXC.
`playbooks/provision/templates/pve/exports.j2` exports `/Tank/media_root` and
`/rpool/data/k8s-configs` directly from the host, which is why
`k8s/apps/media/media-nfs-pv.yaml` points at `192.168.1.16`. The fileserver LXC (102)
receives `/Tank/media_root` as a Proxmox bind mount
(`terraform/lxc-fileserver.tf:58-59`) and re-serves it over **Samba** — it is not in the
cluster's storage path at all. `k8s-migration.md` previously described this incorrectly
and is corrected in this change.

This is a **regression** against the earlier three-node control-plane topology. The
2026-07-22 consolidation onto a dedicated CP VM solved a real problem (etcd instability
on memory-pressured nodes) but moved the cluster from three failure domains to one.

**Recommendation.** Decide deliberately between two coherent end states, rather than
leaving the current implicit one:

1. **Accept it.** Document PVE as a single failure domain with a stated RTO, and put the
   engineering into restore speed (B1–B4) rather than availability.
2. **Restore quorum.** Return beta/gamma to control-plane roles for a three-member etcd
   spanning two physical machines. Costs RPi5 headroom; the machine configs already exist
   (see F1).

Option 1 is the honest default for a homelab and is cheaper — but it is only viable once
the backup findings are closed, because today "accept the outage" silently means "accept
the data loss."

### A2 — VM 110 is memory-saturated, and it is the only node showing probe churn · High · Spine B

`talos-alpha` runs at 35.6 GB of 40 GB allocated, and PVE overall sits at 80.9/94.2 GB
with a load average of 10.9 across 12 cores. All observed probe failures are on this one
node (`k8s_events`): sonarr readiness failed **1048 times**, lidarr 141, prowlarr 41,
plus MetalLB speaker liveness timeouts against 192.168.1.110. The RPi5 nodes and the
control-plane VM show none.

This is the classic signature of a node that is not failing, just permanently short of
headroom — and it matches the chronic pressure noted since the single-node period. ZFS
ARC is capped at 8 GB and currently uses only 3.2 GB at a 99.5% hit rate, so ARC is not
the pressure source and reclaiming its cap would buy little.

**Recommendation.** Rebalance rather than resize — PVE has no memory left to give
(A1/A3). See A3.

### A3 — Workload placement is gravitational rather than deliberate · Medium · Spine B

Pod distribution is 57 / 24 / 15 / 11 across alpha / gamma / beta / alpha-control. Some
concentration is intentional and correctly expressed — four workloads are hard-pinned to
alpha for memory (`sabnzbd` 12Gi, `anagnorisis` 6Gi, `subgen` 4Gi, `tdarr-node` 4Gi), and
`jellyfin` and `tunarr` carry `amd64` nodeSelectors they genuinely need.

The rest is drift. Eight workloads carry a *soft* preference for
`nest.arishaig.site/workloads=general`, which under memory pressure keeps electing the
most loaded node. Around eighteen HelmReleases have no placement constraint at all, and
the asymmetries look accidental rather than reasoned:

- `prowlarr` is the only \*arr without the alpha preference, while its four siblings have it.
- `mealie` is unconstrained while its `postgres` backend prefers alpha — app and database
  can land on different nodes.
- `postgres-exporter` is unconstrained while scraping a node-affine postgres.
- `talos/patches/worker-gamma-rpi5.yaml` sets a `workloads: rpi5` label that nothing consumes.

**Recommendation.** Audit the eighteen unconstrained workloads and either give arm64-safe
ones an explicit preference toward the Pis, or state in the patch comments that placement
is deliberately free. The `workloads: rpi5` label is the natural mechanism and already
exists unused.

---

## 2. Backup and restore recoverability

This is the workstream that turns finding A1 from "degraded" into "unrecoverable," and
it is the most important section of this review.

### B1 — k8s PVC data is not backed up by anything · **Critical** · Spine B

`proxmox_backup_status` shows PBS jobs covering guests only — `ct/100`–`ct/111` and
`vm/107`, `vm/110`, `vm/112`. All succeed, and the freshness exporter confirms they are
recent.

But the 23 `nfs-nvme` PVCs do not live inside any guest. They live at
`rpool/data/k8s-configs` on the **host**, created at `playbooks/provision/pve.yml:73` and
exported at `templates/pve/exports.j2:8`. PBS backs up guests, not host datasets. A
repository-wide search for `zfs send`, syncoid, sanoid, zrepl, restic, borg or an rsync
backup job returns **nothing**, and `playbooks/provision/pbs.yml` defines no sync, remote,
prune or verify job — only the freshness exporter.

Everything stateful the cluster owns is therefore protected by exactly one thing: ZFS
`Retain` on a single pool. That includes the postgres database, Authelia's user store and
session state, every \*arr database, jellyfin metadata and watch history, and all app
configuration.

A pool loss, an accidental `kubectl delete pvc` followed by the reclaim policy being
changed, or a bad `pct`/`zfs destroy` takes all of it with no recovery path.

**Recommendation.** Add a dataset-level backup of `rpool/data/k8s-configs` to PBS or to a
`zfs send` target. This is small, high-churn, high-value data — the highest
value-per-byte in the lab. `Tank/media_root` is a separate decision (see B4).

### B2 — The PBS datastore is a zvol on the pool it backs up · **Critical** · Spine B

`terraform/vm-pbs.tf:31-34` defines VM 500's only disk as
`datastore_id = "local-zfs"`, `interface = "scsi0"`, `size = 500` — a 500 GB zvol on
`rpool`, matching the `pbs-local` datastore at 483.7 GB and the `zd64` zvol visible in
`proxmox_disk_topology`. PBS has **no passthrough disk**; this is confirmed from the VM
definition, not inferred from size.

So the backups of every guest sit on the same pool as the host being backed up. PBS
protects against guest-level mistakes — a bad upgrade, a broken container, an accidental
deletion — which is real value, and its GC status is OK. It provides no protection
whatsoever against loss of `rpool` or of the machine.

Combined with B1, the honest summary is: **PVE loss is total, unrecoverable data loss for
everything except the media library.**

**Recommendation.** Establish one target outside the host — a PBS remote sync to another
machine, or a `zfs send` to `Tank` as a weaker intermediate step that at least survives
rpool loss. Even a periodic manual sync to external media would change the failure mode
from "total" to "bounded."

### B3 — `cold-backup` and `cold-storage` are neither cold nor separate · High · Spine B

`proxmox_storage_status` reports `local`, `local-zfs`, `cold-backup` and `cold-storage`
all with `avail_gb: 323.25` — byte-identical free space, because all four are directories
on the same `rpool`. Both "cold" stores hold 7.07 GB (2.1% used).

The naming actively misleads: it reads as offsite or offline capacity during exactly the
incident where someone would reach for it. This is a documentation and naming problem
rather than a technical one, but it is the kind that costs time in an emergency.

**Recommendation.** Rename to reflect reality (`local-archive`), or repoint them at
genuinely separate media. Do not leave them named as they are.

### B4 — No evidence a restore has ever been tested · Medium · Spine B

`playbooks/provision/files/pbs/pbs-backup-freshness.sh` verifies backups are *recent*. No
mechanism verifies they are *restorable*, and there is no restore runbook.

Both pools are healthy — Tank raidz2 and rpool mirror, zero errors, both scrubbed
2026-07-12 — so this is a design gap, not a current fault.

**Recommendation.** Restore one LXC to a scratch VMID and time it; write down the number.
That number is the lab's real RTO. While doing so, settle explicitly whether
`Tank/media_root` (17.8 TB) is deliberately unprotected as re-acquirable bulk data — that
is a defensible position, but it is currently undocumented, which makes it an assumption
rather than a decision.

---

## 3. Rebuild-from-git gaps

The repository presents as pure GitOps. Four categories of state break that promise.

### C1 — WireGuard ingress on alpha exists only outside git · **Critical** · Spine A

`talos/patches/wireguard-alpha.yaml.example` is a template; the real interface exists only
as out-of-band machine config applied by hand. Its own header documents that a
`talosctl reset` on 2026-07-21 silently wiped it and took down all external ingress.

`unifi_port_forwarding` returns **empty** — there are no port forwards at all. This tunnel
is therefore the *only* path into the lab from the internet, and it is the single piece of
configuration least able to survive a node rebuild.

**Recommendation.** This is the highest-value fix in the review relative to effort. Either
move the key into vault and have `playbooks/provision/k8s.yml` apply the patch as part of
normal convergence, or add an explicit pre-flight check to the reset/rebuild runbook.
Reconciling it properly is better; documenting it is the minimum.

### C2 — All k8s Secrets are pushed out-of-band by Ansible · High · Spine A

`playbooks/provision/k8s.yml` creates `postgres-secret`, `authelia-config`,
`cloudflare-api-token`, `subgen-secrets`, `nest-mcp-secrets`, `nest-mcp-ssh-key`,
`arc-github-secret` and the `*-api-key` set from ansible-vault. There is no SOPS, no
sealed-secrets, no External Secrets Operator, and zero encrypted material in `k8s/`.

This is a push-based step inside a pull-based reconciliation loop. On a fresh cluster Flux
reconciles happily and every HelmRelease referencing `existingSecret` blocks indefinitely
until an operator remembers to run the playbook. Nothing declares that ordering.

**Recommendation.** SOPS with an age key in vault would close this properly and is the
smallest change that makes `k8s/` self-sufficient. Short of that, C5 must cover it.

### C3 — OpenTofu state is unlocked and exists in three uncoordinated copies · High · Spine A

No `backend` block exists. State lives on the workstation, on the CI runner at
`/opt/terraform-state/nest`, and as a vault-encrypted copy on the NAS via
`scripts/backup-state.sh`. There is no locking of any kind.

The repository already carries the scar: `deploy.yml`'s `deploy-tofu` job hard-fails if
the state file is missing, with a comment recording that applying against empty state
once duplicated every AdGuard rewrite and orphaned a VM. That guard prevents the
empty-state case; it does nothing about a concurrent workstation and CI apply, which would
still silently clobber each other.

**Recommendation.** A locking backend. Given the constraints, the CI runner's own
filesystem plus a lock, or an S3-compatible target with conditional writes, both work.
The failure mode here is silent and expensive.

### C4 — `nest-mcp` is deployed twice; the LXC copy is unmanaged · Medium · Spine A

`terraform/lxc-mcp.tf` provisions LXC 109 and `playbooks/provision/mcp.yml` installs the
server there into `/opt/nest-mcp`. Meanwhile `k8s/apps/nest-mcp/helmrelease.yaml` runs
three replicas in-cluster, and the `deploy-mcp` CI job targets only the k8s copy. LXC 109
is running (110 MB / 512 MB) and is re-converged by nothing — neither `site.yml` nor
`deploy.yml` touches it.

It is a live, drifting copy of a service that is authoritatively deployed elsewhere.

**Recommendation.** Decide which is canonical. If k8s, decommission LXC 109 the way LXC
100 was; if the LXC is a deliberate fallback for when the cluster is down — a genuinely
reasonable design for the tool used to diagnose outages — document that and re-converge it
on a schedule.

### C5 — No bootstrap-order documentation · Medium · Spine A

Findings C1–C3 share a root cause: the cold-start sequence exists only in operators'
heads. `scripts/bootstrap-talos.sh` covers cluster creation, but nothing states the full
order — tofu apply, Talos bootstrap, the WireGuard patch, the Ansible secret push, Flux
bootstrap — or which steps block which.

**Recommendation.** One ordered page. It is the cheapest item in this review and it is
what makes C1–C3 survivable in the interim.

---

## 4. Observability truthfulness

The alerting rules in `playbooks/provision/files/monitoring/prometheus/rules/nest.yml` are
genuinely well-designed. The comments explain thresholds, cite the incidents that motivated
them, and show deliberate noise-reduction work — `KubePodCrashLooping` is set at >4 restarts
in 30m specifically because the previous >2-in-15m form fired for 71 hours across 50 pods
from one underlying node condition.

The findings below are two narrow gaps in an otherwise strong system, not a general
absence of coverage.

### D1 — Pods in `Failed` phase match no alert rule · High · Spine B

Four pods are currently in `Failed` phase, one for nine days:
`kube-state-metrics/kube-state-metrics-f4b8f4ddb-nd5cc`,
`media/anagnorisis-5586c69b68-hkl7m`, `media/seerr-5bf5b4bc85-8lkw4`,
`media/subgen-7d5bc8ccd7-mpc5m`. Prometheus reports only `Watchdog` firing.

Each rule misses them for a different and individually reasonable reason:

- `KubePodCrashLooping` keys on `increase(...restarts_total[30m]) > 4`. These pods are not
  restarting — they are stopped. Their counters are static.
- `KubePodNotReady` matches `phase=~"Pending|Unknown"`. **`Failed` is not in the regex.**
- `KubeDeploymentReplicasMismatch` requires a replica shortfall, and there is none —
  `kube_deployment_spec_replicas > kube_deployment_status_replicas_available` returns
  empty. Each Deployment has a healthy replica elsewhere; the Failed pods are surplus.

So a pod can fail and remain failed indefinitely while every rule correctly declines to
fire. The data is present — `kube_pod_status_phase{phase="Failed"}` returns all four — it
is simply unqueried.

**Recommendation.** Add `Failed` to the `KubePodNotReady` regex. One-word fix; the
`for: 30m` already suppresses transient churn.

### D2 — `KubeStateMetricsDown` cannot see a half-dead kube-state-metrics · High · Spine B

`KubeStateMetricsDown` fires on `avg_over_time(up{job="kube-state-metrics"}[30m]) < 0.5`.
But `count(up{job="kube-state-metrics"})` returns **1** — a single scrape target behind
the MetalLB shared metrics IP, served by whichever replica is healthy.

One of the two KSM replicas has been `Failed` for four days. The scrape target never went
down, so the rule never fired — even though the rule's own comment identifies KSM as the
component whose failure blinds all pod-state alerting.

The alert is guarding the metrics *endpoint*, not the *workload*. In a single-replica
deployment those coincide; here they do not.

**Recommendation.** D1's fix covers this case incidentally, since the Failed KSM pod would
then alert. A more direct guard is a rule on KSM's own replica availability.

### D3 — Observability plane runs outside the cluster it watches · Medium · Spine B

Prometheus, Loki, Grafana and Alertmanager run in LXC 105, scraping the cluster over a
MetalLB shared IP. This survives cluster loss, which is a real and probably deliberate
benefit — but it is not written down as a decision anywhere, and it costs fidelity: no
in-cluster service discovery, no scrape of etcd or the API server, and a hard dependency
on 192.168.1.116 staying assigned.

It does *not* survive PVE loss (A1), so it is not a genuinely independent failure domain.

**Recommendation.** Record it as an intentional choice in `design.md` with its trade-offs,
or fold it into the deferred monitoring-stack migration. Either is fine; the current
ambiguity is what should not persist.

### D4 — `lab_health_summary` reports a false Loki failure · Medium · Spine B

`lab_health_summary` reports `loki: error: 503`, while the `blackbox-internal` probe of
`loki:3100/ready` is up, the `loki` scrape job is up, and `loki_logs` queries return
current data. Loki is fine; the MCP health check is hitting the wrong path.

This matters more than a cosmetic bug: `lab_health_summary` is the first call of every
session, and a standing false negative teaches the reader to discount it — which is
precisely the wrong reflex for the tool used to triage outages.

**Recommendation.** Point the check at `/ready`, as the blackbox probe already does.

---

## 5. Ingress, authentication and network exposure

**The external surface is tight, and this is the strongest area of the lab.** There are no
UniFi port forwards at all; the sole ingress path is the Vultr VPS over WireGuard. VPS
nftables runs `policy drop` with only 22, 80/443, 51820 and 853 accepted, and exporters
restricted to `iifname wg0`. Geo-blocking and per-IP block rules are in place upstream.
Nothing below contradicts that.

### E1 — 15 NodePort services and zero NetworkPolicies bypass Traefik and Authelia on the LAN · High · Spine B

Fifteen manifests in `k8s/apps/media/` declare NodePort services — including `jellyfin`,
`sabnzbd`, `mealie`, `watcharr`, `tunarr`, `copyparty` and `storyteller` — and there are
**no NetworkPolicies anywhere** in the tree outside Flux's own components.

UniFi rules permit broad LAN-to-infrastructure traffic (`Allow Internal to Infra`,
`Allow Trunk to Monitoring`, both zone-wide `all`-protocol allows). A client on the main
LAN can therefore reach any of these services directly on a node IP and port, bypassing
Traefik entirely — and with it Authelia forwardAuth, the rate-limit middleware, and TLS.

To be precise about the boundary: **this is not internet-reachable.** It is a LAN-side
bypass of the authentication layer, which matters for IoT devices, guest access, and any
compromised client on the main VLAN. The `local-only` middleware that protects `tunarr`
and `watcharr` is an `ipAllowList` for `192.168.0.0/16` — the same range that can reach
the NodePorts directly, so it adds nothing against this path.

**Recommendation.** For each of the fifteen, decide whether the NodePort is still needed —
several look like migration-era debugging aids that outlived their purpose — and remove
those that are not. NetworkPolicies would be the thorough fix, but simply deleting unused
NodePorts closes most of the gap for far less work.

### E2 — Single wildcard certificate is the sole TLS dependency for all 35 routes · Medium · Spine B

One `Certificate` (`wildcard-arishaig-site`, `*.arishaig.site` plus apex) is surfaced
through a default `TLSStore`, so all 35 IngressRoutes use bare `tls: {}`. This is clean
and low-maintenance, and cert-manager renews it via DNS-01 — but a renewal failure takes
every service simultaneously, and there is no alert on certificate expiry. Currently 37
days remain.

Separately, `cert_expiry` reports the apex `arishaig.site` fails to resolve
(`No address associated with hostname`) while the wildcard resolves fine. Worth confirming
this is intentional, since the apex is in the certificate's SAN list.

**Recommendation.** Add a certificate-expiry alert — the `blackbox` job already probes
these hosts, so `probe_ssl_earliest_cert_expiry` is available at no extra cost.

### E3 — Authentication posture is not uniform across ingress routes · Medium · Spine B

Roughly 22 of 35 routes carry Authelia forwardAuth. The exceptions are mostly deliberate
and defensible — `jellyfin` and `seerr`/`requests` are public by design — but three are
worth revisiting:

- **`watchback`** splits: the bare host is Authelia-protected, but `PathPrefix(/mcp)` is
  not, relying on token auth instead. Two auth models on one route is easy to get wrong
  during a future edit.
- **`tunarr` and `watcharr`** rely on the `local-only` IP allowlist instead of
  authentication — which, per E1, is not a meaningful boundary.
- **`omni-media-server`** is explicitly unauthenticated and flagged temporary in
  `k8s/apps/media/kustomization.yaml`. It has outlived "temporary."

**Recommendation.** Resolve `omni-media-server` one way or the other, and treat the
`local-only`-protected pair as unauthenticated when reasoning about exposure.

---

## 6. Control-plane and IaC layering

### F1 — Contradictory Talos machine configs with no declared authority · Medium · Spine A

`talos/patches/` contains both `controlplane-beta-rpi5.yaml` / `controlplane-gamma-rpi5.yaml`
and `worker-beta-rpi5.yaml` / `worker-gamma-rpi5.yaml` for the same two nodes. Both Pis
currently run as workers. `docs/rpi5-talos.md` still lists the control-plane patches as
their configuration, predating the 2026-07-22 consolidation.

Nothing in the repository declares which set is authoritative. Applying the wrong one
during a rebuild would attempt to join a second etcd member — the exact class of mistake
that is expensive to unwind.

**Recommendation.** Delete the two stale control-plane patches, or move them to an
`examples/` path with a header explaining they belong to the pre-2026-07-22 topology. If
option 2 of A1 is chosen, keep them and mark them as the target state instead. Highest
value-per-minute fix in this section either way.

### F2 — Every BestEffort pod in the cluster is a cluster primitive · Medium · Spine B

Seven workloads declare neither requests nor limits: `traefik`, `authelia`, `redis`,
`redis-exporter`, `alloy`, `nfs-provisioner`, `local-path-provisioner`. Roughly 38 others
declare both.

The inversion is exact: the ingress front door, the authentication provider, the log
shipper and both storage provisioners are BestEffort and first to be evicted, while media
workloads are Burstable or Guaranteed and protected. On a node already at 35.6/40 GB (A2),
this is a live hazard rather than a style issue — memory pressure would take down ingress
and auth before it touched sonarr.

All seven are the un-migrated raw manifests, so this resolves naturally as Helm migration
phase 4 proceeds — but the ordering matters and the pressure exists now.

**Recommendation.** Add requests to these seven ahead of the wider migration. Requests
alone (no limits) is sufficient to lift them out of BestEffort.

### F3 — No `dependsOn` between infrastructure and apps · Medium · Spine A

Exactly two Flux Kustomizations exist — `infrastructure` and `apps`, both at 10m intervals
with `prune: true` and `wait: true`. Neither depends on the other; apps converge by
eventual-consistency retry.

In steady state this is invisible. On a cold bootstrap it means apps churn through failed
reconciliations until cert-manager, MetalLB and the storage provisioners are ready. The
comment at `k8s/infrastructure/sources/kustomization.yaml:6-8` shows this was a conscious
deferral, and the Helm migration doc schedules it for phase 4.

**Recommendation.** Add it with phase 4 as planned. Noted here because it compounds C5 —
cold-start behaviour is the least-exercised path in the system.

### F4 — `local-path` is the default StorageClass but has one consumer · Medium · Spine A

`local-path` is marked default and is used by exactly one PVC (`authelia/redis-pvc.yaml`),
while 23 of 25 PVCs explicitly request `nfs-nvme`. Any future PVC that omits
`storageClassName` therefore lands silently on node-local disk with `Retain` — surviving
neither a node rebuild nor rescheduling, and invisible until the data is needed.

**Recommendation.** Make `nfs-nvme` the default and have the Authelia redis PVC request
`local-path` explicitly. This aligns the default with the overwhelmingly common case and
makes the unusual choice visible at the point of use.

### F5 — cert-manager and MetalLB are vendored by remote URL · Medium · Spine A

Both are pulled as pinned remote manifests via Kustomize `resources:` URLs — cert-manager
v1.21.0 from a GitHub release, MetalLB v0.14.9 from raw.githubusercontent, the latter
carrying a local patch adding `--ignore-exclude-lb`. Builds therefore depend on GitHub
availability, and a rebuild during a GitHub outage or after an upstream tag removal fails.

This is the same class of risk the Helm migration doc calls a "latent bug" — Renovate
bumps an image tag while RBAC and CRDs freeze — which remains live for `traefik`,
`authelia`, `alloy` and both provisioners.

**Recommendation.** Fold into Helm migration phase 4; both have official charts.

### F6 — Decommissioned LXC 100 is still referenced across four layers · Medium · Spine A

Keeping LXC 100 stopped is a settled decision and is not in question. Its unhandled
consequences are:

- `playbooks/site.yml:25` still imports `provision/docker-host.yml`
- `provision/nftables.yml` and `provision/alloy.yml` both target the `docker` host
- `terraform/adguard.tf:60` still publishes `docker.arishaig.site → 192.168.1.158`
- `mcp/nest_mcp/config.py:101` still defaults to that IP
- `docker_hosts` still includes it, so `update_docker.yml` targets it

A full `site.yml` run attempts to converge a powered-off container, and a live DNS record
points at a dead host.

**Recommendation.** Remove the imports and the DNS rewrite. The Docker-era config trees
under `playbooks/provision/files/docker/` are logged as tech debt rather than treated here.

---

## 7. CI and supply chain

### G1 — No `tofu plan` in CI · High · Spine A

`lint.yml` runs `tofu fmt -check` and `tofu validate` (with `-backend=false`) and nothing
more. There is no `plan` job. Infrastructure changes are therefore first *executed* by
`deploy-tofu` on merge to main, against unlocked state (C3), with no human ever seeing the
diff.

`validate` checks syntax and internal consistency; it cannot tell you a change destroys and
recreates a VM. Given the incident recorded in `deploy.yml`'s own comment — duplicated
AdGuard rewrites and an orphaned VM — the gap between "this parses" and "this does what I
think" is exactly where the lab has been bitten.

**Recommendation.** Add a `plan` job on PRs posting the plan output. It needs runner state
access and the secrets file, both of which `deploy-tofu` already has. Highest-severity CI
finding, and the one most likely to prevent a repeat incident.

### G2 — Talos upgrades touch only one of four nodes · High · Spine A

`.github/workflows/deploy.yml:391` reads `for NODE in 192.168.1.110; do`. Bumping
`talos_version` in `terraform.tfvars` therefore upgrades `talos-alpha` alone.
`talos-alpha-control` (.119), `talos-beta-rpi5` (.112) and `talos-gamma-rpi5` (.118) drift
silently, and only the worker is verified afterwards.

The loop is a leftover from the single-node period. The most consequential node — the sole
control plane — is the one never upgraded, so version skew accumulates precisely where it
is least tolerable.

**Recommendation.** Drive the list from inventory or a variable rather than a literal, and
upgrade the control plane last with a health gate between nodes.

### G3 — `lidarr-ui` is built and shipped with no tests and no workflow · Medium · Spine A

`lidarr-ui/` is a FastAPI application built multi-arch to
`ghcr.io/arishaig/lidarr-ui:latest` and deployed to the cluster. It has no tests, no lint
job, and no workflow of its own — nothing gates a broken commit from becoming a running
container.

The contrast is sharp: `mcp/` enforces `--cov-fail-under=85` across 19 test files and
verifies its coverage badge is current.

Note that `mcp-tests.yml` being PR-only is **not** a finding — the workflow header explains
it is deliberate, since the badge check makes a post-merge run redundant. That reasoning is
sound.

**Recommendation.** Even a smoke test that imports the app and asserts the index route
returns 200 would close the gap between "compiles" and "starts."

### G4 — Runner toolchain is pinned in two places · Medium · Spine A

`ci/runner/Dockerfile` (Renovate-managed ARGs) and `playbooks/provision/runner.yml`
(unpinned) install the same toolchain, kept in sync by a comment. Renovate updates one;
the other drifts. Logged in `tech-debt.md`.

---

## Recommended sequence

Ordered by value per unit of effort, not by severity alone.

**Do first — hours, high value:**

1. Add `Failed` to the `KubePodNotReady` regex (D1, D2) — one word.
2. Delete or relocate the stale RPi5 control-plane patches (F1).
3. Fix the `lab_health_summary` Loki check (D4).
4. Write the bootstrap-order page (C5).
5. Add requests to the seven BestEffort cluster primitives (F2).

**Do next — the structural items:**

6. Back up `rpool/data/k8s-configs` (B1) — the single highest-value change in this review.
7. Establish one backup target outside PVE (B2).
8. Bring the WireGuard patch under vault/Ansible (C1).
9. Add `tofu plan` to PR CI (G1).
10. Fix the Talos upgrade node list (G2).

**Decide, then implement:**

11. A1 — accept PVE as a single failure domain with a stated RTO, or restore etcd quorum.
    Everything else in the resilience story follows from this choice.
12. Audit and prune the fifteen NodePorts (E1).
13. State-locking backend for OpenTofu (C3).
14. Resolve the `nest-mcp` double deployment (C4).

**Absorbed by existing plans** — no separate action needed, tracked by the Helm migration:
F3, F5, and the raw-manifest half of F2.
