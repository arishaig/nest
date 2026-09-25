#!/usr/bin/env bash
# pacman-updates-textfile.sh — Emit Prometheus textfile metrics for pending
# PiKVM OS updates.
#
# Works like pacman-contrib's checkupdates (not installed on PiKVM OS): it
# syncs the repo databases into a throwaway --dbpath under /tmp, with `local`
# symlinked to the real installed-packages db. The real sync db is never
# touched, so this can't set up a partial upgrade, and nothing is written to
# the read-only rootfs. The result goes to /run (tmpfs), which the
# textfile-only node_exporter reads.
#
# Installed and scheduled by playbooks/provision/pikvm.yml.

set -euo pipefail

TEXTFILE_DIR=/run/node_exporter
OUT="$TEXTFILE_DIR/os_updates.prom"

# pikvm-update (or a manual pacman) is running. Skip this run and keep the
# previous file; the next timer run picks it up.
[ -e /var/lib/pacman/db.lck ] && exit 0

mkdir -p "$TEXTFILE_DIR"

db="$(mktemp -d /tmp/pacman-updates.XXXXXX)"
tmp="$(mktemp "$OUT.XXXXXX")"
trap 'rm -rf "$db" "$tmp"' EXIT

ln -s /var/lib/pacman/local "$db/local"
pacman -Sy --dbpath "$db" --logfile /dev/null >/dev/null
# -Qu exits 1 when nothing is upgradable.
pending="$(pacman -Qu --dbpath "$db" 2>/dev/null | wc -l || true)"

# Arch replaces the kernel's modules dir on upgrade, so a running kernel
# whose modules are gone means a newer kernel is installed and not booted.
if [ -d "/usr/lib/modules/$(uname -r)" ]; then
  reboot=0
else
  reboot=1
fi

{
  echo "# HELP os_updates_pending Packages a full system upgrade would upgrade."
  echo "# TYPE os_updates_pending gauge"
  echo "os_updates_pending{manager=\"pacman\"} $pending"
  echo "# HELP os_reboot_required 1 if a newer kernel is installed than the one running."
  echo "# TYPE os_reboot_required gauge"
  echo "os_reboot_required $reboot"
} > "$tmp"

chmod 0644 "$tmp"
mv "$tmp" "$OUT"
