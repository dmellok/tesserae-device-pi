"""inky high-level wrapper.

Unlike the bin client (which writes 4-bpp packed bytes straight into inky's
internal buffer), this client hands a PIL image to `inky.set_image()` and
lets the library's palette-projection + dithering code do the quantise.

That means:
  - We pay a PIL roundtrip and a per-frame quantise — slower than the bin path
  - We work with any panel inky supports (pHAT, wHAT, Impression 4/5.7/7.3/13.3)
  - We're not coupled to private inky internals, so the `inky` pin can be a
    range (>=2.0,<3) instead of an exact version

`inky` is imported lazily inside `auto_panel()` so:
  - The pure transform tests don't need inky installed
  - Module import succeeds on dev machines that can't `pip install inky[rpi]`
    (the RPi.GPIO transitive dep is Linux-only)
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Protocol

from PIL import ImageDraw
from PIL.Image import Image
from PIL.Image import new as new_image

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


def paint(panel: Any, img: Image, saturation: float) -> None:
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
    "auto_panel",
    "model_name",
    "paint",
    "panel_resolution",
    "stripe_test_image",
]
