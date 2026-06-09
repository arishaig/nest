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
    # Direct IPs avoid DNS resolution dependency on LXC 109; plain HTTP avoids cert mismatch
    url: str = "http://192.168.7.7:3000"
    url_secondary: str = "http://192.168.7.8:80"
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
    # k8s Traefik API/metrics — hostPort on Talos node
    url: str = "http://192.168.1.110:8080"


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
    # Jellyfin k8s NodePort (k8s/apps/media/jellyfin-pgsql-service.yaml)
    url: str = "http://192.168.1.110:30814"
    key: str = ""


class JellyseerrSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEST_JELLYSEERR_")
    # Seerr k8s NodePort (k8s/apps/media/seerr-service.yaml)
    url: str = "http://192.168.1.110:30801"
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
    # Mealie k8s NodePort (k8s/apps/media/mealie-service.yaml)
    url: str = "http://192.168.1.110:30813"
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
