from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUBGEN_UI_")
    # Loki lives on the monitoring LXC, outside the cluster (Alloy pushes there).
    loki_url: str = "http://192.168.1.44:3100"
    kube_url: str = "https://kubernetes.default.svc"
    namespace: str = "media"
    deployment: str = "subgen"
    # Matches the app.kubernetes.io/instance label bjw-s app-template sets.
    pod_selector: str = "app.kubernetes.io/instance=subgen"
    window_hours: int = 3
    listen_port: int = 8080


settings = Settings()
