from __future__ import annotations

import io
import json
from typing import Any

import pytest
from PIL.Image import Image
from PIL.Image import new as new_image

from tesserae_pi_client.config import (
    Config,
    HttpConfig,
    LoggingConfig,
    MqttConfig,
)
from tesserae_pi_client.heartbeat import Heartbeat, Status, status_topic
from tesserae_pi_client.mqtt_loop import (
    FrameDispatcher,
    FrameRequest,
    MessageHandler,
    decode_png,
    frame_topic,
    make_mqtt_loop,
    parse_frame_payload,
)

DEVICE_ID = "pi_png"
FRAME_TOPIC = frame_topic(DEVICE_ID)
STATUS_TOPIC = status_topic(DEVICE_ID)


def _config() -> Config:
    return Config(
        mqtt=MqttConfig(
            host="h", port=1883, username="", password="", client_id="cid", keepalive=60
        ),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
    )


def _valid_payload(
    *,
    url: str = "http://h/renders/3f7a91b2c4e5d6f8.png",
    rotate: int = 0,
    scale: str = "fit",
    bg: str = "white",
    saturation: float = 0.5,
) -> bytes:
    return json.dumps(
        {"url": url, "rotate": rotate, "scale": scale, "bg": bg, "saturation": saturation}
    ).encode("utf-8")


def _red_png_bytes(width: int = 50, height: int = 50) -> bytes:
    img = new_image("RGB", (width, height), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes, int, bool]] = []

    def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any:
        self.publishes.append((topic, payload, qos, retain))
        return None


class CapturingDispatcher:
    def __init__(self) -> None:
        self.submitted: list[FrameRequest] = []

    def submit(self, request: FrameRequest) -> None:
        self.submitted.append(request)


# --- parse_frame_payload ------------------------------------------------------


def test_parse_extracts_all_required_fields() -> None:
    digest = "3f7a91b2c4e5d6f8" * 4
    raw = _valid_payload(
        url=f"http://server:8000/renders/{digest}.png",
        rotate=2,
        scale="fill",
        bg="black",
        saturation=0.7,
    )
    req = parse_frame_payload(raw)
    assert req.url.endswith(f"/renders/{digest}.png")
    assert req.digest == digest
    assert req.rotate == 2
    assert req.scale == "fill"
    assert req.bg == "black"
    assert req.saturation == 0.7


def test_parse_accepts_int_saturation() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abcdef01.png",
            "rotate": 0,
            "scale": "fit",
            "bg": "white",
            "saturation": 1,  # JSON int — should coerce to float
        }
    ).encode()
    req = parse_frame_payload(raw)
    assert req.saturation == 1.0
    assert isinstance(req.saturation, float)


def test_parse_rejects_missing_url() -> None:
    raw = json.dumps(
        {"rotate": 0, "scale": "fit", "bg": "white", "saturation": 0.5}
    ).encode()
    with pytest.raises(ValueError, match="url"):
        parse_frame_payload(raw)


def test_parse_rejects_missing_rotate() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abc.png",
            "scale": "fit",
            "bg": "white",
            "saturation": 0.5,
        }
    ).encode()
    with pytest.raises(ValueError, match="rotate"):
        parse_frame_payload(raw)


def test_parse_rejects_missing_scale() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abc.png",
            "rotate": 0,
            "bg": "white",
            "saturation": 0.5,
        }
    ).encode()
    with pytest.raises(ValueError, match="scale"):
        parse_frame_payload(raw)


def test_parse_rejects_missing_bg() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abc.png",
            "rotate": 0,
            "scale": "fit",
            "saturation": 0.5,
        }
    ).encode()
    with pytest.raises(ValueError, match="bg"):
        parse_frame_payload(raw)


def test_parse_rejects_missing_saturation() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abc.png",
            "rotate": 0,
            "scale": "fit",
            "bg": "white",
        }
    ).encode()
    with pytest.raises(ValueError, match="saturation"):
        parse_frame_payload(raw)


def test_parse_rejects_invalid_rotate_range() -> None:
    with pytest.raises(ValueError, match="rotate"):
        parse_frame_payload(_valid_payload(rotate=4))
    with pytest.raises(ValueError, match="rotate"):
        parse_frame_payload(_valid_payload(rotate=-1))


