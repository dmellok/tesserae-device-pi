"""Apply a signed OTA update (docs/ota/contract.md), the Pi way.

The server hands a device ``{"ota": {"payload", "signature"}}`` on /status
when its kind has a newer release. Here that is checked exactly as the
firmware checks it, then installed with pip instead of written to an A/B
flash slot:

1. Signature: Ed25519 over the raw payload bytes, against the public keys
   built into this client (:data:`TRUSTED_KEYS`); the manifest's ``key_id``
   must name the key that verified it.
2. Manifest: schema 1, a kind this client runs (``pi_client`` or either
   older Pi kind), and a version newer than the one running.
3. Image: downloaded (relative URLs against the server), size and SHA-256
   against the signed manifest, and it must be a wheel of this package at
   that version.
4. Install: the current install is recorded first (the editable checkout
   ``install.sh`` makes, or the wheel a previous update left), then
   ``pip install`` the wheel into this venv. A failed install puts the old
   one straight back.
5. Restart: the transport exits with :data:`OTA_RESTART_EXIT` and systemd
   starts the new version. The rollback guard (:mod:`ota_guard`, copied out
   of the package by *this* version) counts the starts; the new version
   :meth:`confirm`\\s after its first healthy status round-trip, and one that
   never does is replaced by the old version after three tries.

Progress is reported through :class:`ota_report.OtaReporter`, the same
``ota`` object the firmware sends, so the server's Devices UI reads alike.
The attempt's own log lines go to :class:`ota_log.OtaLog` when one is given,
which sends them to the server as an OTA log (best effort, never in the way).
"""

from __future__ import annotations

import binascii
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .ota_log import OtaLog
from .ota_report import OtaReporter

log = logging.getLogger(__name__)

#: Ed25519 public keys this client accepts updates signed by: key_id -> hex.
#: The production key's public half, as published in the server repo
#: (ota/keys/prod-1.pub). A new key ships in a release signed by an old one.
TRUSTED_KEYS: dict[str, str] = {
    "prod-1": "97f3294e7bf8f2dfb965ac2739e6d2d7ee346c42681a94d87c9189942db3965e",
}
SCHEMA_VERSION = 1
MANIFEST_FIELDS = (
    "schema_version",
    "key_id",
    "device_kind",
    "fw_version",
    "image_url",
    "size_bytes",
    "sha256",
)
#: Server kinds this one package runs as.
KINDS = frozenset({"pi_client", "pi_png_client", "pi_bin_client"})
DIST_NAME = "tesserae-device-pi"
PACKAGE = "tesserae_device_pi"
#: A wheel of this client is a few hundred KB; refuse anything absurd.
MAX_IMAGE_BYTES = 64 * 1024 * 1024
#: Exit code after installing an update: systemd (Restart=on-failure) starts
#: the new version.
OTA_RESTART_EXIT = 4

PENDING_FILE = "ota-pending.json"
GUARD_FILE = "ota-guard.py"
INSTALLED_WHEEL = "ota-installed.whl"


class OtaRejected(Exception):
    """The update was refused or failed; ``reason`` is the contract's word."""

    def __init__(self, phase: str, reason: str, detail: str = "") -> None:
        super().__init__(f"{phase}/{reason}: {detail}" if detail else f"{phase}/{reason}")
        self.phase = phase
        self.reason = reason


