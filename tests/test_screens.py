"""Status screens: drawn at the panel's size, painted only when they change."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tesserae_device_pi import screens
from tesserae_device_pi.screens import ScreenPainter, render
from tesserae_device_pi.transports import rest


@pytest.mark.parametrize("size", [(250, 122), (800, 480), (1600, 1200), (480, 800)])
def test_every_screen_draws_at_the_panel_size(size: tuple[int, int]) -> None:
    for screen in (
        screens.pairing_with_code("http://tesserae.local:8765", "10.0.0.5", "0.9.0"),
        screens.waiting_for_register("http://tesserae.local:8765", "10.0.0.5", "0.9.0", "hall"),
        screens.waiting_for_frame("10.0.0.5", "0.9.0"),
        screens.server_unreachable(
            "http://a-very-long-hostname.example.internal:8765", "", "0.9.0"
        ),
        screens.battery_empty(3),
    ):
        img = render(screen, size)
        assert img.size == size and img.mode == "RGB"
        # Something was drawn: not a blank white panel.
        assert img.getextrema() != ((255, 255), (255, 255), (255, 255))


def _painter(tmp_path: Path) -> tuple[ScreenPainter, list[Any]]:
    painted: list[Any] = []
    return ScreenPainter(painted.append, (400, 300), marker=tmp_path / "painted"), painted


def test_a_screen_is_painted_once_not_on_every_retry(tmp_path: Path) -> None:
    painter, painted = _painter(tmp_path)
    screen = screens.waiting_for_frame("10.0.0.5", "0.9.0")
    assert painter.show(screen) is True
    assert painter.show(screen) is False
    assert len(painted) == 1
    assert painter.show(screens.battery_empty(2)) is True
    assert len(painted) == 2


def test_offline_is_shown_only_until_a_real_frame_has_been_painted(tmp_path: Path) -> None:
    painter, painted = _painter(tmp_path)
    offline = screens.server_unreachable("http://h", "", "0.9.0")
    assert painter.show(offline, only_if_never_painted=True) is True
    painter.note_frame_painted()
    assert (tmp_path / "painted").exists()  # survives a restart
    again = ScreenPainter(painted.append, (400, 300), marker=tmp_path / "painted")
    assert again.show(offline, only_if_never_painted=True) is False
    # Pairing after a factory reset still shows.
    assert again.show(screens.waiting_for_register("http://h", "", "0.9.0", "hall")) is True


def test_after_a_frame_the_same_screen_can_show_again(tmp_path: Path) -> None:
    painter, painted = _painter(tmp_path)
    battery = screens.battery_empty(4)
    painter.show(battery)
    painter.note_frame_painted()
    assert painter.show(battery) is True


def test_a_painter_without_a_panel_does_nothing() -> None:
    assert ScreenPainter().show(screens.battery_empty(1)) is False


def test_a_failing_paint_never_raises(tmp_path: Path) -> None:
    def boom(_img: Any) -> None:
        raise OSError("SPI")

    painter = ScreenPainter(boom, (400, 300), marker=tmp_path / "p")
    assert painter.show(screens.battery_empty(1)) is False


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ({"battery_pct": 4, "power_plugged": False}, True),
        ({"battery_pct": 4, "power_plugged": True}, False),  # charging
        ({"battery_pct": 40, "power_plugged": False}, False),
        ({}, False),  # no UPS
    ],
)
def test_battery_empty_only_on_a_ups_running_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], shown: bool
) -> None:
    painter, painted = _painter(tmp_path)
    monkeypatch.setattr(screens, "SCREENS", painter)
    rest._show_if_battery_empty(body)
    assert bool(painted) is shown


def test_the_unlock_screen_carries_a_qr_code_beside_the_text() -> None:
    url = "http://192.168.1.20:8790/"
    with_qr = render(screens.settings_unlocked(url, "192.168.1.20", "0.9.0"), (800, 480))
    right = with_qr.crop((520, 0, 800, 480))
    assert right.getextrema() != ((255, 255), (255, 255), (255, 255))  # the code is drawn
    # Too small for a scannable code: text only, no crash.
    assert render(screens.settings_unlocked(url, "", "0.9.0"), (212, 104)).size == (212, 104)


@pytest.mark.parametrize("size", [(250, 122), (800, 480), (1600, 1200), (480, 800)])
def test_every_screen_carries_the_tesserae_mark_top_left(size: tuple[int, int]) -> None:
    img = render(screens.waiting_for_frame("10.0.0.5", "0.9.0"), size)
    unit = max(8, min(size) // 20)
    margin, side = unit * 2, unit * 3

    def at(ux: int, uy: int) -> tuple[int, ...]:
        # A point of the mark, in its 256-unit drawing.
        return img.getpixel((margin + ux * side // 256, margin + uy * side // 256))  # type: ignore[return-value]

    assert at(164, 92) == (255, 0, 0)  # the red square, top right
    assert at(92, 164) == (255, 255, 255)  # the paper square, bottom left
    assert at(92, 92) == (0, 0, 0)  # the ink tile around them
    assert at(164, 164) == (0, 0, 0)


def test_a_long_title_shrinks_to_fit_beside_the_mark() -> None:
    img = render(screens.pairing_with_code("http://tesserae.local:8765", "", "0.9.0"), (400, 300))
    # Nothing of the title reaches the right margin (unit 15, margin 30).
    assert img.crop((370, 0, 400, 75)).getextrema() == ((255, 255), (255, 255), (255, 255))