def test_parse_rejects_bool_rotate() -> None:
    raw = json.dumps(
        {
            "url": "http://h/renders/abc.png",
            "rotate": True,
            "scale": "fit",
            "bg": "white",
            "saturation": 0.5,
        }
    ).encode()
    with pytest.raises(ValueError, match="rotate"):
        parse_frame_payload(raw)


def test_parse_rejects_unknown_scale() -> None:
    with pytest.raises(ValueError, match="scale"):
        parse_frame_payload(_valid_payload(scale="zoom"))


def test_parse_rejects_saturation_out_of_range() -> None:
    with pytest.raises(ValueError, match="saturation"):
        parse_frame_payload(_valid_payload(saturation=1.5))
    with pytest.raises(ValueError, match="saturation"):
        parse_frame_payload(_valid_payload(saturation=-0.1))


def test_parse_accepts_short_hex_digest() -> None:
    raw = _valid_payload(url="http://h/renders/3f7a91b2.png")
    req = parse_frame_payload(raw)
    assert req.digest == "3f7a91b2"


def test_parse_url_without_digest_pattern_returns_none() -> None:
    raw = _valid_payload(url="http://h/some/other/path.png")
    req = parse_frame_payload(raw)
    assert req.digest is None


def test_parse_rejects_non_object() -> None:
    raw = json.dumps([1, 2, 3]).encode()
    with pytest.raises(ValueError, match="object"):
        parse_frame_payload(raw)


def test_parse_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="JSON"):
        parse_frame_payload(b"\x00not-json\xff")


def test_parse_rejects_non_http_scheme() -> None:
    raw = _valid_payload(url="ftp://h/renders/abc.png")
    with pytest.raises(ValueError, match="scheme"):
        parse_frame_payload(raw)


def test_parse_rejects_url_without_host() -> None:
    raw = _valid_payload(url="http:///renders/abc.png")
    with pytest.raises(ValueError, match="host"):
        parse_frame_payload(raw)


# --- decode_png ---------------------------------------------------------------


def test_decode_png_returns_pil_image() -> None:
    raw = _red_png_bytes(20, 10)
    img = decode_png(raw)
    assert isinstance(img, Image)
    assert img.size == (20, 10)
    assert img.getpixel((0, 0)) == (255, 0, 0)


def test_decode_png_raises_on_garbage() -> None:
    with pytest.raises(OSError):
        decode_png(b"this is not a png")


# --- MessageHandler -----------------------------------------------------------


def _handler_pair() -> tuple[MessageHandler, CapturingDispatcher, Status, FakePublisher]:
    status = Status(panel="inky_impression_7_3")
    publisher = FakePublisher()
    heartbeat = Heartbeat(status=status, publisher=publisher, topic=STATUS_TOPIC)
    dispatcher = CapturingDispatcher()
    handler = MessageHandler(
        dispatcher=dispatcher,
        status=status,
        heartbeat=heartbeat,
        frame_topic=FRAME_TOPIC,
    )
    return handler, dispatcher, status, publisher


def test_handler_dispatches_valid_payload() -> None:
    handler, dispatcher, status, _ = _handler_pair()
    handler.handle(FRAME_TOPIC, _valid_payload())
    assert len(dispatcher.submitted) == 1
    req = dispatcher.submitted[0]
    assert req.scale == "fit"
    assert req.bg == "white"
    assert status.state == "idle"
    assert status.last_error is None


def test_handler_records_error_on_bad_payload() -> None:
    handler, dispatcher, status, _ = _handler_pair()
    handler.handle(FRAME_TOPIC, b"not json")
    assert dispatcher.submitted == []
    assert status.state == "error"
    assert status.last_error is not None
    assert "bad payload" in status.last_error


def test_handler_ignores_unexpected_topic() -> None:
    handler, dispatcher, status, _ = _handler_pair()
    handler.handle("some/other/topic", _valid_payload())
    assert dispatcher.submitted == []
    assert status.state == "idle"


def test_handler_publishes_no_paint_on_malformed_payload() -> None:
    """The contract: malformed payload must NOT cause a paint."""
    handler, dispatcher, _status, _ = _handler_pair()
    handler.handle(FRAME_TOPIC, b'{"url": "not-a-url"}')
    assert dispatcher.submitted == []


