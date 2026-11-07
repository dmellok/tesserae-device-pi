from __future__ import annotations

import pytest
from PIL.Image import new as new_image

from tesserae_device_pi.transforms import (
    BG_COLORS,
    DEFAULT_BG,
    VALID_SCALES,
    apply_rotate,
    apply_scale,
    apply_transforms,
    bg_color,
    ensure_rgb,
)

# --- bg_color -----------------------------------------------------------------


def test_bg_color_known_names_map_to_expected_rgb() -> None:
    assert bg_color("white") == (255, 255, 255)
    assert bg_color("black") == (0, 0, 0)
    assert bg_color("red") == (255, 0, 0)
    assert bg_color("green") == (0, 255, 0)
    assert bg_color("blue") == (0, 0, 255)
    assert bg_color("yellow") == (255, 255, 0)
    assert bg_color("orange") == (255, 128, 0)


def test_bg_color_unknown_falls_back_to_white() -> None:
    assert bg_color("magenta") == DEFAULT_BG == (255, 255, 255)
    assert bg_color("") == DEFAULT_BG
    assert bg_color("WHITE") == DEFAULT_BG  # case-sensitive, contract is lowercase


def test_bg_colors_dict_covers_contract_set() -> None:
    expected = {"white", "black", "red", "green", "blue", "yellow", "orange"}
    assert set(BG_COLORS.keys()) == expected


# --- ensure_rgb ---------------------------------------------------------------


def test_ensure_rgb_passthrough_when_already_rgb() -> None:
    img = new_image("RGB", (10, 10), (1, 2, 3))
    out = ensure_rgb(img)
    assert out is img  # no copy


def test_ensure_rgb_converts_rgba() -> None:
    img = new_image("RGBA", (10, 10), (10, 20, 30, 128))
    out = ensure_rgb(img)
    assert out.mode == "RGB"
    assert out.size == (10, 10)


# --- apply_rotate -------------------------------------------------------------


def test_rotate_0_is_identity() -> None:
    img = new_image("RGB", (4, 2), (255, 0, 0))
    out = apply_rotate(img, 0)
    assert out is img
    assert out.size == (4, 2)


def test_rotate_1_rotates_cw_and_expands() -> None:
    img = new_image("RGB", (4, 2), (255, 0, 0))
    # Top-left red, bottom-left blue so we can detect orientation.
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((0, 1), (0, 0, 255))
    out = apply_rotate(img, 1)
    # 90° CW: width and height swap.
    assert out.size == (2, 4)
    # The pixel at (0,0) of the source ends up at the top-right of the
    # rotated image after a 90° CW turn.
    # Source (0, 0) -> destination (new_w - 1, 0) = (1, 0).
    assert out.getpixel((1, 0)) == (255, 0, 0)


def test_rotate_2_flips_both_axes() -> None:
    img = new_image("RGB", (3, 2), (0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0))
    out = apply_rotate(img, 2)
    assert out.size == (3, 2)
    # Source (0, 0) -> destination (2, 1) under 180°.
    assert out.getpixel((2, 1)) == (255, 0, 0)


def test_rotate_3_rotates_ccw_relative_to_original() -> None:
    img = new_image("RGB", (3, 2), (0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0))
    out = apply_rotate(img, 3)
    # 270° CW == 90° CCW. width/height swap. (0,0) -> (0, new_h - 1).
    assert out.size == (2, 3)
    assert out.getpixel((0, 2)) == (255, 0, 0)


def test_rotate_rejects_out_of_range() -> None:
    img = new_image("RGB", (4, 2), (0, 0, 0))
    with pytest.raises(ValueError, match="0..3"):
        apply_rotate(img, 4)
    with pytest.raises(ValueError, match="0..3"):
        apply_rotate(img, -1)


