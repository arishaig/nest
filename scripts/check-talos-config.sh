#!/usr/bin/env bash
# Statically validate every Talos control-plane and worker machine config.
#
# Renders each talos/patches/{controlplane,worker}*.yaml into a full machine config with
# throwaway secrets (no real cluster material needed, so this runs anywhere) and
# validates it for the metal platform. Catches malformed patches — bad field
# types, an invalid network interface, a broken VIP — before they ever reach a
# node. This is the static counterpart to the docker integration test: it covers
# the VM/network surface (ens18, static IPs, the .115 VIP, the RPi5
# deviceSelector/HostnameConfig) that a docker-provisioned cluster structurally
# cannot exercise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCHES_DIR="${REPO_ROOT}/talos/patches"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

secrets="${workdir}/secrets.yaml"
talosctl gen secrets -o "${secrets}" >/dev/null

rc=0
for patch in "${PATCHES_DIR}"/controlplane*.yaml; do
  out="${workdir}/$(basename "${patch}" .yaml).yaml"
  if ! talosctl gen config test-cluster https://192.168.1.115:6443 \
        --with-secrets "${secrets}" \
        --config-patch-control-plane "@${patch}" \
        --output-types controlplane \
        --output "${out}" \
        --with-docs=false --with-examples=false 2>"${workdir}/err"; then
    echo "FAIL: ${patch#"${REPO_ROOT}"/}: gen config rejected the patch" >&2
    sed 's/^/      /' "${workdir}/err" >&2
    rc=1
    continue
  fi
  if talosctl validate -c "${out}" -m metal >/dev/null 2>"${workdir}/err"; then
    echo "[ok] ${patch#"${REPO_ROOT}"/}"
  else
    echo "FAIL: ${patch#"${REPO_ROOT}"/}: invalid machine config for metal mode" >&2
    sed 's/^/      /' "${workdir}/err" >&2
    rc=1
  fi
done

for patch in "${PATCHES_DIR}"/worker*.yaml; do
  [[ -e "${patch}" ]] || continue
  out="${workdir}/$(basename "${patch}" .yaml).yaml"
  if ! talosctl gen config test-cluster https://192.168.1.115:6443 \
        --with-secrets "${secrets}" \
        --config-patch-worker "@${patch}" \
        --output-types worker \
        --output "${out}" \
        --with-docs=false --with-examples=false 2>"${workdir}/err"; then
    echo "FAIL: ${patch#"${REPO_ROOT}"/}: gen config rejected the patch" >&2
    sed 's/^/      /' "${workdir}/err" >&2
    rc=1
    continue
  fi
  if talosctl validate -c "${out}" -m metal >/dev/null 2>"${workdir}/err"; then
    echo "[ok] ${patch#"${REPO_ROOT}"/}"
  else
    echo "FAIL: ${patch#"${REPO_ROOT}"/}: invalid machine config for metal mode" >&2
    sed 's/^/      /' "${workdir}/err" >&2
    rc=1
  fi
done
exit "${rc}"
