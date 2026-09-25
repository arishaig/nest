# Talos on Raspberry Pi 5

Runbook for replacing the temporary x86 control-plane test VMs (beta VM 113,
delta VM 115) with two Raspberry Pi 5 (8GB) nodes booting Talos from the SD
card.

> **Current state (verified 2026-07-30):** neither Pi has an NVMe drive fitted.
> `talosctl get disks` on .112 and .118 shows only `mmcblk0` (31 GB SD) and the
> squashfs loop devices. The NVMe material below is retained as **rationale**,
> not as a description of the running hardware — it records why NVMe boot was
> abandoned and why `PCIE_PROBE=0` is still required even with no drive
> installed. Ignore the "insert the NVMe drive" steps unless one is being added.

> **Upstream update (2026-09-20):** the root cause below — "mainline U-Boot has
> no PCIe driver support on RPi5 at all" — is no longer true. PCIe support
> (`ARM: RPi5: Enable PCIe`) merged to mainline U-Boot `master` mid-2026, and a
> follow-on fix for the RPi5's 64 GiB PCIe inbound-DMA offset
> (`nvme: Fix missing address translation for PCIe inbound access`) merged
> 2026-07-21. But mainline's `configs/rpi_arm64_defconfig` still doesn't set
> `CONFIG_NVME`/`CONFIG_NVME_PCI` or add `nvme scan` to `CONFIG_PREBOOT`, so
> upstream U-Boot still won't auto-detect an NVMe drive out of the box — and
> more importantly, the `siderolabs/sbc-raspberrypi` overlay this cluster
> actually boots from (latest release v0.2.2, 2026-09-14) hasn't picked up a
> U-Boot bump reflecting any of this yet. Decision unchanged for now — see the
> known-issues entry below, which the watcher now tracks against these two
> concrete upstream artifacts instead of the (unrelated) issue it used to poll.

