# Commands Cheatsheet

## Ansible

```bash
# Run everything (apt updates, Docker, Proxmox)
ansible-playbook playbooks/site.yml

# Individual update playbooks
ansible-playbook playbooks/update_apt.yml
ansible-playbook playbooks/update_docker.yml
ansible-playbook playbooks/update_proxmox.yml

# Provision / configure a specific host
ansible-playbook playbooks/provision/site.yml --limit docker

# Dry run (check mode)
ansible-playbook playbooks/site.yml --check
```

## OpenTofu

```bash
cd terraform

# Plan changes
tofu plan -var-file=secrets.tfvars

# Apply changes
tofu apply -var-file=secrets.tfvars

# Target a specific resource (e.g. AdGuard rewrites)
tofu apply -var-file=secrets.tfvars -target=adguard_rewrite_rule.rewrites
```

## Kubernetes

```bash
# Bulk subtitle re-sync (one-time setup: create secret from NFS config on PVE)
kubectl create secret generic bazarr-sync-config -n media \
  --from-file=config.yaml=/rpool/data/docker-apps/bazarr-sync/config.yaml

# Run bazarr-sync (auto-cleans after 10 minutes)
kubectl apply -f k8s/apps/media/bazarr-sync-job.yaml
```

## Talos

```bash
# Upgrade a node to the version in terraform/terraform.tfvars (one node at a
# time; wait for it to return Ready before the next — etcd quorum holds 1 loss)
talosctl upgrade --nodes 192.168.1.110 \
  --image factory.talos.dev/installer/<schematic_id>:<version>

# Watch the node come back
talosctl --nodes 192.168.1.110 health
kubectl get nodes -o wide   # confirm VERSION/OS-IMAGE updated, STATUS Ready
```

## Secrets (SOPS, see docs/secrets.md)

```bash
# Edit in $EDITOR (decrypts to a temp file, re-encrypts on save)
sops edit inventory/group_vars/all.sops.yaml

# Read or set one key
sops decrypt --extract '["some_key"]' inventory/group_vars/all.sops.yaml
sops set inventory/group_vars/all.sops.yaml '["some_key"]' '"value"'

# Re-pull secrets from live infrastructure
./scripts/pull-secrets.sh
```
