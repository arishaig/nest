# Disaster recovery

What survives what, what is deliberately not protected, and the order to rebuild
things in from cold.

Written 2026-07-30 to close findings **A1**, **B3**, **B4**, **C5** and **D3** of
[`architecture-review.md`](architecture-review.md). Where that review asked a
question, this page records the answer as a **decision** rather than leaving it
an assumption.

---

## The accepted failure domain

**Proxmox is a single point of failure, and that is accepted.**

Finding A1 offered two coherent end states: accept PVE as one failure domain and
invest in restore speed, or return the Raspberry Pis to control-plane roles for a
three-member etcd spanning two physical machines. **Option 1 is chosen.**

The reasoning is concrete rather than philosophical. Each Pi has 4 GB of RAM
(3.5 GB allocatable) against alpha's 37.5 GB — alpha is 78% of all cluster
memory, and `talos-gamma-rpi5` already sits at 69% of its memory requested. Etcd
quorum on hardware that small buys availability for the API server while leaving
every workload, all storage and all ingress on PVE anyway. It would not make the
lab survive losing PVE; it would only make it survive losing *one control-plane
VM*, which is a much rarer event.

What follows from that decision:

- Recovery effort goes into **restore speed**, not availability.
- The `controlplane-*-rpi5.yaml` machine configs were deleted (finding F1). If
  this decision is ever revisited, they are recoverable from git history and the
  etcd learner-promotion procedure is preserved in
  [`rpi5-talos.md`](rpi5-talos.md).
- An **RTO is stated below** and is expected to be measured, not estimated
  forever.

### What is in the blast radius

| On PVE (192.168.1.16) | Lost with the host |
|---|---|
| VM 112 `talos-alpha-control` | Sole control plane and sole etcd member — cluster API |
| VM 110 `talos-alpha` | 57 pods; every heavy and every amd64-only workload |
| `rpool/data/k8s-configs` | 23 of 25 PVCs — PVE is the NFS server, not the fileserver LXC |
| `Tank/media_root` | The `media-nfs` PV and the Samba shares |
| VM 500 `backup` (PBS) | The backup server itself |
| VM 107 `homeassistant` | Home automation |
| LXCs 101–111 | musicbrainz, fileserver, scrutiny, seedbox, monitoring, mcp, runner |

`talos-beta-rpi5` and `talos-gamma-rpi5` survive physically but are useless in
isolation: no API server, no storage, no ingress.

Note that the **observability plane does not form an independent failure
domain** (finding D3). Prometheus, Loki, Grafana and Alertmanager run in LXC 105,
which survives *cluster* loss — a real and deliberate benefit, since it can still
report on a dead cluster — but not *host* loss. It is not a second site.

---

## What is protected, and what is not

| Data | Size | Protection | Survives rpool loss? | Survives host loss? |
|---|---|---|---|---|
| Guest disks (LXCs, VMs) | — | PBS guest jobs → `pbs-local`, synced to `tank-archive` | **yes** (via Tank) | no |
| `rpool/data/k8s-configs` | 32 GB | PBS host backup daily 03:00, synced to Tank 05:00 | **yes** | no |
| `Tank/media_root` | 17.8 TB | **none — deliberate** | n/a | no |
| OpenTofu state | small | `scripts/backup-state.sh` → vault-encrypted copy on the NAS | partly | no |
| Talos cluster CA (`talos/clusterconfig/`) | small | **gitignored; backup unverified** | unknown | unknown |

### `Tank/media_root` is deliberately unprotected

This is a **decision**, not an oversight (finding B4). 17.8 TB of media is
re-acquirable bulk data; backing it up would require a second array of comparable
size for content that can be re-downloaded. The \*arr databases that know *what*
was acquired live in `k8s-configs` and **are** protected, which is the part that
would actually be painful to reconstruct.

### The "cold" storage names are misleading

`local`, `local-zfs`, `cold-backup` and `cold-storage` all report identical free
space because **all four are directories on rpool** (finding B3). Neither "cold"
store is cold, offline, or separate. They currently hold 7 GB each.

> **Do not reach for `cold-backup` during an incident expecting off-host data.**
> Renaming them to something honest (`local-archive`) is outstanding work.

---

## Cold bootstrap order

The sequence below exists nowhere else; before this page it lived only in
operators' heads (finding C5). Steps are ordered by hard dependency — each one
blocks the next.

> **`scripts/bootstrap-talos.sh` is stale.** It hardcodes
> `TALOS_IP="192.168.1.110"` and `controlplane.yaml`, describing the pre-
> 2026-07-22 topology where .110 was the control plane. Today .110 is a worker
> and the control plane is `talos-alpha-control` at **.119**. Read the script as
> a reference for the *shape* of the flow, not as a runnable path.

### 0. Prerequisites

`talosctl`, `kubectl`, `flux`, `gh`, `tofu`, `ansible`, plus:

- the **ansible-vault password** (`~/.config/ansible-on-nest/vault-pass`)
- the **OpenTofu state** — see step 1
- a **GITHUB_TOKEN** with repo scope for `flux bootstrap`
- `~/.ssh/ansible-on-nest`

