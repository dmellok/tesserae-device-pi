"""OTA logs (docs/ota/contract.md, "OTA logs"): the log of each update
attempt, sent without the server asking.

An attempt's own lines are captured by a :class:`diagnostics.LogRing` that is
attached to the root logger only while :meth:`OtaLog.begin` .. :meth:`end`
runs (download, checks, ``pip install``), so it holds the attempt and not the
hours of polling around it, redacted as the routine ring is. When the attempt
ends in a state worth a log, the batch is written to the cache dir before any
upload is tried, so a failed upload or the restart into the new version does
not lose it:

1. ``failed`` (or a ``rejected`` from the apply-time battery gate): the
   attempt's lines, sent the same cycle.
2. ``pending_confirm``: the same, sent before the client exits for systemd
   to start the new version, under a short timeout. Its lines are also kept
   as the *tail*, for the next start.
3. The start after that resolves (``confirmed``, or ``rolled_back`` by the
   guard): the kept tail, then this process's ring up to the status report
   that carried the outcome. Only a start whose attempt left a tail sends
   one, so a device that was confirmed long before this module existed does
   not upload an old success.

One log per change of (``attempt_id``, ``phase``, ``reason``): the last one
sent is persisted, and an outcome that repeats is not re-sent. A failed
upload stays on disk and is retried on each later accepted status; a newer
outcome replaces it. Pre-download refusals (``already_current``, a bad
signature, a kind mismatch) are not an attempt and send nothing.

Everything here is best effort: no method raises, and the only wait is the
upload's own timeout. Uploads need a home server's ``/log`` route, which
only the REST transport has (and only the REST transport applies updates);
without an uploader the batch simply stays on disk, bounded.

Disk: ``ota-log.json`` (a few hundred bytes), ``ota-log-pending.log`` (at
most the 64 KB upload cap) and ``ota-log-tail.log`` (at most
:data:`TAIL_BYTES`). Memory: :data:`CAPTURE_BYTES` while an attempt runs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import diagnostics
from .ota_report import OtaState

log = logging.getLogger(__name__)

#: The attempt buffer, in memory while one runs.
CAPTURE_BYTES = 32 * 1024
#: The pre-restart lines kept on disk for the start after the switch.
TAIL_BYTES = 24 * 1024
#: The upload before the restart waits no longer than this.
RESTART_UPLOAD_TIMEOUT_S = 5.0

STATE_FILE = "ota-log.json"
PENDING_FILE = "ota-log-pending.log"
TAIL_FILE = "ota-log-tail.log"

#: Apply-time refusals that are an attempt (the contract's battery gate).
GATE_REASONS = frozenset({"battery_low", "battery_unknown"})
#: Phases a log is sent for (``rejected`` only with a gate reason).
LOGGED_PHASES = frozenset({"failed", "pending_confirm", "confirmed", "rolled_back"})
RESOLVED_PHASES = frozenset({"confirmed", "rolled_back"})

#: POST an OTA log: (body, extra headers, timeout or None) -> HTTP status.
Uploader = Callable[[bytes, dict[str, str], float | None], int]


def wants_log(phase: str, reason: str) -> bool:
    """Whether an attempt ending in ``phase``/``reason`` sends a log."""
    return phase in LOGGED_PHASES or (phase == "rejected" and reason in GATE_REASONS)


def upload_headers(phase: str, target_fw: str, attempt_id: str) -> dict[str, str]:
    """The contract's headers; an empty value is left out."""
    headers = {"X-Tesserae-Log-Kind": "ota"}
    for name, value in (
        ("X-Tesserae-Ota-Phase", phase),
        ("X-Tesserae-Ota-Target", target_fw),
        ("X-Tesserae-Ota-Attempt", attempt_id),
    ):
        if value:
            headers[name] = value
    return headers


def _key(attempt_id: str, phase: str, reason: str) -> str:
    return f"{attempt_id}/{phase}/{reason}"


