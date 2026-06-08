pve_endpoint   = "https://192.168.1.16:8006"
pve_node       = "proxmox"
gateway        = "192.168.1.1"
adguard_host   = "dns.arishaig.site"
adguard_port   = 443
adguard_scheme = "https"

# Talos — fill in before applying
# 1. Check latest release: https://github.com/siderolabs/talos/releases
# 2. Generate schematic at https://factory.talos.dev (add siderolabs/qemu-guest-agent)
talos_version      = "v1.13.3"
talos_schematic_id = "ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515"
talos_ip           = "192.168.1.110"
