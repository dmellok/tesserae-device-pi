"""LAN settings page: http://<pi>:8790/, the Pi's version of the firmware's
LAN editor.

Always a read-only status page. It takes changes only while it is
*unlocked*:

* while the Pi has no pairing (a fresh install, or after a factory reset),
  so a Pi can be pointed at a server without a keyboard; or
* for 15 minutes after the refresh button is held and released between 3
  and 19 seconds (held 20 s it factory-resets instead), as releasing the
  firmware's Refresh in that window opens its maintenance mode. The panel
  then shows the page's address and a QR code.

Editing never writes the config under the running client, which saves its
own state as it goes and would overwrite the edit. The page records the
change in :data:`PENDING` and stops the client; main applies it once the
transport has returned, then exits for systemd to start the client again.

Forms carry a per-process token, so another site cannot post to the page
from a browser on the same LAN. Standard library only.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import TRANSPORT_MODES, Config, load_config, with_rest_updates

log = logging.getLogger(__name__)

LAN_PORT = 8790
UNLOCK_S = 15 * 60
#: Exit code after settings were changed here: systemd starts the client again.
SETTINGS_RESTART_EXIT = 5

_DEVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_PAIRING_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")


def paired(cfg: Config) -> bool:
    """Whether this Pi already belongs to a server (nothing to set up)."""
    if cfg.transport_mode == "rest":
        return bool(cfg.rest.device_token)
    if cfg.transport_mode == "relay":
        return bool(cfg.relay.device_token)
    return True  # MQTT has no pairing; it is unlocked by the button only


class LanAccess:
    """Whether the page takes changes right now."""

    def __init__(self) -> None:
        self.unlocked_until = 0.0
        self._on_unlock: list[Callable[[], object]] = []

    def unlock(self, now: float | None = None) -> None:
        self.unlocked_until = (time.time() if now is None else now) + UNLOCK_S
        for callback in self._on_unlock:
            callback()

    def on_unlock(self, callback: Callable[[], object]) -> None:
        self._on_unlock.append(callback)

    def editable(self, cfg: Config, now: float | None = None) -> bool:
        return not paired(cfg) or (time.time() if now is None else now) < self.unlocked_until


@dataclass
class Pending:
    """A change made on the page, applied by main after the client stops."""

    edits: dict[str, str] = field(default_factory=dict)
    factory_reset: bool = False

    def any(self) -> bool:
        return bool(self.edits) or self.factory_reset


ACCESS = LanAccess()
PENDING = Pending()

#: The fields the page edits: name -> (label, help).
FIELDS: dict[str, tuple[str, str]] = {
    "server_url": ("Tesserae server", "e.g. http://tesserae.local:8765"),
    "transport_mode": ("Connect by", "rest (recommended), mqtt or relay"),
    "pairing_code": ("Pairing code", "From Settings > Devices > Pair. Optional."),
    "device_id": ("Device id", "lowercase letters, digits, _ or -"),
}


def validate(edits: dict[str, str]) -> dict[str, str]:
    """Clean the posted fields. Raises ValueError naming the first bad one."""
    out: dict[str, str] = {}
    url = edits.get("server_url", "").strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Tesserae server must be an http:// or https:// address")
        out["server_url"] = url.rstrip("/")
    mode = edits.get("transport_mode", "").strip()
    if mode:
        if mode not in TRANSPORT_MODES:
            raise ValueError(f"Connect by must be one of {', '.join(sorted(TRANSPORT_MODES))}")
        out["transport_mode"] = mode
    code = edits.get("pairing_code", "").strip()
    if code:
        if not _PAIRING_RE.match(code):
            raise ValueError("Pairing code: letters, digits and - only")
        out["pairing_code"] = code
    device_id = edits.get("device_id", "").strip()
    if device_id:
        if not _DEVICE_ID_RE.match(device_id):
            raise ValueError("Device id: lowercase, starting with a letter, 2-32 characters")
        out["device_id"] = device_id
    return out


def apply_edits(cfg: Config, edits: dict[str, str]) -> Config:
    """``edits`` (already validated) applied to ``cfg``.

    A new server or device id drops the REST token: it belonged to the old
    server, and keeping it would only earn a 401 from the new one.
    """
    if "transport_mode" in edits:
        cfg = replace(cfg, transport_mode=edits["transport_mode"])
    if "device_id" in edits and edits["device_id"] != cfg.mqtt.device_id:
        cfg = replace(cfg, mqtt=replace(cfg.mqtt, device_id=edits["device_id"]))
        cfg = with_rest_updates(cfg, device_token="", last_frame_etag="")
    if "server_url" in edits and edits["server_url"] != cfg.rest.server_url:
        cfg = with_rest_updates(
            cfg, server_url=edits["server_url"], device_token="", last_frame_etag=""
        )
    if "pairing_code" in edits:
        cfg = with_rest_updates(cfg, pairing_code=edits["pairing_code"])
    return cfg


def lan_url(ip: str, port: int = LAN_PORT) -> str:
    return f"http://{ip}:{port}/" if ip else f"http://<this-pi>:{port}/"


def _page(cfg: Config, status: Any, token: str, editable: bool, message: str = "") -> str:
    e = html.escape
    rows = [
        ("Device id", cfg.mqtt.device_id),
        ("Kind", getattr(status, "kind", "")),
        ("Panel", f"{getattr(status, 'panel', '')} ({status.panel_w}x{status.panel_h})"),
        ("Connect by", cfg.transport_mode),
        ("Server", cfg.rest.server_url),
        ("Paired", "yes" if paired(cfg) else "no"),
        ("State", getattr(status, "state", "")),
        ("Last error", getattr(status, "last_error", "") or "none"),
        ("Version", getattr(status, "fw_version", "")),
    ]
    table = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in rows)
    if editable:
        values = {
            "server_url": cfg.rest.server_url,
            "transport_mode": cfg.transport_mode,
            "pairing_code": "",
            "device_id": cfg.mqtt.device_id,
        }
        inputs = "".join(
            f'<label>{e(label)}<input name="{name}" value="{e(values[name])}">'
            f"<small>{e(hint)}</small></label>"
            for name, (label, hint) in FIELDS.items()
        )
        body = (
            f'<form method="post" action="/settings"><input type="hidden" name="token" '
            f'value="{token}">{inputs}<button>Save and restart</button></form>'
            f'<form method="post" action="/reset" onsubmit="return confirm(\'Forget this '
            f'Pi\'s pairing?\')"><input type="hidden" name="token" value="{token}">'
            '<button class="danger">Factory reset</button></form>'
        )
    else:
        body = (
            "<p>Read only. To change settings, hold the refresh button for 3 seconds "
            "and let go (20 seconds factory-resets instead).</p>"
        )
    note = f'<p class="msg">{e(message)}</p>' if message else ""
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width">'
        "<title>Tesserae Pi client</title><style>"
        "body{font:16px system-ui,sans-serif;max-width:36em;margin:2em auto;padding:0 1em}"
        "th{text-align:left;padding-right:1em;font-weight:500;color:#555}"
        "label{display:block;margin:1em 0}input{display:block;width:100%;padding:.4em}"
        "small{color:#666}button{padding:.5em 1em;margin:.5em 0}"
        ".danger{color:#b00}.msg{background:#eef;padding:.5em}"
        f"</style></head><body><h1>Tesserae Pi client</h1>{note}<table>{table}</table>"
        f"{body}</body></html>"
    )


class LanServer:
    """The page, served from a daemon thread."""

    def __init__(
        self,
        config_path: Path,
        status: Any,
        shutdown: threading.Event,
        port: int = LAN_PORT,
        host: str = "0.0.0.0",  # noqa: S104 - it is a LAN page; reads are harmless
        access: LanAccess | None = None,
        pending: Pending | None = None,
    ) -> None:
        self.config_path = config_path
        self.status = status
        self.shutdown = shutdown
        self.access = access or ACCESS
        self.pending = pending or PENDING
        self.token = secrets.token_urlsafe(16)
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="tesserae-lan", daemon=True
        )
        self._thread.start()
        log.info("settings page on port %d", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("lan: " + fmt, *args)

            def _send(self, code: int, text: str) -> None:
                data = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)

            def _render(self, code: int = 200, message: str = "") -> None:
                cfg = load_config(server.config_path)
                editable = server.access.editable(cfg)
                self._send(code, _page(cfg, server.status, server.token, editable, message))

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path.split("?", 1)[0] != "/":
                    self._send(404, "not found")
                    return
                self._render()

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = min(int(self.headers.get("Content-Length") or 0), 8192)
                form = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(
                        self.rfile.read(length).decode("utf-8", "replace")
                    ).items()
                }
                cfg = load_config(server.config_path)
                if not secrets.compare_digest(form.get("token", ""), server.token):
                    self._send(403, "stale form: reload the page")
                    return
                if not server.access.editable(cfg):
                    self._render(403, "Locked. Hold refresh 3 seconds and let go to unlock.")
                    return
                if self.path == "/settings":
                    try:
                        edits = validate(form)
                    except ValueError as exc:
                        self._render(400, str(exc))
                        return
                    server.pending.edits = edits
                elif self.path == "/reset":
                    server.pending.factory_reset = True
                else:
                    self._send(404, "not found")
                    return
                self._send(200, "<p>Saved. The client restarts in a few seconds.</p>")
                server.shutdown.set()

        return Handler


def apply_pending(cfg: Config, pending: Pending) -> Config:
    """What main writes once the transport has stopped."""
    from .config import factory_reset

    if pending.factory_reset:
        cfg = factory_reset(cfg)
    if pending.edits:
        cfg = apply_edits(cfg, pending.edits)
    return cfg


__all__ = [
    "ACCESS",
    "LAN_PORT",
    "PENDING",
    "SETTINGS_RESTART_EXIT",
    "UNLOCK_S",
    "LanAccess",
    "LanServer",
    "Pending",
    "apply_edits",
    "apply_pending",
    "lan_url",
    "paired",
    "validate",
]
