"""REST transport tests — exercise the discover/register flows, frame ETag
short-circuit, status POST config-merge, and the per-status-code error paths
against a hand-rolled FakeSession (avoids pulling in `responses`/`requests-mock`).
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
import requests
from PIL.Image import Image
from PIL.Image import new as new_image

from tesserae_device_pi.button_report import ButtonReport
from tesserae_device_pi.config import (
    Config,
    HttpConfig,
    LoggingConfig,
    MqttConfig,
    RestConfig,
    parse_toml,
    save_config,
)
from tesserae_device_pi.heartbeat import Status
from tesserae_device_pi.transports import rest
from tesserae_device_pi.transports.rest import (
    API_PREFIX,
    RestClient,
    _apply_status_response,
    _auth_headers,
    _claim_via_discover,
    _claim_via_register,
    _wake_cycle,
)

# --- test fakes ----------------------------------------------------------------


class _RaiseOnCall:
    """Sentinel queued in FakeSession to make .post/.get raise instead of return."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        if isinstance(body, dict):
            self.content = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            self.content = body
        else:
            self.content = b""

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size: int = 64 * 1024) -> Any:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class FakeSession:
    """Queue of responses keyed by (METHOD, URL). Tests assert on .calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: dict[tuple[str, str], list[FakeResponse | _RaiseOnCall]] = {}

    def queue(self, method: str, url: str, *responses: FakeResponse | _RaiseOnCall) -> None:
        self._queue.setdefault((method.upper(), url), []).extend(responses)

    def _consume(self, method: str, url: str) -> FakeResponse:
        key = (method.upper(), url)
        if key not in self._queue or not self._queue[key]:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self._queue[key].pop(0)
        if isinstance(item, _RaiseOnCall):
            raise item.exc
        return item

    def post(
        self,
        url: str,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": dict(headers or {}),
                "data": data,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        return self._consume("POST", url)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        stream: bool = False,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(headers or {}),
                # Queued by URL alone, so a button press changes `params`
                # without changing the key a test queues against.
                "params": dict(params or {}),
                "timeout": timeout,
                "allow_redirects": allow_redirects,
                "stream": stream,
            }
        )
        return self._consume("GET", url)


class FakeShutdown:
    """Drop-in for threading.Event whose .wait() returns immediately so the
    discover/register/wake loops don't block the test in real time."""

    def __init__(self, stop_after_waits: int | None = None) -> None:
        self._set = False
        self._waits = 0
        self._stop_after = stop_after_waits
        self.wait_calls: list[float | None] = []

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self._waits += 1
        self.wait_calls.append(timeout)
        if self._stop_after is not None and self._waits >= self._stop_after:
            self._set = True
        return self._set


# --- fixtures ------------------------------------------------------------------


SERVER_URL = "http://srv:8765"
BASE = SERVER_URL + API_PREFIX


def _config(
    *,
    device_id: str = "pi_png",
    device_token: str = "",
    pairing_code: str = "",
    last_frame_etag: str = "",
    poll_interval_s: int = 60,
    tz: str = "",
) -> Config:
    return Config(
        mqtt=MqttConfig(
            host="h",
            port=1883,
            username="",
            password="",
            client_id="cid",
            keepalive=60,
            device_id=device_id,
        ),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
        transport_mode="rest",
        rest=RestConfig(
            server_url=SERVER_URL,
            device_token=device_token,
            pairing_code=pairing_code,
            last_frame_etag=last_frame_etag,
            poll_interval_s=poll_interval_s,
            tz=tz,
        ),
    )


def _status() -> Status:
    s = Status(panel="inky_impression_5_7")
    s.panel_w, s.panel_h = 600, 448
    s.ip = "10.0.0.5"
    return s


