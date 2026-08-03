from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxmoxSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_PROXMOX_")
    url: str = "https://192.168.1.16:8006"
    token: str = ""
    node: str = "proxmox"
    verify_tls: bool = False


class UniFiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_UNIFI_")
    url: str = "https://192.168.1.1"
    username: str = ""
    password: str = ""
    verify_tls: bool = False


class AdGuardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_ADGUARD_")
    # Direct IPs avoid DNS resolution dependency on LXC 109. AdGuard redirects
    # HTTP to HTTPS, and its cert doesn't cover raw IPs — hence verify_tls off.
    url: str = "https://192.168.7.7"
    url_secondary: str = "https://192.168.7.8"
    verify_tls: bool = False
    username: str = "adguard"
    password: str = ""


class HomeAssistantSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_HA_")
    # Direct IP avoids local DNS rewrite dependency on LXC 109
    url: str = "http://192.168.4.50:8123"
    token: str = ""


class PrometheusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_PROMETHEUS_")
    url: str = "http://192.168.1.44:9090"


class LokiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_LOKI_")
    url: str = "http://192.168.1.44:3100"


class GrafanaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_GRAFANA_")
    url: str = "http://192.168.1.44:3000"
    username: str = "admin"
    password: str = ""


class ScrutinySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_SCRUTINY_")
    url: str = "http://192.168.1.46:8888"


class TraefikSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_TRAEFIK_")
    # k8s Traefik API/metrics — MetalLB ingress LB, floats across nodes
    url: str = "http://192.168.1.117:8080"


class ArrSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_")
    # Talos node IP; ports are k8s NodePorts (see k8s/apps/media/*-service.yaml)
    arr_host: str = "192.168.1.110"
    sonarr_key: str = ""
    radarr_key: str = ""
    lidarr_key: str = ""
    prowlarr_key: str = ""


class JellyfinSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_JELLYFIN_")
    # nest-mcp runs in-cluster; reach jellyfin via its ClusterIP Service
    # rather than the NodePort removed in architecture-review.md E1.
    url: str = "http://jellyfin.media.svc.cluster.local:8096"
    key: str = ""


class JellyseerrSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_JELLYSEERR_")
    # nest-mcp runs in-cluster; reach seerr via its ClusterIP Service
    # rather than the NodePort removed in architecture-review.md E1.
    url: str = "http://seerr.media.svc.cluster.local:5055"
    key: str = ""


class VpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_VPS_")
    host: str = "66.42.79.175"
    ssh_user: str = "root"
    ssh_key: str = "~/.ssh/ansible-on-nest"
    instance_id: str = "60c6d8aa-0f76-44a4-a91d-ead0ab380cf2"
    vultr_api_key: str = ""


class DockerHostSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_DOCKER_HOST_")
    host: str = "192.168.1.158"
    ssh_key: str = "~/.ssh/ansible-on-nest"


class MealieSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_MEALIE_")
    # nest-mcp runs in-cluster; reach mealie via its ClusterIP Service
    # rather than the NodePort removed in architecture-review.md E1.
    url: str = "http://mealie.media.svc.cluster.local:9000"
    key: str = ""


class PbsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_PBS_")
    url: str = "https://192.168.1.113:8007"
    username: str = "root@pam"
    password: str = ""
    node: str = "backup"


class SeedboxSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_SEEDBOX_")
    host: str = "192.168.1.182"
    ssh_key: str = "~/.ssh/ansible-on-nest"


class FileserverSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_FILESERVER_")
    host: str = "192.168.1.16"  # PVE — ZFS pool owner, NFS source
    ssh_key: str = "~/.ssh/ansible-on-nest"
    media_root: str = "/Tank/media_root"


class AdguardHostSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_ADGUARD_HOST_")
    # SSH access for host-level checks (fail2ban, systemd unit status) — separate
    # from AdguardSettings, which talks to the AdGuard Home HTTP API.
    host: str = "192.168.7.7"
    user: str = "adguard"
    host_secondary: str = "192.168.7.8"
    user_secondary: str = "root"
    ssh_key: str = "~/.ssh/ansible-on-nest"


class KubernetesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_K8S_")
    # API VIP (6443 only — not a scrape target). Token from k8s_mcp_sa_token vault var.
    api_url: str = "https://192.168.1.115:6443"
    token: str = ""


proxmox = ProxmoxSettings()
unifi = UniFiSettings()
adguard = AdGuardSettings()
homeassistant = HomeAssistantSettings()
prometheus = PrometheusSettings()
loki = LokiSettings()
grafana = GrafanaSettings()
scrutiny = ScrutinySettings()
traefik = TraefikSettings()
arr = ArrSettings()
jellyfin = JellyfinSettings()
jellyseerr = JellyseerrSettings()
vps = VpsSettings()
docker_host = DockerHostSettings()
mealie = MealieSettings()
pbs = PbsSettings()
seedbox = SeedboxSettings()
fileserver = FileserverSettings()
kubernetes = KubernetesSettings()
adguard_host = AdguardHostSettings()
