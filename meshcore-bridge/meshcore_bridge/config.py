from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MESHCORE_BRIDGE_")
    # HA's Mosquitto add-on, on the IoT VLAN.
    mqtt_host: str = "192.168.4.50"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    # Observer topics are meshcore/<IATA>/<node key>/{packets,status}.
    subscribe_topic: str = "meshcore/#"
    # Decoded hits are republished here as <prefix>/<channel without '#'>;
    # HA triggers on them. Outside the observer's per-node topic tree.
    decoded_topic_prefix: str = "meshcore/decoded"
    # Hashtag channels to decrypt (key = sha256(name)[:16]). Only these are
    # ever decoded; every other channel's text stays encrypted in the logs.
    decode_channels: str = "#emergency"
    # The same flood packet is heard repeatedly, with a growing path but an
    # identical hash; notify once per hash within this window.
    dedupe_ttl_secs: int = 1800
    listen_port: int = 8080
    # /healthz fails once the broker connection has been down this long.
    unhealthy_after_secs: int = 120


settings = Settings()
