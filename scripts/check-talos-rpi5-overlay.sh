#!/usr/bin/env bash
# Gate Talos version bumps on Raspberry Pi 5 image availability.
#
# The Image Factory only builds rpi_5 images once the sbc-raspberrypi overlay
# is released for that Talos version, and overlay releases can lag a fresh
# Talos patch release by a few days (siderolabs/talos#12748). Merging a bump
# before the overlay exists would leave the Pi nodes unable to upgrade.
#
# Checks:
#   1. factory.talos.dev serves the rpi_5 overlay for the pinned talos_version
#   2. the installer image tags in talos/patches/controlplane-*-rpi5.yaml
#      match talos_version (Renovate bumps both; this catches drift)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TFVARS="${REPO_ROOT}/terraform/terraform.tfvars"

TALOS_VERSION=$(grep -oP '^talos_version\s*=\s*"\K[^"]+' "${TFVARS}")
if [[ -z "${TALOS_VERSION}" ]]; then
  echo "FAIL: could not parse talos_version from ${TFVARS}" >&2
  exit 1
fi
echo "Pinned Talos version: ${TALOS_VERSION}"

# ── 1. Factory serves the rpi_5 overlay for this version ────────────────────
OVERLAYS_URL="https://factory.talos.dev/version/${TALOS_VERSION}/overlays/official"
if ! overlays=$(curl -fsS --max-time 30 "${OVERLAYS_URL}"); then
  echo "FAIL: could not fetch ${OVERLAYS_URL}" >&2
  echo "      (unknown version, or the factory is unreachable)" >&2
  exit 1
fi

if ! grep -q '"name":"rpi_5"' <<<"${overlays}"; then
  echo "FAIL: Image Factory has no rpi_5 overlay for Talos ${TALOS_VERSION} yet." >&2
  echo "      The sbc-raspberrypi overlay release usually follows within days —" >&2
  echo "      hold this bump until it appears at ${OVERLAYS_URL}" >&2
  exit 1
fi
echo "[ok] Image Factory serves rpi_5 overlay for ${TALOS_VERSION}"

# ── 2. Pi machine-config installer tags match talos_version ─────────────────
rc=0
for patch in "${REPO_ROOT}"/talos/patches/controlplane-*-rpi5.yaml; do
  tag=$(grep -oP 'factory\.talos\.dev/installer/[a-f0-9]+:\K\S+' "${patch}")
  if [[ "${tag}" == "${TALOS_VERSION}" ]]; then
    echo "[ok] ${patch#"${REPO_ROOT}"/}: installer tag ${tag}"
  else
    echo "FAIL: ${patch#"${REPO_ROOT}"/}: installer tag ${tag} != talos_version ${TALOS_VERSION}" >&2
    rc=1
  fi
done
exit "${rc}"
