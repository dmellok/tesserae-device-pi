from __future__ import annotations

from typing import Any

import pytest
from PIL.Image import Image
from PIL.Image import new as new_image

from tesserae_pi_client.paint import (
    STRIPE_COLORS,
    model_name,
    paint,
    panel_resolution,
    stripe_test_image,
)


class FakePanel:
    """Stand-in for the real inky panel object.

    Records set_image + show calls; mimics the resolution-exposing attrs
    so panel_resolution() / model_name() can probe it the way they would
    a real inky instance.
    """

    def __init__(
        self,
        resolution: tuple[int, int] | None = (800, 480),
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if resolution is not None:
            self.resolution = resolution
        if width is not None:
            self.width = width
        if height is not None:
            self.height = height
        self.set_image_calls: list[tuple[Any, float]] = []
        self.show_calls = 0

    def set_image(self, image: Any, saturation: float = 0.5) -> None:
        self.set_image_calls.append((image, saturation))

    def show(self) -> None:
        self.show_calls += 1


# --- paint() ------------------------------------------------------------------


class MonoPanel(FakePanel):
    """An InkyWHAT / InkyPHAT: ``set_image`` takes the image and nothing else."""

    def set_image(self, image: Any) -> None:  # type: ignore[override]
        self.set_image_calls.append((image, -1.0))


def test_paint_leaves_saturation_out_for_panels_that_do_not_take_it() -> None:
    panel = MonoPanel(resolution=(400, 300))
    img = new_image("RGB", (10, 10), (0, 0, 0))
    paint(panel, img, saturation=0.75)
    assert panel.set_image_calls == [(img, -1.0)]
    assert panel.show_calls == 1


def test_paint_passes_image_and_saturation_to_set_image() -> None:
    panel = FakePanel()
    img = new_image("RGB", (10, 10), (255, 0, 0))
    paint(panel, img, saturation=0.75)
    assert len(panel.set_image_calls) == 1
    sent_img, sent_sat = panel.set_image_calls[0]
    assert sent_img is img
    assert sent_sat == 0.75


def test_paint_calls_show_after_set_image() -> None:
    panel = FakePanel()
    img = new_image("RGB", (10, 10), (255, 0, 0))
    paint(panel, img, saturation=0.5)
    assert panel.show_calls == 1


# --- panel_resolution ---------------------------------------------------------


def test_panel_resolution_from_resolution_attr() -> None:
    panel = FakePanel(resolution=(800, 480))
    assert panel_resolution(panel) == (800, 480)


def test_panel_resolution_falls_back_to_width_height_attrs() -> None:
    panel = FakePanel(resolution=None, width=1600, height=1200)
    assert panel_resolution(panel) == (1600, 1200)


def test_panel_resolution_raises_when_nothing_exposed() -> None:
    panel = FakePanel(resolution=None)
    with pytest.raises(RuntimeError, match="resolution"):
        panel_resolution(panel)


# --- model_name ---------------------------------------------------------------


@pytest.mark.parametrize(
    "size,name",
    [
        ((1600, 1200), "inky_impression_13_3"),
        ((800, 480), "inky_impression_7_3"),
        ((600, 448), "inky_impression_5_7"),
        ((640, 400), "inky_impression_4"),
        ((400, 300), "inky_what_400_300"),
        ((250, 122), "inky_phat_250_122"),
        ((212, 104), "inky_phat_212_104"),
    ],
)
def test_model_name_known_resolutions(size: tuple[int, int], name: str) -> None:
    panel = FakePanel(resolution=size)
    assert model_name(panel) == name


def test_model_name_unknown_resolution_falls_back_to_generic() -> None:
    panel = FakePanel(resolution=(123, 456))
    assert model_name(panel) == "inky_123x456"


def test_model_name_rotated_resolution_still_matches() -> None:
    # Some inky drivers report the rotated dims (height, width). The lookup
    # should match either orientation against the known panels.
    panel = FakePanel(resolution=(1200, 1600))
    assert model_name(panel) == "inky_impression_13_3"


# --- stripe_test_image --------------------------------------------------------


def test_stripe_test_image_returns_panel_sized_rgb() -> None:
    img: Image = stripe_test_image(800, 480)
    assert img.size == (800, 480)
    assert img.mode == "RGB"


def test_stripe_test_image_paints_all_stripe_colors() -> None:
    width = 800
    height = 100
    img = stripe_test_image(width, height)
    n = len(STRIPE_COLORS)
    stripe_w = width // n
    # Sample a column near the centre of each stripe and confirm the colour.
    for i, expected in enumerate(STRIPE_COLORS):
        x = i * stripe_w + stripe_w // 2
        # The final stripe extends to the right edge, so this is always safe.
        assert img.getpixel((x, height // 2)) == expected, (
            f"stripe {i} at x={x} expected {expected}"
        )


def test_stripe_test_image_handles_narrow_panel() -> None:
    # 7 colours on a 10px-wide panel — each stripe should be at least 1px.
    img = stripe_test_image(10, 4)
    assert img.size == (10, 4)
