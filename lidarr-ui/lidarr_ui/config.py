from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIDARR_UI_")
    lidarr_url: str = "http://lidarr:8686"
    lidarr_key: str = ""
    listen_port: int = 8080


settings = Settings()
