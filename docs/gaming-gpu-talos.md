# Talos on the gaming PC (`talos-omega`)

Runbook for onboarding the user's gaming PC as a Talos worker node, dual-booted
alongside Windows on a dedicated second disk, for its RTX 3080.

## Why dual-boot, not a VM or WSL2

Evaluated and rejected first:

- **Native Windows kubelet** — Windows nodes can only schedule Windows
  containers, never Linux images. None of the intended workloads
  (whisper/subgen/radarr/sonarr, all Linux/CUDA images) could run there
  regardless of GPU access. Not viable at all, independent of GPU concerns.
- **Talos in a Hyper-V VM** — consumer GeForce cards are blocked from clean
  GPU passthrough (DDA) by NVIDIA's driver. Hyper-V's GPU-PV
  paravirtualization (the mechanism that *does* work reliably for consumer
  cards) is WSL2/Windows-Sandbox-only — not available to an arbitrary Linux
  guest VM like a Talos VM.
- **WSL2 kubelet joined to the cluster** — technically viable (WSL2 has
  official NVIDIA CUDA passthrough, and mirrored networking mode makes it
  LAN-reachable), but rejected: it sits outside Talos's cert-rotation/upgrade
  lifecycle, outside this repo's GitOps discipline (manual node-cert bootstrap
  can't go through the pipeline), has a much larger attack surface than an
  immutable Talos node, and shares the GPU with Windows while both are
  running rather than a clean handoff.

Bare-metal dual-boot gives Talos exclusive hardware access when booted (no
contention, native driver performance), stays fully inside this cluster's
GitOps/Renovate/CI machinery the same as every other node, and fails cleanly —
when the owner reboots into Windows, the node is just absent, not flaky. The
cost is a full reboot to switch between "gaming" and "cluster node."

## Node identity

| Field | Value |
|---|---|
| Hostname | `talos-omega` |
| IP | `192.168.1.120` (static — reserve in UniFi before joining) |
| Node label | `nest.arishaig.site/workloads: omega` |
| Patch | `talos/patches/worker-omega.yaml` |
| Managed by | **Not** Terraform/Proxmox — physical hardware, unlike every other node in this cluster |

Worker only — never joins etcd.

## Image

Image Factory schematic with `siderolabs/nvidia-open-gpu-kernel-modules`
(production branch) + `siderolabs/nvidia-container-toolkit` (production
branch). Open kernel modules were chosen over the legacy proprietary
(`nonfree-kmod-nvidia`) extension because they officially support Turing and
later architectures — the 3080 (Ampere) is well within range, and this is the
path NVIDIA/Talos are steering toward going forward. No
`siderolabs/qemu-guest-agent` — that extension is for the Proxmox VM nodes
only and is meaningless (untested) on bare metal.

```text
6698d6f136c5bb37ca8bb8482c9084305084da0a5ead1f4dcae760796f8ab3a2
```

Installer (pinned in `talos/patches/worker-omega.yaml`):
`factory.talos.dev/installer/6698d6f136c5bb37ca8bb8482c9084305084da0a5ead1f4dcae760796f8ab3a2:<talos_version>`

Use the version pinned as `talos_version` in `terraform/terraform.tfvars` so
the whole cluster stays on one release. A generic `ghcr.io/siderolabs/installer`
would silently strip both NVIDIA extensions on the next upgrade — same failure
mode already documented for the RPi5 nodes' overlay in `docs/rpi5-talos.md`.

**Production vs. LTS driver branch**: chosen "production" for both
extensions during setup. NVIDIA's Production Branch gets newer driver
releases more frequently than their LTS branch, trading a longer stable/patch
window for staying current. Neither restricts CUDA compute compatibility for
Ampere. Production means this schematic will need regenerating and
reinstalling somewhat more often than LTS would have — acceptable for a
homelab node, just worth knowing when deciding whether to bump it.

## Networking: Talos requires wired Ethernet, permanently

