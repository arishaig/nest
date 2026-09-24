#!/usr/bin/env bash
# Guards against silent deploy drift: every Docker service whose compose file
# lives in the repo must be wired into the deploy workflow's paths-filter.
#
# When you add a new service under playbooks/provision/files/<svc>/ but forget
# to add its path to the 'docker' filter in .github/workflows/deploy.yml, the
# deploy job simply never runs for that service — no error, no signal. This
# check turns that silent miss into a loud CI failure.
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

workflow=".github/workflows/deploy.yml"
files_root="playbooks/provision/files"
missing=0

while IFS= read -r compose; do
  dir=$(dirname "$compose")            # e.g. playbooks/provision/files/seedbox
  if ! grep -q "$dir" "$workflow"; then
    echo "ERROR: $dir has a docker-compose.yml but no matching path in $workflow"
    missing=1
  fi
done < <(find "$files_root" -mindepth 2 -maxdepth 2 -name docker-compose.yml | sort)

if [ "$missing" -ne 0 ]; then
  echo
  echo "Add the missing path(s) to the 'docker' filter in $workflow so that"
  echo "changes to those services trigger a deploy."
  exit 1
fi

echo "OK: all Docker service compose files are covered by the deploy filter."

# Same guard one level up: every provision playbook must be wired into a
# deploy path filter, or be listed here with the reason it isn't. A new host
# playbook without a deploy job would otherwise only ever converge during a
# manual site.yml run (pbs.yml did exactly that until it got deploy-pbs).
# Shrink this list as gaps get wired in; never grow it without a reason.
declare -A not_deployed=(
  [alloy.yml]="multi-host log shipper, converged by site.yml only"
  [common.yml]="base config for lxcs:dns, converged by site.yml only"
  [docker-host.yml]="LXC 100 is decommissioned (architecture-review F6)"
  [nftables.yml]="LXC firewall, converged by site.yml only"
  [pve-passthrough.yml]="parametrized per-LXC, invoked by OpenTofu"
  [runner.yml]="provisions the CI runner itself; running it from CI is circular"
  [scrutiny.yml]="compose stack ships via deploy-docker; playbook is site.yml only"
  [seedbox.yml]="compose stack ships via deploy-docker; playbook is site.yml only"
)

pb_missing=0
while IFS= read -r pb; do
  name=$(basename "$pb")
  if grep -q "'$pb'" "$workflow"; then
    if [ -n "${not_deployed[$name]+x}" ]; then
      echo "ERROR: $name is now in $workflow; remove it from the not_deployed list"
      pb_missing=1
    fi
  elif [ -z "${not_deployed[$name]+x}" ]; then
    echo "ERROR: $pb has no path in $workflow and no not_deployed entry"
    pb_missing=1
  fi
done < <(find playbooks/provision -maxdepth 1 -name '*.yml' | sort)

for name in "${!not_deployed[@]}"; do
  if [ ! -f "playbooks/provision/$name" ]; then
    echo "ERROR: not_deployed lists $name, which no longer exists"
    pb_missing=1
  fi
done

if [ "$pb_missing" -ne 0 ]; then
  echo
  echo "Wire the playbook into a paths-filter + deploy job in $workflow, or add"
  echo "it to not_deployed in $0 with the reason it can't be."
  exit 1
fi

echo "OK: every provision playbook is deployed by CI or explicitly exempted."