def _cut_tail(text: str, limit: int) -> str:
    """The newest whole lines of ``text`` within ``limit`` bytes."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    tail = data[len(data) - limit :]
    cut = tail.find(b"\n")
    return (tail[cut + 1 :] if cut >= 0 else tail).decode("utf-8", errors="ignore")


class OtaLog:
    """Capture, persist and send the OTA log of each attempt."""

    def __init__(
        self,
        cache_root: Path,
        fw_version: str,
        started_at: float,
        upload: Uploader | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = cache_root
        self._fw = fw_version
        self._started_at = started_at
        self.upload = upload
        self._logger = logger if logger is not None else logging.getLogger()
        self._capture: diagnostics.LogRing | None = None
        self._state = self._load()

    # --- persistence ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads((self._root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / STATE_FILE).write_text(json.dumps(self._state), encoding="utf-8")
        except OSError as exc:
            log.warning("ota log: could not persist state (%s)", exc)

    def _write(self, name: str, data: bytes) -> bool:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / name).write_bytes(data)
        except OSError as exc:
            log.warning("ota log: could not write %s (%s)", name, exc)
            return False
        return True

    def _read(self, name: str) -> bytes | None:
        try:
            return (self._root / name).read_bytes()
        except OSError:
            return None

    def _unlink(self, name: str) -> None:
        try:
            (self._root / name).unlink(missing_ok=True)
        except OSError:
            pass

    # --- the attempt ----------------------------------------------------------

    def begin(self) -> None:
        """Start capturing: the attempt has passed its pre-download checks."""
        try:
            self._detach()
            self._capture = diagnostics.RING.child(CAPTURE_BYTES)
            self._logger.addHandler(self._capture)
        except Exception as exc:  # noqa: BLE001 - never in the update's way
            log.warning("ota log: capture not started (%s)", exc)
            self._capture = None

    def stop(self) -> None:
        """Stop capturing without queuing anything (idempotent)."""
        try:
            self._detach()
        except Exception:  # noqa: BLE001
            self._capture = None

    @property
    def has_pending(self) -> bool:
        """A log is queued and not yet sent."""
        return isinstance(self._state.get("pending"), dict)

    def _detach(self) -> str:
        capture, self._capture = self._capture, None
        if capture is None:
            return ""
        self._logger.removeHandler(capture)
        return capture.text()

    def end(self, phase: str, reason: str, target_fw: str, attempt_id: str) -> None:
        """The attempt has reached ``phase``: stop capturing and, for an
        outcome worth a log not already sent, queue the batch."""
        try:
            lines = self._detach()
            if not wants_log(phase, reason) or self._sent(attempt_id, phase, reason):
                return
            if phase == "pending_confirm":
                # Kept for the start after the switch (case 3).
                if self._write(TAIL_FILE, _cut_tail(lines, TAIL_BYTES).encode("utf-8")):
                    self._state["tail_attempt"] = attempt_id
                    self._save()
            self._queue(phase, reason, target_fw, attempt_id, lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("ota log: not queued (%s)", exc)

    def resolved(self, state: OtaState) -> None:
        """The status carrying ``state`` was accepted. When it resolves the
        attempt this client switched into, queue that start's log: the tail
        from before the restart, then this process's ring so far."""
        try:
            if state.phase not in RESOLVED_PHASES or not state.attempt_id:
                return
            if self._state.get("tail_attempt") != state.attempt_id:
                return
            if self._sent(state.attempt_id, state.phase, state.reason):
                return
            tail = (self._read(TAIL_FILE) or b"").decode("utf-8", errors="replace")
            ring = diagnostics.RING.text()
            body = (
                "# --- before the restart ---\n"
                + tail
                + ("" if not tail or tail.endswith("\n") else "\n")
                + "# --- this start ---\n"
                + ring
            )
            self._queue(state.phase, state.reason, state.target_fw, state.attempt_id, body)
            self._unlink(TAIL_FILE)
            self._state.pop("tail_attempt", None)
            self._save()
        except Exception as exc:  # noqa: BLE001
            log.warning("ota log: resolution not queued (%s)", exc)

    def _sent(self, attempt_id: str, phase: str, reason: str) -> bool:
        key = _key(attempt_id, phase, reason)
        pending = self._state.get("pending")
        return self._state.get("sent") == key or (
            isinstance(pending, dict) and pending.get("key") == key
        )

    def _queue(self, phase: str, reason: str, target_fw: str, attempt_id: str, body: str) -> None:
        header = diagnostics.log_header(self._fw, self._started_at)
        header += (
            f"\n# ota phase={phase} reason={reason or '-'} target={target_fw or '-'} "
            f"attempt={attempt_id or '-'}"
        )
        # Redacted again with the secrets known now (lines were as stored).
        data = diagnostics.fit_upload(header, diagnostics.RING.redact(body))
        if not self._write(PENDING_FILE, data):
            return
        self._state["pending"] = {
            "key": _key(attempt_id, phase, reason),
            "phase": phase,
            "target_fw": target_fw,
            "attempt_id": attempt_id,
        }
        self._save()

    # --- sending --------------------------------------------------------------

    def flush(self, timeout_s: float | None = None) -> bool:
        """Send the queued log, if any. True once it is stored server-side.

        A transport error or a 5xx/408/429 keeps it for the next call; any
        other refusal drops it (the same bytes would only be refused again).
        """
        try:
            pending = self._state.get("pending")
            if self.upload is None or not isinstance(pending, dict):
                return False
            data = self._read(PENDING_FILE)
            if data is None:
                self._state.pop("pending", None)
                self._save()
                return False
            headers = upload_headers(
                str(pending.get("phase") or ""),
                str(pending.get("target_fw") or ""),
                str(pending.get("attempt_id") or ""),
            )
            try:
                code = self.upload(data, headers, timeout_s)
            except Exception as exc:  # noqa: BLE001 - retried on a later cycle
                log.warning("ota log upload failed (%s: %s)", type(exc).__name__, exc)
                return False
            if 200 <= code < 300:
                log.info("ota log uploaded (%s)", pending.get("phase"))
            elif code >= 500 or code in (408, 429):
                log.warning("ota log upload answered %d; will retry", code)
                return False
            else:
                log.warning("ota log upload answered %d; dropping it", code)
            self._state["sent"] = pending.get("key")
            self._state.pop("pending", None)
            self._save()
            self._unlink(PENDING_FILE)
            return 200 <= code < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("ota log: flush failed (%s)", exc)
            return False


__all__ = [
    "CAPTURE_BYTES",
    "GATE_REASONS",
    "RESTART_UPLOAD_TIMEOUT_S",
    "TAIL_BYTES",
    "OtaLog",
    "Uploader",
    "upload_headers",
    "wants_log",
]
