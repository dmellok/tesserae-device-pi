from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    Config,
    load_config,
    save_config,
    with_relay_updates,
    with_rest_updates,
)
from .heartbeat import Status, _primary_ip
from .paint import auto_panel, model_name, paint, panel_resolution, stripe_test_image

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _detect_panel() -> Any:
    """Auto-detect the inky panel. Raises a clear error if HAT/SPI is off."""
    try:
        return auto_panel()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "could not auto-detect inky panel: "
            f"{type(exc).__name__}: {exc}\n"
            "Troubleshooting:\n"
            "  1. enable BOTH interfaces: raspi-config -> Interface Options ->\n"
            "     SPI -> enable, and I2C -> enable. I2C is how the HAT EEPROM is\n"
            "     read; without it you get 'No EEPROM detected'.\n"
            "  2. reboot after enabling SPI/I2C, then check the EEPROM is visible:\n"
            "     ls /dev/i2c-1 && sudo i2cdetect -y 1   (expect '50' in the grid)\n"
            "  3. user running the service must be in the 'gpio' and 'spi' groups\n"
            "  4. if i2cdetect shows no '50', the board has no readable EEPROM and\n"
            "     inky.auto() cannot identify it (some Impression/Spectra units)"
        ) from exc


def _do_paint_test(_: Config) -> int:
    panel = _detect_panel()
    width, height = panel_resolution(panel)
    name = model_name(panel)
    log.info("detected panel %s (%dx%d)", name, width, height)
    img = stripe_test_image(width, height)
    log.info("painting stripe test pattern")
    paint(panel, img, saturation=0.5)
    log.info("paint-test complete")
    return 0


def _build_status_and_painter(_config: Config) -> tuple[Status, Any, Any]:
    """Shared by both transports: detect the panel, pre-fill discovery
    fields on Status, return (status, panel, paint_fn)."""
    panel = _detect_panel()
    panel_size = panel_resolution(panel)
    name = model_name(panel)
    log.info("detected panel %s (%dx%d)", name, panel_size[0], panel_size[1])

    status = Status(panel=name, panel_w=panel_size[0], panel_h=panel_size[1])
    # Resolved once at startup — neither value changes between cycles.
    status.ip = _primary_ip()

    def paint_fn(img: Any, saturation: float) -> None:
        paint(panel, img, saturation)

    return status, panel, paint_fn


def _do_run(config: Config, config_path: Path) -> int:
    """Dispatch to the configured transport's wake loop."""
    status, _panel, paint_fn = _build_status_and_painter(config)

    shutdown = threading.Event()

    def _signal_handler(signum: int, _frame: Any) -> None:
        log.info("signal %d received; shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if config.transport_mode == "rest":
        log.info(
            "transport_mode=rest; using HTTP polling against %s",
            config.rest.server_url,
        )
        from .transports import rest

        return rest.run(config, status, paint_fn, shutdown, config_path)

    if config.transport_mode == "relay":
        log.info(
            "transport_mode=relay; polling the cloud mailbox at %s",
            config.relay.relay_url,
        )
        from .transports import relay

        return relay.run(config, status, paint_fn, shutdown, config_path)

    log.info(
        "transport_mode=mqtt; using MQTT broker at %s:%d",
        config.mqtt.host, config.mqtt.port,
    )
    from .transports import mqtt

    return mqtt.run(config, status, paint_fn, shutdown, config_path)


def _apply_pair_flag(config: Config, pair_code: str, config_path: Path) -> Config:
    """In-memory override: --pair beats whatever pairing_code was in config.

    The new code is NOT persisted by this function — the REST claim flow
    persists it implicitly by wiping it once the resulting device_token is
    saved. If the user pre-empts the daemon (Ctrl-C before pairing
    completes), the next run starts fresh and they re-supply --pair.
    """
    if config.transport_mode == "relay":
        return _apply_relay_pair_flag(config, pair_code, config_path)
    if config.transport_mode != "rest":
        raise SystemExit(
            "--pair only applies when transport_mode = 'rest' or 'relay' "
            f"(current: {config.transport_mode!r})"
        )
    if config.rest.device_token:
        log.warning(
            "--pair ignored: a device_token is already saved at %s. "
            "Wipe it (set device_token = \"\" in [rest]) and re-run to re-pair.",
            config_path,
        )
        return config
    if config.rest.pairing_code and config.rest.pairing_code != pair_code:
        log.info("--pair overrides config.rest.pairing_code for this run")
    return with_rest_updates(config, pairing_code=pair_code)


def _apply_relay_pair_flag(config: Config, pair_code: str, config_path: Path) -> Config:
    """--pair for the relay transport.

    Persisted, unlike the REST path: relay pairing spans restarts by design
    (the code has no expiry once submitted, and home may take a while to
    complete the exchange), so the code has to survive a reboot mid-pairing.

    A code supplied while a different pairing is already half-done clears the
    stored private key, since that key is bound to the code it was submitted
    with — reusing it against a new code would derive a frame key home never
    agrees with, and the only symptom would be frames that never appear.
    """
    if config.relay.frame_key:
        log.warning(
            "--pair ignored: this panel is already relay-paired (frame_key is "
            "set in %s). Clear [relay].frame_key and re-run to re-pair.",
            config_path,
        )
        return config
    if config.relay.private_key and config.relay.pairing_code != pair_code:
        log.info("--pair supplies a new code; discarding the half-done pairing")
        config = with_relay_updates(config, private_key="")
    config = with_relay_updates(config, pairing_code=pair_code)
    save_config(config, config_path)
    return config


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tesserae-pi-client",
        description="Subscribe to a Tesserae server and paint PNG frames "
        "onto a Pimoroni e-ink panel via the inky library.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--paint-test",
        action="store_true",
        help="paint a colour stripe pattern and exit (no MQTT/REST)",
    )
    parser.add_argument(
        "--pair",
        metavar="CODE",
        default=None,
        help="Pair this panel. In rest mode: present the code to the server on "
        "first run (overrides [rest].pairing_code; ignored once a device_token "
        "is saved). In relay mode: start the cloud rendezvous with a code from "
        "Settings -> Cloud relay (persisted, since pairing can span restarts).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    _setup_logging(config.logging.level)

    if args.paint_test:
        return _do_paint_test(config)
    if args.pair is not None:
        config = _apply_pair_flag(config, args.pair, args.config)
    return _do_run(config, args.config)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
