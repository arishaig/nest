#!/usr/bin/env bash
# Guards against CI toolchain drift: ci/runner/Dockerfile (arc-lint) and
# playbooks/provision/runner.yml (the LXC self-hosted runner) install the
# same CLI tools for the same lint/deploy jobs and must stay on the same
# versions. Renovate manages each pin independently (separate PRs, separate
# merge times), so nothing previously stopped them drifting apart between
# merges — this turns that into a loud CI failure instead of a silent one.
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

dockerfile="ci/runner/Dockerfile"
runner_yml="playbooks/provision/runner.yml"
mismatch=0

# dep name : Dockerfile ARG : runner.yml var
declare -A pins=(
  [opentofu]="OPENTOFU_VERSION:opentofu_version"
  [prometheus]="PROMETHEUS_VERSION:prometheus_version"
  [alertmanager]="ALERTMANAGER_VERSION:alertmanager_version"
  [kustomize]="KUSTOMIZE_VERSION:kustomize_version"
  [kubeconform]="KUBECONFORM_VERSION:kubeconform_version"
  [kubectl]="KUBECTL_VERSION:kubectl_version"
  [helm]="HELM_VERSION:helm_version"
  [talosctl]="TALOSCTL_VERSION:talosctl_version"
  [flux_local]="FLUX_LOCAL_VERSION:flux_local_version"
  [flux]="FLUX_VERSION:flux_version"
)

for dep in "${!pins[@]}"; do
  arg_name="${pins[$dep]%%:*}"
  var_name="${pins[$dep]#*:}"

  dockerfile_version=$(grep -oP "ARG ${arg_name}=\K\S+" "$dockerfile")
  runner_version=$(grep -oP "${var_name}: \"\K[^\"]+" "$runner_yml")

  if [ "$dockerfile_version" != "$runner_version" ]; then
    echo "ERROR: $dep version mismatch — $dockerfile has $dockerfile_version, $runner_yml has $runner_version"
    mismatch=1
  fi
done

if [ "$mismatch" -ne 0 ]; then
  echo
  echo "Bring both pins to the same version. Whichever Renovate PR lands second"
  echo "for a shared dependency should bump the other file too."
  exit 1
fi

echo "OK: runner toolchain versions match between $dockerfile and $runner_yml."