**Talos has no Wi-Fi stack at all** — no wpa_supplicant, nothing. This isn't
just an install-time requirement; every single time this node boots into
Talos, it needs a wired Ethernet path to function, for the life of the node.
A USB Wi-Fi adapter (which this machine used under Windows) will not work
under Talos, ever — the interface simply won't come up.

If this machine doesn't have a convenient wired run to your switch/router,
this is a real, permanent constraint on whether the node can exist at all —
not something to solve once during setup and forget. Options explored
2026-08-05: a GL.iNet Opal (GL-SFT1200) travel router as a Wi-Fi-to-Ethernet
bridge turned out to be a dead end on this specific hardware — its stock
firmware's "Repeater" mode is actually WISP (NAT'd, isolated subnet), a
manual `relayd` bridge hit a known hairpin-NAT packet-loss bug, and the
community `opal-bridge` fix package that addressed the L2/DHCP side still
left the bridged interface in a WAN-like firewall zone that blocks new
inbound connections — which is fatal for Talos, since the control plane needs
to open connections *to* this node's kubelet (10250) and `talosctl` API
(50000), not just receive replies to connections the node itself initiates.
A dumb powerline or MoCA adapter (no firmware, no NAT, no firewall zones —
just an electrical/coax Ethernet extension) avoids this whole failure class
and is the more likely path if a direct cable run isn't practical. If the
answer ends up being "just run the cable," that's the simplest and most
reliable option and shouldn't be dismissed on inconvenience alone given how
much this detour cost.

## One-time hardware prep

1. **Confirm there's a genuinely separate physical disk for Talos.** Talos
   takes ownership of a whole disk and writes its own partition table — it
   cannot install into a partition alongside Windows.
2. **BitLocker / Secure Boot**: check BitLocker status on the Windows drive
   and have the recovery key ready, or suspend it. Installing a
   non-Secure-Boot Talos image likely means disabling Secure Boot in
   firmware, which is a global setting (not per-disk) and can trip a
   BitLocker recovery prompt on the next Windows boot.
3. **Hard safeguard against installing to the wrong disk**: physically
   unplug the Windows drive, or disable it in BIOS, for the duration of the
   install. This removes the entire "wrong disk got wiped" failure class
   rather than relying on getting a disk selector exactly right.

## Flash and join

1. Generate the ISO (already done for this node — see schematic above) and
   put it on a Ventoy drive, or download directly:

   ```bash
   VER=$(grep -oP 'talos_version\s*=\s*"\K[^"]+' terraform/terraform.tfvars)
   curl -LO "https://factory.talos.dev/image/6698d6f136c5bb37ca8bb8482c9084305084da0a5ead1f4dcae760796f8ab3a2/${VER}/metal-amd64.iso"
   ```

2. Boot the machine from the ISO (with the Windows drive unplugged/disabled
   per the prep step above). It comes up in Talos maintenance mode on a DHCP
   address — find it in UniFi.

3. Identify the target disk **by serial number, not by `/dev/nvmeXn1` path**
   — with two NVMe drives normally present in this machine, enumeration order
   is not guaranteed stable across reboots, and a path-based target is
   exactly how a future reinstall (once the Windows drive is reconnected)
   could hit the wrong disk:

   ```bash
   talosctl -n <dhcp-ip> -e <dhcp-ip> get disks --insecure
   ```

   Fill the recorded serial into `talos/patches/worker-omega.yaml`'s
   `install.diskSelector.serial` (replacing the `REPLACE_WITH_DISK_SERIAL`
   placeholder) before joining.

   `diskSelector` is untested elsewhere in this repo — every other node uses
   a plain `disk:` path — but it validates cleanly against
   `scripts/check-talos-config.sh` (`talosctl gen config … && talosctl
   validate -m metal`) as of `talosctl v1.13.8`. If a future Talos version
   ever rejects it, fall back to plain `disk: /dev/<confirmed-path>` — the
   physical-unplug safeguard above already removes the risk `diskSelector`
   was hedging against, so that fallback is not a compromise.

