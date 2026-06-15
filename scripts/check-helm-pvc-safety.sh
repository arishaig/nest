#!/usr/bin/env bash
# Enforce the data-safety contract: no workload owns its own PersistentVolumeClaim.
#
# Every app's config/state PVC is a raw, Flux-owned manifest (a *-config-pvc.yaml
# under k8s/apps/) bound by the HelmRelease via persistence.<vol>.existingClaim.
# Keeping the PVC out of Helm's release ownership is what stops Flux from pruning
# it when the HelmRelease is updated or removed — the difference between a pod
# restart and silent data loss. A HelmRelease that omits existingClaim makes the
# bjw-s app-template chart render (and own) the PVC, labelled
# app.kubernetes.io/managed-by: Helm.
#
# This renders every HelmRelease and fails if any PersistentVolumeClaim carries
# that Helm-ownership label.
#
# Usage: check-helm-pvc-safety.sh [rendered-manifests.yaml]
#   With no argument, renders the cluster itself via `flux-local build all`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rendered="${1:-}"
if [[ -z "${rendered}" ]]; then
  rendered="$(mktemp)"
  trap 'rm -f "${rendered}"' EXIT
  flux-local build all --enable-helm --output-file "${rendered}" "${REPO_ROOT}/k8s/flux-system"
fi

# Walk the multi-document YAML. For each document track its kind, name, and
# whether it carries the Helm-ownership label; on the document boundary, flag
# any PVC that is Helm-owned. Uses [ \t] rather than [[:space:]] for mawk
# portability (the default awk on the Ubuntu-based runners).
violations=$(awk '
  function flush() {
    if (kind == "PersistentVolumeClaim" && helm) print "  " (name == "" ? "<unnamed>" : name)
  }
  /^---[ \t]*$/      { flush(); kind=""; name=""; helm=0; in_meta=0; next }
  /^kind:[ \t]/      { v=$0; sub(/^kind:[ \t]*/, "", v); kind=v; next }
  /^metadata:[ \t]*$/ { in_meta=1; next }
  in_meta && name == "" && /^  name:[ \t]/ { v=$0; sub(/^  name:[ \t]*/, "", v); name=v }
  /app\.kubernetes\.io\/managed-by:[ \t]*Helm/ { helm=1 }
  END { flush() }
' "${rendered}")

if [[ -n "${violations}" ]]; then
  echo "FAIL: Helm-owned PersistentVolumeClaim(s) found:" >&2
  echo "${violations}" >&2
  echo "      A HelmRelease is missing persistence.<vol>.existingClaim, so the" >&2
  echo "      app-template chart created the PVC — Flux will prune it (data loss)." >&2
  echo "      Add a raw *-config-pvc.yaml and bind it via existingClaim." >&2
  exit 1
fi

echo "[ok] no Helm-owned PersistentVolumeClaims — all PVCs are raw/Flux-owned"
