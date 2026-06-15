# Tech debt log

Known shortcuts taken deliberately, with the better path noted. Newest first.

## ARC auth: migrate PAT → GitHub App

**Added:** 2026-06-15 · **Severity:** medium · **Area:** CI / k8s

The `arc-lint` runner scale set authenticates to GitHub with a **PAT**
(`vault_github_runner_pat`, surfaced as the `arc-github-secret` Secret by
`playbooks/provision/k8s.yml`). This reuses the token the LXC runner already
uses, so it shipped with zero new GitHub setup.

**Why it's debt:** the PAT changed role from a *one-shot* provision credential
(minted a registration token once, in `runner.yml`) to a *continuous* runtime
credential — ARC's listener long-polls with it indefinitely. Fine-grained PATs
expire (≤1 year). When it lapses, the listener stops minting runners and **PR
checks silently hang** with no obvious cause.

**Better path:** create a **GitHub App**, install it on `arishaig/nest`, and
store `github_app_id` / `github_app_installation_id` / `github_app_private_key`
in vault. App credentials don't expire and have higher rate limits — the
ARC-recommended auth method. Changes needed:
- vault: add the three app values; remove the PAT reuse for ARC.
- `playbooks/provision/k8s.yml`: write the three keys into `arc-github-secret`
  instead of `github_token`.
- `k8s/infrastructure/arc/runner-set-lint.yaml`: no change (it already
  references the secret by name; ARC auto-detects PAT vs App from the keys).

See [`arc-runners.md`](arc-runners.md).
