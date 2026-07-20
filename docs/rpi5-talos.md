# Talos on Raspberry Pi 5

Runbook for replacing the temporary x86 control-plane test VMs (beta VM 113,
delta VM 115) with two Raspberry Pi 5 (8GB) nodes booting Talos from the SD
card, with NVMe used as a data disk.

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
| Pi 1 | `talos-beta-rpi5` | 192.168.1.112 | talos-beta-vm (VM 113, .111) | `talos/patches/controlplane-beta-rpi5.yaml` |
| Pi 2 | `talos-gamma-rpi5` | 192.168.1.118 | talos-delta-vm (VM 115, .114) | `talos/patches/controlplane-gamma-rpi5.yaml` |

Both are control-plane only (`allowSchedulingOnControlPlanes: false`);
workloads stay on alpha. **Reserve .112 and .118 in UniFi** before joining so
DHCP never hands them out (the nodes configure them statically).

## Image

Image Factory schematic with the `rpi_5` overlay (no extensions — the
qemu-guest-agent extension is for the x86 VMs only):

```text
a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4
```

```yaml
overlay:
  image: siderolabs/sbc-raspberrypi
  name: rpi_5
```

Disk image (flash this): `https://factory.talos.dev/image/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4/<talos_version>/metal-arm64.raw.xz`
Installer (upgrades, pinned in the patches): `factory.talos.dev/installer/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4:<talos_version>`

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
4. Shut down, insert the NVMe drive (used later as a data disk only).

## Flash and join (per Pi, one at a time)

1. Flash the **SD card** (not the NVMe) with the Talos image, over a USB
   adapter/reader:

   ```bash
   VER=$(grep -oP 'talos_version\s*=\s*"\K[^"]+' terraform/terraform.tfvars)
   curl -LO "https://factory.talos.dev/image/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4/${VER}/metal-arm64.raw.xz"
   xz -d metal-arm64.raw.xz
   sudo dd if=metal-arm64.raw of=/dev/sdX conv=fsync bs=4M status=progress
   ```

2. Insert the SD card (with the NVMe also installed), connect ethernet, power
   on. It boots into Talos maintenance mode on a DHCP address — find it in
   UniFi. Before joining, you can confirm the NVMe is visible as a data disk
   with `talosctl -n <dhcp-ip> -e <dhcp-ip> get disks --insecure`.
3. Join (same rehearsed flow as the gamma swap). The `controlplane-*-rpi5.yaml`
   patches install to `/dev/mmcblk0` (SD), not `/dev/nvme0n1` — installing to
   NVMe would hit the same u-boot boot hang described above.

   ```bash
   ./scripts/join-talos-node.sh beta-rpi5 <dhcp-ip>    # or gamma-rpi5
   ```

4. Verify before touching the next node: `talosctl --nodes 192.168.1.115 etcd members`
   and `kubectl get nodes -o wide` (expect `arm64`, Ready). Confirm the
   `rpi5-net-tuning` DaemonSet (kube-system) has a pod on the new node.

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

## Retire the VMs (after both Pis are healthy)

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
  --image factory.talos.dev/installer/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4:<new-version>
```

(alpha keeps using the x86 schematic from `terraform.tfvars`.)

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
- **NVMe is not usable on RPi5 with Talos at all — conclusively ruled out
  2026-07-20, not just a beta-rpi5-specific quirk.** Relocating EPHEMERAL
  onto NVMe via `VolumeConfig` and then running `talosctl reset --graceful`
  on beta-rpi5 reproducibly left the node either stuck on the RPi5
  bootloader recovery screen ("Configure this Raspberry Pi 5", no bootable
  partition found on any device) or, on the one occasion it did boot, with
  kubelet crash-looping on `exec /usr/local/bin/kubelet: exec format error`.
  Root cause: **mainline U-Boot has no PCIe driver support on RPi5 at all** —
  not a bug awaiting a fix, a feature never upstreamed
  ([siderolabs/sbc-raspberrypi#23](https://github.com/siderolabs/sbc-raspberrypi/issues/23),
  RFC patch series at
  https://lists.denx.de/pipermail/u-boot/2025-February/579540.html). U-Boot's
  own device enumeration touches NVMe regardless of `BOOT_ORDER` or
  `PCIE_PROBE` — confirmed by testing with `BOOT_ORDER=0xf461` (SD-first) and
  `PCIE_PROBE=0` both verified correct on the board, and by reproducing the
  failure identically across two different drives (an Intel Optane H10 and a
  plain NVMe SSD — ruling out drive-specific behavior; the H10's dual-die
  bifurcated architecture is a real but separate compatibility concern on top
  of this). A community fork, `talos-rpi5/talos-builder`, carries the
  out-of-tree U-Boot PCIe patches, but its last real commit was 2025-11-08 —
  predating the official `rpi_5` overlay this cluster depends on for
  ethernet — so it isn't worth adopting over the officially-supported image.
  **Decision: RPi5 nodes in this cluster run SD-card-only, no NVMe, until
  siderolabs/sbc-raspberrypi#23 closes or those U-Boot PCIe patches land
  upstream** (tracked automatically — see `scripts/check-rpi5-nvme-issue.sh`
  / `.github/workflows/rpi5-nvme-watch.yml`). This applies to gamma's
  eventual worker conversion too, not just beta.
