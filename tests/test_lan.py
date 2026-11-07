"""LAN settings page: read-only unless unpaired or unlocked; edits queued."""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tesserae_pi_client import lan
from tesserae_pi_client.config import load_config, render_config_toml
from tesserae_pi_client.heartbeat import Status
from tesserae_pi_client.lan import (
    UNLOCK_S,
    LanAccess,
    LanServer,
    Pending,
    apply_edits,
    apply_pending,
    paired,
    validate,
)

PAIRED = render_config_toml(
    device_id="hall", rest_server_url="http://old.local:8765", rest_device_token="TOKEN_12345"
)
UNPAIRED = render_config_toml(device_id="hall", rest_server_url="http://old.local:8765")


def _cfg_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


# --- rules -------------------------------------------------------------------------


def test_an_unpaired_pi_is_editable_and_a_paired_one_only_once_unlocked(tmp_path: Path) -> None:
    access = LanAccess()
    assert access.editable(load_config(_cfg_file(tmp_path, UNPAIRED)), now=1000)
    cfg = load_config(_cfg_file(tmp_path, PAIRED))
    assert paired(cfg) and not access.editable(cfg, now=1000)
    access.unlock(now=1000)
    assert access.editable(cfg, now=1000 + UNLOCK_S - 1)
    assert not access.editable(cfg, now=1000 + UNLOCK_S + 1)


def test_unlocking_tells_the_panel() -> None:
    access = LanAccess()
    seen: list[int] = []
    access.on_unlock(lambda: seen.append(1))
    access.unlock()
    assert seen == [1]


@pytest.mark.parametrize(
    "form",
    [
        {"server_url": "ftp://x"},
        {"server_url": "tesserae.local"},
        {"transport_mode": "carrier-pigeon"},
        {"pairing_code": "<script>"},
        {"device_id": "Hall"},
        {"device_id": "9lives"},
    ],
)
def test_bad_values_are_refused(form: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        validate(form)


def test_a_new_server_drops_the_old_servers_token(tmp_path: Path) -> None:
    cfg = load_config(_cfg_file(tmp_path, PAIRED))
    edits = validate({"server_url": "http://new.local:8765/", "pairing_code": "ABCD-1234"})
    new = apply_edits(cfg, edits)
    assert new.rest.server_url == "http://new.local:8765"
    assert new.rest.device_token == "" and new.rest.pairing_code == "ABCD-1234"


def test_unchanged_server_keeps_the_token(tmp_path: Path) -> None:
    cfg = load_config(_cfg_file(tmp_path, PAIRED))
    new = apply_edits(cfg, validate({"server_url": "http://old.local:8765"}))
    assert new.rest.device_token == "TOKEN_12345"


def test_pending_reset_then_edits(tmp_path: Path) -> None:
    cfg = load_config(_cfg_file(tmp_path, PAIRED))
    out = apply_pending(cfg, Pending(edits={"transport_mode": "relay"}, factory_reset=True))
    assert out.transport_mode == "relay" and out.rest.device_token == ""


# --- over HTTP ---------------------------------------------------------------------


@pytest.fixture
def page(tmp_path: Path) -> Iterator[tuple[LanServer, Path, threading.Event]]:
    path = _cfg_file(tmp_path, PAIRED)
    shutdown = threading.Event()
    status = Status(panel="inky_impression_7_3", panel_w=800, panel_h=480)
    server = LanServer(
        path, status, shutdown, port=0, host="127.0.0.1", access=LanAccess(), pending=Pending()
    )
    server.start()
    yield server, path, shutdown
    server.stop()


def _get(server: LanServer) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as resp:  # noqa: S310
        return str(resp.read().decode())


def _post(server: LanServer, path: str, form: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data)
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_a_paired_pi_shows_a_read_only_page(page: tuple[LanServer, Path, threading.Event]) -> None:
    server, _path, shutdown = page
    text = _get(server)
    assert "hall" in text and "Read only" in text and "<form" not in text
    code, _ = _post(server, "/settings", {"token": server.token, "transport_mode": "relay"})
    assert code == 403 and not server.pending.any() and not shutdown.is_set()


def test_unlocked_it_saves_by_queueing_and_restarting(
    page: tuple[LanServer, Path, threading.Event],
) -> None:
    server, path, shutdown = page
    server.access.unlock()
    assert "Save and restart" in _get(server)
    code, _ = _post(
        server, "/settings", {"token": server.token, "server_url": "http://new.local:8765"}
    )
    assert code == 200 and shutdown.is_set()
    assert server.pending.edits == {"server_url": "http://new.local:8765"}
    # Not written under the running client; main applies it after it stops.
    assert load_config(path).rest.server_url == "http://old.local:8765"


def test_a_form_without_the_pages_token_is_refused(
    page: tuple[LanServer, Path, threading.Event],
) -> None:
    server, _path, shutdown = page
    server.access.unlock()
    code, _ = _post(server, "/reset", {"token": "forged"})
    assert code == 403 and not server.pending.factory_reset and not shutdown.is_set()


def test_reset_is_queued(page: tuple[LanServer, Path, threading.Event]) -> None:
    server, _path, shutdown = page
    server.access.unlock()
    code, _ = _post(server, "/reset", {"token": server.token})
    assert code == 200 and server.pending.factory_reset and shutdown.is_set()


def test_a_bad_value_is_shown_not_saved(page: tuple[LanServer, Path, threading.Event]) -> None:
    server, _path, shutdown = page
    server.access.unlock()
    code, text = _post(server, "/settings", {"token": server.token, "server_url": "nope"})
    assert code == 400 and "http://" in text and not shutdown.is_set()


def test_values_are_escaped(tmp_path: Path) -> None:
    cfg = load_config(_cfg_file(tmp_path, PAIRED))
    status = Status(panel="x", panel_w=1, panel_h=1, last_error="<script>alert(1)</script>")
    text = lan._page(cfg, status, "tok", editable=True)
    assert "<script>alert" not in text and "&lt;script&gt;" in text


def test_unknown_paths_404(page: tuple[LanServer, Path, threading.Event]) -> None:
    server, _path, _ = page
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/etc/passwd")  # noqa: S310
    assert exc.value.code == 404