### 1. Recover OpenTofu state — before anything else

There is no backend and no locking; state exists as three uncoordinated copies
(finding C3): the workstation, the CI runner at `/opt/terraform-state/nest/`, and
a vault-encrypted copy on the NAS from `scripts/backup-state.sh`.

**Applying against empty or stale state is the single most damaging mistake
available here.** It has already happened once: it duplicated every AdGuard
rewrite and orphaned a VM, which is why `deploy.yml`'s `deploy-tofu` job carries
a hard-fail guard on a missing state file. Recover the newest copy and verify it
before running anything.

### 2. `tofu apply`

Recreates the PVE guests, the Talos VMs and the AdGuard DNS rewrites. Note the
PBS `scsi1` disk on Tank is **not** managed here — `vm-pbs.tf` carries
`ignore_changes = [disk]`, so reattach it manually:

```bash
qm set 500 --scsi1 Tank:1000,iothread=1
```

### 3. Bootstrap Talos on the control plane

`talos-alpha-control` (VM 112, **192.168.1.119**), using
`talos/patches/controlplane-alpha-control.yaml`. This creates etcd and the
cluster VIP at 192.168.1.115. Export kubeconfig.

### 4. Join the workers

`talos-alpha` (.110, amd64) via `talos/patches/worker-alpha.yaml`, then the two
Pis via `worker-beta-rpi5.yaml` / `worker-gamma-rpi5.yaml`. The Pis use the
**arm64 `rpi_5` schematic** `b01e4d4c…` (which bundles `nfs-utils`), *not* the
amd64 `talos_schematic_id` from `terraform.tfvars` — see
[`rpi5-talos.md`](rpi5-talos.md).

### 5. Reapply the WireGuard patch on alpha — easy to forget, total if missed

`talos/patches/wireguard-alpha.yaml.example` is a **template**; the real
interface is out-of-band machine config (finding C1). There are **no UniFi port
forwards at all**, so this tunnel is the *only* path into the lab from the
internet. A `talosctl reset` on 2026-07-21 wiped it and silently took down all
external ingress.

Nothing in the pipeline reapplies this. Until C1 is closed, it is a manual step
and belongs on any reset/rebuild checklist.

### 6. Push the k8s Secrets — Flux cannot proceed without them

```bash
ansible-playbook -i inventory/hosts.yml playbooks/provision/k8s.yml
```

This creates `postgres-secret`, `authelia-config`, `authelia-env-secret`,
`cloudflare-api-token`, `subgen-secrets`, `nest-mcp-secrets`,
`nest-mcp-ssh-key`, `arc-github-secret` and the `*-api-key` set from
ansible-vault.

This is a **push step inside a pull-based reconciliation loop** (finding C2).
There is no SOPS, no sealed-secrets and no External Secrets Operator, and nothing
in `k8s/` declares the ordering. On a fresh cluster Flux will reconcile happily
while every HelmRelease referencing `existingSecret` blocks indefinitely — with
no error that points at the real cause. **Run this before Flux, or expect a
confusing debugging session.**

### 7. Bootstrap Flux

`flux bootstrap github` installs Flux and reconciles `k8s/`. Note the
`infrastructure` and `apps` Kustomizations have **no `dependsOn`** between them
(finding F3), so apps churn through failed reconciliations until cert-manager,
MetalLB and the storage provisioners come up. This is noisy but self-correcting
at the 10m interval.

### 8. Restore data

Restore `rpool/data/k8s-configs` from the PBS `host/k8s-configs` group before
scaling workloads up. When restoring any SQLite-backed app, follow the WAL
checkpoint rule in [`k8s-migration.md`](k8s-migration.md) — copying a `.db`
without checkpointing has already corrupted a Sonarr database once.

### 9. Reconverge everything else

```bash
ansible-playbook -i inventory/hosts.yml playbooks/site.yml
```

---

## Recovery time objective

**Stated RTO: one working day** to a functioning lab, media library intact,
app state restored to within 24 hours.

This is an **estimate, not a measurement**, and it should not stay that way.
Nothing has ever verified that a PBS backup is restorable — the freshness
exporter checks only that snapshots are *recent* (finding B4). The weekly
`tank-archive` verify job now re-reads and checksums chunks, which is the cheap
half of the problem, but a verify is not a restore.

**Outstanding: restore one LXC to a scratch VMID and time it.** That number is
the lab's real RTO, and it replaces the estimate above.

---

## Known gaps in this plan

| Gap | Finding | Consequence |
|---|---|---|
| WireGuard config is not in git | C1 | Step 5 is manual; forgetting it means no external ingress and no obvious symptom |
| Secrets are pushed, not reconciled | C2 | Step 6 must precede Flux, and nothing enforces it |
| No state locking | C3 | Concurrent workstation and CI applies clobber each other silently |
| `talos/clusterconfig/` backup unverified | C5 | Cluster CA loss means rebuilding the cluster, not restoring it |
| Nothing survives host loss | A1 (accepted) | Every backup is on-site; a fire or theft is total |
| `bootstrap-talos.sh` describes the old topology | — | Following it verbatim builds the wrong control plane |