def test_rotate_rejects_non_int() -> None:
    img = new_image("RGB", (4, 2), (0, 0, 0))
    with pytest.raises(ValueError):
        apply_rotate(img, 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        apply_rotate(img, True)  # type: ignore[arg-type]


# --- apply_scale: 'fit' -------------------------------------------------------


def test_fit_4x3_into_panel_4x3_no_letterbox() -> None:
    # Spec reference test: 400x300 red into 1600x1200 -> every pixel red.
    img = new_image("RGB", (400, 300), (255, 0, 0))
    out = apply_scale(img, (1600, 1200), "fit", bg_color("white"))
    assert out.size == (1600, 1200)
    # Sample corners + centre — all should be red, no letterbox.
    assert out.getpixel((0, 0)) == (255, 0, 0)
    assert out.getpixel((1599, 0)) == (255, 0, 0)
    assert out.getpixel((0, 1199)) == (255, 0, 0)
    assert out.getpixel((1599, 1199)) == (255, 0, 0)
    assert out.getpixel((800, 600)) == (255, 0, 0)


def test_fit_tall_into_wide_panel_letterboxes_sides() -> None:
    # Spec reference test: 100x400 green into 800x480 with white bg.
    # Scale factor = min(800/100, 480/400) = 1.2 -> resized to (120, 480)
    # Pasted at x=340 (centred horizontally) in 800x480 white canvas.
    img = new_image("RGB", (100, 400), (0, 255, 0))
    out = apply_scale(img, (800, 480), "fit", bg_color("white"))
    assert out.size == (800, 480)
    # Left letterbox should be white.
    assert out.getpixel((0, 240)) == (255, 255, 255)
    assert out.getpixel((339, 240)) == (255, 255, 255)
    # Centre region should be green.
    assert out.getpixel((400, 240)) == (0, 255, 0)
    # Right letterbox should be white.
    assert out.getpixel((460, 240)) == (255, 255, 255)
    assert out.getpixel((799, 240)) == (255, 255, 255)


def test_fit_letterbox_uses_configured_bg_color() -> None:
    img = new_image("RGB", (100, 400), (0, 255, 0))
    out = apply_scale(img, (800, 480), "fit", bg_color("black"))
    assert out.getpixel((0, 240)) == (0, 0, 0)


def test_fit_wide_into_tall_panel_letterboxes_top_bottom() -> None:
    img = new_image("RGB", (400, 100), (255, 0, 0))
    out = apply_scale(img, (480, 800), "fit", bg_color("blue"))
    assert out.size == (480, 800)
    # Top/bottom should be blue, middle should be red.
    assert out.getpixel((240, 0)) == (0, 0, 255)
    assert out.getpixel((240, 799)) == (0, 0, 255)
    assert out.getpixel((240, 400)) == (255, 0, 0)


# --- apply_scale: 'fill' ------------------------------------------------------


def test_fill_crops_long_axis_to_fill_panel() -> None:
    img = new_image("RGB", (100, 400), (0, 255, 0))
    out = apply_scale(img, (800, 480), "fill", bg_color("white"))
    assert out.size == (800, 480)
    # The whole panel should be green — fill never letterboxes.
    assert out.getpixel((0, 0)) == (0, 255, 0)
    assert out.getpixel((799, 479)) == (0, 255, 0)


def test_fill_matched_aspect_is_lossless_resize() -> None:
    img = new_image("RGB", (400, 300), (255, 0, 0))
    out = apply_scale(img, (1600, 1200), "fill", bg_color("white"))
    assert out.size == (1600, 1200)
    assert out.getpixel((0, 0)) == (255, 0, 0)


# --- apply_scale: 'stretch' ---------------------------------------------------


def test_stretch_distorts_to_exact_panel_size() -> None:
    img = new_image("RGB", (100, 100), (255, 0, 0))
    out = apply_scale(img, (800, 480), "stretch", bg_color("white"))
    assert out.size == (800, 480)
    assert out.getpixel((0, 0)) == (255, 0, 0)
    assert out.getpixel((799, 479)) == (255, 0, 0)


# --- apply_scale: 'center' ----------------------------------------------------


def test_center_paste_native_size_letterboxes_around() -> None:
    img = new_image("RGB", (200, 100), (255, 0, 0))
    out = apply_scale(img, (800, 480), "center", bg_color("yellow"))
    assert out.size == (800, 480)
    # Centred at ((800-200)/2, (480-100)/2) = (300, 190).
    # Inside the red rectangle.
    assert out.getpixel((400, 240)) == (255, 0, 0)
    assert out.getpixel((300, 190)) == (255, 0, 0)
    assert out.getpixel((499, 289)) == (255, 0, 0)
    # Outside — yellow bg.
    assert out.getpixel((0, 0)) == (255, 255, 0)
    assert out.getpixel((799, 479)) == (255, 255, 0)


def test_center_crops_when_image_larger_than_panel() -> None:
    # 1000x100 red on 800x480 -> overflows on x, fits on y.
    img = new_image("RGB", (1000, 100), (255, 0, 0))
    out = apply_scale(img, (800, 480), "center", bg_color("white"))
    assert out.size == (800, 480)
    # Centred horizontally: source columns [100..900) end up at panel [0..800).
    # Whole row 240 should be red (within the pasted band).
    # Centred vertically: pasted at y = (480 - 100) / 2 = 190.
    assert out.getpixel((400, 200)) == (255, 0, 0)
    # Above/below the band should be white.
    assert out.getpixel((400, 0)) == (255, 255, 255)
    assert out.getpixel((400, 479)) == (255, 255, 255)


# --- apply_scale errors -------------------------------------------------------


def test_unknown_scale_mode_rejected() -> None:
    img = new_image("RGB", (10, 10), (0, 0, 0))
    with pytest.raises(ValueError, match="scale must be one of"):
        apply_scale(img, (100, 100), "nope", bg_color("white"))


def test_zero_panel_size_rejected() -> None:
    img = new_image("RGB", (10, 10), (0, 0, 0))
    with pytest.raises(ValueError, match="positive"):
        apply_scale(img, (0, 100), "fit", bg_color("white"))


def test_valid_scales_contract_set() -> None:
    assert VALID_SCALES == frozenset({"fit", "fill", "stretch", "center"})


# --- apply_transforms (full pipeline) ----------------------------------------


def test_full_pipeline_rotate_then_scale() -> None:
    # 100x200 red -> rotate 1 (CW) -> 200x100 red -> fit into 800x480.
    # After rotate, aspect is 2:1. Panel is 800:480 ≈ 1.67:1.
    # min(800/200, 480/100) = min(4.0, 4.8) = 4.0 -> resized to (800, 400).
    # Top/bottom letterbox of 40px each in white.
    img = new_image("RGB", (100, 200), (255, 0, 0))
    out = apply_transforms(img, (800, 480), rotate=1, scale="fit", bg="white")
    assert out.size == (800, 480)
    # Center is red.
    assert out.getpixel((400, 240)) == (255, 0, 0)
    # Top/bottom is white (40px letterbox).
    assert out.getpixel((400, 0)) == (255, 255, 255)
    assert out.getpixel((400, 479)) == (255, 255, 255)


def test_full_pipeline_unknown_bg_falls_back_to_white() -> None:
    img = new_image("RGB", (100, 400), (0, 255, 0))
    out = apply_transforms(img, (800, 480), rotate=0, scale="fit", bg="puce")
    # Letterbox should be white per the documented fallback.
    assert out.getpixel((0, 240)) == (255, 255, 255)


def test_full_pipeline_converts_rgba_input() -> None:
    img = new_image("RGBA", (100, 100), (255, 0, 0, 200))
    out = apply_transforms(img, (200, 200), rotate=0, scale="fit", bg="white")
    assert out.mode == "RGB"
    assert out.size == (200, 200)
