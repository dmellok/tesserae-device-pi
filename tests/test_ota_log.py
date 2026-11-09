"""OTA logs (docs/ota/contract.md, "OTA logs"): captured per attempt, sent
once per outcome, kept across the restart, never in the update's way."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from tesserae_device_pi import diagnostics, ota_guard
from tesserae_device_pi.diagnostics import LogRing
from tesserae_device_pi.ota_apply import PENDING_FILE, OtaUpdater
from tesserae_device_pi.ota_log import (
    RESTART_UPLOAD_TIMEOUT_S,
    TAIL_FILE,
    OtaLog,
    upload_headers,
    wants_log,
)
from tesserae_device_pi.ota_report import OtaReporter, OtaState
from tests.test_ota_apply import KEYS, _descriptor, _wheel
from tests.test_rest import BASE, SERVER_URL, FakeResponse, FakeSession


class Uploads:
    """A fake uploader: records each call, answers with the queued codes
    (or raises an exception queued in their place)."""

    def __init__(self, *answers: int | Exception) -> None:
        self.calls: list[tuple[str, dict[str, str], float | None]] = []
        self.answers = list(answers)

    def __call__(self, data: bytes, headers: dict[str, str], timeout_s: float | None) -> int:
        self.calls.append((data.decode("utf-8"), dict(headers), timeout_s))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _ring(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[LogRing]:
    """A fresh process ring on the root logger, at DEBUG, as main installs it."""
    caplog.set_level(logging.DEBUG)
    ring = LogRing(secrets=["TOKEN_ABC123"])
    monkeypatch.setattr(diagnostics, "RING", ring)
    root = logging.getLogger()
    root.addHandler(ring)
    yield ring
    root.removeHandler(ring)


def _updater(
    tmp_path: Path,
    ota_log: OtaLog,
    *,
    running: str = "0.9.0",
    image: bytes | None = None,
    fail_install: bool = False,
) -> OtaUpdater:
    def install(args: Sequence[str]) -> None:
        logging.getLogger("tesserae_device_pi.pip").info("pip install %s", " ".join(args))
        if fail_install and "--force-reinstall" not in args:
            raise RuntimeError("pip exploded")

    wheel = _wheel() if image is None else image
    return OtaUpdater(
        OtaReporter(tmp_path / "ota-state.json"),
        tmp_path,
        running,
        lambda _u, _n: wheel,
        install=install,
        keys=KEYS,
        ota_log=ota_log,
    )


# --- which outcomes, which headers ----------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "reason", "want"),
    [
        ("failed", "digest_mismatch", True),
        ("failed", "flash_error", True),
        ("rejected", "battery_low", True),
        ("rejected", "battery_unknown", True),
        ("pending_confirm", "", True),
        ("confirmed", "ok", True),
        ("rolled_back", "boot_failed", True),
        ("rejected", "already_current", False),
        ("rejected", "kind_mismatch", False),
        ("rejected", "bad_signature", False),
        ("downloading", "", False),
    ],
)
def test_which_outcomes_send_a_log(phase: str, reason: str, want: bool) -> None:
    assert wants_log(phase, reason) is want


def test_the_headers_name_the_kind_and_leave_empty_fields_out() -> None:
    assert upload_headers("failed", "1.0.0", "ab12cd34") == {
        "X-Tesserae-Log-Kind": "ota",
        "X-Tesserae-Ota-Phase": "failed",
        "X-Tesserae-Ota-Target": "1.0.0",
        "X-Tesserae-Ota-Attempt": "ab12cd34",
    }
    assert upload_headers("failed", "", "") == {
        "X-Tesserae-Log-Kind": "ota",
        "X-Tesserae-Ota-Phase": "failed",
    }


def test_an_ota_log_posts_text_with_its_headers_over_rest() -> None:
    from tesserae_device_pi.transports.rest import RestClient

    session = FakeSession()
    session.queue("POST", f"{BASE}/device/pi_png/log", FakeResponse(200, {"stored": True}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    code = client.post_log(
        "pi_png", "TOKEN", b"# tesserae-log v1\n", upload_headers("failed", "1.0.0", "ab"), 5.0
    )
    assert code == 200
    (call,) = session.calls
    assert call["headers"]["Content-Type"] == "text/plain; charset=utf-8"
    assert call["headers"]["Authorization"] == "Bearer TOKEN"
    assert call["headers"]["X-Tesserae-Log-Kind"] == "ota"
    assert call["headers"]["X-Tesserae-Ota-Phase"] == "failed"
    assert call["headers"]["X-Tesserae-Ota-Target"] == "1.0.0"
    assert call["headers"]["X-Tesserae-Ota-Attempt"] == "ab"
    assert call["timeout"] == 5.0


def test_the_routine_upload_carries_no_ota_headers() -> None:
    from tesserae_device_pi.transports.rest import RestClient

    session = FakeSession()
    session.queue("POST", f"{BASE}/device/pi_png/log", FakeResponse(200))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    client.post_log("pi_png", "TOKEN", b"x")
    sent = session.calls[0]["headers"]
    assert "X-Tesserae-Log-Kind" not in sent
    assert not any(h.startswith("X-Tesserae-Ota") for h in sent)


# --- case 1: an attempt that fails ---------------------------------------------------------


def test_a_failed_attempt_sends_its_own_lines_once(tmp_path: Path) -> None:
    logging.getLogger("tesserae_device_pi.rest").info("polling, long before the offer")
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    updater = _updater(tmp_path, ota_log)
    desc = _descriptor(_wheel(), sha256="0" * 64)

    assert updater.apply(desc, "pi_client") is False
    assert ota_log.flush() is True
    (body, headers, _timeout) = uploads.calls[0]
    assert headers["X-Tesserae-Ota-Phase"] == "failed"
    assert headers["X-Tesserae-Ota-Target"] == "1.0.0"
    assert headers["X-Tesserae-Ota-Attempt"] == updater.reporter.state.attempt_id
    assert body.startswith("# tesserae-log v1 fw=0.9.0 ")
    assert "# ota phase=failed reason=digest_mismatch target=1.0.0" in body
    assert "ota: updating 0.9.0 -> 1.0.0" in body
    assert "digest_mismatch" in body
    assert "long before the offer" not in body  # the attempt, not the whole process

    # Nothing left to send; the capture is off.
    assert ota_log.flush() is False
    assert len(uploads.calls) == 1
    assert not any(
        isinstance(h, LogRing) and h is not diagnostics.RING for h in logging.getLogger().handlers
    )


def test_the_same_outcome_again_is_not_sent_again(tmp_path: Path) -> None:
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    for _ in range(3):  # e.g. a battery gate refusing on every wake
        ota_log.begin()
        logging.getLogger("x").warning("battery 4%%, refusing")
        ota_log.end("rejected", "battery_low", "1.0.0", "a1")
        ota_log.flush()
    assert len(uploads.calls) == 1
    # Even from a new process: what was sent is persisted.
    again = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    again.begin()
    again.end("rejected", "battery_low", "1.0.0", "a1")
    again.flush()
    assert len(uploads.calls) == 1
    # A change of reason (or phase, or attempt) is a new log.
    again.begin()
    again.end("rejected", "battery_unknown", "1.0.0", "a1")
    again.flush()
    assert len(uploads.calls) == 2


@pytest.mark.parametrize(
    ("running", "overrides"),
    [
        ("1.0.0", {}),  # already_current
        ("0.9.0", {"device_kind": "esp32_client"}),  # kind_mismatch
    ],
)
def test_pre_download_refusals_send_nothing(
    tmp_path: Path, running: str, overrides: dict[str, str]
) -> None:
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, running, 0.0, upload=uploads)
    updater = _updater(tmp_path, ota_log, running=running)
    assert updater.apply(_descriptor(_wheel(), **overrides), "pi_client") is False
    bad_sig = dict(_descriptor(_wheel()))
    bad_sig["signature"] = _descriptor(_wheel(), fw_version="2.0.0")["signature"]
    assert updater.apply(bad_sig, "pi_client") is False
    assert ota_log.flush() is False and uploads.calls == []
    assert not ota_log.has_pending


def test_the_log_is_redacted_like_a_routine_upload(tmp_path: Path) -> None:
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    ota_log.begin()
    logging.getLogger("x").info("auth TOKEN_ABC123 Bearer sekrit")
    logging.getLogger("x").info("fetch https://h/api/v1/ota/x.whl?sig=deadbeef&exp=9 done")
    ota_log.end("failed", "download_error", "1.0.0", "a1")
    ota_log.flush()
    body = uploads.calls[0][0]
    assert "TOKEN_ABC123" not in body and "sekrit" not in body and "deadbeef" not in body
    assert "https://h/api/v1/ota/x.whl?[redacted] done" in body


def test_the_upload_is_capped_at_64_kb(tmp_path: Path) -> None:
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    ota_log.begin()
    for i in range(3000):
        logging.getLogger("x").info("line %05d %s", i, "y" * 40)
    ota_log.end("failed", "download_error", "1.0.0", "a1")
    assert (tmp_path / "ota-log-pending.log").stat().st_size <= diagnostics.MAX_UPLOAD_BYTES
    ota_log.flush()
    body = uploads.calls[0][0]
    assert len(body.encode()) <= diagnostics.MAX_UPLOAD_BYTES
    assert "line 02999" in body  # newest kept


# --- best effort ------------------------------------------------------------------------------


def test_a_failing_upload_never_breaks_the_update(tmp_path: Path) -> None:
    uploads = Uploads(ConnectionError("no route"), 503)
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    updater = _updater(tmp_path, ota_log)
    assert updater.apply(_descriptor(_wheel()), "pi_client") is True
    assert updater.reporter.state.phase == "pending_confirm"
    assert (tmp_path / PENDING_FILE).exists()  # the install's own record

    assert ota_log.flush() is False  # raised
    assert ota_log.flush() is False  # 503
    assert ota_log.has_pending  # kept for a later cycle
    assert ota_log.flush() is True
    assert not ota_log.has_pending and len(uploads.calls) == 3


def test_a_broken_disk_never_breaks_the_update(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.write_text("a file where the cache dir should be")
    ota_log = OtaLog(root, "0.9.0", 0.0, upload=Uploads())
    updater = _updater(tmp_path, ota_log, image=_wheel(), fail_install=True)
    assert updater.apply(_descriptor(_wheel()), "pi_client") is False
    assert (updater.reporter.state.phase, updater.reporter.state.reason) == (
        "failed",
        "flash_error",
    )
    assert ota_log.flush() is False


def test_a_refused_upload_is_dropped_not_retried_forever(tmp_path: Path) -> None:
    uploads = Uploads(404)
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    ota_log.begin()
    ota_log.end("failed", "download_error", "1.0.0", "a1")
    assert ota_log.flush() is False
    assert not ota_log.has_pending
    ota_log.begin()
    ota_log.end("failed", "download_error", "1.0.0", "a1")  # same outcome
    assert not ota_log.has_pending and len(uploads.calls) == 1


def test_without_an_uploader_the_log_waits_on_disk(tmp_path: Path) -> None:
    ota_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=None)
    ota_log.begin()
    ota_log.end("failed", "download_error", "1.0.0", "a1")
    assert ota_log.has_pending and ota_log.flush() is False


# --- cases 2 and 3: across the restart ----------------------------------------------------


def _install_and_restart(tmp_path: Path, uploads: Uploads) -> tuple[OtaUpdater, str]:
    old_log = OtaLog(tmp_path, "0.9.0", 0.0, upload=uploads)
    old = _updater(tmp_path, old_log)
    assert old.apply(_descriptor(_wheel()), "pi_client") is True
    old_log.flush(RESTART_UPLOAD_TIMEOUT_S)
    return old, old.reporter.state.attempt_id


def test_before_the_restart_the_install_log_is_sent_briefly(tmp_path: Path) -> None:
    uploads = Uploads()
    _old, attempt = _install_and_restart(tmp_path, uploads)
    (body, headers, timeout) = uploads.calls[0]
    assert headers["X-Tesserae-Ota-Phase"] == "pending_confirm"
    assert headers["X-Tesserae-Ota-Attempt"] == attempt
    assert timeout == RESTART_UPLOAD_TIMEOUT_S
    assert "pip install" in body and "restarting into it" in body
    assert (tmp_path / TAIL_FILE).exists()  # kept for the next start


def _telemetry(tmp_path: Path, running: str, uploads: Uploads):  # type: ignore[no-untyped-def]
    from tesserae_device_pi.config import parse_toml, render_config_toml
    from tesserae_device_pi.transports import rest

    cfg = parse_toml(render_config_toml(cache_root=str(tmp_path)))
    return rest.Telemetry(
        cfg, running, download=lambda _u, _n: _wheel(), upload_log=uploads, started_at=0.0
    )


def test_the_start_after_the_switch_sends_its_log_with_the_tail(tmp_path: Path) -> None:
    uploads = Uploads()
    _old, attempt = _install_and_restart(tmp_path, uploads)
    diagnostics.RING._lines.clear()  # a new process: an empty ring
    logging.getLogger("tesserae_device_pi.main").info("started 1.0.0")

    telemetry = _telemetry(tmp_path, "1.0.0", uploads)  # resolve_boot: confirmed
    telemetry.after_status({"next_poll_s": 60}, "pi_client")

    assert len(uploads.calls) == 2
    (body, headers, _t) = uploads.calls[1]
    assert headers["X-Tesserae-Ota-Phase"] == "confirmed"
    assert headers["X-Tesserae-Ota-Target"] == "1.0.0"
    assert headers["X-Tesserae-Ota-Attempt"] == attempt
    assert body.startswith("# tesserae-log v1 fw=1.0.0 ")
    before, after = body.split("# --- this start ---\n")
    assert "restarting into it" in before  # the pre-restart tail
    assert "started 1.0.0" in after and "confirmed" in after
    assert not (tmp_path / TAIL_FILE).exists()

    # Confirmed stays the state on every later beat; it is not sent again.
    telemetry.after_status({"next_poll_s": 60}, "pi_client")
    _telemetry(tmp_path, "1.0.0", uploads).after_status({}, "pi_client")
    assert len(uploads.calls) == 2


def test_a_log_the_restart_cut_off_is_sent_first_by_the_new_version(tmp_path: Path) -> None:
    uploads = Uploads(ConnectionError("restarting"))
    _install_and_restart(tmp_path, uploads)
    telemetry = _telemetry(tmp_path, "1.0.0", uploads)
    telemetry.after_status({}, "pi_client")
    phases = [c[1]["X-Tesserae-Ota-Phase"] for c in uploads.calls]
    assert phases == ["pending_confirm", "pending_confirm", "confirmed"]


def test_a_rollback_by_the_guard_sends_rolled_back(tmp_path: Path) -> None:
    uploads = Uploads()
    _old, attempt = _install_and_restart(tmp_path, uploads)
    pending = tmp_path / PENDING_FILE
    record = json.loads(pending.read_text())
    record["previous"] = []  # nothing for the guard to pip install here
    pending.write_text(json.dumps(record))
    for _ in range(ota_guard.MAX_BOOTS + 1):  # the new version never confirms
        ota_guard.guard(pending)

    telemetry = _telemetry(tmp_path, "0.9.0", uploads)  # the old version is back
    telemetry.after_status({}, "pi_client")
    (body, headers, _t) = uploads.calls[-1]
    assert headers["X-Tesserae-Ota-Phase"] == "rolled_back"
    assert headers["X-Tesserae-Ota-Attempt"] == attempt
    assert "reason=boot_failed" in body and "restarting into it" in body
    assert len(uploads.calls) == 2


def test_a_confirmation_this_client_never_switched_into_sends_nothing(tmp_path: Path) -> None:
    # Confirmed by a version from before OTA logs: no tail, so no upload.
    OtaReporter(tmp_path / "ota-state.json").set("confirmed", "ok", "1.0.0", "old1")
    uploads = Uploads()
    ota_log = OtaLog(tmp_path, "1.0.0", 0.0, upload=uploads)
    ota_log.resolved(OtaState("confirmed", "ok", "1.0.0", "old1"))
    assert ota_log.flush() is False and uploads.calls == []
    _telemetry(tmp_path, "1.0.0", uploads).after_status({}, "pi_client")
    assert uploads.calls == []


def test_an_offer_in_the_loop_uploads_before_the_restart(tmp_path: Path) -> None:
    uploads = Uploads(ConnectionError("down"))
    telemetry = _telemetry(tmp_path, "0.9.0", uploads)
    assert telemetry._updater is not None
    telemetry._updater.install = lambda _args: None
    telemetry._updater.keys = KEYS
    telemetry.after_status({"ota": _descriptor(_wheel())}, "pi_client")
    # The upload failed, and the restart still goes ahead.
    assert telemetry.restart_requested is True
    assert uploads.calls[0][2] == RESTART_UPLOAD_TIMEOUT_S
