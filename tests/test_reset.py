"""Factory reset (Refresh held 20 s, or --factory-reset) and always-on beats."""

from __future__ import annotations

import json
from pathlib import Path

from tesserae_pi_client.buttons import RESET_HOLD_S, UNLOCK_HOLD_S, check_holds
from tesserae_pi_client.config import (
    factory_reset,
    load_config,
    render_config_toml,
    save_config,
)
from tesserae_pi_client.heartbeat import Status
from tesserae_pi_client.main import run

PAIRED = render_config_toml(
    device_id="hall",
    rest_server_url="http://tesserae.local:8765",
    rest_device_token="TOK",
    rest_last_frame_etag='"abc"',
    relay_install_id="inst",
    relay_device_id="dev",
    relay_device_token="RTOK",
    relay_frame_key="k",
    relay_private_key="p",
    buttons_enabled=True,
    buttons_pin_c=16,
)


def _cfg(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(PAIRED)
    return path


def test_factory_reset_forgets_pairings_and_keeps_the_pi(tmp_path: Path) -> None:
    cfg = factory_reset(load_config(_cfg(tmp_path)))
    assert cfg.rest.device_token == "" and cfg.rest.last_frame_etag == ""
    assert (cfg.relay.install_id, cfg.relay.device_id, cfg.relay.device_token) == ("", "", "")
    assert (cfg.relay.frame_key, cfg.relay.private_key) == ("", "")
    # What is about this Pi, not one pairing, stays.
    assert cfg.rest.server_url == "http://tesserae.local:8765"
    assert cfg.mqtt.device_id == "hall"
    assert cfg.buttons.enabled and cfg.buttons.pin_c == 16


def test_the_cli_reset_persists(tmp_path: Path) -> None:
    path = _cfg(tmp_path)
    assert run(["--config", str(path), "--factory-reset"]) == 0
    cfg = load_config(path)
    assert cfg.rest.device_token == "" and cfg.relay.device_token == ""
    assert cfg.mqtt.device_id == "hall"


def test_a_reset_config_round_trips(tmp_path: Path) -> None:
    path = _cfg(tmp_path)
    save_config(factory_reset(load_config(path)), path)
    assert load_config(path).rest.device_token == ""


# --- the 20 s hold -------------------------------------------------------------


def test_a_quick_press_does_nothing_more() -> None:
    held = {6: 0.0}
    assert check_holds(held, 1.0, pressed=lambda _pin: False) == set()
    assert held == {}


def test_releasing_after_a_few_seconds_unlocks_settings() -> None:
    held = {6: 0.0}
    assert check_holds(held, UNLOCK_HOLD_S + 1, pressed=lambda _pin: False) == {"unlock"}
    assert held == {}


def test_a_hold_still_down_keeps_waiting() -> None:
    held = {6: 0.0}
    assert check_holds(held, RESET_HOLD_S - 1, pressed=lambda _pin: True) == set()
    assert held == {6: 0.0}


def test_a_full_hold_resets_once_and_never_unlocks() -> None:
    held = {6: 0.0}
    assert check_holds(held, RESET_HOLD_S, pressed=lambda _pin: True) == {"reset"}
    assert held == {}
    assert check_holds(held, RESET_HOLD_S + 5, pressed=lambda _pin: False) == set()


# --- always on -----------------------------------------------------------------


def test_every_beat_says_a_pi_can_stay_awake() -> None:
    assert json.loads(Status(panel="x").to_json())["can_stay_awake"] is True