def _png_bytes(width: int = 32, height: int = 32) -> bytes:
    """Tiny solid-colour PNG used as a stand-in for a real render."""
    img = new_image("RGB", (width, height), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _frame_body(
    *,
    url: str = "http://srv:8765/renders/abc123.png",
    render_id: str = "abc123",
    rotate: int = 0,
    scale: str = "fit",
    bg: str = "white",
    saturation: float = 0.5,
) -> dict[str, Any]:
    """PNG frame announcement — same shape as the MQTT payload plus REST extras."""
    return {
        "url": url,
        "format": "png",
        "render_id": render_id,
        "rotate": rotate,
        "scale": scale,
        "bg": bg,
        "saturation": saturation,
        "panel_w": 600,
        "panel_h": 448,
    }


# --- auth + URL shape ----------------------------------------------------------


def test_auth_headers_send_both_forms() -> None:
    headers = _auth_headers("TOK123")
    assert headers["Authorization"] == "Bearer TOK123"
    assert headers["X-Tesserae-Token"] == "TOK123"


def test_restclient_builds_api_v1_url() -> None:
    session = FakeSession()
    session.queue("POST", f"{BASE}/device/discover", FakeResponse(200, {"registered": False}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    client.discover({"device_id": "x"})
    assert session.calls[0]["url"] == f"{BASE}/device/discover"
    assert session.calls[0]["headers"]["Content-Type"] == "application/json"
    assert session.calls[0]["allow_redirects"] is False


# --- discover→claim ------------------------------------------------------------


def test_discover_not_registered_then_registered_claims_token(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/discover",
        FakeResponse(200, {"status": "ok", "registered": False, "retry_after_s": 0}),
        FakeResponse(
            200,
            {
                "status": "ok",
                "registered": True,
                "device_token": "TOK_FROM_DISCOVER",
                "device_id": "pi_png",
                "server_time": 1781941884.34,
            },
        ),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)

    assert updated is not None
    assert updated.rest.device_token == "TOK_FROM_DISCOVER"
    assert updated.mqtt.device_id == "pi_png"
    # Saved to disk.
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == "TOK_FROM_DISCOVER"


def test_discover_adopts_server_canonical_device_id(tmp_path: Path) -> None:
    """If the admin renamed the device server-side, the discover response
    returns the canonical id and we must adopt it — otherwise subsequent
    /device/<id>/frame calls 403."""
    cfg = _config(device_id="pi_png")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/discover",
        FakeResponse(
            200,
            {
                "status": "ok",
                "registered": True,
                "device_token": "TOK",
                "device_id": "pi_png_renamed",
            },
        ),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.mqtt.device_id == "pi_png_renamed"
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.mqtt.device_id == "pi_png_renamed"


def test_discover_retries_on_connection_error(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/discover",
        _RaiseOnCall(requests.ConnectionError("refused")),
        FakeResponse(200, {"registered": True, "device_token": "TOK", "device_id": "pi_png"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.rest.device_token == "TOK"


def test_discover_shutdown_stops_loop(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    # Queue infinite not-registered responses; shutdown stops it.
    for _ in range(20):
        session.queue(
            "POST",
            f"{BASE}/device/discover",
            FakeResponse(200, {"registered": False, "retry_after_s": 0}),
        )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    shutdown = FakeShutdown(stop_after_waits=2)

    result = _claim_via_discover(client, cfg, _status(), shutdown, cfg_path)
    assert result is None


# --- register (pairing-code path) ----------------------------------------------


def test_register_201_saves_token_and_wipes_pairing_code(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(
            201,
            {
                "status": "ok",
                "device_token": "PAIRED_TOK",
                "device_id": "pi_png",
                "reused_existing": False,
            },
        ),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.rest.device_token == "PAIRED_TOK"
    assert updated.rest.pairing_code == ""

    # Pairing code sent as X-Pairing-Code header (not body).
    assert session.calls[0]["headers"].get("X-Pairing-Code") == "ABC123"

    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == "PAIRED_TOK"
    assert persisted.rest.pairing_code == ""


def test_register_403_exits(tmp_path: Path) -> None:
    cfg = _config(pairing_code="STALE")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("POST", f"{BASE}/device/register", FakeResponse(403, {"error": "expired"}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)


def test_register_429_honors_retry_after(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(429, None, headers={"Retry-After": "7"}),
        FakeResponse(201, {"device_token": "TOK", "device_id": "pi_png"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    shutdown = FakeShutdown()

    updated = _claim_via_register(client, cfg, _status(), shutdown, cfg_path)
    assert updated is not None
    # The first wait honored Retry-After=7.
    assert 7 in shutdown.wait_calls


def test_register_names_the_unified_kind_and_a_known_gamut(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(201, {"device_token": "TOK", "device_id": "pi_png"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    status = _status()
    status.gamuts = ["spectra_6"]

    assert _claim_via_register(client, cfg, status, FakeShutdown(), cfg_path) is not None
    sent = json.loads(session.calls[0]["data"])
    assert sent["kind"] == "pi_client" and sent["gamuts"] == ["spectra_6"]


def test_register_leaves_gamuts_out_when_the_panel_is_unknown(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(201, {"device_token": "TOK", "device_id": "pi_png"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert "gamuts" not in json.loads(session.calls[0]["data"])


def test_an_older_server_gets_the_legacy_png_kind(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(400, {"error": "unknown device kind 'pi_client'", "status": 400}),
        FakeResponse(201, {"device_token": "TOK", "device_id": "pi_png"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    status = _status()

    updated = _claim_via_register(client, cfg, status, FakeShutdown(), cfg_path)
    assert updated is not None and updated.rest.device_token == "TOK"
    kinds = [json.loads(c["data"])["kind"] for c in session.calls]
    assert kinds == ["pi_client", "pi_png_client"]
    assert status.kind == "pi_png_client"


def test_a_bad_body_is_still_fatal_and_never_loops(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST",
        f"{BASE}/device/register",
        FakeResponse(400, {"error": "unknown device kind 'pi_client'"}),
        FakeResponse(400, {"error": "unknown device kind 'pi_png_client'"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert len(session.calls) == 2


# --- frame GET (ETag) ----------------------------------------------------------


def test_wake_cycle_304_skips_paint_and_keeps_etag(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", last_frame_etag='"e1"')
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {"next_poll_s": 120}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    paints: list[tuple[Image, float]] = []

    def paint_fn(img: Image, saturation: float) -> None:
        paints.append((img, saturation))

    new_cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), paint_fn, cfg_path)
    assert not should_exit
    assert next_poll == 120
    assert paints == []
    # If-None-Match was sent.
    assert session.calls[0]["headers"]["If-None-Match"] == '"e1"'
    # etag unchanged on disk.
    assert new_cfg.rest.last_frame_etag == '"e1"'


def test_wake_cycle_200_paints_and_persists_new_etag(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", last_frame_etag="")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    frame_url = "http://srv:8765/renders/abc123.png"
    png = _png_bytes(32, 32)
    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_png/frame",
        FakeResponse(
            200,
            _frame_body(url=frame_url, saturation=0.7),
            headers={"ETag": '"newetag"'},
        ),
    )
    session.queue("GET", frame_url, FakeResponse(200, png))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {"next_poll_s": 60}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    paints: list[tuple[Image, float]] = []

    def paint_fn(img: Image, saturation: float) -> None:
        paints.append((img, saturation))

    new_cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), paint_fn, cfg_path)
    assert not should_exit
    assert next_poll == 60
    assert len(paints) == 1
    painted_img, painted_sat = paints[0]
    # Transform pipeline produces a panel-sized image.
    assert painted_img.size == (600, 448)
    assert painted_sat == 0.7
    assert new_cfg.rest.last_frame_etag == '"newetag"'
    # Etag round-trips quotes verbatim through the saved config.
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.last_frame_etag == '"newetag"'


def test_wake_cycle_204_no_frame_yet_does_not_paint(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(204))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {"next_poll_s": 30}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    paints: list[Any] = []
    new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda i, s: paints.append((i, s)), cfg_path
    )
    assert not should_exit
    assert paints == []
    assert next_poll == 30


# --- error handling ------------------------------------------------------------


def test_wake_cycle_401_wipes_token_and_exits(tmp_path: Path) -> None:
    cfg = _config(device_token="STALE_TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(401))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    new_cfg, _next, should_exit = _wake_cycle(client, cfg, _status(), lambda i, s: None, cfg_path)
    assert should_exit
    assert new_cfg.rest.device_token == ""
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == ""


def test_wake_cycle_403_exits_without_wiping_token(tmp_path: Path) -> None:
    """403 from /frame means token/id mismatch — typically because the local
    device_id is stale. Token isn't necessarily wrong, so we don't wipe it
    (re-pair would re-mint anyway); we just exit so the user notices."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(403))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, _next, should_exit = _wake_cycle(client, cfg, _status(), lambda i, s: None, cfg_path)
    assert should_exit


def test_wake_cycle_500_falls_back_to_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=42)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(503))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda i, s: None, cfg_path
    )
    assert not should_exit
    assert next_poll == 42


def test_wake_cycle_timeout_falls_back_to_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=77)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_png/frame",
        _RaiseOnCall(requests.Timeout("read timed out")),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda i, s: None, cfg_path
    )
    assert not should_exit
    assert next_poll == 77


# --- /status response merge ----------------------------------------------------


def test_apply_status_response_merges_sleep_interval_durably(tmp_path: Path) -> None:
    cfg = _config(poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    new_cfg, next_poll = _apply_status_response(
        cfg, {"config": {"sleep_interval_s": 300}, "next_poll_s": 300}, cfg_path
    )
    assert new_cfg.rest.poll_interval_s == 300
    assert next_poll == 300
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.poll_interval_s == 300


def test_apply_status_response_clamps_next_poll() -> None:
    cfg = _config(poll_interval_s=60)
    # A stay-awake device is answered from awake_poll_s (5..300): honoured
    # down to the awake floor, not the 30 s floor for the local interval.
    _new_cfg, next_poll = _apply_status_response(cfg, {"next_poll_s": 5}, Path("/dev/null"))
    assert next_poll == 5
    _new_cfg, next_poll = _apply_status_response(cfg, {"next_poll_s": 1}, Path("/dev/null"))
    assert next_poll == rest.SERVER_POLL_MIN_S
    # next_poll_s over ceiling (week) → clamped down.
    _new_cfg, next_poll = _apply_status_response(
        cfg, {"next_poll_s": 99_999_999}, Path("/dev/null")
    )
    assert next_poll == rest.POLL_INTERVAL_MAX_S


def test_with_no_server_cadence_the_local_interval_is_used() -> None:
    cfg = _config(poll_interval_s=60)
    assert _apply_status_response(cfg, {}, Path("/dev/null"))[1] == 60
    assert _apply_status_response(cfg, None, Path("/dev/null"))[1] == 60


def test_wake_at_is_honoured_as_an_absolute_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest.time, "time", lambda: 1_000_000.0)
    cfg = _config(poll_interval_s=60)
    body = {"next_poll_s": 90, "wake_at": 1_000_042}
    assert _apply_status_response(cfg, body, Path("/dev/null"))[1] == 42
    # A wake_at already past falls back to next_poll_s.
    body = {"next_poll_s": 90, "wake_at": 999_000}
    assert _apply_status_response(cfg, body, Path("/dev/null"))[1] == 90


def test_apply_status_response_ignores_out_of_range_sleep_interval(tmp_path: Path) -> None:
    """Server pushes a daft value (1 second) — the durable field stays put."""
    cfg = _config(poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    new_cfg, _next = _apply_status_response(cfg, {"config": {"sleep_interval_s": 1}}, cfg_path)
    assert new_cfg.rest.poll_interval_s == 60


# --- run() entry-point sanity --------------------------------------------------


def test_run_aborts_without_server_url(tmp_path: Path) -> None:
    cfg = Config(
        mqtt=MqttConfig(
            host="h",
            port=1883,
            username="",
            password="",
            client_id="cid",
            keepalive=60,
            device_id="pi_png",
        ),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
        transport_mode="rest",
        rest=RestConfig(server_url=""),  # missing!
    )
    rc = rest.run(cfg, _status(), lambda i, s: None, threading.Event(), tmp_path / "x.toml")
    assert rc == 2


# --- button delivery through the wake cycle -----------------------------------


def _paint_noop(img: Image, saturation: float) -> None:
    del img, saturation


def test_button_press_rides_the_frame_query(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=77)
    report.set("refresh")

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    assert session.calls[0]["params"] == {
        "button": "refresh",
        "button_event_id": "77",
    }
    # 304 acknowledged it, so /status must NOT carry it too.
    status_body = json.loads(session.calls[1]["data"])
    assert "button" not in status_body
    assert report.pending() is None


def test_unacknowledged_frame_falls_back_to_status_delivery(tmp_path: Path) -> None:
    """A 500 on the frame GET does not prove dispatch — /status delivers it."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(500))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=5)
    report.set("left")
    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    # The 500 path returns before /status, so the press is still pending.
    assert report.status_fields() == {"button": "left", "button_event_id": 5}


def test_status_fallback_body_carries_the_press(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    # A 404 is not an acknowledgement, but the cycle still reaches /status —
    # which is the fallback delivery path.
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(404))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=9)
    report.set("right")
    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    status_body = json.loads(session.calls[1]["data"])
    assert status_body["button"] == "right"
    assert status_body["button_event_id"] == 9
    # Delivered; it must not re-fire on the next cycle.
    assert report.pending() is None


def test_no_press_sends_no_button_fields(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)

    assert session.calls[0]["params"] == {}
    status_body = json.loads(session.calls[1]["data"])
    assert "button" not in status_body
    assert "button_event_id" not in status_body


# --- Retry-After + tz ---------------------------------------------------------


def test_frame_429_honours_retry_after(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_png/frame",
        FakeResponse(429, headers={"Retry-After": "300"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert not should_exit
    assert next_poll == 300


def test_frame_429_without_header_uses_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=90)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(429))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, _exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert next_poll == 90


def test_frame_429_retry_after_is_clamped(tmp_path: Path) -> None:
    """A hostile header must not pin the daemon at sub-second retries."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_png/frame",
        FakeResponse(429, headers={"Retry-After": "1"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, _exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert next_poll == rest.POLL_INTERVAL_MIN_S


def test_status_429_honours_retry_after(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue(
        "POST",
        f"{BASE}/device/pi_png/status",
        FakeResponse(429, headers={"Retry-After": "240"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert not should_exit
    assert next_poll == 240


def test_status_adopts_and_persists_tz(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    new_cfg, _next = _apply_status_response(
        cfg, {"config": {"tz": "Australia/Melbourne"}}, cfg_path
    )
    assert new_cfg.rest.tz == "Australia/Melbourne"
    assert parse_toml(cfg_path.read_text()).rest.tz == "Australia/Melbourne"


def test_top_level_tz_echo_is_not_adopted(tmp_path: Path) -> None:
    """The response's top-level `tz` is an echo of whichever zone the server
    resolved — including one we sent. Adopting it would pin the panel to a
    stale zone the first time the operator changes the server's timezone,
    because our heartbeat tz overrides theirs."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    new_cfg, _next = _apply_status_response(
        cfg, {"tz": "Europe/Berlin", "tz_offset_seconds": 7200}, cfg_path
    )
    assert new_cfg.rest.tz == ""


def test_status_body_reports_tz_and_next_sleep(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=120, tz="Australia/Perth")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)

    body = json.loads(session.calls[1]["data"])
    assert body["tz"] == "Australia/Perth"
    assert body["next_sleep_s"] == 120


# --- capability gating --------------------------------------------------------


def test_cacheless_status_body_carries_no_cache_keys(tmp_path: Path) -> None:
    """With no cache the wire body must be byte-identical to a client that
    never had one — advertising is what makes the server offer the feature."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)

    body = json.loads(session.calls[1]["data"])
    for key in ("deck_cache", "frame_cache", "ota", "deck_page_id", "collection"):
        assert key not in body


def test_disabled_cache_advertises_nothing_through_the_cycle(tmp_path: Path) -> None:
    from tesserae_device_pi.cache_sync import CacheManager

    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    cache = CacheManager(
        tmp_path / "cache",
        "pi_png",
        max_frame_bytes=1000,
        download_timeout_s=1.0,
        panel_size=(600, 448),
        enabled=False,
    )

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, ButtonReport(seed=1), cache)

    body = json.loads(session.calls[1]["data"])
    assert "deck_cache" not in body
    assert "frame_cache" not in body


def test_enabled_cache_advertises_both_capabilities(tmp_path: Path) -> None:
    from tesserae_device_pi.cache_sync import CacheManager

    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    cache = CacheManager(
        tmp_path / "cache",
        "pi_png",
        max_frame_bytes=1000,
        download_timeout_s=1.0,
        panel_size=(600, 448),
        enabled=True,
    )

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, ButtonReport(seed=1), cache)

    body = json.loads(session.calls[1]["data"])
    assert body["deck_cache"]["schema"] == 1
    assert body["frame_cache"]["schema"] == 1


def test_ota_capability_rides_the_status_body(tmp_path: Path) -> None:
    """schema alone means idle, and is what makes the server willing to offer
    an update at all."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    telemetry = rest.Telemetry(cfg, "0.1.0")

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(
        client,
        cfg,
        _status(),
        _paint_noop,
        cfg_path,
        ButtonReport(seed=1),
        None,
        telemetry,
    )

    body = json.loads(session.calls[1]["data"])
    assert body["ota"] == {"schema": 1}


# --- locally-served deck press -------------------------------------------------


def test_locally_served_press_is_not_reported_as_a_button_action(
    tmp_path: Path,
) -> None:
    """The server saw no request, so reporting the press would double-count
    it. The page report still goes out, so nav state stays truthful."""
    import io as _io

    from PIL.Image import new as _new_image

    from tesserae_device_pi.cache_sync import CacheManager
    from tesserae_device_pi.frame_cache import digest_of

    def _png(color: int) -> bytes:
        buf = _io.BytesIO()
        _new_image("RGB", (600, 448), (color, color, color)).save(buf, format="PNG")
        return buf.getvalue()

    home, nxt = _png(1), _png(2)
    d_home, d_next = digest_of(home), digest_of(nxt)
    manifest = json.dumps(
        {
            "deck_id": "deck1",
            "version": "v1",
            "entry_page_id": "home",
            "pages": [
                {
                    "page_id": "home",
                    "digest": d_home,
                    "bytes": len(home),
                    "ttl_s": 0,
                    "links": [{"button": "right", "zone": None, "target_page_id": "next"}],
                },
                {
                    "page_id": "next",
                    "digest": d_next,
                    "bytes": len(nxt),
                    "ttl_s": 0,
                    "links": [],
                },
            ],
        }
    )

    class _DeckClient:
        def get_deck_manifest(self, device_id: str, token: str) -> tuple[int, str]:
            return 200, manifest

        def get_collection_manifest(self, device_id: str, token: str) -> tuple[int, str]:
            return 204, ""

        def deck_frame_url(self, device_id: str, digest: str) -> str:
            return f"http://srv/{digest}"

        def absolute(self, url: str) -> str:
            return url

        def download_authed(self, url: str, token: str, max_bytes: int, timeout_s: float) -> bytes:
            return {d_home: home, d_next: nxt}[url.rsplit("/", 1)[-1]]

    cache = CacheManager(
        tmp_path / "cache",
        "pi_png",
        max_frame_bytes=10_000_000,
        download_timeout_s=1.0,
        panel_size=(600, 448),
        enabled=True,
    )
    cache.sync_deck(_DeckClient(), "TOK")

    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_png/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=3)
    report.set("right")
    painted: list[Image] = []
    _wake_cycle(
        client,
        cfg,
        _status(),
        lambda img, sat: painted.append(img),
        cfg_path,
        report,
        cache,
    )

    assert len(painted) == 1  # served from disk
    # The press was consumed, not sent — on either endpoint.
    assert "button" not in session.calls[0]["params"]
    status_body = json.loads(session.calls[1]["data"])
    assert "button" not in status_body
    # ...but the page it painted IS reported.
    assert session.calls[0]["params"]["deck_page_id"] == "next"
    assert status_body["deck_page_id"] == "next"
    assert status_body["deck_version"] == "v1"
