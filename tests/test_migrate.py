"""Installs of the pi-png / pi-bin clients carry over without re-pairing."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from tesserae_device_pi.config import load_config, render_config_toml
from tesserae_device_pi.migrate import adopt_legacy_config, legacy_config_paths

# Written the way the old installers wrote them (render_config_toml with the
# prompted values); the bin client's also had a [panel] section.
PNG_CFG = render_config_toml(device_id="lounge", rest_device_token="TOKEN_FROM_PNG")
BIN_CFG = (
    render_config_toml(device_id="hall", rest_device_token="TOKEN_FROM_BIN", buttons_pin_c=16)
    + '\n[panel]\nmodel = "inky_7_3"\n'
)


def _write(path: Path, body: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.utime(path, (mtime, mtime))


def _target(home: Path) -> Path:
    return home / ".config" / "tesserae-device-pi" / "config.toml"


def test_nothing_to_adopt_on_a_fresh_pi(tmp_path: Path) -> None:
    assert adopt_legacy_config(_target(tmp_path), home=tmp_path) is None
    assert not _target(tmp_path).exists()


def test_a_bin_install_keeps_its_id_token_and_buttons(tmp_path: Path) -> None:
    png_path, bin_path = legacy_config_paths(tmp_path)
    _write(bin_path, BIN_CFG, 1000)

    assert adopt_legacy_config(_target(tmp_path), home=tmp_path) == bin_path
    cfg = load_config(_target(tmp_path))
    assert cfg.mqtt.device_id == "hall"
    assert cfg.rest.device_token == "TOKEN_FROM_BIN"
    assert cfg.buttons.pin_c == 16
    # Copied, not moved, and private: it holds the device token.
    assert bin_path.exists()
    assert stat.S_IMODE(_target(tmp_path).stat().st_mode) == 0o600


def test_the_newer_of_two_old_installs_wins(tmp_path: Path) -> None:
    png_path, bin_path = legacy_config_paths(tmp_path)
    _write(png_path, PNG_CFG, 2000)
    _write(bin_path, BIN_CFG, 1000)

    assert adopt_legacy_config(_target(tmp_path), home=tmp_path) == png_path
    assert load_config(_target(tmp_path)).rest.device_token == "TOKEN_FROM_PNG"


def test_an_existing_config_is_never_overwritten(tmp_path: Path) -> None:
    _png_path, bin_path = legacy_config_paths(tmp_path)
    _write(bin_path, BIN_CFG, 1000)
    _write(_target(tmp_path), PNG_CFG, 500)

    assert adopt_legacy_config(_target(tmp_path), home=tmp_path) is None
    assert load_config(_target(tmp_path)).mqtt.device_id == "lounge"
