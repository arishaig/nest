#!/usr/bin/env bash
# apt-updates-textfile.sh — Emit Prometheus textfile metrics for pending apt updates.
#
# Counts what a dist-upgrade would install (and how much of that comes from a
# -security suite) and writes it to a .prom file read by node_exporter's
# textfile collector. Prometheus alerts on it (the `updates` group in
# rules/nest.yml).
#
# Doesn't refresh the apt lists itself: apt-daily.timer does, daily, because
# unattended-upgrades.yml sets APT::Periodic::Update-Package-Lists. Without
# that setting the timer fires but does nothing, and these counts go stale.
#
# os_reboot_required is only written on hosts with their own kernel. LXCs run
# the PVE host's kernel, so the running kernel says nothing about them.
#
# Installed and scheduled by playbooks/provision/update-metrics.yml.

set -euo pipefail

TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
OUT="$TEXTFILE_DIR/os_updates.prom"

# A simulation needs no lock. If dpkg is mid-upgrade apt-get fails, set -e
# exits, and the previous file stays until the next run.
sim="$(apt-get -s -o Debug::NoLocking=1 dist-upgrade 2>/dev/null)"
pending="$(grep -c '^Inst ' <<<"$sim" || true)"
security="$(grep '^Inst ' <<<"$sim" | grep -c -- '-security' || true)"

# Running kernel vs the newest installed kernel of the same flavour (the last
# dash-separated part: "amd64", or "2712" on the Pi, which also ships -v8
# kernels for other boards).
reboot=""
if [ "$(systemd-detect-virt --container 2>/dev/null || true)" = "none" ]; then
  running="$(uname -r)"
  flavour="${running##*-}"
  newest="$(find /lib/modules -mindepth 1 -maxdepth 1 -printf '%f\n' | grep -- "-$flavour\$" | sort -V | tail -1)"
  if [ -n "$newest" ] && [ "$newest" != "$running" ]; then
    reboot=1
  else
    reboot=0
  fi
fi

tmp="$(mktemp "$OUT.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

{
  echo "# HELP os_updates_pending Packages a dist-upgrade would install or upgrade."
  echo "# TYPE os_updates_pending gauge"
  echo "os_updates_pending{manager=\"apt\"} $pending"
  echo "# HELP os_updates_pending_security Pending packages that come from a -security suite."
  echo "# TYPE os_updates_pending_security gauge"
  echo "os_updates_pending_security{manager=\"apt\"} $security"
  if [ -n "$reboot" ]; then
    echo "# HELP os_reboot_required 1 if a newer kernel is installed than the one running."
    echo "# TYPE os_reboot_required gauge"
    echo "os_reboot_required $reboot"
  fi
} > "$tmp"

chmod 0644 "$tmp"
mv "$tmp" "$OUT"
