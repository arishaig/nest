# Talos on Raspberry Pi 5

Runbook for replacing the temporary x86 control-plane test VMs (beta VM 113,
delta VM 115) with two Raspberry Pi 5 (8GB) nodes booting Talos from NVMe.

Pi 5 support is in the **official** `siderolabs/sbc-raspberrypi` overlay
(v0.1.8+, Jan 2026) and served by the official Image Factory — no community
images involved. The docs still class Pi 5 as community-tested, so the known
issues below matter.

## Node identity

| Node | Hostname | IP | Replaces | Patch |
|---|---|---|---|---|
| Pi 1 | `talos-beta-rpi5` | 192.168.1.112 | talos-beta-vm (VM 113, .111) | `talos/patches/controlplane-beta-rpi5.yaml` |
| Pi 2 | `talos-delta-rpi5` | 192.168.1.118 | talos-delta-vm (VM 115, .114) | `talos/patches/controlplane-delta-rpi5.yaml` |

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

1. Boot Raspberry Pi OS Lite from a scratch SD card.
2. Update the bootloader EEPROM: `sudo rpi-eeprom-update -a && sudo reboot`.
3. Set NVMe-first boot order: `sudo rpi-eeprom-config --edit` →
   `BOOT_ORDER=0xf416` (NVMe → USB → SD → repeat). The official M.2 HAT needs
   no `PCIE_PROBE` setting.
4. Shut down, remove the SD card.

## Flash and join (per Pi, one at a time)

1. Flash NVMe over a USB adapter:

   ```bash
   VER=$(grep -oP 'talos_version\s*=\s*"\K[^"]+' terraform/terraform.tfvars)
   curl -LO "https://factory.talos.dev/image/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4/${VER}/metal-arm64.raw.xz"
   xz -d metal-arm64.raw.xz
   sudo dd if=metal-arm64.raw of=/dev/sdX conv=fsync bs=4M status=progress
   ```

2. Install NVMe in the Pi, connect ethernet, power on. It boots into Talos
   maintenance mode on a DHCP address — find it in UniFi.
3. Join (same rehearsed flow as the gamma swap):

   ```bash
   ./scripts/join-talos-node.sh beta-rpi5 <dhcp-ip>    # or delta-rpi5
   ```

4. Verify before touching the next node: `talosctl --nodes 192.168.1.115 etcd members`
   and `kubectl get nodes -o wide` (expect `arm64`, Ready). Confirm the
   `rpi5-net-tuning` DaemonSet (kube-system) has a pod on the new node.

## Retire the VMs (after both Pis are healthy)

Alternate joins and removals so etcd member count stays sane
(3 → 4 → 3 → 4 → 3), exactly like the gamma rehearsal:

1. Join beta-rpi5 → remove beta-vm: `kubectl drain <node> --ignore-daemonsets`,
   `talosctl --nodes 192.168.1.111 reset --graceful`, `kubectl delete node <node>`.
2. Join delta-rpi5 → remove delta-vm (same, against .114).
3. PR: delete `terraform/vm-talos-test-nodes.tf`,
   `talos/patches/controlplane-beta-vm.yaml`, `controlplane-delta-vm.yaml`;
   CI terraform apply removes VMs 113/115.

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
