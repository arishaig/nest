#!/usr/bin/env bash
# Watches the upstream issue/fork that block NVMe support on RPi5 nodes in
# this cluster (see docs/rpi5-talos.md "Known issues" for full context) and
# opens a tracking issue in THIS repo if anything material changes — so a
# fix landing surfaces via normal GitHub notifications instead of requiring
# anyone to remember to periodically re-check.
#
# Tracks two independent signals:
#   1. siderolabs/sbc-raspberrypi#23 closing — the actual upstream issue
#      (mainline U-Boot has no PCIe driver support on RPi5 at all).
#   2. New commits on the talos-rpi5/talos-builder community fork (carries
#      out-of-tree U-Boot PCIe patches) — last checked 2026-07-20, stale
#      since 2025-11-08. Renewed activity there is worth knowing about even
#      before/instead of an upstream fix landing.
#
# No new tooling required: uses curl + jq (both already on the CI runner),
# not the gh CLI.
set -euo pipefail

NVME_ISSUE_API="https://api.github.com/repos/siderolabs/sbc-raspberrypi/issues/23"
FORK_REPO_API="https://api.github.com/repos/talos-rpi5/talos-builder"
WATCH_LABEL="rpi5-nvme-watch"
# Fork's last known push at the time of the 2026-07-20 investigation. Any
# push after this means the fork became active again.
FORK_STALE_SINCE="2025-11-08T10:35:45Z"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set (owner/repo of this repo)}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"

auth_curl() {
  curl -sS -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" "$@"
}

# Skip entirely if a notice from a prior run is still open and unreviewed.
existing_open=$(auth_curl \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues?labels=${WATCH_LABEL}&state=open" \
  | jq 'length')
if [[ "${existing_open}" -gt 0 ]]; then
  echo "Tracking issue already open (label: ${WATCH_LABEL}), skipping check."
  exit 0
fi

nvme_state=$(auth_curl "${NVME_ISSUE_API}" | jq -r '.state')
fork_pushed_at=$(auth_curl "${FORK_REPO_API}" | jq -r '.pushed_at')

reason=""

if [[ "${nvme_state}" == "closed" ]]; then
  reason="${reason}- siderolabs/sbc-raspberrypi#23 is now CLOSED — NVMe boot support may have landed upstream.\n"
fi

if [[ "${fork_pushed_at}" > "${FORK_STALE_SINCE}" ]]; then
  reason="${reason}- talos-rpi5/talos-builder has new commits (last push ${fork_pushed_at}, was stale at ${FORK_STALE_SINCE}) — may be worth re-checking even without an upstream fix.\n"
fi

if [[ -z "${reason}" ]]; then
  echo "No change: sbc-raspberrypi#23 is ${nvme_state}, fork last pushed ${fork_pushed_at} (stale reference: ${FORK_STALE_SINCE})."
  exit 0
fi

body=$(printf 'Automated check found a status change relevant to the RPi5 NVMe blocker documented in docs/rpi5-talos.md known issues:\n\n%b\nRe-read https://github.com/siderolabs/sbc-raspberrypi/issues/23 and re-evaluate whether NVMe is viable on beta-rpi5 (and gamma, if joined by then). Close this issue once reviewed, whether or not NVMe gets re-enabled.\n' "${reason}")

payload=$(jq -n --arg title "RPi5 NVMe upstream status changed — re-check docs/rpi5-talos.md" \
  --arg body "${body}" \
  --arg label "${WATCH_LABEL}" \
  '{title: $title, body: $body, labels: [$label]}')

auth_curl -X POST "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" -d "${payload}" >/dev/null
echo "Opened tracking issue: ${reason}"
