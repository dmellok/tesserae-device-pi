from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tesserae_device_pi.heartbeat import (
    OFFLINE_WILL_PAYLOAD,
    Heartbeat,
    Status,
    _fw_version,
    _wifi_rssi,
    status_summary,
    status_topic,
)

DEVICE_ID = "pi_png"
STATUS_TOPIC = status_topic(DEVICE_ID)


class FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes, int, bool]] = []

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any:
        self.publishes.append((topic, payload, qos, retain))
        return None


def test_status_json_round_trips() -> None:
    status = Status(panel="inky_impression_7_3", last_digest="abc123")
    obj = json.loads(status.to_json())
    assert obj["state"] == "idle"
    assert obj["panel"] == "inky_impression_7_3"
    assert obj["last_digest"] == "abc123"
    assert obj["last_paint_at"] is None
    assert obj["last_error"] is None
    assert "uptime_s" in obj
    assert "fw_version" in obj


def test_publish_now_uses_retained_qos1_on_status_topic() -> None:
    status = Status(panel="x")
    publisher = FakePublisher()
    heartbeat = Heartbeat(status=status, publisher=publisher, topic=STATUS_TOPIC)
    heartbeat.publish_now()
    assert len(publisher.publishes) == 1
    topic, payload, qos, retain = publisher.publishes[0]
    assert topic == STATUS_TOPIC
    assert qos == 1
    assert retain is True
    assert json.loads(payload)["state"] == "idle"


def test_publish_offline_sends_offline_payload_retained() -> None:
    status = Status(panel="x")
    publisher = FakePublisher()
    heartbeat = Heartbeat(status=status, publisher=publisher, topic=STATUS_TOPIC)
    heartbeat.publish_offline()
    assert len(publisher.publishes) == 1
    topic, payload, qos, retain = publisher.publishes[0]
    assert topic == STATUS_TOPIC
    assert qos == 1
    assert retain is True
    assert payload == OFFLINE_WILL_PAYLOAD
    assert json.loads(payload)["state"] == "offline"


def test_status_summary_is_human_readable() -> None:
    status = Status(panel="x", last_digest="deadbeef")
    summary = status_summary(status)
    assert "state=idle" in summary
    assert "digest=deadbeef" in summary


def test_status_topic_builds_from_device_id() -> None:
    assert status_topic("pi_png") == "tesserae/pi_png/status"
    assert status_topic("pi_lounge") == "tesserae/pi_lounge/status"


def test_status_discovery_fields_round_trip() -> None:
    status = Status(
        panel="inky_impression_7_3",
        kind="pi_png_client",
        panel_w=800,
        panel_h=480,
        fw_version="1.2.3",
        ip="192.168.1.42",
    )
    obj = json.loads(status.to_json())
    assert obj["kind"] == "pi_png_client"
    assert obj["panel_w"] == 800
    assert obj["panel_h"] == 480
    assert obj["fw_version"] == "1.2.3"
    assert obj["ip"] == "192.168.1.42"


def test_status_kind_defaults_to_the_unified_pi_client() -> None:
    obj = json.loads(Status(panel="x").to_json())
    assert obj["kind"] == "pi_client"


def test_gamuts_are_sent_only_when_known() -> None:
    assert "gamuts" not in json.loads(Status(panel="x").to_json())
    obj = json.loads(Status(panel="x", gamuts=["spectra_6"]).to_json())
    assert obj["gamuts"] == ["spectra_6"]


# --- telemetry fields added for firmware parity --------------------------------


def test_optional_telemetry_omitted_when_unset() -> None:
    """Omitted, not zeroed: the server must be able to tell "not reported"
    from "reported as none"."""
    payload = Status(panel="inky_impression_13_3").payload()
    for key in ("rssi", "tz", "next_sleep_s", "button", "button_event_id"):
        assert key not in payload


def test_telemetry_included_when_set() -> None:
    status = Status(panel="inky_impression_13_3")
    status.rssi = -56
    status.tz = "Australia/Melbourne"
    status.next_sleep_s = 300
    payload = status.payload()
    assert payload["rssi"] == -56
    assert payload["tz"] == "Australia/Melbourne"
    assert payload["next_sleep_s"] == 300


def test_button_needs_both_name_and_event_id() -> None:
    """A half-populated press would be dropped server-side anyway; not
    sending it keeps the body honest."""
    status = Status(panel="inky_impression_13_3")
    status.button = "refresh"
    assert "button" not in status.payload()
    status.button_event_id = 12
    assert status.payload()["button_event_id"] == 12


def test_fw_version_is_plain_semver() -> None:
    """A PEP 440 local segment (+unknown) does not parse as SemVer, so the
    server's update comparison would reject it."""
    assert "+" not in _fw_version()


def test_wifi_rssi_parses_proc_net_wireless(tmp_path: Path, monkeypatch: Any) -> None:
    proc = tmp_path / "wireless"
    proc.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        " wlan0: 0000   54.  -56.  -256        0      0      0\n",
        encoding="utf-8",
    )
    real_open = open

    def fake_open(path: Any, *a: Any, **kw: Any) -> Any:
        if path == "/proc/net/wireless":
            return real_open(proc, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert _wifi_rssi() == -56


def test_wifi_rssi_none_without_wireless_interface(monkeypatch: Any) -> None:
    def fake_open(path: Any, *a: Any, **kw: Any) -> Any:
        raise OSError("no such file")

    monkeypatch.setattr("builtins.open", fake_open)
    assert _wifi_rssi() is None
