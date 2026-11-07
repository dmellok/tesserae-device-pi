"""Carry an install over from the two clients this one replaces.

``tesserae-device-pi-png`` and ``tesserae-device-pi-bin`` kept their config at
``~/.config/tesserae-pi-png-client/config.toml`` and
``~/.config/tesserae-pi-bin-client/config.toml``. The first time this client
starts without a config of its own, it copies the newer of those into place,
so the device keeps its id, its pairing token (no re-pair) and its button
mapping. The old file is copied, never moved: reinstalling the old client
still finds it.

Both old formats parse here. The bin client's ``[panel] model`` is ignored:
this client reads the panel off the HAT, which cannot disagree with itself.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_APP_NAMES: tuple[str, ...] = ("tesserae-pi-png-client", "tesserae-pi-bin-client")


def legacy_config_paths(home: Path | None = None) -> list[Path]:
    """Where the replaced clients kept their config, whether or not present."""
    base = (home or Path.home()) / ".config"
    return [base / name / "config.toml" for name in LEGACY_APP_NAMES]


def adopt_legacy_config(target: Path, home: Path | None = None) -> Path | None:
    """Copy the newest legacy config to ``target`` when ``target`` is absent.

    Returns the path copied from, or None when there was nothing to do (a
    config already exists, or no old client was ever installed).
    """
    if target.exists():
        return None
    found = [p for p in legacy_config_paths(home) if p.is_file()]
    if not found:
        return None
    source = max(found, key=lambda p: p.stat().st_mtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o600)  # it holds the device token
    log.info("adopted the config of the client this one replaces: %s -> %s", source, target)
    return source


__all__ = ["LEGACY_APP_NAMES", "adopt_legacy_config", "legacy_config_paths"]
