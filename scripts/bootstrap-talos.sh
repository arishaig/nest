#!/usr/bin/env bash
# Bootstrap Talos Linux and Flux on VM 110.
#
# Prerequisites (install before running):
#   - talosctl  https://www.talos.dev/latest/introduction/getting-started/
#   - kubectl   https://kubernetes.io/docs/tasks/tools/
#   - flux      https://fluxcd.io/flux/installation/
#   - gh        https://cli.github.com/
#
# Usage:
#   1. Run `terraform apply` to create the VM and download the ISO.
#   2. Boot VM 110 in Proxmox — it will start in Talos maintenance mode and
#      get a DHCP IP. Find that IP in the Proxmox console or your DHCP leases.
#   3. Run: ./scripts/bootstrap-talos.sh <dhcp-ip>
#
# What this script does:
#   1. Generates Talos machine config (saved to ~/.talos/clusterconfig/)
#   2. Applies the config to the VM — triggers install and reboot to static IP
#   3. Bootstraps etcd (one-time cluster init)
#   4. Exports kubeconfig to ~/.kube/config
#   5. Runs `flux bootstrap github` to install Flux and push k8s/flux-system/
#   6. Adds the apps Flux Kustomization so nginx-test gets deployed

set -euo pipefail

# ── Config (must match talos/patches/controlplane.yaml and terraform.tfvars) ──
TALOS_IP="192.168.1.110"
CLUSTER_NAME="talos-nest"
CLUSTER_ENDPOINT="https://${TALOS_IP}:6443"
CONFIG_DIR="${HOME}/.talos/clusterconfig"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITHUB_OWNER="arishaig"
GITHUB_REPO="nest"

# ── Prerequisite check ───────────────────────────────────────────────────────
check_cmd() {
  if ! command -v "$1" &>/dev/null; then
    echo "ERROR: '$1' not found. Install it before running this script." >&2
    exit 1
  fi
}

check_cmd talosctl
check_cmd kubectl
check_cmd flux
check_cmd gh
check_cmd git

# flux bootstrap reads GITHUB_TOKEN from the environment; gh's stored creds are not enough.
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  GITHUB_TOKEN="$(gh auth token 2>/dev/null)" || true
  if [[ -z "${GITHUB_TOKEN}" ]]; then
    echo "ERROR: GITHUB_TOKEN is not set and 'gh auth token' returned nothing." >&2
    echo "       Run 'gh auth login' first, then re-run this script." >&2
    exit 1
  fi
  export GITHUB_TOKEN
fi

# ── Parse args ───────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <dhcp-ip-of-vm>"
  echo "  Find the VM's current DHCP IP in the Proxmox console or your router's DHCP leases."
  exit 1
fi

DHCP_IP="$1"

echo "==> Talos bootstrap"
echo "    DHCP IP (maintenance mode): ${DHCP_IP}"
echo "    Static IP (after install):  ${TALOS_IP}"
echo "    Cluster endpoint:           ${CLUSTER_ENDPOINT}"
echo ""

# ── Step 1: Generate machine config ─────────────────────────────────────────
echo "==> [1/6] Generating Talos machine config..."
mkdir -p "${CONFIG_DIR}"

talosctl gen config "${CLUSTER_NAME}" "${CLUSTER_ENDPOINT}" \
  --config-patch @"${REPO_ROOT}/talos/patches/controlplane.yaml" \
  --output-dir "${CONFIG_DIR}" \
  --force

echo "    Config written to ${CONFIG_DIR}/"
echo "    WARNING: ${CONFIG_DIR}/talosconfig contains cluster credentials — do not commit it."

# ── Step 2: Apply config (triggers install + reboot) ────────────────────────
echo ""
echo "==> [2/6] Applying machine config to VM at ${DHCP_IP}..."
echo "    The VM will install Talos to disk and reboot. This takes ~2 minutes."

talosctl apply-config \
  --nodes "${DHCP_IP}" \
  --insecure \
  --file "${CONFIG_DIR}/controlplane.yaml"

echo "    Config applied. Waiting for VM to reboot and come up on ${TALOS_IP}..."

# ── Step 3: Wait for static IP to respond ───────────────────────────────────
export TALOSCONFIG="${CONFIG_DIR}/talosconfig"

echo ""
echo "==> [3/6] Waiting for Talos to come up on ${TALOS_IP}..."
until talosctl --nodes "${TALOS_IP}" version --timeout 5s &>/dev/null; do
  echo "    Still waiting..."
  sleep 10
done
echo "    Talos is up."

# ── Step 4: Bootstrap etcd (one-time only) ──────────────────────────────────
echo ""
echo "==> [4/6] Bootstrapping etcd..."
talosctl bootstrap --nodes "${TALOS_IP}"

echo "    Waiting for Kubernetes API to become ready..."
until talosctl --nodes "${TALOS_IP}" health --wait-timeout 5m &>/dev/null; do
  sleep 10
done

# ── Step 5: Export kubeconfig ────────────────────────────────────────────────
echo ""
echo "==> [5/6] Exporting kubeconfig..."
talosctl kubeconfig --nodes "${TALOS_IP}" --force
echo "    kubeconfig merged into ~/.kube/config"
echo ""
kubectl get nodes

# ── Step 5b: PSA exemptions ─────────────────────────────────────────────────
# local-path-provisioner needs privileged pods; label its namespace to exempt
# it from the default baseline PodSecurity policy before Flux deploys it.
echo ""
echo "==> [5b] Applying PodSecurity exemption for local-path-storage namespace..."
kubectl create namespace local-path-storage --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace local-path-storage \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/warn=privileged \
  pod-security.kubernetes.io/audit=privileged \
  --overwrite

# ── Step 6: Flux bootstrap ───────────────────────────────────────────────────
echo ""
echo "==> [6/6] Bootstrapping Flux..."
echo "    This will push k8s/flux-system/ to GitHub and install Flux controllers."
echo "    You need a GitHub PAT with 'repo' scope. gh auth login if not already done."
echo ""

flux bootstrap github \
  --owner="${GITHUB_OWNER}" \
  --repository="${GITHUB_REPO}" \
  --branch=main \
  --path=k8s/flux-system \
  --personal \
  --token-auth

# ── Step 7: Add apps Kustomization ──────────────────────────────────────────
# flux bootstrap pushed flux-system/ to main; switch there before committing.
echo ""
echo "==> [post-bootstrap] Adding apps Flux Kustomization..."

cd "${REPO_ROOT}"
git fetch origin main
git checkout main
git pull --rebase

cat > k8s/flux-system/apps.yaml <<'YAML'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./k8s/apps
  prune: true
  wait: true
  timeout: 5m
YAML

git add k8s/flux-system/apps.yaml
git commit -m "feat(k8s): add apps Flux Kustomization"
git push

echo ""
echo "==> Done!"
echo ""
echo "    Flux will now sync k8s/apps/ — nginx-test should appear shortly:"
echo "      flux get kustomizations"
echo "      kubectl get pods -n nginx-test"
echo ""
echo "    Verify the reconciliation loop:"
echo "      flux logs --follow"
echo ""
echo "    talosconfig: ${CONFIG_DIR}/talosconfig"
echo "    Set TALOSCONFIG=${CONFIG_DIR}/talosconfig or merge into ~/.talos/config"
