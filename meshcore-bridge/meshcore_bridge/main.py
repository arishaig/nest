import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import paho.mqtt.client as mqtt

from . import decode
from .bridge import Bridge
from .config import settings


def log(record: dict) -> None:
    # One flat JSON object per line on stdout; Alloy ships it to Loki.
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class State:
    # Monotonic time the broker connection was last known good (or startup).
    last_ok = time.monotonic()
    connected = False


def healthz_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = State.connected or time.monotonic() - State.last_ok < settings.unhealthy_after_secs
            self.send_response(200 if ok and self.path == "/healthz" else 503)
            self.end_headers()

        def log_message(self, *args):
            pass

    return Handler


def serve() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="meshcore-bridge")
    client.username_pw_set(settings.mqtt_user, settings.mqtt_pass)
    bridge = Bridge(
        decode.parse_channels(settings.decode_channels),
        settings.decoded_topic_prefix,
        settings.dedupe_ttl_secs,
        log,
        lambda topic, payload: client.publish(topic, payload, qos=1),
    )

    def on_connect(c, userdata, flags, reason, props):
        State.connected = not reason.is_failure
        State.last_ok = time.monotonic()
        log({"kind": "bridge", "event": "connect", "reason": str(reason)})
        if not reason.is_failure:
            c.subscribe(settings.subscribe_topic)

    def on_disconnect(c, userdata, flags, reason, props):
        State.connected = False
        log({"kind": "bridge", "event": "disconnect", "reason": str(reason)})

    def on_message(c, userdata, msg):
        try:
            bridge.handle(msg.topic, msg.payload)
        except Exception as e:  # one bad packet must not kill the subscriber
            log({"kind": "bridge", "event": "error", "error": repr(e), "topic": msg.topic})

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    threading.Thread(
        target=HTTPServer(("", settings.listen_port), healthz_handler()).serve_forever,
        daemon=True).start()
    log({"kind": "bridge", "event": "start", "decode_channels": settings.decode_channels})
    client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=30)
    client.loop_forever(retry_first_connection=True)
