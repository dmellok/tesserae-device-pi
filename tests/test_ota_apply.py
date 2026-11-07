"""Signed OTA: verify like the firmware, install with pip, roll back by guard."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tesserae_device_pi import ota_apply, ota_guard
from tesserae_device_pi.ota_apply import (
    OtaRejected,
    OtaUpdater,
    is_newer,
    verify_descriptor,
)
from tesserae_device_pi.ota_report import OtaReporter

KEY = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()


def _hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


KEYS = {"test-1": _hex(KEY)}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _wheel(version: str = "1.0.0", package: str = "tesserae_device_pi") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{package}-{version}.dist-info/METADATA", f"Name: x\nVersion: {version}\n")
        z.writestr(f"{package}/__init__.py", f'__version__ = "{version}"\n')
    return buf.getvalue()


def _descriptor(
    image: bytes,
    *,
    key: Ed25519PrivateKey = KEY,
    key_id: str = "test-1",
    **overrides: Any,
) -> dict[str, str]:
    manifest = {
        "schema_version": 1,
        "key_id": key_id,
        "device_kind": "pi_client",
        "fw_version": "1.0.0",
        "image_url": "/api/v1/ota/pi_client/1.0.0.whl",
        "size_bytes": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
        **overrides,
    }
    payload = json.dumps(manifest, sort_keys=True).encode()
    return {"payload": _b64u(payload), "signature": _b64u(key.sign(payload))}


class _Setup:
    def __init__(self, tmp_path: Path, image: bytes, running: str = "0.9.0") -> None:
        self.reporter = OtaReporter(tmp_path / "ota-state.json")
        self.installs: list[list[str]] = []
        self.downloads: list[tuple[str, int]] = []
        self.fail_install = False
        self.image = image
        self.cache = tmp_path

        def download(url: str, size: int) -> bytes:
            self.downloads.append((url, size))
            return self.image

        def install(args: Sequence[str]) -> None:
            self.installs.append(list(args))
            if self.fail_install and "--force-reinstall" not in args:
                raise RuntimeError("pip exploded")

        self.updater = OtaUpdater(
            self.reporter, tmp_path, running, download, install=install, keys=KEYS
        )


# --- verification ----------------------------------------------------------------


def test_a_good_descriptor_verifies() -> None:
    assert verify_descriptor(_descriptor(_wheel()), KEYS)["fw_version"] == "1.0.0"


@pytest.mark.parametrize(
    ("descriptor", "reason"),
    [
        (_descriptor(_wheel(), key=OTHER), "bad_signature"),
        (_descriptor(_wheel(), key_id="prod-1"), "bad_signature"),  # names another key
        ({"payload": "@@", "signature": "x"}, "malformed_descriptor"),
        ({"payload": _b64u(b"{}"), "signature": _b64u(b"x" * 10)}, "malformed_descriptor"),
        ("not a dict", "malformed_descriptor"),
        (_descriptor(_wheel(), schema_version=2), "schema_version"),
    ],
)
def test_bad_descriptors_are_rejected(descriptor: Any, reason: str) -> None:
    with pytest.raises(OtaRejected) as exc:
        verify_descriptor(descriptor, KEYS)
    assert exc.value.reason == reason


def test_a_signed_manifest_missing_a_field_is_malformed() -> None:
    payload = json.dumps({"schema_version": 1, "key_id": "test-1"}).encode()
    desc = {"payload": _b64u(payload), "signature": _b64u(KEY.sign(payload))}
    with pytest.raises(OtaRejected, match="malformed_manifest"):
        verify_descriptor(desc, KEYS)


@pytest.mark.parametrize(
    ("a", "b", "newer"),
    [
        ("1.0.0", "0.9.0", True),
        ("0.10.0", "0.9.0", True),
        ("0.9.0", "0.9.0", False),
        ("0.9.0", "1.0.0", False),
        ("v1.2", "1.1.9", True),
        ("junk", "0.1", False),
    ],
)
def test_versions_compare_numerically(a: str, b: str, newer: bool) -> None:
    assert is_newer(a, b) is newer


# --- applying ----------------------------------------------------------------------


def test_an_update_installs_and_asks_for_a_restart(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _wheel())
    assert s.updater.apply(_descriptor(s.image), "pi_client") is True
    assert s.downloads == [("/api/v1/ota/pi_client/1.0.0.whl", len(s.image))]
    (wheel_args,) = s.installs
    assert wheel_args[0].endswith("tesserae_device_pi-1.0.0-py3-none-any.whl")
    pending = json.loads((tmp_path / "ota-pending.json").read_text())
    assert pending["target"] == "1.0.0" and pending["boots"] == 0
    assert pending["state_path"] == str(s.reporter.path)
    assert (tmp_path / "ota-guard.py").read_text() == Path(ota_guard.__file__).read_text()
    assert s.reporter.state.phase == "pending_confirm" and s.reporter.state.target_fw == "1.0.0"


@pytest.mark.parametrize(
    ("image", "overrides", "reason"),
    [
        (_wheel(), {"size_bytes": 5}, "size_mismatch"),
        (_wheel(), {"sha256": "0" * 64}, "digest_mismatch"),
        (b"not a zip at all", {}, "image_invalid"),
        (_wheel("0.0.1"), {}, "image_invalid"),  # the wheel is some other version
        (_wheel(package="someone_else"), {}, "image_invalid"),
    ],
)
def test_a_bad_image_is_never_installed(
    tmp_path: Path, image: bytes, overrides: dict[str, Any], reason: str
) -> None:
    s = _Setup(tmp_path, image)
    desc = _descriptor(image, **overrides)
    if "size_bytes" in overrides:
        s.image = image  # the download really is the other size
    assert s.updater.apply(desc, "pi_client") is False
    assert s.installs == []
    assert (s.reporter.state.phase, s.reporter.state.reason) == ("failed", reason)


def test_the_running_version_is_not_reinstalled(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _wheel(), running="1.0.0")
    assert s.updater.apply(_descriptor(s.image), "pi_client") is False
    assert s.downloads == [] and s.installs == []
    assert s.reporter.state.phase == "idle"  # not worth a report


def test_an_esp_image_is_refused(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _wheel())
    assert s.updater.apply(_descriptor(s.image, device_kind="esp32_client"), "pi_client") is False
    assert s.reporter.state.reason == "kind_mismatch" and s.downloads == []


def test_older_pi_kinds_take_the_same_update(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _wheel())
    desc = _descriptor(s.image, device_kind="pi_png_client")
    assert s.updater.apply(desc, "pi_png_client") is True


def test_a_failed_install_puts_the_previous_one_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ota_apply, "current_install", lambda **_: ["-e", "/home/pi/checkout"])
    s = _Setup(tmp_path, _wheel())
    s.fail_install = True
    assert s.updater.apply(_descriptor(s.image), "pi_client") is False
    assert s.installs[-1] == ["--no-deps", "--force-reinstall", "-e", "/home/pi/checkout"]
    assert not (tmp_path / "ota-pending.json").exists()
    assert (s.reporter.state.phase, s.reporter.state.reason) == ("failed", "flash_error")


def test_the_new_version_confirms_after_a_healthy_round_trip(tmp_path: Path) -> None:
    old = _Setup(tmp_path, _wheel())
    assert old.updater.apply(_descriptor(old.image), "pi_client")
    # ... systemd restarts; the new version comes up:
    new = OtaUpdater(
        OtaReporter(tmp_path / "ota-state.json"), tmp_path, "1.0.0", old.downloads.append
    )
    new.confirm()
    assert not (tmp_path / "ota-pending.json").exists()
    assert (tmp_path / ota_apply.INSTALLED_WHEEL).read_bytes() == old.image  # next rollback target
    assert new.reporter.state.phase == "confirmed"


def test_confirm_ignores_a_record_for_another_version(tmp_path: Path) -> None:
    old = _Setup(tmp_path, _wheel())
    old.updater.apply(_descriptor(old.image), "pi_client")
    still_old = OtaUpdater(old.reporter, tmp_path, "0.9.0", old.downloads.append)
    still_old.confirm()
    assert (tmp_path / "ota-pending.json").exists()


# --- the guard ----------------------------------------------------------------------


def test_the_guard_counts_starts_then_rolls_back(tmp_path: Path) -> None:
    pending = tmp_path / "ota-pending.json"
    state = tmp_path / "ota-state.json"
    pending.write_text(
        json.dumps(
            {
                "target": "1.0.0",
                "previous": [],  # nothing to reinstall in a test
                "python": "/nonexistent/python",
                "attempt_id": "abcd",
                "state_path": str(state),
                "boots": 0,
            }
        )
    )
    for _ in range(ota_guard.MAX_BOOTS):
        assert ota_guard.guard(pending) == "counted"
    assert ota_guard.guard(pending) == "rolled_back"
    assert not pending.exists()
    assert OtaReporter(state).payload() == {
        "schema": 1,
        "phase": "rolled_back",
        "reason": "boot_failed",
        "target_fw": "1.0.0",
        "attempt_id": "abcd",
    }


def test_no_record_no_guard(tmp_path: Path) -> None:
    assert ota_guard.guard(tmp_path / "ota-pending.json") == "none"


def test_the_guard_is_standard_library_only() -> None:
    """It must run when the package it guards is broken."""
    source = Path(ota_guard.__file__).read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= {"__future__", "json", "subprocess", "sys", "pathlib"}


# --- the server's own signed fixtures ----------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "ota"
SERVER_TEST_KEYS = {
    "test-ed25519-1": "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
}


def test_a_descriptor_signed_by_the_server_verifies() -> None:
    desc = json.loads((FIXTURES / "valid.json").read_text())
    manifest = verify_descriptor(desc, SERVER_TEST_KEYS)
    assert manifest["key_id"] == "test-ed25519-1" and manifest["size_bytes"] == 1152


def test_the_servers_wrong_key_fixture_is_refused() -> None:
    desc = json.loads((FIXTURES / "wrong_key.json").read_text())
    with pytest.raises(OtaRejected, match="bad_signature"):
        verify_descriptor(desc, SERVER_TEST_KEYS)


def test_the_servers_esp_fixture_is_never_applied_to_a_pi(tmp_path: Path) -> None:
    s = _Setup(tmp_path, b"")
    s.updater.keys = SERVER_TEST_KEYS
    desc = json.loads((FIXTURES / "valid.json").read_text())
    assert s.updater.apply(desc, "pi_client") is False
    assert s.reporter.state.reason == "kind_mismatch" and s.downloads == []


def test_production_trusts_only_the_production_key() -> None:
    assert set(ota_apply.TRUSTED_KEYS) == {"prod-1"}


# --- in the REST loop -----------------------------------------------------------------


def test_an_offer_in_a_status_reply_installs_and_exits_for_systemd(tmp_path: Path) -> None:
    from tesserae_device_pi.config import parse_toml, render_config_toml
    from tesserae_device_pi.transports import rest

    cfg = parse_toml(render_config_toml(cache_root=str(tmp_path)))
    image = _wheel()
    telemetry = rest.Telemetry(cfg, "0.9.0", download=lambda _u, _n: image)
    installed: list[Sequence[str]] = []
    assert telemetry._updater is not None
    telemetry._updater.install = installed.append
    telemetry._updater.keys = KEYS

    telemetry.after_status({"next_poll_s": 60}, "pi_client")
    assert telemetry.restart_requested is False
    telemetry.after_status({"ota": _descriptor(image)}, "pi_client")
    assert telemetry.restart_requested is True and len(installed) == 1
    assert rest.OTA_RESTART_EXIT == 4
