from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image as PILImage
from PIL.Image import Image

from .config import Config
from .heartbeat import (
    OFFLINE_WILL_PAYLOAD,
    Heartbeat,
    Status,
)
from .transforms import VALID_SCALES, apply_transforms

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_MIN_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0


def frame_topic(device_id: str) -> str:
    return f"tesserae/{device_id}/frame/png"

# The contract says "/renders/<64-hex-digest>.png" but we accept any
# reasonable hex suffix length — older Tesserae builds used 8-char prefixes
# and we don't want to refuse those.
_DIGEST_RE = re.compile(r"/renders/([0-9a-fA-F]{8,128})\.png$")


@dataclass(frozen=True)
class FrameRequest:
    url: str
    digest: str | None
    rotate: int
    scale: str
    bg: str
    saturation: float


class PaintFn(Protocol):
    def __call__(self, img: Image, saturation: float) -> None: ...


class Downloader(Protocol):
    def __call__(self, url: str, timeout_s: float, max_bytes: int) -> bytes: ...


def _require_field(obj: dict[str, Any], key: str) -> Any:
    if key not in obj:
        raise ValueError(f"payload missing '{key}'")
    return obj[key]


def _parse_rotate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"rotate must be int 0..3, got {type(value).__name__}")
    if not 0 <= value <= 3:
        raise ValueError(f"rotate must be 0..3, got {value}")
    return value


def _parse_scale(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"scale must be str, got {type(value).__name__}")
    if value not in VALID_SCALES:
        raise ValueError(
            f"scale must be one of {sorted(VALID_SCALES)}, got {value!r}"
        )
    return value


def _parse_bg(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"bg must be str, got {type(value).__name__}")
    # bg is intentionally tolerant of unknown names — transforms.bg_color
    # falls back to white. We do require *some* string so the contract is
    # explicit on the wire.
    return value


def _parse_saturation(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"saturation must be number, got {type(value).__name__}")
    sat = float(value)
    if not 0.0 <= sat <= 1.0:
        raise ValueError(f"saturation must be in 0.0..1.0, got {sat}")
    return sat


def parse_frame_payload(raw: bytes) -> FrameRequest:
    """Parse the JSON announcement from the frame topic. Raises on bad input.

    All five contract fields (url/rotate/scale/bg/saturation) are required
    present per the spec — we do not apply our own defaults that would
    override the server's choice. The single permissive corner is `bg`: any
    string is accepted at parse time, and transforms applies a white
    fallback for names the local palette table doesn't know.
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"payload is not valid utf-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("payload must be a JSON object")

    url_raw = _require_field(obj, "url")
    if not isinstance(url_raw, str) or not url_raw:
        raise ValueError("payload missing 'url' string")
    parsed = urllib.parse.urlparse(url_raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"url scheme must be http(s): {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError("url missing host")

    rotate = _parse_rotate(_require_field(obj, "rotate"))
    scale = _parse_scale(_require_field(obj, "scale"))
    bg = _parse_bg(_require_field(obj, "bg"))
    saturation = _parse_saturation(_require_field(obj, "saturation"))

    digest_match = _DIGEST_RE.search(parsed.path)
    digest = digest_match.group(1).lower() if digest_match else None
    return FrameRequest(
        url=url_raw,
        digest=digest,
        rotate=rotate,
        scale=scale,
        bg=bg,
        saturation=saturation,
    )


def http_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
    """Fetch the PNG via HTTP. Refuses responses larger than max_bytes."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise ValueError(
                        f"server reports {declared} bytes; "
                        f"exceeds max_frame_bytes={max_bytes}"
                    )
            except ValueError:
                raise
        body: bytes = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"frame larger than max_frame_bytes={max_bytes}")
    return body


def decode_png(raw: bytes) -> Image:
    """Decode raw PNG bytes into a PIL Image. Forces load so errors surface here.

    Raises OSError (or a PIL subclass thereof, like UnidentifiedImageError)
    for malformed input — the dispatcher's except clause catches both.
    """
    img = PILImage.open(io.BytesIO(raw))
    img.load()
    return img


