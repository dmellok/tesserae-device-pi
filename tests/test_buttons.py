"""Front-button pin resolution.

The bin client derives button C's pin from the panel model in its config; this
client has no such setting, so the panel inky.auto() detected decides. Getting
this wrong is quiet: the watcher arms a line nothing is wired to and presses
simply never arrive.
"""

from __future__ import annotations

from tesserae_device_pi.buttons import PIN_C_LARGE, PIN_C_SMALL, default_pin_c
from tesserae_device_pi.config import BUTTON_PIN_C_AUTO, ButtonsConfig


def test_13_3_inch_gets_gpio_25() -> None:
    assert default_pin_c((1600, 1200)) == PIN_C_LARGE == 25


def test_13_3_inch_portrait_is_still_the_large_panel() -> None:
    """inky may report the pair either way round depending on driver."""
    assert default_pin_c((1200, 1600)) == PIN_C_LARGE


def test_smaller_impressions_get_gpio_16() -> None:
    for size in ((640, 400), (600, 448), (800, 480)):
        assert default_pin_c(size) == PIN_C_SMALL == 16


def test_unknown_panel_falls_back_to_the_common_pin() -> None:
    """16 is shared by the 4"/5.7"/7.3"; the 13.3" is the one exception."""
    assert default_pin_c((1234, 567)) == PIN_C_SMALL


def test_resolved_fills_in_the_auto_sentinel() -> None:
    cfg = ButtonsConfig(enabled=True)
    assert cfg.pin_c == BUTTON_PIN_C_AUTO
    assert cfg.resolved((1600, 1200)).pin_c == 25
    assert cfg.resolved((800, 480)).pin_c == 16


def test_resolved_leaves_an_explicit_pin_alone() -> None:
    """An override must win for a board we haven't catalogued."""
    cfg = ButtonsConfig(enabled=True, pin_c=12)
    assert cfg.resolved((1600, 1200)).pin_c == 12


def test_unresolved_pin_c_is_not_armed() -> None:
    """GPIO 0 is ID_SD, reserved for the HAT EEPROM. If something forgot to
    call resolved(), button C drops out rather than watching that line."""
    buttons = ButtonsConfig(enabled=True).pin_buttons()
    assert 0 not in buttons
    assert buttons == {5: ("a", "left"), 6: ("b", "refresh"), 24: ("d", "")}


def test_resolved_config_maps_all_three_default_buttons() -> None:
    buttons = ButtonsConfig(enabled=True).resolved((800, 480)).pin_buttons()
    assert buttons == {5: ("a", "left"), 6: ("b", "refresh"), 16: ("c", "right"), 24: ("d", "")}


def test_duplicate_pins_collapse_after_resolution() -> None:
    """Two watchers on one line is something libgpiod would refuse anyway."""
    cfg = ButtonsConfig(enabled=True, pin_a=16).resolved((800, 480))
    assert len(cfg.pin_buttons()) == 3