# --- FrameDispatcher.process (synchronous variant for tests) ------------------


def test_dispatcher_paints_valid_png() -> None:
    cfg = _config()
    png_bytes = _red_png_bytes(50, 50)

    def fake_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
        return png_bytes

    paint_calls: list[tuple[Any, float]] = []

    def fake_paint(img: Any, saturation: float) -> None:
        paint_calls.append((img, saturation))

    status = Status(panel="inky_impression_7_3")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    dispatcher = FrameDispatcher(
        config=cfg,
        paint_fn=fake_paint,
        panel_size=(800, 480),
        status=status,
        heartbeat=heartbeat,
        download_fn=fake_download,
    )
    req = FrameRequest(
        url="http://h/renders/abcdef01.png",
        digest="abcdef01",
        rotate=0,
        scale="fit",
        bg="white",
        saturation=0.6,
    )
    dispatcher.process(req)
    assert len(paint_calls) == 1
    img, sat = paint_calls[0]
    assert sat == 0.6
    # The transform pipeline must produce a panel-sized image.
    assert img.size == (800, 480)
    assert status.state == "idle"
    assert status.last_digest == "abcdef01"
    assert status.last_error is None
    assert status.last_paint_at is not None


def test_dispatcher_records_download_failure() -> None:
    cfg = _config()

    def boom_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
        raise TimeoutError("server unreachable")

    paint_calls: list[Any] = []

    def fake_paint(img: Any, saturation: float) -> None:
        paint_calls.append((img, saturation))

    status = Status(panel="inky_impression_7_3")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    dispatcher = FrameDispatcher(
        config=cfg,
        paint_fn=fake_paint,
        panel_size=(800, 480),
        status=status,
        heartbeat=heartbeat,
        download_fn=boom_download,
    )
    req = FrameRequest(
        url="http://h/renders/xx.png",
        digest="xx",
        rotate=0,
        scale="fit",
        bg="white",
        saturation=0.5,
    )
    dispatcher.process(req)
    assert paint_calls == []
    assert status.state == "error"
    assert status.last_error is not None
    assert "TimeoutError" in status.last_error


def test_dispatcher_records_decode_failure() -> None:
    cfg = _config()

    def garbage_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
        return b"not actually a png"

    paint_calls: list[Any] = []

    def fake_paint(img: Any, saturation: float) -> None:
        paint_calls.append((img, saturation))

    status = Status(panel="inky_impression_7_3")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    dispatcher = FrameDispatcher(
        config=cfg,
        paint_fn=fake_paint,
        panel_size=(800, 480),
        status=status,
        heartbeat=heartbeat,
        download_fn=garbage_download,
    )
    req = FrameRequest(
        url="http://h/renders/xx.png",
        digest="xx",
        rotate=0,
        scale="fit",
        bg="white",
        saturation=0.5,
    )
    dispatcher.process(req)
    assert paint_calls == []
    assert status.state == "error"
    assert status.last_error is not None


def test_dispatcher_skips_duplicate_digest() -> None:
    cfg = _config()
    png_bytes = _red_png_bytes(50, 50)

    download_calls = [0]

    def counting_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
        download_calls[0] += 1
        return png_bytes

    def fake_paint(img: Any, saturation: float) -> None:
        pass

    status = Status(panel="inky_impression_7_3")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    dispatcher = FrameDispatcher(
        config=cfg,
        paint_fn=fake_paint,
        panel_size=(800, 480),
        status=status,
        heartbeat=heartbeat,
        download_fn=counting_download,
    )
    req = FrameRequest(
        url="http://h/renders/aaaa.png",
        digest="aaaa",
        rotate=0,
        scale="fit",
        bg="white",
        saturation=0.5,
    )
    dispatcher.process(req)
    assert download_calls[0] == 1
    # Second submit with same digest should be skipped before download.
    dispatcher.submit(req)
    assert download_calls[0] == 1


