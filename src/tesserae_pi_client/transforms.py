"""Pure PNG → panel-image transform pipeline.

No hardware, no inky, no PIL hardware backends — just PIL image manipulation.
The order matters and is defined by the MQTT contract:

    decode -> ensure RGB -> rotate (CW quarter-turns) -> scale (per mode) -> bg fill

`apply_transforms()` is the single entry point used by mqtt_loop. The lower
helpers are exposed individually so the tests can pin each stage's behaviour.
"""

from __future__ import annotations

import math
from typing import Final

from PIL.Image import Image, Resampling
from PIL.Image import new as new_image

VALID_SCALES: Final[frozenset[str]] = frozenset({"fit", "fill", "stretch", "center"})

BG_COLORS: Final[dict[str, tuple[int, int, int]]] = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 128, 0),
}

DEFAULT_BG: Final[tuple[int, int, int]] = BG_COLORS["white"]


def bg_color(name: str) -> tuple[int, int, int]:
    """Map a bg colour name to its RGB triple. Unknown names fall back to white.

    The fallback is a deliberate choice: the contract says the server picks
    from the panel-palette-safe set, but we'd rather paint *something* on a
    typo than refuse the frame outright.
    """
    return BG_COLORS.get(name, DEFAULT_BG)


def ensure_rgb(img: Image) -> Image:
    """Coerce to mode='RGB'. PNGs can be RGBA / P / LA; inky wants RGB."""
    if img.mode == "RGB":
        return img
    return img.convert("RGB")


def apply_rotate(img: Image, rotate: int) -> Image:
    """Rotate by `rotate` quarter-turns clockwise. expand=True so we don't crop.

    rotate=0 returns the image unchanged (no copy). Anything outside 0..3 is
    a protocol violation — we raise rather than silently mod-4 it.
    """
    if not isinstance(rotate, int) or isinstance(rotate, bool) or not 0 <= rotate <= 3:
        raise ValueError(f"rotate must be int 0..3, got {rotate!r}")
    if rotate == 0:
        return img
    # PIL rotates CCW for positive angles; we want CW.
    return img.rotate(-90 * rotate, expand=True)


def _fit(
    img: Image, panel_w: int, panel_h: int, bg: tuple[int, int, int]
) -> Image:
    img_w, img_h = img.size
    factor = min(panel_w / img_w, panel_h / img_h)
    # floor so the scaled image always fits *inside* the panel; any sub-pixel
    # remainder becomes letterbox, never overflow.
    new_w = max(1, math.floor(img_w * factor))
    new_h = max(1, math.floor(img_h * factor))
    resized = img.resize((new_w, new_h), Resampling.LANCZOS)
    canvas = new_image("RGB", (panel_w, panel_h), bg)
    canvas.paste(resized, ((panel_w - new_w) // 2, (panel_h - new_h) // 2))
    return canvas


def _fill(img: Image, panel_w: int, panel_h: int) -> Image:
    img_w, img_h = img.size
    factor = max(panel_w / img_w, panel_h / img_h)
    # ceil so the scaled image always covers the panel; the centred crop then
    # discards the overflowing axis. No bg colour ever shows through fill.
    new_w = max(panel_w, math.ceil(img_w * factor))
    new_h = max(panel_h, math.ceil(img_h * factor))
    resized = img.resize((new_w, new_h), Resampling.LANCZOS)
    left = (new_w - panel_w) // 2
    top = (new_h - panel_h) // 2
    return resized.crop((left, top, left + panel_w, top + panel_h))


def _stretch(img: Image, panel_w: int, panel_h: int) -> Image:
    return img.resize((panel_w, panel_h), Resampling.LANCZOS)


def _center(
    img: Image, panel_w: int, panel_h: int, bg: tuple[int, int, int]
) -> Image:
    img_w, img_h = img.size
    canvas = new_image("RGB", (panel_w, panel_h), bg)
    # PIL.paste handles negative offsets by clipping — so if the source is
    # larger than the panel on one or both axes, the overflow is cropped and
    # what remains is centred. That's exactly the contract's "center" mode.
    canvas.paste(img, ((panel_w - img_w) // 2, (panel_h - img_h) // 2))
    return canvas


def apply_scale(
    img: Image,
    panel: tuple[int, int],
    scale: str,
    bg: tuple[int, int, int],
) -> Image:
    """Dispatch to the right scale implementation based on `scale` mode."""
    if scale not in VALID_SCALES:
        raise ValueError(
            f"scale must be one of {sorted(VALID_SCALES)}, got {scale!r}"
        )
    panel_w, panel_h = panel
    if panel_w <= 0 or panel_h <= 0:
        raise ValueError(f"panel size must be positive, got {panel}")
    if scale == "fit":
        return _fit(img, panel_w, panel_h, bg)
    if scale == "fill":
        return _fill(img, panel_w, panel_h)
    if scale == "stretch":
        return _stretch(img, panel_w, panel_h)
    return _center(img, panel_w, panel_h, bg)


def apply_transforms(
    img: Image,
    panel: tuple[int, int],
    rotate: int,
    scale: str,
    bg: str,
) -> Image:
    """Full pipeline: ensure RGB -> rotate -> scale -> bg fill."""
    img = ensure_rgb(img)
    img = apply_rotate(img, rotate)
    return apply_scale(img, panel, scale, bg_color(bg))


__all__ = [
    "BG_COLORS",
    "DEFAULT_BG",
    "VALID_SCALES",
    "apply_rotate",
    "apply_scale",
    "apply_transforms",
    "bg_color",
    "ensure_rgb",
]