4. Join (same script every other node uses):

   ```bash
   ./scripts/join-talos-node.sh --worker omega <dhcp-ip>
   ```

5. Verify: `kubectl get nodes -o wide` shows `talos-omega` Ready, `amd64`.

6. Reconnect/re-enable the Windows drive and confirm Windows still boots
   normally via the firmware boot-menu override (F11/F12, or whatever this
   board uses) — Talos's installer only touched its own disk, so Windows's
   boot entry should be untouched, but verify rather than assume.

## GPU verification

- `kubectl describe node talos-omega` — `nvidia.com/gpu` present under
  Allocatable once `k8s/infrastructure/nvidia-gpu-operator/` reconciles.
- `kubectl get pods -n gpu-operator -o wide` — the heavy operand pods
  (device-plugin, dcgm-exporter, gpu-feature-discovery) should land on
  `talos-omega` only. See the comment in
  `k8s/infrastructure/nvidia-gpu-operator/helmrelease.yaml` for how that
  self-scoping actually works (NFD PCI-vendor detection + the operator's
  ClusterPolicy controller — not a manual nodeSelector, the chart doesn't
  expose one for the operand DaemonSets).
- On the node: `talosctl -n 192.168.1.120 get extensions` (both NVIDIA
  extensions listed) and `/proc/driver/nvidia/version`.

## Upgrades

Renovate bumps `talos_version` in `terraform.tfvars` and the installer tag in
`talos/patches/worker-omega.yaml` together (separate custom regex manager in
`renovate.json`, mirroring the RPi5 one — kept as its own entry so it doesn't
double-track alongside the RPi5 pattern). After merge, upgrade manually
(this node isn't covered by CI's Talos upgrade loop, same caveat as the RPi5
nodes — see `docs/rpi5-talos.md`'s Upgrades section):

```bash
talosctl upgrade --nodes 192.168.1.120 \
  --image factory.talos.dev/installer/6698d6f136c5bb37ca8bb8482c9084305084da0a5ead1f4dcae760796f8ab3a2:<new-version>
```

The machine needs to be booted into Talos (not Windows) for an upgrade to
apply, obviously.

## Known issues / design notes

- **Node is routinely offline for hours at a time.** This is expected, not an
  incident. `KubeNodeNotReady` excludes `talos-omega` explicitly (see
  `playbooks/provision/files/monitoring/prometheus/rules/nest.yml`) —
  confirmed live against Prometheus that `HostDown` doesn't apply to Talos
  nodes at all (that alert only covers the LXC/VM/VPS `node`/`vps` job), so
  no exclusion was needed there.
- **Jellyfin must never schedule here** — it's user-facing and needs to stay
  up, unlike whisper/subgen/radarr/sonarr which the user explicitly said are
  fine to reschedule. Hard anti-affinity in `k8s/apps/media/jellyfin.yaml`
  (`nest.arishaig.site/workloads NotIn [omega]`) enforces this.
- **subgen wired to the GPU** (`k8s/apps/media/subgen.yaml`): hard-pinned to
  `omega` (was `general`/alpha), `mccloud/subgen:2026.07.3` (the CUDA-capable
  default image — the old `-cpu` tag can't use the GPU at all),
  `TRANSCRIBE_DEVICE: cuda`, `nvidia.com/gpu: "1"` in both requests and
  limits, and `runtimeClassName: nvidia` — required because the GPU
  Operator's ClusterPolicy has `cdi.default: false`, confirmed against the
  working `nvidia-cuda-validator` pod, which sets it explicitly. Without it
  the pod schedules and gets the GPU resource count but no actual device — a
  failure invisible to `kustomize build`/CI. Fallback when omega is offline
  is simply pending — same reschedule-tolerant behavior as every other
  workload on this node.
- **Storage**: anything that opts into scheduling on this node must use
  NFS-backed storage (the `nfs-nvme` StorageClass or the shared `media-nfs`
  PV), never `local-path` — a pod bound to node-local storage would not
  reschedule when this node goes offline, which defeats the entire point of
  treating it as reschedule-tolerant capacity.
