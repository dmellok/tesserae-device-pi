"""Device log upload and failure reporting (the firmware's protocol v1).

Two pieces, both riding the REST status beat:

* :class:`LogRing` keeps this process's recent log lines in memory, the Pi's
  version of the firmware's RAM ring. Every beat advertises
  ``{"logs": {"schema": 1, "ring_bytes": N}}``; when the server answers a
  status with ``{"logs": {"upload": true}}`` (the operator is collecting
  logs, or the beat carried a new failure) the ring is POSTed as
  ``text/plain`` to ``/api/v1/device/<id>/log``, where it shows on the
  device page. Secrets are redacted twice: as each line is stored, and
  again over the whole batch when it is uploaded, with the secrets known by
  then, so a line logged before pairing taught the client its token is
  still clean. Redacted: the configured tokens and keys, anything after
  ``Bearer``, and the query string of any http(s) URL (a signed frame URL
  carries its signature there), as the firmware's ring does.
* :class:`DiagLatch` remembers the last paint failure as
  ``{"diag": {"id", "paint_error", "at"}}`` and re-sends it on every beat
  until a status POST is accepted, so a failure on a beat that never
  reached the server still arrives. ``id`` tells a re-send from a new one.

Upload is capped at the server's 64 KB, newest lines kept.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

LOGS_SCHEMA = 1
#: The server's limit for one upload (app/state/device_logs.py).
MAX_UPLOAD_BYTES = 64 * 1024
#: The ring's size. Below the upload cap so header + ring always fit.
RING_BYTES = 48 * 1024

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
#: An http(s) URL's query string, up to whitespace, a quote or a bracket.
_QUERY_RE = re.compile(r"(https?://[^\s\"'<>`?]*)\?[^\s\"'<>`]*", re.IGNORECASE)
_REDACTED = "[redacted]"


class LogRing(logging.Handler):
    """A logging handler holding the newest ``RING_BYTES`` of formatted lines."""

    def __init__(self, ring_bytes: int = RING_BYTES, secrets: Iterable[str] = ()) -> None:
        super().__init__(level=logging.DEBUG)
        self.ring_bytes = ring_bytes
        self._lines: deque[str] = deque()
        self._size = 0
        self._lock_ring = threading.Lock()
        self._secrets: list[str] = []
        self.set_secrets(secrets)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )

    def set_secrets(self, secrets: Iterable[str]) -> None:
        """Values to blank out of every line from now on (tokens change on pairing)."""
        # Longest first, so a token that contains another is replaced whole.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        text = _QUERY_RE.sub(r"\1?" + _REDACTED, text)
        return _BEARER_RE.sub(r"\1" + _REDACTED, text)

    def child(self, ring_bytes: int) -> LogRing:
        """A new, empty ring with this one's secrets, level and format: a
        buffer for one stretch of the log (an OTA attempt) redacted alike."""
        ring = LogRing(ring_bytes)
        ring._secrets = list(self._secrets)
        ring.setLevel(self.level)
        if self.formatter is not None:
            ring.setFormatter(self.formatter)
        return ring

    def text(self) -> str:
        """The ring's lines as they stand (redacted when stored)."""
        with self._lock_ring:
            return "".join(self._lines)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.redact(self.format(record)) + "\n"
        except Exception:  # noqa: BLE001 - logging must never raise
            return
        size = len(line.encode("utf-8"))
        with self._lock_ring:
            self._lines.append(line)
            self._size += size
            while self._size > self.ring_bytes and len(self._lines) > 1:
                self._size -= len(self._lines.popleft().encode("utf-8"))

    def dump(self, header: str) -> bytes:
        """``header`` and the ring as one upload, cut to ``MAX_UPLOAD_BYTES``."""
        # Again over the batch: a line stored before a secret was known.
        return fit_upload(header, self.redact(self.text()))


def fit_upload(header: str, body: str, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """``header`` then ``body`` as UTF-8, keeping the newest lines of ``body``
    so the whole fits in ``limit`` bytes."""
    head = (header.rstrip("\n") + "\n").encode("utf-8")
    data = head + body.encode("utf-8")
    if len(data) <= limit:
        return data
    tail = data[len(data) - (limit - len(head)) :]
    # Start on a whole line, and on a whole UTF-8 character.
    cut = tail.find(b"\n")
    return head + (tail[cut + 1 :] if cut >= 0 else tail)


def log_header(fw_version: str, started_at: float, now: float | None = None) -> str:
    """The firmware's ``# tesserae-log v1`` header line, Pi fields filled in.

    There is no reset reason or wake cause on a Pi; ``boot`` is the epoch
    this process started rather than an NVS boot counter.
    """
    epoch = int(time.time() if now is None else now)
    return (
        f"# tesserae-log v1 fw={fw_version} boot={int(started_at)} "
        f"reset=none wake=none epoch={epoch}"
    )


class DiagLatch:
    """The last paint failure, re-sent until the server has it."""

    def __init__(self) -> None:
        self._diag: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def latch_paint_error(self, exc: BaseException, now: float | None = None) -> None:
        """Record a failed paint. A newer failure replaces an unsent one."""
        reason = f"{type(exc).__name__}: {exc}".strip()[:64]
        with self._lock:
            self._diag = {
                "id": random.getrandbits(32),
                "paint_error": reason,
                "at": int(time.time() if now is None else now),
            }

    def status_fields(self) -> dict[str, Any]:
        with self._lock:
            return {"diag": dict(self._diag)} if self._diag is not None else {}

    def acknowledge(self, sent: dict[str, Any]) -> None:
        """Clear the latch once the beat carrying it was accepted, unless a
        newer failure has replaced it since that beat was built."""
        diag = sent.get("diag")
        if not isinstance(diag, dict):
            return
        with self._lock:
            if self._diag is not None and self._diag.get("id") == diag.get("id"):
                self._diag = None


#: The process's ring and latch, installed by main (like logging's own root
#: handler: one per process, shared by whichever transport is running).
RING = LogRing()
DIAG = DiagLatch()


def capability(ring: LogRing) -> dict[str, Any]:
    return {"logs": {"schema": LOGS_SCHEMA, "ring_bytes": ring.ring_bytes}}


def upload_requested(body: Any) -> bool:
    """Whether a /status response asks for this wake's log."""
    logs = body.get("logs") if isinstance(body, dict) else None
    return isinstance(logs, dict) and logs.get("upload") is True


__all__ = [
    "LOGS_SCHEMA",
    "MAX_UPLOAD_BYTES",
    "RING_BYTES",
    "DIAG",
    "RING",
    "DiagLatch",
    "LogRing",
    "capability",
    "fit_upload",
    "log_header",
    "upload_requested",
]