class FrameDispatcher:
    """Single-flight frame worker.

    submit() never blocks; if a paint is in progress, the new request replaces
    any pending-but-not-yet-started request (newer wins). The worker thread
    serialises the actual paint calls.
    """

    def __init__(
        self,
        config: Config,
        paint_fn: PaintFn,
        panel_size: tuple[int, int],
        status: Status,
        heartbeat: Heartbeat,
        download_fn: Downloader = http_download,
    ) -> None:
        self._config = config
        self._paint = paint_fn
        self._panel_size = panel_size
        self._status = status
        self._heartbeat = heartbeat
        self._download = download_fn
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: FrameRequest | None = None
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="tesserae-paint", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def submit(self, request: FrameRequest) -> None:
        with self._cv:
            if (
                request.digest is not None
                and self._status.last_digest == request.digest
                and self._status.state != "error"
            ):
                log.info("skipping duplicate digest=%s", request.digest)
                return
            replaced = self._pending is not None
            self._pending = request
            self._cv.notify_all()
        if replaced:
            log.info("replaced pending frame (newer wins)")

    def process(self, request: FrameRequest) -> None:
        """Synchronously download + paint a single request. Used by tests."""
        self._handle(request)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._stop and self._pending is None:
                    self._cv.wait()
                if self._stop:
                    return
                request = self._pending
                self._pending = None
            assert request is not None
            self._handle(request)

    def _handle(self, request: FrameRequest) -> None:
        self._status.state = "rendering"
        self._heartbeat.kick()
        try:
            raw = self._download(
                request.url,
                timeout_s=self._config.http.download_timeout_s,
                max_bytes=self._config.http.max_frame_bytes,
            )
            img = decode_png(raw)
            transformed = apply_transforms(
                img,
                self._panel_size,
                rotate=request.rotate,
                scale=request.scale,
                bg=request.bg,
            )
            self._paint(transformed, request.saturation)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            ValueError,
            OSError,
        ) as exc:
            self._status.state = "error"
            self._status.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("frame paint failed url=%s", request.url)
            self._heartbeat.kick()
            return
        self._status.state = "idle"
        self._status.last_error = None
        self._status.last_paint_at = time.time()
        if request.digest is not None:
            self._status.last_digest = request.digest
        log.info("painted digest=%s", self._status.last_digest)
        self._heartbeat.kick()


class MqttClientLike(Protocol):
    def connect_async(self, host: str, port: int, keepalive: int) -> Any: ...

    def disconnect(self) -> Any: ...

    def loop_start(self) -> Any: ...

    def loop_stop(self) -> Any: ...

    def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any: ...

    def subscribe(self, topic: str, qos: int = 0) -> Any: ...

    def reconnect_delay_set(self, min_delay: float, max_delay: float) -> Any: ...

    def username_pw_set(self, username: str, password: str | None = None) -> Any: ...

    def will_set(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any: ...


class _SubmitDispatcher(Protocol):
    def submit(self, request: FrameRequest) -> None: ...


class MessageHandler:
    """Parses incoming MQTT payloads and submits them to the dispatcher.

    Tests construct one with a stub dispatcher and assert the call shape.
    """

    def __init__(
        self,
        dispatcher: _SubmitDispatcher,
        status: Status,
        heartbeat: Heartbeat,
        frame_topic: str,
    ) -> None:
        self._dispatcher = dispatcher
        self._status = status
        self._heartbeat = heartbeat
        self._frame_topic = frame_topic

    def handle(self, topic: str, payload: bytes) -> None:
        if topic != self._frame_topic:
            log.warning("ignored message on unexpected topic %r", topic)
            return
        try:
            request = parse_frame_payload(payload)
        except ValueError as exc:
            self._status.state = "error"
            self._status.last_error = f"bad payload: {exc}"
            log.warning("bad frame payload: %s", exc)
            self._heartbeat.kick()
            return
        self._dispatcher.submit(request)


def _build_paho_client(client_id: str) -> Any:
    """Build a paho-mqtt v2 client. Isolated so tests can stub it."""
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        client_id=client_id,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
    )


def make_mqtt_loop(
    config: Config,
    handler: MessageHandler,
    frame_topic: str,
    status_topic: str,
    client_factory: Callable[[str], Any] = _build_paho_client,
) -> Any:
    """Wire up a paho client with LWT, callbacks, and reconnect tuning."""
    client = client_factory(config.mqtt.client_id)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password or None)
    client.will_set(status_topic, OFFLINE_WILL_PAYLOAD, qos=1, retain=True)
    client.reconnect_delay_set(RECONNECT_BACKOFF_MIN_S, RECONNECT_BACKOFF_MAX_S)

    def on_connect(
        client_: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        log.info("MQTT connected reason_code=%s", reason_code)
        client_.subscribe(frame_topic, qos=1)

    def on_disconnect(
        client_: Any,
        userdata: Any,
        disconnect_flags: Any = None,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        log.warning("MQTT disconnected reason_code=%s", reason_code)

    def on_message(client_: Any, userdata: Any, msg: Any) -> None:
        handler.handle(msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


__all__ = [
    "RECONNECT_BACKOFF_MAX_S",
    "RECONNECT_BACKOFF_MIN_S",
    "Downloader",
    "FrameDispatcher",
    "FrameRequest",
    "MessageHandler",
    "MqttClientLike",
    "PaintFn",
    "decode_png",
    "frame_topic",
    "http_download",
    "make_mqtt_loop",
    "parse_frame_payload",
]
