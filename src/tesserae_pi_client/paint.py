"""inky wrapper: paints either kind of frame (see :mod:`frame`).

* An image (a PNG frame) goes through `inky.set_image()`, which does the
  palette projection and dithering on the Pi. Slower, but works on every
  panel inky supports (pHAT, wHAT, Impression 4/5.7/7.3/13.3).
* A packed 4-bpp buffer (a ``.bin`` frame, already quantised and packed by
  the server for a Spectra 6 Impression) skips that: it is unpacked into the
  (rows, cols) array of palette indices `inky.show()` paints from and
  assigned to `panel.buf`. `show()` still runs, so each driver keeps its own
  orientation transform and controller split (the 13.3" driver rotates the
  buffer and splits it across its two controllers; the smaller ones don't),
  which is why there is no lower-level "push bytes" call here.

`panel.buf` is not a documented inky API. When a driver has none, a packed
frame is expanded to an RGB image in the Spectra 6 palette and painted the
slow way instead, so an inky release that renames it costs speed, not a
blank panel. That is what lets `inky` be a range (>=2.0,<3), not a pin.

`inky` is imported lazily inside `auto_panel()` so:
  - The pure transform tests don't need inky installed
  - Module import succeeds on dev machines that can't `pip install inky[rpi]`
    (the RPi.GPIO transitive dep is Linux-only)
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol

import numpy as np
from PIL import ImageDraw
from PIL.Image import Image, fromarray
from PIL.Image import new as new_image

from .frame import PackedFrame, Paintable

log = logging.getLogger(__name__)

# Canonical inky-Impression 7-colour palette. On 2/3-colour panels (pHAT/wHAT)
# inky.set_image() projects these down to whatever the device supports — the
# stripe pattern still validates the SPI path even if some bands collapse.
STRIPE_COLORS: list[tuple[int, int, int]] = [
    (0, 0, 0),  # black
    (255, 255, 255),  # white
    (255, 255, 0),  # yellow
    (255, 128, 0),  # orange
    (255, 0, 0),  # red
    (0, 255, 0),  # green
    (0, 0, 255),  # blue
]


class Panel(Protocol):
    def set_image(self, image: Any, saturation: float = 0.5) -> None: ...

    def show(self) -> None: ...


def auto_panel() -> Any:
    """Detect the attached panel via HAT EEPROM and return an inky instance.

    Raises whatever inky raises if detection fails — main.py turns that into
    a clear error message pointing at SPI / EEPROM troubleshooting.
    """
    from inky.auto import auto

    return auto()


def panel_resolution(panel: Any) -> tuple[int, int]:
    """Pull (width, height) off the inky panel object.

    Different inky driver classes expose this in different ways; try the
    common ones in order. Raises RuntimeError if none match — that's an
    inky-version-skew problem worth surfacing loudly.
    """
    resolution = getattr(panel, "resolution", None)
    if isinstance(resolution, tuple) and len(resolution) == 2:
        w, h = resolution
        if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
            return (w, h)
    w = getattr(panel, "width", None)
    h = getattr(panel, "height", None)
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return (w, h)
    raise RuntimeError(
        "could not determine panel resolution from inky panel object; "
        "your inky version may expose dimensions differently — please file an issue"
    )


# Maps known panel resolutions to a stable, kebab-ish identifier suitable for
# putting in the status MQTT payload. Falls back to a generic "inky_WxH" form
# for anything inky adds in the future that we haven't catalogued here.
_KNOWN_PANELS: dict[tuple[int, int], str] = {
    (212, 104): "inky_phat_212_104",
    (250, 122): "inky_phat_250_122",
    (400, 300): "inky_what_400_300",
    (640, 400): "inky_impression_4",
    (600, 448): "inky_impression_5_7",
    (800, 480): "inky_impression_7_3",
    (1600, 1200): "inky_impression_13_3",
}


def model_name(panel: Any) -> str:
    """Best-effort identifier for the panel, derived from its resolution."""
    w, h = panel_resolution(panel)
    if (w, h) in _KNOWN_PANELS:
        return _KNOWN_PANELS[(w, h)]
    if (h, w) in _KNOWN_PANELS:
        return _KNOWN_PANELS[(h, w)]
    return f"inky_{w}x{h}"


# Spectra 6 palette by nibble value, for painting a packed frame on a driver
# with no `buf` to write. Index 4 is unused by the panel; white stands in.
SPECTRA6_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),  # 0 black
    (255, 255, 255),  # 1 white
    (255, 255, 0),  # 2 yellow
    (255, 0, 0),  # 3 red
    (255, 255, 255),  # 4 (unused)
    (0, 0, 255),  # 5 blue
    (0, 255, 0),  # 6 green
]


def unpack_to_buf(packed: bytes, width: int, height: int) -> np.ndarray:
    """A packed 4-bpp buffer as the (rows, cols) uint8 array inky paints from.

    High nibble = even column (x = 0, 2, 4 ...), low nibble = odd column; one
    byte per pixel out, holding the palette index.
    """
    flat = np.frombuffer(packed, dtype=np.uint8)
    if width % 2 or flat.size != width * height // 2:
        raise ValueError(f"packed length {flat.size} does not match {width}x{height}")
    buf = np.empty((height, width), dtype=np.uint8)
    buf[:, 0::2] = (flat >> 4).reshape(height, width // 2)
    buf[:, 1::2] = (flat & 0x0F).reshape(height, width // 2)
    return buf


def packed_to_image(frame: PackedFrame) -> Image:
    """A packed frame as an RGB image in the Spectra 6 palette."""
    indices = unpack_to_buf(frame.data, frame.width, frame.height)
    palette = np.array(SPECTRA6_RGB + [(255, 255, 255)] * (16 - len(SPECTRA6_RGB)), np.uint8)
    return fromarray(palette[indices], "RGB")


def paint(panel: Any, frame: Paintable, saturation: float) -> None:
    """Paint one frame: a packed buffer the fast way when the driver allows
    it, otherwise through inky's quantise (see the module docstring)."""
    if isinstance(frame, PackedFrame):
        if hasattr(panel, "buf"):
            panel.buf = unpack_to_buf(frame.data, frame.width, frame.height)
            panel.show()
            return
        log.warning("this inky driver has no buf; painting the packed frame via set_image")
        frame = packed_to_image(frame)
    paint_image(panel, frame, saturation)


