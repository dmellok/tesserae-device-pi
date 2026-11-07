"""Log upload and failure reporting (protocol v1), unit and over REST."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tesserae_pi_client import diagnostics
from tesserae_pi_client.config import save_config
from tesserae_pi_client.diagnostics import (
    MAX_UPLOAD_BYTES,
    DiagLatch,
    LogRing,
    log_header,
    upload_requested,
)
from tesserae_pi_client.transports.rest import RestClient, _wake_cycle
from tests.test_rest import BASE, SERVER_URL, FakeResponse, FakeSession, _config, _status


def _ring(**kw: object) -> tuple[LogRing, logging.Logger]:
    ring = LogRing(**kw)  # type: ignore[arg-type]
    logger = logging.getLogger(f"test.diag.{id(ring)}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(ring)
    return ring, logger


def test_the_ring_keeps_the_newest_lines_within_its_size() -> None:
    ring, logger = _ring(ring_bytes=200)
    for i in range(50):
        logger.info("line %03d", i)
    text = ring.dump("# h").decode()
    assert "line 049" in text and "line 000" not in text
    assert len(text.encode()) < 200 + 100  # header + at most one over


def test_secrets_never_enter_the_ring() -> None:
    ring, logger = _ring(secrets=["SEKRIT_TOKEN_123"])
    logger.info("got token SEKRIT_TOKEN_123 from server")
    logger.info("GET /frame Authorization: Bearer abc.def.ghi")
    text = ring.dump("# h").decode()
    assert "SEKRIT_TOKEN_123" not in text and "abc.def.ghi" not in text
    assert text.count("[redacted]") == 2


def test_an_upload_is_capped_and_starts_on_a_whole_line() -> None:
    ring, logger = _ring(ring_bytes=MAX_UPLOAD_BYTES * 2)
    for i in range(4000):
        logger.info("%05d %s", i, "x" * 40)
    data = ring.dump(log_header("0.9.0", 1000.0, now=2000.0))
    assert len(data) <= MAX_UPLOAD_BYTES
    lines = data.decode().splitlines()
    assert lines[0] == "# tesserae-log v1 fw=0.9.0 boot=1000 reset=none wake=none epoch=2000"
    assert lines[1].split(" ")[0].count(":") == 2  # a timestamp: not a cut line
    assert "03999" in lines[-1]


def test_a_diag_is_resent_until_acknowledged() -> None:
    latch = DiagLatch()
    assert latch.status_fields() == {}
    latch.latch_paint_error(RuntimeError("SPI timeout"), now=1234)
    first = latch.status_fields()
    assert first["diag"]["paint_error"] == "RuntimeError: SPI timeout"
    assert first["diag"]["at"] == 1234 and 0 <= first["diag"]["id"] <= 0xFFFFFFFF
    assert latch.status_fields() == first  # unsent: re-sent as is
    latch.acknowledge(first)
    assert latch.status_fields() == {}


def test_a_newer_failure_is_not_cleared_by_the_older_ack() -> None:
    latch = DiagLatch()
    latch.latch_paint_error(RuntimeError("one"))
    sent = latch.status_fields()
    latch.latch_paint_error(RuntimeError("two"))
    latch.acknowledge(sent)
    assert latch.status_fields()["diag"]["paint_error"] == "RuntimeError: two"


@pytest.mark.parametrize(
    ("body", "want"),
    [({"logs": {"upload": True}}, True), ({"logs": {}}, False), ({}, False), (None, False)],
)
def test_upload_requested(body: object, want: bool) -> None:
    assert upload_requested(body) is want


# --- over REST -----------------------------------------------------------------


def test_a_beat_advertises_logs_carries_diag_and_uploads_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ring, logger = _ring()
    latch = DiagLatch()
    monkeypatch.setattr(diagnostics, "RING", ring)
    monkeypatch.setattr(diagnostics, "DIAG", latch)
    latch.latch_paint_error(RuntimeError("refresh_timeout"))
    # Logged before the ring knew the token: still redacted at upload.
    logger.info("painting with token TOKEN_ABC123")

    cfg = _config(device_token="TOKEN_ABC123", last_frame_etag='"e1"')
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_png/frame", FakeResponse(304))
    session.queue(
        "POST",
        f"{BASE}/device/pi_png/status",
        FakeResponse(200, {"next_poll_s": 60, "logs": {"upload": True}}),
    )
    session.queue("POST", f"{BASE}/device/pi_png/log", FakeResponse(200, {"status": 200}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), lambda *_: None, cfg_path)

    status_call, log_call = session.calls[1], session.calls[2]
    sent = json.loads(status_call["data"])
    assert sent["logs"] == {"schema": 1, "ring_bytes": ring.ring_bytes}
    assert sent["diag"]["paint_error"] == "RuntimeError: refresh_timeout"
    assert latch.status_fields() == {}  # accepted, so not re-sent
    assert log_call["url"].endswith("/device/pi_png/log")
    assert log_call["headers"]["Content-Type"].startswith("text/plain")
    upload = log_call["data"].decode()
    assert upload.startswith("# tesserae-log v1 ")
    assert "painting with token [redacted]" in upload
    assert "TOKEN_ABC123" not in upload
