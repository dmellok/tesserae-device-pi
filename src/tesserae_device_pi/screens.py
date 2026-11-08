"""On-panel status screens: what the glass says when there is no dashboard.

The firmware draws a handful of these (splash.c): how to pair, "almost
done", "connected, waiting for your first frame", "can't reach the server",
"battery empty". This is the Pi's set, drawn with Pillow at the panel's size
and painted like any frame.

E-ink holds its picture with the power off and a full refresh takes
seconds (tens of seconds on the 13.3"), so the rules matter as much as the
drawing:

* A screen is painted only when it changes, never on every retry.
* "Can't reach the server" is shown only on a Pi that has never painted a
  real frame. Otherwise the last dashboard stays up, which says more than
  an error would, and the outage is in the log and on the server.
* Pairing screens are always shown when there is no pairing (a fresh
  install, or after a factory reset), so the panel says what to do next.
* Once a real frame is painted, the screens stay out of the way.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_INK = (0, 0, 0)
_PAPER = (255, 255, 255)
_ACCENT = (255, 0, 0)  # red: on every colour Inky, and a dither on the rest


@dataclass(frozen=True)
class Screen:
    """One status screen: a name (to tell repaints apart) and its text."""

    name: str
    title: str
    lines: tuple[str, ...] = ()
    footer: str = ""
    #: A URL drawn as a QR code on the right, for a phone to open.
    qr: str = ""


def pairing_with_code(server_url: str, ip: str, version: str) -> Screen:
    return Screen(
        "pairing_code",
        "Pairing with Tesserae",
        (
            "Presenting the pairing code to",
            server_url,
            "",
            "If it was rejected, make a new code in",
            "Settings > Devices and run:",
            "tesserae-device-pi --pair CODE",
        ),
        _footer(ip, version),
    )


def waiting_for_register(
    server_url: str, ip: str, version: str, device_id: str, settings_url: str = ""
) -> Screen:
    lines: tuple[str, ...] = (
        f"Open {server_url}",
        "Settings > Devices, and click",
        f"Register on {device_id}.",
    )
    if settings_url:
        lines += ("", "Wrong server? Scan to change it:", settings_url)
    return Screen("register", "Almost done", lines, _footer(ip, version), qr=settings_url)


def settings_unlocked(settings_url: str, ip: str, version: str) -> Screen:
    return Screen(
        "unlocked",
        "Settings unlocked",
        ("For 15 minutes, open:", settings_url, "", "or scan the code."),
        _footer(ip, version),
        qr=settings_url,
    )


def waiting_for_frame(ip: str, version: str) -> Screen:
    return Screen(
        "connected",
        "Connected",
        (
            "Waiting for the first dashboard.",
            "Send one from Tesserae, or add",
            "this display to a lineup.",
        ),
        _footer(ip, version),
    )


def server_unreachable(server_url: str, ip: str, version: str) -> Screen:
    return Screen(
        "offline",
        "Can't reach the server",
        (server_url, "", "Retrying. Check the server is up", "and this Pi is on the same network."),
        _footer(ip, version),
    )


def battery_empty(pct: int) -> Screen:
    return Screen(
        "battery",
        "Battery empty",
        (f"{pct}% left. Plug in the power", "and the dashboard comes back."),
    )


def _footer(ip: str, version: str) -> str:
    parts = [p for p in (f"IP {ip}" if ip else "", f"tesserae-device-pi {version}") if p]
    return "   ".join(parts)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1: fixed-size bitmap font
        return ImageFont.load_default()


def render(screen: Screen, size: tuple[int, int]) -> Image.Image:
    """Draw ``screen`` on a white panel-sized image."""
    width, height = size
    img = Image.new("RGB", (width, height), _PAPER)
    draw = ImageDraw.Draw(img)
    unit = max(8, min(width, height) // 20)
    margin = unit * 2
    body_font, small_font = _font(unit), _font(max(8, unit * 2 // 3))

    text_w = width - 2 * margin - unit
    if screen.qr:
        side = _draw_qr(img, screen.qr, width, height, margin)
        if side:
            text_w -= side + unit

    y = margin
    logo = unit * 3
    _draw_logo(draw, margin, y, logo)
    title_w = width - margin * 2 - logo - unit
    for pt in range(unit * 2, unit - 1, -max(1, unit // 8)):
        title_font = _font(pt)
        if draw.textlength(screen.title, font=title_font) <= title_w:
            break
    draw.text(
        (margin + logo + unit, y + logo // 2),
        _fit(draw, screen.title, title_font, title_w),
        font=title_font,
        fill=_INK,
        anchor="lm",
    )
    y += logo + unit
    for line in screen.lines:
        if line:
            draw.text(
                (margin + unit, y),
                _fit(draw, line, body_font, text_w),
                font=body_font,
                fill=_INK,
            )
        y += int(unit * 1.4)
    if screen.footer:
        draw.text((margin, height - margin), screen.footer, font=small_font, fill=_INK, anchor="ls")
    return img


def _draw_logo(draw: ImageDraw.ImageDraw, x: int, y: int, side: int) -> None:
    """The Tesserae mark, ``side`` px square with its top-left at (x, y).

    The drawing in tesserae.ink's Logo.astro (a 256-unit tile: ink, red square
    top right, paper square bottom left), in the panel's own black, white and
    red rather than the brand tones, so it lands on the glass undithered, as
    the firmware's splash does.
    """

    def at(units: float) -> int:
        return round(units * side / 256)

    draw.rounded_rectangle([(x, y), (x + side - 1, y + side - 1)], radius=at(72), fill=_INK)
    corner = max(1, at(27))
    # Red top right: x 128-201, y 55-128, its outer (top-right) corner rounded.
    draw.rounded_rectangle(
        [(x + at(128), y + at(55)), (x + at(201) - 1, y + at(128) - 1)],
        radius=corner,
        fill=_ACCENT,
        corners=(False, True, False, False),
    )
    # Paper bottom left: x 55-128, y 128-201, its outer (bottom-left) corner rounded.
    draw.rounded_rectangle(
        [(x + at(55), y + at(128)), (x + at(128) - 1, y + at(201) - 1)],
        radius=corner,
        fill=_PAPER,
        corners=(False, False, False, True),
    )


def _draw_qr(img: Image.Image, data: str, width: int, height: int, margin: int) -> int:
    """A QR code for ``data`` at the right edge; returns its side, or 0 when
    the panel is too small to hold a scannable one (or segno is missing)."""
    try:
        import segno
    except ImportError:
        return 0
    qr = segno.make(data, error="m", micro=False)
    modules = int(qr.symbol_size(scale=1, border=0)[0])
    scale = min((height - 2 * margin) // modules, (width // 3) // modules)
    if scale < 2:
        return 0
    side = modules * scale
    x0, y0 = width - margin - side, (height - side) // 2
    draw = ImageDraw.Draw(img)
    for row_index, row in enumerate(qr.matrix):
        for col_index, dark in enumerate(row):
            if dark:
                x, y = x0 + col_index * scale, y0 + row_index * scale
                draw.rectangle([(x, y), (x + scale - 1, y + scale - 1)], fill=_INK)
    return side


def _fit(draw: ImageDraw.ImageDraw, text: str, font: object, max_w: int) -> str:
    """``text``, shortened with an ellipsis until it fits ``max_w`` pixels."""
    if draw.textlength(text, font=font) <= max_w:  # type: ignore[arg-type]
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:  # type: ignore[arg-type]
        text = text[:-1]
    return text + "…"


class ScreenPainter:
    """Paints status screens under the rules in the module docstring."""

    def __init__(
        self,
        paint_image: Callable[[Image.Image], None] | None = None,
        size: tuple[int, int] = (0, 0),
        marker: Path | None = None,
    ) -> None:
        self._paint = paint_image
        self._size = size
        self._marker = marker
        self._shown: str | None = None
        self._lock = threading.Lock()

    @property
    def ever_painted_a_frame(self) -> bool:
        return self._marker is not None and self._marker.exists()

    def note_frame_painted(self) -> None:
        """A real frame reached the glass: screens step aside from now on."""
        with self._lock:
            self._shown = None
        if self._marker is not None and not self._marker.exists():
            try:
                self._marker.parent.mkdir(parents=True, exist_ok=True)
                self._marker.touch()
            except OSError as exc:
                log.debug("could not write %s: %s", self._marker, exc)

    def show(self, screen: Screen, *, only_if_never_painted: bool = False) -> bool:
        """Paint ``screen`` unless it is already up. Returns True when painted."""
        if self._paint is None or self._size[0] <= 0:
            return False
        if only_if_never_painted and self.ever_painted_a_frame:
            return False
        with self._lock:
            if self._shown == screen.name:
                return False
            self._shown = screen.name
        try:
            self._paint(render(screen, self._size))
        except Exception as exc:  # noqa: BLE001 - a status screen must never stop the client
            log.warning("could not paint the %s screen: %s", screen.name, exc)
            return False
        log.info("panel shows the %s screen", screen.name)
        return True


#: The process's painter, installed by main once the panel is open; a no-op
#: until then (and in tests), so transports can call it unconditionally.
SCREENS = ScreenPainter()


__all__ = [
    "SCREENS",
    "Screen",
    "ScreenPainter",
    "battery_empty",
    "pairing_with_code",
    "render",
    "server_unreachable",
    "settings_unlocked",
    "waiting_for_frame",
    "waiting_for_register",
]