def _b64u(text: Any) -> bytes:
    import base64

    if not isinstance(text, str):
        raise ValueError("not a string")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def version_tuple(version: str) -> tuple[int, ...]:
    """``1.2.10`` -> (1, 2, 10); anything after the numeric run is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def is_newer(candidate: str, current: str) -> bool:
    return bool(version_tuple(candidate)) and version_tuple(candidate) > version_tuple(current)


def verify_descriptor(descriptor: Any, keys: dict[str, str]) -> dict[str, Any]:
    """Steps 1 and the shape half of 2. Returns the manifest or raises."""
    if not isinstance(descriptor, dict):
        raise OtaRejected("rejected", "malformed_descriptor", "not an object")
    try:
        payload = _b64u(descriptor.get("payload"))
        signature = _b64u(descriptor.get("signature"))
    except (ValueError, binascii.Error) as exc:
        raise OtaRejected("rejected", "malformed_descriptor", str(exc)) from exc
    if len(signature) != 64:
        raise OtaRejected("rejected", "malformed_descriptor", "signature is not 64 bytes")

    signed_by = None
    for key_id, hex_key in keys.items():
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key)).verify(signature, payload)
        except (InvalidSignature, ValueError):
            continue
        signed_by = key_id
        break
    if signed_by is None:
        raise OtaRejected("rejected", "bad_signature")

    try:
        manifest = json.loads(payload)
    except ValueError as exc:
        raise OtaRejected("rejected", "malformed_manifest", str(exc)) from exc
    if not isinstance(manifest, dict) or any(k not in manifest for k in MANIFEST_FIELDS):
        raise OtaRejected("rejected", "malformed_manifest", "missing fields")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise OtaRejected("rejected", "schema_version", str(manifest["schema_version"]))
    if manifest["key_id"] != signed_by:
        raise OtaRejected("rejected", "bad_signature", "key_id names another key")
    return manifest


def check_image(manifest: dict[str, Any], image: bytes) -> None:
    """Step 3: the bytes are the signed wheel of this package at that version."""
    if len(image) != int(manifest["size_bytes"]):
        raise OtaRejected("failed", "size_mismatch", f"{len(image)} != {manifest['size_bytes']}")
    if hashlib.sha256(image).hexdigest() != str(manifest["sha256"]).lower():
        raise OtaRejected("failed", "digest_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(image)) as wheel:
            names = wheel.namelist()
    except zipfile.BadZipFile as exc:
        raise OtaRejected("failed", "image_invalid", "not a wheel") from exc
    want = f"{PACKAGE}-{manifest['fw_version']}.dist-info/METADATA"
    if want not in names:
        raise OtaRejected("failed", "image_invalid", f"no {want} in the wheel")


def current_install(dist: str = DIST_NAME, cache_root: Path | None = None) -> list[str]:
    """pip arguments that put the running install back: the editable checkout
    install.sh made, the wheel a previous update installed, or else the
    running version from PyPI (a ``pip install tesserae-device-pi``)."""
    try:
        installed = metadata.distribution(dist)
    except metadata.PackageNotFoundError:
        return []
    raw = installed.read_text("direct_url.json")
    if raw:
        try:
            info = json.loads(raw)
        except ValueError:
            info = {}
        url = str(info.get("url") or "")
        if (info.get("dir_info") or {}).get("editable") and url.startswith("file://"):
            return ["-e", url[len("file://") :]]
    if cache_root is not None and (cache_root / INSTALLED_WHEEL).exists():
        return [str(cache_root / INSTALLED_WHEEL)]
    if version_tuple(installed.version or ""):
        return [f"{dist}=={installed.version}"]
    return []


def pip_install(args: Sequence[str]) -> None:
    """Install into this venv. Raises on failure."""
    subprocess.run([sys.executable, "-m", "pip", "install", *args], check=True)


Downloader = Callable[[str, int], bytes]
Installer = Callable[[Sequence[str]], None]


@dataclass
class OtaUpdater:
    reporter: OtaReporter
    cache_root: Path
    running_fw: str
    download: Downloader
    install: Installer = pip_install
    keys: dict[str, str] | None = None
    python: str = sys.executable
    ota_log: OtaLog | None = None

    def apply(self, descriptor: Any, kind: str) -> bool:
        """Verify and install an offered update. True when the client should
        exit for systemd to start the new version; False when refused."""
        try:
            return self._apply(descriptor, kind)
        finally:
            if self.ota_log is not None:
                self.ota_log.stop()

    def _apply(self, descriptor: Any, kind: str) -> bool:
        attempt_id = secrets.token_hex(4)
        target = ""
        try:
            manifest = verify_descriptor(descriptor, self.keys or TRUSTED_KEYS)
            target = str(manifest["fw_version"])
            if manifest["device_kind"] not in KINDS or kind not in KINDS:
                raise OtaRejected("rejected", "kind_mismatch", str(manifest["device_kind"]))
            if not is_newer(target, self.running_fw):
                raise OtaRejected("rejected", "already_current", f"running {self.running_fw}")
            size = int(manifest["size_bytes"])
            if not 0 < size <= MAX_IMAGE_BYTES:
                raise OtaRejected("rejected", "malformed_manifest", f"size_bytes {size}")

            # From here on it is an attempt, and its log is worth sending.
            if self.ota_log is not None:
                self.ota_log.begin()
            log.info("ota: updating %s -> %s (attempt %s)", self.running_fw, target, attempt_id)
            self.reporter.set("downloading", target_fw=target, attempt_id=attempt_id)
            try:
                image = self.download(str(manifest["image_url"]), size)
            except Exception as exc:  # noqa: BLE001 - any fetch failure is download_error
                raise OtaRejected("failed", "download_error", str(exc)) from exc
            self.reporter.set("validating", target_fw=target, attempt_id=attempt_id)
            check_image(manifest, image)
            self._install(image, target, attempt_id)
        except OtaRejected as exc:
            log.warning("ota: %s", exc)
            if exc.reason != "already_current":
                self.reporter.set(exc.phase, exc.reason, target, attempt_id)
            if self.ota_log is not None:
                self.ota_log.end(exc.phase, exc.reason, target, attempt_id)
            return False
        self.reporter.set("pending_confirm", target_fw=target, attempt_id=attempt_id)
        log.warning("ota: installed %s; restarting into it", target)
        if self.ota_log is not None:
            self.ota_log.end("pending_confirm", "", target, attempt_id)
        return True

    def _install(self, image: bytes, target: str, attempt_id: str) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        wheel = self.cache_root / f"{PACKAGE}-{target}-py3-none-any.whl"
        wheel.write_bytes(image)
        previous = current_install(cache_root=self.cache_root)
        # This version, known good, writes the guard the next start runs.
        shutil.copyfile(Path(__file__).with_name("ota_guard.py"), self.cache_root / GUARD_FILE)
        pending = {
            "target": target,
            "previous": previous,
            "python": self.python,
            "attempt_id": attempt_id,
            "state_path": str(self.reporter.path),
            "wheel": str(wheel),
            "boots": 0,
        }
        (self.cache_root / PENDING_FILE).write_text(json.dumps(pending), encoding="utf-8")
        try:
            self.install([str(wheel)])
        except Exception as exc:  # noqa: BLE001 - put the old version back, then report
            log.error("ota: pip install failed (%s); restoring the previous install", exc)
            if previous:
                try:
                    self.install(["--no-deps", "--force-reinstall", *previous])
                except Exception as restore_exc:  # noqa: BLE001
                    log.error("ota: restoring failed too: %s", restore_exc)
            (self.cache_root / PENDING_FILE).unlink(missing_ok=True)
            raise OtaRejected("failed", "flash_error", str(exc)) from exc

    def confirm(self) -> None:
        """The running version has made a healthy round-trip: keep it."""
        pending_path = self.cache_root / PENDING_FILE
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(pending, dict) or pending.get("target") != self.running_fw:
            return
        wheel = Path(str(pending.get("wheel") or ""))
        if wheel.is_file():
            # The rollback target for the update after this one.
            shutil.copyfile(wheel, self.cache_root / INSTALLED_WHEEL)
            wheel.unlink(missing_ok=True)
        pending_path.unlink(missing_ok=True)
        if self.reporter.state.phase in ("pending_confirm", "confirmed"):
            self.reporter.set("confirmed", "ok", self.running_fw, self.reporter.state.attempt_id)
        log.info("ota: %s confirmed after a healthy round-trip", self.running_fw)


__all__ = [
    "KINDS",
    "OTA_RESTART_EXIT",
    "TRUSTED_KEYS",
    "OtaRejected",
    "OtaUpdater",
    "check_image",
    "current_install",
    "is_newer",
    "verify_descriptor",
]