def test_dispatcher_applies_rotate_and_scale_from_payload() -> None:
    cfg = _config()
    # 100x50 source -> rotate 1 (CW) -> 50x100 -> stretched to (200, 200).
    img_src = new_image("RGB", (100, 50), (0, 255, 0))
    buf = io.BytesIO()
    img_src.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    def fake_download(url: str, timeout_s: float, max_bytes: int) -> bytes:
        return png_bytes

    painted: list[Image] = []

    def fake_paint(img: Any, saturation: float) -> None:
        painted.append(img)

    status = Status(panel="test")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    dispatcher = FrameDispatcher(
        config=cfg,
        paint_fn=fake_paint,
        panel_size=(200, 200),
        status=status,
        heartbeat=heartbeat,
        download_fn=fake_download,
    )
    dispatcher.process(
        FrameRequest(
            url="http://h/renders/abcd1234.png",
            digest="abcd1234",
            rotate=1,
            scale="stretch",
            bg="white",
            saturation=0.5,
        )
    )
    assert len(painted) == 1
    assert painted[0].size == (200, 200)


# --- make_mqtt_loop wiring ----------------------------------------------------


class FakeMqttClient:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.will: tuple[str, bytes, int, bool] | None = None
        self.username: tuple[str, str | None] | None = None
        self.backoff: tuple[float, float] | None = None
        self.subscribed: list[tuple[str, int]] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None

    def will_set(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any:
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username: str, password: str | None = None) -> Any:
        self.username = (username, password)

    def reconnect_delay_set(self, min_delay: float, max_delay: float) -> Any:
        self.backoff = (min_delay, max_delay)

    def subscribe(self, topic: str, qos: int = 0) -> Any:
        self.subscribed.append((topic, qos))


def test_make_mqtt_loop_sets_lwt_and_backoff() -> None:
    cfg = _config()
    status = Status(panel="test")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    handler = MessageHandler(
        dispatcher=CapturingDispatcher(),
        status=status,
        heartbeat=heartbeat,
        frame_topic=FRAME_TOPIC,
    )
    fake_client = FakeMqttClient("cid")
    client = make_mqtt_loop(
        cfg,
        handler,
        frame_topic=FRAME_TOPIC,
        status_topic=STATUS_TOPIC,
        client_factory=lambda cid: fake_client,
    )
    assert client is fake_client
    assert fake_client.will is not None
    topic, payload, qos, retain = fake_client.will
    assert topic == STATUS_TOPIC
    assert qos == 1 and retain is True
    assert json.loads(payload.decode())["state"] == "offline"
    assert fake_client.backoff == (1.0, 60.0)
    # on_connect should subscribe to the frame topic at QoS 1.
    fake_client.on_connect(fake_client, None, None, 0, None)
    assert (FRAME_TOPIC, 1) in fake_client.subscribed


def test_make_mqtt_loop_passes_credentials_when_set() -> None:
    cfg = Config(
        mqtt=MqttConfig(
            host="h",
            port=1883,
            username="alice",
            password="hunter2",
            client_id="cid",
            keepalive=60,
        ),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=1_000_000),
        logging=LoggingConfig(level="INFO"),
    )
    status = Status(panel="test")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    handler = MessageHandler(
        dispatcher=CapturingDispatcher(),
        status=status,
        heartbeat=heartbeat,
        frame_topic=FRAME_TOPIC,
    )
    fake_client = FakeMqttClient("cid")
    make_mqtt_loop(
        cfg,
        handler,
        frame_topic=FRAME_TOPIC,
        status_topic=STATUS_TOPIC,
        client_factory=lambda cid: fake_client,
    )
    assert fake_client.username == ("alice", "hunter2")


def test_make_mqtt_loop_omits_credentials_when_unset() -> None:
    cfg = _config()
    status = Status(panel="test")
    heartbeat = Heartbeat(status=status, publisher=FakePublisher(), topic=STATUS_TOPIC)
    handler = MessageHandler(
        dispatcher=CapturingDispatcher(),
        status=status,
        heartbeat=heartbeat,
        frame_topic=FRAME_TOPIC,
    )
    fake_client = FakeMqttClient("cid")
    make_mqtt_loop(
        cfg,
        handler,
        frame_topic=FRAME_TOPIC,
        status_topic=STATUS_TOPIC,
        client_factory=lambda cid: fake_client,
    )
    assert fake_client.username is None
