#!/usr/bin/env bash
# Watches for upstream progress that would unblock NVMe boot on RPi5 nodes in
# this cluster (see docs/rpi5-talos.md "Known issues" for full context) and
# opens a tracking issue in THIS repo if anything material changes — so a fix
# landing surfaces via normal GitHub notifications instead of requiring
# anyone to remember to periodically re-check.
#
# Previously tracked siderolabs/sbc-raspberrypi#23 closing and activity on the
# talos-rpi5/talos-builder fork. #23 closed 2026-01-24 for unrelated reasons
# (general RPi5 boot/ethernet, not PCIe/NVMe) and produced a false-positive
# tracking issue (#380) that sat unreviewed for two months; the fork remains
# stale since 2025-11-08 and isn't a live signal either. Replaced with the two
# artifacts that actually gate this cluster:
#
#   1. Mainline U-Boot's configs/rpi_arm64_defconfig gaining CONFIG_NVME (or
#      CONFIG_NVME_PCI) — the PCIe enable and NVMe inbound-DMA-offset fixes
#      already merged to master (as of 2026-09-20) but the defconfig doesn't
#      turn NVMe on yet. This is the "upstream fix is complete" signal.
#   2. A new siderolabs/sbc-raspberrypi release whose notes mention U-Boot,
#      NVMe, or PCIe — the overlay this cluster's Talos image actually boots
#      from needs to pick up whatever lands in (1) before it matters here.
#      Checked against the latest release known to have no such change
#      (v0.2.2, 2026-09-14) as a fallback, but the body grep fires regardless
#      of tag so a same-tag re-release with updated notes still gets caught.
#
# No new tooling required: uses curl + jq (both already on the CI runner),
# not the gh CLI.
set -euo pipefail

DEFCONFIG_URL="https://raw.githubusercontent.com/u-boot/u-boot/master/configs/rpi_arm64_defconfig"
OVERLAY_RELEASES_API="https://api.github.com/repos/siderolabs/sbc-raspberrypi/releases?per_page=5"
WATCH_LABEL="rpi5-nvme-watch"
# Latest overlay release confirmed to have no U-Boot/NVMe/PCIe changes as of
# the 2026-09-20 re-investigation.
OVERLAY_LAST_CLEAN_TAG="v0.2.2"

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

defconfig=$(curl -sS "${DEFCONFIG_URL}")
nvme_enabled="no"
if grep -qE '^CONFIG_NVME(_PCI)?=y' <<<"${defconfig}"; then
  nvme_enabled="yes"
fi

releases=$(auth_curl "${OVERLAY_RELEASES_API}")
latest_tag=$(jq -r '.[0].tag_name' <<<"${releases}")
# Grep every fetched release's notes, not just the latest, in case a relevant
# change landed in a patch release between runs. Requires BOTH a u-boot
# mention AND an nvme/rpi5 mention — "pcie" alone is too broad (v0.2.1's
# notes mention an unrelated Amlogic Meson PCIe driver bump, a false match
# on a bare "pcie" grep).
matching_release=$(jq -r '.[]
  | select(((.body // "") | test("u-?boot"; "i"))
    and ((.body // "") | test("nvme|rpi.?5|raspberry.?pi.?5"; "i")))
  | .tag_name' <<<"${releases}" | head -1)

reason=""

if [[ "${nvme_enabled}" == "yes" ]]; then
  reason="${reason}- mainline U-Boot's configs/rpi_arm64_defconfig now enables CONFIG_NVME — the upstream fix chain (PCIe enable + inbound-DMA-offset patch) appears complete.\n"
fi

if [[ -n "${matching_release}" ]]; then
  reason="${reason}- siderolabs/sbc-raspberrypi release ${matching_release} mentions U-Boot/NVMe/PCIe in its notes — may have picked up the upstream fix.\n"
fi

if [[ -z "${reason}" ]]; then
  echo "No change: rpi_arm64_defconfig NVMe=${nvme_enabled}, latest overlay release ${latest_tag} (last known clean: ${OVERLAY_LAST_CLEAN_TAG}), no matching release notes."
  exit 0
fi

body=$(printf 'Automated check found upstream progress relevant to the RPi5 NVMe blocker documented in docs/rpi5-talos.md known issues:\n\n%bRe-read docs/rpi5-talos.md and re-evaluate whether NVMe is viable on beta-rpi5 (and gamma). Verify against the actual overlay image before re-enabling anything — the defconfig and release-notes signals above are necessary but not sufficient; confirm the built image actually boots NVMe before touching worker-*-rpi5.yaml. Close this issue once reviewed, whether or not NVMe gets re-enabled.\n' "${reason}")

payload=$(jq -n --arg title "RPi5 NVMe upstream status changed — re-check docs/rpi5-talos.md" \
  --arg body "${body}" \
  --arg label "${WATCH_LABEL}" \
  '{title: $title, body: $body, labels: [$label]}')

auth_curl -X POST "https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" -d "${payload}" >/dev/null
echo "Opened tracking issue: ${reason}"
