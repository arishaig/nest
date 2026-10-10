# Secrets

Moving from ansible-vault to **SOPS + age**, in phases. This page is the
reference for the key and the rules. It's updated as each phase lands.

| Phase | What | Status |
|---|---|---|
| 0 | sops/age on both CI runners (#696), age + SSH recovery keys, `.sops.yaml`, this doc | done |
| 1 | `vault.yml` → `inventory/group_vars/all.sops.yaml` (`community.sops` vars plugin); scripts and CI move to sops | done |
| 2 | k8s Secrets generated into `k8s/**/*.sops.yaml`, decrypted by Flux (closes architecture-review C2) | in progress: `media` done; authelia, cert-manager, nest-mcp next |
| 3 | `terraform/secrets.tfvars` → `terraform/secrets.sops.json` | — |

The source of truth is `inventory/group_vars/all.sops.yaml`.

**Why `group_vars/all.sops.yaml` and not `group_vars/all/…`:** Ansible's
default vars loader reads every YAML file *inside* `group_vars/all/`, so an
encrypted file there would also be loaded raw. Without the SOPS plugin that
silently yields ciphertext as the value (tested: a 151-char `ENC[…]` string
instead of a 32-char API key). As a sibling file, only the
`community.sops.sops` vars plugin loads it, and a run without the plugin fails
loudly on an undefined variable instead.

**Enabling the plugin:**
- The repo's `ansible.cfg` sets `vars_plugins_enabled = host_group_vars,community.sops.sops`.
- Anything that runs Ansible from another directory (the `terraform/`
  provisioners) sets `ANSIBLE_VARS_ENABLED=host_group_vars,community.sops.sops`.
- CI writes `SOPS_AGE_KEY` to a file and exports `SOPS_AGE_KEY_FILE`.

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

## Keys

Every SOPS file is encrypted to **two recipients**. Either one alone decrypts
it (tested).

**Working key: native age.** Flux's kustomize-controller parses its key Secret
line by line as native age identities, so it can't use an SSH key.

```
age17pgmpmdrwrdxtw3rxlyumtcl78nwv4d3errll2u687uvhkxmzg4qefuw95
```

| Where | Used by |
|---|---|
| `~/.config/sops/age/keys.txt` (0600) on the workstation | sops, Ansible, scripts |
| GitHub secret `SOPS_AGE_KEY` | CI jobs |
| k8s Secret `flux-system/sops-age` (phase 2) | Flux kustomize-controller |

**Recovery key: ed25519 SSH, held in Bitwarden** as an SSH key item, so
Bitwarden imports it natively. Nothing automated uses it. It exists so that
losing every copy of the age key doesn't mean losing the secrets.

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPpbfa/Dw9jY/uvl6k549/LREsabJ21/rM52NGuofFCN nest-sops-recovery
```

To recover, export the key from Bitwarden to a file, then:

```sh
SOPS_AGE_SSH_PRIVATE_KEY_FILE=./nest-sops-recovery sops decrypt <file>
# then re-key: generate a new age key and follow "Rotating" below
```

**If both keys are lost, every SOPS-encrypted secret is lost.** Values would
have to be re-collected from the live systems (`scripts/pull-secrets.sh` covers
some).

## Encryption rules (`.sops.yaml`)

| Path | Encrypted |
|---|---|
| `k8s/**/*.sops.yaml` | only `data`/`stringData`, so kind and metadata stay readable |
| `inventory/**/*.sops.yaml` | every value |
| `terraform/*.sops.json` | every value |

`sops` refuses files matching no rule (`no matching creation rules found`).

## Everyday use
```sh
sops edit inventory/group_vars/all.sops.yaml        # edit in $EDITOR
sops set inventory/group_vars/all.sops.yaml '["new_key"]' '"value"'
sops decrypt --extract '["some_key"]' inventory/group_vars/all.sops.yaml
```

## k8s Secrets (phase 2)

Flux reconciles k8s Secrets from SOPS-encrypted files. Nothing pushes them.

- **Spec:** `playbooks/provision/k8s-secrets.yml` lists each Secret (name,
  namespace, data). Values reference the same vars as everything else, so
  `group_vars/all.sops.yaml` stays the single copy.
- **Generated files:** `scripts/render-k8s-secrets.sh` renders the spec through
  Ansible and writes `k8s/<dir>/<name>.sops.yaml`. Only `stringData`/`data` is
  encrypted. It also adds the file to that directory's `kustomization.yaml`.
  It rewrites a file only when the decrypted content would change, so
  re-running it never creates noise diffs.
- **Flux:** the `apps` and `infrastructure` Kustomizations decrypt with
  `flux-system/sops-age`. That's the one Secret `k8s.yml` still pushes,
  because Flux can't decrypt the key it needs to decrypt.
- **CI:** `lint.yml` runs `render-k8s-secrets.sh --check`. It fails if a
  generated file is missing, stale, orphaned or not listed. The k8s-validate
  job strips the `sops:` metadata block before strict kubeconform, so the
  Secrets themselves are still schema-checked.

To add or change a k8s Secret:
1. Edit the entry in `k8s-secrets.yml`, and the value in `all.sops.yaml`
   (`sops edit` / `sops set`) if needed.
2. Run `./scripts/render-k8s-secrets.sh`.
3. Commit both. Flux applies it on merge.

## Rotating the age key
1. `age-keygen -o new.txt`. Add the new public key to the `recipients`
   anchor in `.sops.yaml`, alongside the old one.
2. `sops updatekeys -y <file>` for every encrypted file, then remove the old
   key from `.sops.yaml` and run `updatekeys` again.
3. Replace every copy in the tables above. The recovery key rotates the same
   way: `ssh-keygen -t ed25519`, then import the new key into Bitwarden.
4. Old ciphertext in git history stays readable with the old key, so rotate
   any value that the old key exposed.

## History note
Pre-migration `vault.yml` revisions stay in git history, encrypted with the
ansible-vault password, and so do the `terraform.tfstate.*.vault` copies on the
NAS (`scripts/backup-state.sh` writes `*.age` from now on). Keep that password
secured, or rotate the secrets it covered. Nothing current uses it: the
`ANSIBLE_VAULT_PASS` GitHub secret and `~/.config/ansible-on-nest/vault-pass`
can be deleted once phase 1 has had a clean deploy.
