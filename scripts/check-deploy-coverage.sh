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