def paint_image(panel: Any, img: Image, saturation: float) -> None:
    """Push a fully-transformed RGB image through inky's quantise + SPI path.

    The image must already be panel-sized and in RGB mode. Transform pipeline
    (in transforms.py) is responsible for both — we do not second-guess here.

    Only the 7-colour Impression classes take ``saturation``; the wHAT and
    pHAT drivers' ``set_image(image)`` raise ``TypeError`` on the keyword, so
    they get the image alone.
    """
    if _takes_saturation(panel):
        panel.set_image(img, saturation=saturation)
    else:
        panel.set_image(img)
    panel.show()


def _takes_saturation(panel: Any) -> bool:
    """Whether ``panel.set_image`` accepts a ``saturation`` keyword."""
    try:
        params = inspect.signature(panel.set_image).parameters
    except (TypeError, ValueError):
        return True  # no signature to read (a C extension): try the keyword
    return "saturation" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def stripe_test_image(width: int, height: int) -> Image:
    """Vertical stripes in the canonical inky palette colours.

    Used by --paint-test to validate the SPI path + panel orientation without
    needing an MQTT broker or a Tesserae server.
    """
    img = new_image("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    n = len(STRIPE_COLORS)
    stripe_w = max(1, width // n)
    for i, color in enumerate(STRIPE_COLORS):
        x0 = i * stripe_w
        x1 = (i + 1) * stripe_w if i < n - 1 else width
        draw.rectangle([(x0, 0), (x1 - 1, height - 1)], fill=color)
    return img


__all__ = [
    "STRIPE_COLORS",
    "Panel",
    "SPECTRA6_RGB",
    "auto_panel",
    "model_name",
    "packed_to_image",
    "paint",
    "paint_image",
    "unpack_to_buf",
    "panel_resolution",
    "stripe_test_image",
]
