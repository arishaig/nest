import json
import time
from collections.abc import Callable

from . import decode

# Observer payloads carry numbers as strings; LogQL `unwrap` and the health
# rules want real numbers.
_NUMERIC = ("len", "payload_len", "packet_type", "SNR", "RSSI", "score")


def _number(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f.is_integer() else f


class Bridge:
    """Turn observer MQTT messages into log records and decoded-hit publishes.

    `log` receives one flat dict per message (shipped to Loki by Alloy as a
    JSON line); `publish` receives (topic, payload) for hits on a decoded
    channel, once per packet hash.
    """

    def __init__(self, channels, decoded_topic_prefix, dedupe_ttl_secs,
                 log: Callable[[dict], None],
                 publish: Callable[[str, str], None],
                 clock: Callable[[], float] = time.monotonic):
        self.channels = channels
        self.prefix = decoded_topic_prefix.rstrip("/")
        self.ttl = dedupe_ttl_secs
        self.log = log
        self.publish = publish
        self.clock = clock
        self._seen: dict[str, float] = {}

    def handle(self, topic: str, payload: bytes) -> None:
        if topic.startswith(self.prefix + "/"):
            return  # our own republish
        kind = topic.rsplit("/", 1)[-1]
        try:
            body = json.loads(payload)
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self.log({"kind": "unparsed", "topic": topic,
                      "payload": payload.decode("utf-8", "replace")})
            return
        if kind == "packets":
            for k in _NUMERIC:
                if k in body:
                    body[k] = _number(body[k])
            self.log({"kind": "packet", **body})
            self._maybe_decode(body)
        elif kind == "status":
            self.log({"kind": "status", **body})
        else:
            self.log({"kind": "other", "topic": topic, **body})

    def _maybe_decode(self, packet: dict) -> None:
        # decode() reads the string-typed wire fields; _NUMERIC already
        # converted them, which it accepts too.
        hit = decode.decode(packet, self.channels)
        if hit is None:
            return
        key = str(packet.get("hash", ""))
        now = self.clock()
        self._seen = {h: t for h, t in self._seen.items() if t > now}
        first = key not in self._seen
        if first:
            self._seen[key] = now + self.ttl
        record = {"channel": hit.channel, "sender": hit.sender, "text": hit.text,
                  "sent_at": hit.sent_at, "hash": key,
                  "snr": packet.get("SNR"), "rssi": packet.get("RSSI"),
                  "origin": packet.get("origin")}
        self.log({"kind": "decoded", "duplicate": not first, **record})
        if first:
            self.publish(f"{self.prefix}/{hit.channel.lstrip('#')}", json.dumps(record))
