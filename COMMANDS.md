# Commands Cheatsheet

## Ansible

```bash
# Run everything (apt updates, Docker, Proxmox)
ansible-playbook playbooks/site.yml --ask-vault-pass

# Individual update playbooks
ansible-playbook playbooks/update_apt.yml --ask-vault-pass
ansible-playbook playbooks/update_docker.yml --ask-vault-pass
ansible-playbook playbooks/update_proxmox.yml --ask-vault-pass

# Provision / configure a specific host
ansible-playbook playbooks/provision/site.yml --ask-vault-pass --limit docker

# Dry run (check mode)
ansible-playbook playbooks/site.yml --ask-vault-pass --check
```

## Terraform

```bash
cd terraform

# Plan changes
terraform plan -var-file=secrets.tfvars

# Apply changes
terraform apply -var-file=secrets.tfvars

# Target a specific resource (e.g. AdGuard rewrites)
terraform apply -var-file=secrets.tfvars -target=adguard_rewrite_rule.rewrites
```

## Kubernetes

```bash
# Bulk subtitle re-sync (one-time setup: create secret from NFS config on PVE)
kubectl create secret generic bazarr-sync-config -n media \
  --from-file=config.yaml=/rpool/data/docker-apps/bazarr-sync/config.yaml

# Run bazarr-sync (auto-cleans after 10 minutes)
kubectl apply -f k8s/apps/media/bazarr-sync-job.yaml
```

## Vault

```bash
# Encrypt vault file (after editing in plaintext)
ansible-vault encrypt inventory/group_vars/all/vault.yml

# Decrypt to plaintext (for editing)
ansible-vault decrypt inventory/group_vars/all/vault.yml

# Edit in-place without decrypting to disk
ansible-vault edit inventory/group_vars/all/vault.yml

# Re-pull secrets from live infrastructure
./scripts/pull-secrets.sh
```
