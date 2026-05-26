# ansible-on-nest

Terraform + Ansible IaC for a Proxmox home lab with a Vultr VPS proxy.

See [docs/design.md](docs/design.md) for the full technical design and architecture overview.
See [docs/dependencies.md](docs/dependencies.md) for a dependency inventory with licenses and GitHub links.

## Architecture

### Full diagram
![Architecture](docs/architecture.png)

### Client ingress flow
![Client flow](docs/architecture-flow.png)

> Diagrams are generated from the live IaC — run `python3 scripts/generate_diagram.py` to regenerate.
> Requires `graphviz` and `pip install -r scripts/requirements-diagram.txt`.

## Layout

```
terraform/          HCL resources (PVE LXCs/VMs, AdGuard DNS rewrites)
playbooks/          Ansible — post-provision config, Docker services, nftables
inventory/          hosts.yml
scripts/            Tooling (diagram generation)
docs/               Generated outputs
```

## Quick commands

```bash
# Apply infra
terraform -chdir=terraform apply -var-file=secrets.tfvars

# Run all playbooks
ansible-playbook playbooks/site.yml --ask-vault-pass

# Regenerate diagrams
python3 scripts/generate_diagram.py
```