**NVMe boot does not work on Pi5 as of 2026-07-17.** u-boot's NVMe driver
hangs silently at the boot logo — confirmed on both an Intel Optane H10 and a
plain Samsung NVMe drive, so it's a generic upstream limitation
([siderolabs/sbc-raspberrypi#23](https://github.com/siderolabs/sbc-raspberrypi/issues/23)),
not a drive-compatibility issue. UART is disabled for `[pi5]` in Talos's
shipped config.txt, so there's no serial output to debug the hang. NVMe works
fine as a secondary data disk once Linux (Talos's real kernel) is up — it's
on a separate PCIe root complex from RP1/Ethernet, using the mainlined
`pcie-brcmstb` driver, confirmed via `talosctl get disks --insecure` showing
a clean single-namespace `nvme0n1`.

Pi 5 support is in the **official** `siderolabs/sbc-raspberrypi` overlay
(v0.1.8+, Jan 2026) and served by the official Image Factory — no community
images involved. The docs still class Pi 5 as community-tested, so the known
issues below matter.

## Node identity

| Node | Hostname | IP | Replaces | Patch |
|---|---|---|---|---|
| Pi 1 | `talos-beta-rpi5` | 192.168.1.112 | talos-beta-vm (VM 113, .111) | `talos/patches/worker-beta-rpi5.yaml` |
| Pi 2 | `talos-gamma-rpi5` | 192.168.1.118 | talos-delta-vm (VM 115, .114) | `talos/patches/worker-gamma-rpi5.yaml` |

Both are **workers**. They were originally planned as control-plane nodes, but
the 2026-07-22 consolidation moved the entire control plane onto the dedicated
`talos-alpha-control` VM, leaving `talos-alpha-control` as the sole etcd member.
The `controlplane-*-rpi5.yaml` patches were deleted on 2026-07-30; see
[architecture-review.md](architecture-review.md) finding F1 for why keeping
both sets was actively harmful rather than merely untidy.

**Reserve .112 and .118 in UniFi** before joining so DHCP never hands them out
(the nodes configure them statically).

## Image

Image Factory schematic with the `rpi_5` overlay plus the `nfs-utils` system
extension, which the Pis need in order to mount NFS PVCs. (The StorageClass is
called `nfs-nvme` after the NVMe pool on the *PVE host* that backs the export —
it implies nothing about local disks on the Pis. The qemu-guest-agent extension
is for the x86 VMs only.)

```text
b01e4d4c84232eef19a4e5613ea847d99e9c080cd198273c18f7611fa41eed6f
```

```yaml
overlay:
  image: siderolabs/sbc-raspberrypi
  name: rpi_5
customization:
  systemExtensions:
    officialExtensions:
      - siderolabs/nfs-utils
```

> **Corrected 2026-07-30.** This page previously gave
> `a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4`, which is
> the bare `rpi_5` schematic with **no extensions**. That was the schematic used
> for the original SD flash, but the machine-config patches pin the
> `nfs-utils` one above. Following the old upgrade command would have silently
> stripped `nfs-utils` from a Pi and broken every NFS PVC mount on that node.
> Always cross-check against the `image:` line in `talos/patches/worker-*-rpi5.yaml`.

Disk image (flash this): `https://factory.talos.dev/image/b01e4d4c84232eef19a4e5613ea847d99e9c080cd198273c18f7611fa41eed6f/<talos_version>/metal-arm64.raw.xz`
Installer (upgrades, pinned in the patches): `factory.talos.dev/installer/b01e4d4c84232eef19a4e5613ea847d99e9c080cd198273c18f7611fa41eed6f:<talos_version>`

Use the version pinned as `talos_version` in `terraform/terraform.tfvars` so
the whole cluster stays on one release.

## One-time hardware prep (per Pi)

1. Boot Raspberry Pi OS Lite from a scratch SD card (needed once, to set the
   EEPROM — not needed again after this).
2. Update the bootloader EEPROM: `sudo rpi-eeprom-update -a && sudo reboot`.
3. Set SD-first boot order and disable PCIe probing: `sudo rpi-eeprom-config --edit` →
   `BOOT_ORDER=0xf461` (SD → NVMe → USB → repeat) **and `PCIE_PROBE=0`**.
   `PCIE_PROBE=1` is the factory default on some units and forces the
   bootloader to link-train PCIe/NVMe as part of its own init sequence,
   independent of `BOOT_ORDER` — this hangs at the u-boot logo on affected
   units regardless of whether NVMe is even physically present (confirmed
   2026-07-19 on talos-beta-rpi5: hung both with and without the drive
   inserted until this was set to 0). NVMe is still fully usable as a data
   disk under Talos's own kernel with `PCIE_PROBE=0` — that's a completely
   separate, mainlined driver path unrelated to the bootloader's probe.
   Verify with `sudo rpi-eeprom-config` (no `--edit`) before moving on — don't
   assume a prior prep pass actually stuck.
4. Shut down. (Historically this is where an NVMe drive was fitted as a data
   disk. Neither Pi has one today — skip unless you are adding one. Keep
   `PCIE_PROBE=0` regardless: the boot hang above occurred with *and* without a
   drive present.)

## Flash and join (per Pi, one at a time)

1. Flash the **SD card** (not the NVMe) with the Talos image, over a USB
   adapter/reader:

   ```bash
   VER=$(grep -oP 'talos_version\s*=\s*"\K[^"]+' terraform/terraform.tfvars)
   curl -LO "https://factory.talos.dev/image/b01e4d4c84232eef19a4e5613ea847d99e9c080cd198273c18f7611fa41eed6f/${VER}/metal-arm64.raw.xz"
   xz -d metal-arm64.raw.xz
   sudo dd if=metal-arm64.raw of=/dev/sdX conv=fsync bs=4M status=progress
   ```

2. Insert the SD card, connect ethernet, power on. It boots into Talos
   maintenance mode on a DHCP address — find it in UniFi. Confirm what the node
   sees with `talosctl -n <dhcp-ip> -e <dhcp-ip> get disks --insecure`; expect
   `mmcblk0` only, plus an `nvme0n1` if a drive has been fitted.
3. Join (same rehearsed flow as the gamma swap). The `worker-*-rpi5.yaml`
   patches install to `/dev/mmcblk0` (SD), not `/dev/nvme0n1` — installing to
   NVMe would hit the same u-boot boot hang described above.

   ```bash
   ./scripts/join-talos-node.sh beta-rpi5 <dhcp-ip>    # or gamma-rpi5
   ```

4. Verify before touching the next node: `kubectl get nodes -o wide` (expect
   `arm64`, Ready). Confirm the `rpi5-net-tuning` DaemonSet (kube-system) has a
   pod on the new node.

   <details>
   <summary><b>Historical: etcd learner promotion</b> — applies only to
   control-plane joins, which the Pis no longer do</summary>

   Retained because it would apply again if option 2 of architecture review
   finding A1 (restore etcd quorum) is ever chosen.

   **If the new member stays `LEARNER: true` indefinitely** even after its
   raft index matches the leader's (`talosctl -n <leader-ip>,<node-ip> etcd
   status`), Talos isn't retrying promotion — it appears to attempt this once
   during the join/upgrade flow and doesn't retry later (confirmed
   2026-07-19: beta-rpi5 sat fully caught-up-but-unpromoted for 10+ minutes
   after an `talosctl upgrade`-triggered reboot delayed catch-up past that
   window). `talosctl` has no CLI command to force it. Fix by promoting
   directly against etcd's own API using a short-lived client cert signed by
   the cluster's etcd CA (from `~/.talos/clusterconfig/secrets.yaml`,
   `certs.etcd`), then delete the cert/key material immediately:

   ```bash
   # extract certs.etcd.{crt,key} (base64) from secrets.yaml to ca.crt/ca.key,
   # generate+sign a throwaway client cert against that CA, then:
   etcdctl --endpoints=https://<leader-ip>:2379 \
     --cacert=ca.crt --cert=client.crt --key=client.key \
     member promote <learner-member-id>
   ```

   </details>

## Retire the VMs (after both Pis are healthy) — ✅ done

> Completed. VMs 113/115 are gone and both Pis run as workers. Kept for the
> record; the etcd juggling below is not something to re-run as written.

Alternate joins and removals so etcd member count stays sane
(3 → 4 → 3 → 4 → 3), exactly like the gamma rehearsal:

1. Join beta-rpi5 → remove beta-vm: `kubectl drain <node> --ignore-daemonsets`,
   `talosctl --nodes 192.168.1.111 reset --graceful`, `kubectl delete node <node>`.
2. Join gamma-rpi5 → remove delta-vm (same, against .114).
3. PR: delete `terraform/vm-talos-test-nodes.tf`,
   `talos/patches/controlplane-beta-vm.yaml`, `controlplane-delta-vm.yaml`;
   CI tofu apply removes VMs 113/115.

## Upgrades

Renovate PRs bump `talos_version` in `terraform.tfvars` and the installer tags
in the rpi5 patches together. CI (`talos-rpi5-overlay` job) blocks the PR until
the Image Factory serves the `rpi_5` overlay for that version — overlay
releases can lag a Talos patch release by a few days
([siderolabs/talos#12748](https://github.com/siderolabs/talos/issues/12748)).
After merge, upgrade one node at a time, Pis with the rpi_5 installer:

```bash
talosctl upgrade --nodes 192.168.1.112 \
  --image factory.talos.dev/installer/b01e4d4c84232eef19a4e5613ea847d99e9c080cd198273c18f7611fa41eed6f:<new-version>
```

(alpha keeps using the x86 schematic from `terraform.tfvars`.)

> **This step is manual and is currently behind.** CI's Talos upgrade loop in
> `deploy.yml` is hardcoded to `192.168.1.110`, so it upgrades the amd64 worker
> and nothing else (architecture review finding **G2**). As of 2026-07-30 both
> Pis *and* `talos-alpha-control` run v1.13.6 against a pinned v1.13.7.
>
> Do **not** fix that by adding the Pi IPs to the loop: it builds one image from
> `talos_schematic_id` (`ed304c51…`, amd64), which would push an x86 installer
> onto both Pis. Any fix needs a per-node schematic map.

## Known issues / watch list

- [sbc-raspberrypi#91](https://github.com/siderolabs/sbc-raspberrypi/issues/91)
  — the RP1/macb NIC can silently wedge under sustained traffic (EEE LPI race,
  TSO/GSO ring hang). Mitigated by the `rpi5-net-tuning` DaemonSet
  (`k8s/infrastructure/rpi5-net-tuning/`); remove it when the kernel fix lands.
- [sbc-raspberrypi#82](https://github.com/siderolabs/sbc-raspberrypi/issues/82)
  — control-plane ethernet drops; same mitigation. Watch etcd-member-stale
  alerts, not just node-down.
- [sbc-raspberrypi#93](https://github.com/siderolabs/sbc-raspberrypi/issues/93)
  — proposal to fold `rpi_5` into `rpi_generic`. If that ships, the schematic
  ID changes; read overlay release notes on minor bumps.
- [sbc-raspberrypi#89](https://github.com/siderolabs/sbc-raspberrypi/issues/89)
  — no hardware watchdog yet; a hard-hung Pi needs a power cycle.
- [sbc-raspberrypi#90](https://github.com/siderolabs/sbc-raspberrypi/issues/90)
  — the official Active Cooler fan doesn't spin under Talos; passive cooling
  or an always-on fan case is safer.
- **NVMe is not usable on RPi5 with Talos yet — ruled out 2026-07-20, but the
  upstream picture has since moved (re-checked 2026-09-20).** Relocating
  EPHEMERAL onto NVMe via `VolumeConfig` and then running
  `talosctl reset --graceful` on beta-rpi5 reproducibly left the node either
  stuck on the RPi5 bootloader recovery screen ("Configure this Raspberry Pi
  5", no bootable partition found on any device) or, on the one occasion it
  did boot, with kubelet crash-looping on
  `exec /usr/local/bin/kubelet: exec format error`.
  Root cause at the time: **mainline U-Boot had no PCIe driver support on
  RPi5 at all**
  ([siderolabs/sbc-raspberrypi#23](https://github.com/siderolabs/sbc-raspberrypi/issues/23)
  — closing that issue turned out to be unrelated noise, see #380 in this
  repo). That root cause is now **stale**: `ARM: RPi5: Enable PCIe` merged to
  mainline U-Boot `master` mid-2026 (confirmed applied by maintainer Peter
  Robinson), and a follow-on NVMe PCIe-inbound-DMA-offset fix merged
  2026-07-21 (`nvme: Fix missing address translation for PCIe inbound
  access`). What's still missing: mainline's `configs/rpi_arm64_defconfig`
  doesn't enable `CONFIG_NVME`/`CONFIG_NVME_PCI` or scan for NVMe at preboot,
  and — the part that actually gates this cluster — the
  `siderolabs/sbc-raspberrypi` overlay Talos ships hasn't picked up a U-Boot
  bump carrying any of this (latest release v0.2.2, 2026-09-14, no
  U-Boot/NVMe changes in its notes). The community fork
  `talos-rpi5/talos-builder` remains stale since 2025-11-08 and isn't worth
  adopting over the officially-supported image regardless.
  **Decision: RPi5 nodes in this cluster run SD-card-only, no NVMe, until the
  `sbc-raspberrypi` overlay ships a U-Boot with working NVMe boot** (tracked
  automatically — see `scripts/check-rpi5-nvme-issue.sh` /
  `.github/workflows/rpi5-nvme-watch.yml`, which now polls the actual
  upstream defconfig and the overlay's release notes instead of the
  now-closed, unrelated #23). This applies to gamma's eventual worker
  conversion too, not just beta.
