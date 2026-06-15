# ARC lint runners (`arc-lint`)

Stateless PR lint/validate jobs run on **ephemeral Kubernetes pods** via [Actions
Runner Controller (ARC)](https://docs.github.com/en/actions/concepts/runners/actions-runner-controller).
The recovery-critical **deploy** jobs deliberately stay on the dedicated LXC
runner (`ci`, 192.168.1.18) — see [Why the split](#why-the-split).

## Architecture

```
GitHub Actions ──long-poll──► listener pod (arc-systems)
                                   │ job queued for runs-on: arc-lint
                                   ▼
                         ephemeral runner pod (arc-runners)
                         image: ghcr.io/arishaig/nest-ci-runner:latest
                         runs ONE job, then is destroyed
```

| Piece | Where |
|-------|-------|
| Controller HelmRelease | `k8s/infrastructure/arc/controller.yaml` (ns `arc-systems`) |
| Runner scale set HelmRelease | `k8s/infrastructure/arc/runner-set-lint.yaml` (ns `arc-runners`, name `arc-lint`) |
| Chart source (OCI) | `k8s/infrastructure/sources/actions-runner-controller.yaml` |
| Runner image | `ci/runner/Dockerfile` → built by `build-ci-runner` in `deploy.yml` |
| GitHub auth secret | `arc-github-secret` (ns `arc-runners`), created by `playbooks/provision/k8s.yml` from `vault_github_runner_pat` |
| Workflow label | `runs-on: arc-lint` in `.github/workflows/lint.yml` (Phase 2 — see [Rollout](#rollout-two-phase--avoids-a-bootstrap-deadlock)) |

`minRunners: 0` → no pods at idle. `maxRunners: 4` → burst cap.

## Why the split

Deploy jobs (`deploy-tofu`, `deploy-k8s`, the ansible deploys) hold local
OpenTofu state, secrets, and kubeconfig, and are what *recovers* the cluster.
Running them as pods inside the cluster they manage is a bootstrap/blast-radius
trap. So lint moves to the cluster; deploys stay on the LXC. Tradeoff: PR lint
now depends on cluster uptime (acceptable — nothing recovery-critical needs it).

## The runner image

`ci/runner/Dockerfile` bakes the tooling the lint jobs need (OpenTofu, promtool,
amtool, kustomize, kubeconform, kubectl, shellcheck 0.10.0, docker CLI, and an
Ansible venv at `/opt/ansible-runner/venv`) at the **same paths** the LXC runner
uses, so workflow steps run unchanged on either runner. Tool versions mirror
`playbooks/provision/runner.yml` and are Renovate-managed via `# renovate:` ARG
annotations.

**To bump tooling:** edit the ARG (or let Renovate), merge → `build-ci-runner`
rebuilds and pushes `:latest`; the next lint job pulls it (imagePullPolicy
Always). No rollout needed.

## Rollout (two-phase — avoids a bootstrap deadlock)

`lint.yml` triggers on `pull_request` using the workflow file **from the PR
branch**. If the label flip to `arc-lint` shipped in the same PR as the ARC
infra, that PR's own required checks would target a runner set that doesn't
exist until *after* merge — an unmergeable deadlock. So:

- **Phase 1 (this infra):** ARC manifests, source, namespace, the `k8s.yml`
  secret, `ci/runner/Dockerfile`, and `build-ci-runner`. `lint.yml` still says
  `self-hosted`, so Phase 1's own checks pass on the LXC.
- **Gate:** after Phase 1 merges, confirm (a) `build-ci-runner` pushed the
  image, (b) **the GHCR package is public** (new packages default to private →
  `ImagePullBackOff`), (c) the `arc-lint` listener is registered, and (d) **one
  real job runs to completion on `arc-lint`** (e.g. a throwaway
  `workflow_dispatch` job) — "listener Running" alone doesn't prove a runner pod
  can pull the image and register.
- **Phase 2:** flip the nine stateless jobs in `lint.yml` to `runs-on: arc-lint`
  and drop the `tofu` job's `TF_PLUGIN_CACHE_DIR` env (ephemeral pods have no
  persistent cache). The runners now exist, so this PR's own checks validate on
  them.

## Operations

```bash
# Controller + listener health
kubectl -n arc-systems get pods
kubectl -n arc-runners  get pods            # ephemeral runners (empty at idle)
kubectl -n arc-runners  get autoscalingrunnerset

# Listener stuck / not creating runners → check auth + listener logs
kubectl -n arc-systems logs -l app.kubernetes.io/component=runner-scale-set-listener
kubectl -n arc-runners  get secret arc-github-secret    # must exist (from k8s.yml)
```

Common issues:
- **No runners spawn, listener crashlooping:** PAT expired or wrong scope.
  Re-run `playbooks/provision/k8s.yml` after rotating `vault_github_runner_pat`.
- **`ImagePullBackOff` on runner pods:** `ghcr.io/arishaig/nest-ci-runner` must
  be public (like `nest-mcp`) or have a pull secret.
- **A lint job fails only on ARC:** a tool/path differs from the LXC — reconcile
  `ci/runner/Dockerfile` with `playbooks/provision/runner.yml`.

## Known tech debt

Auth uses a PAT, which expires and silently halts runners. Migrate to a GitHub
App — see [`tech-debt.md`](tech-debt.md).
