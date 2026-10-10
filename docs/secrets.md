# Secrets

Moving from ansible-vault to **SOPS + age**, in phases. This page is the
reference for the key and the rules. It's updated as each phase lands.

| Phase | What | Status |
|---|---|---|
| 0 | sops/age on both CI runners (#696), age key, `.sops.yaml`, this doc | in progress |
| 1 | `vault.yml` → `inventory/group_vars/all/secrets.sops.yaml` (`community.sops` vars plugin); scripts and CI move to sops | — |
| 2 | k8s Secrets generated into `k8s/**/*.sops.yaml`, decrypted by Flux (closes architecture-review C2) | — |
| 3 | `terraform/secrets.tfvars` → `terraform/secrets.sops.json` | — |

Until phase 1 lands, `inventory/group_vars/all/vault.yml` (ansible-vault) is
still the source of truth.

## Why
- **Reviewable diffs.** SOPS encrypts values, not keys, so a diff shows which
  key changed. ansible-vault re-encrypts the whole file on every change.
- **No more push.** Flux decrypts SOPS natively, so k8s Secrets can live in
  `k8s/` and be reconciled instead of pushed by `playbooks/provision/k8s.yml`.
- **One source.** It replaces the untracked `terraform/secrets.tfvars`.

Rejected: a secrets server (OpenBao or Infisical with External Secrets),
Sealed Secrets (k8s only), and Bitwarden Secrets Manager at runtime (an outside
service in the deploy path). Key names are visible in this public repo by
design; values never are.

## The age key

One keypair. Public key, which is also in `.sops.yaml`:

```
age17pgmpmdrwrdxtw3rxlyumtcl78nwv4d3errll2u687uvhkxmzg4qefuw95
```

The private key lives in:

| Where | Used by |
|---|---|
| `~/.config/sops/age/keys.txt` (0600) on the workstation | sops, Ansible, scripts |
| GitHub secret `SOPS_AGE_KEY` | CI jobs |
| k8s Secret `flux-system/sops-age` (phase 2) | Flux kustomize-controller |
| Bitwarden secure note | custody only, nothing automated reads it |
| Flash drive | custody only |

**If every copy is lost, every SOPS-encrypted secret is lost.** Values would
have to be re-collected from the live systems (`scripts/pull-secrets.sh` covers
some). Check both custody copies after any key change.

## Encryption rules (`.sops.yaml`)

| Path | Encrypted |
|---|---|
| `k8s/**/*.sops.yaml` | only `data`/`stringData`, so kind and metadata stay readable |
| `inventory/**/*.sops.yaml` | every value |
| `terraform/*.sops.json` | every value |

`sops` refuses files matching no rule (`no matching creation rules found`).

## Everyday use
```sh
sops edit inventory/group_vars/all/secrets.sops.yaml        # edit in $EDITOR
sops set inventory/group_vars/all/secrets.sops.yaml '["new_key"]' '"value"'
sops decrypt --extract '["some_key"]' inventory/group_vars/all/secrets.sops.yaml
```

## Rotating the age key
1. `age-keygen -o new.txt`. Add the new public key to every rule in
   `.sops.yaml` alongside the old one.
2. `sops updatekeys -y <file>` for every encrypted file, then remove the old
   key from `.sops.yaml` and run `updatekeys` again.
3. Replace every copy in the table above, both custody copies included.
4. Old ciphertext in git history stays readable with the old key, so rotate
   any value that the old key exposed.

## History note
Pre-migration `vault.yml` revisions stay in git history, encrypted with the
ansible-vault password. Keep that password secured after phase 1, or rotate the
secrets it covered.
