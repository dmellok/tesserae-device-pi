"""One frame, whichever format the server sent.

A Pi paired as the unified ``pi_client`` kind gets a packed 4-bpp ``.bin``
when its panel is a Spectra 6 Impression (the server's ``pi_bin`` renderer
quantises and packs it) and a PNG otherwise (``pi_png``: the Pi quantises it
with ``inky``). An install still paired as the older ``pi_bin_client`` or
``pi_png_client`` kind keeps getting the format it always did. So the client
never assumes a format: every frame goes through :func:`load`, which looks
at the bytes themselves.

* PNG: starts with the PNG signature. With the server's rotate / scale / bg
  hints (a frame announced by the server) it is run through
  :mod:`transforms`; without them (a cached deck or album frame, a relay
  frame) it must already be exactly the panel's size, so a frame rendered
  for other hardware, or altered on the way, is refused, not stretched.
* Packed ``.bin``: exactly ``width * height / 2`` bytes for this panel, two
  pixels per byte (high nibble = even column). Already in the panel's own
  palette and orientation, so it is painted as is (:class:`PackedFrame`).

Anything else is refused with :class:`FrameRejected` rather than painted.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL.Image import Image

from .png_frame import FrameRejected, decode_frame, looks_like_png
from .transforms import apply_transforms


@dataclass(frozen=True)
class PackedFrame:
    """A panel-native 4-bpp buffer: one palette index per nibble."""

    data: bytes
    width: int
    height: int


#: What the paint function accepts: a quantise-on-the-Pi image or a packed buffer.
Paintable = Image | PackedFrame


@dataclass(frozen=True)
class Hints:
    """The server's PNG transform hints. Neutral when a frame carries none
    (a ``.bin`` descriptor, a cached deck or album frame)."""

    rotate: int = 0
    scale: str = "fit"
    bg: str = "white"
    saturation: float = 0.5


#: The hints a frame announcement implies when it names none.
NO_HINTS = Hints()


def packed_size(width: int, height: int) -> int:
    """Bytes in a packed 4-bpp frame for a ``width`` x ``height`` panel."""
    return width * height // 2


def load(raw: bytes, panel_size: tuple[int, int], hints: Hints | None = None) -> Paintable:
    """Recognise ``raw`` and return it ready to paint on a ``panel_size`` panel.

    ``hints`` are the server's PNG transforms; ``None`` means the frame must
    already fit the panel exactly (see the module docstring). Raises
    :class:`FrameRejected` (a ``ValueError``) when the bytes are neither a
    PNG nor a packed frame of exactly this panel's size.
    """
    if not raw:
        raise FrameRejected("frame is empty")
    width, height = int(panel_size[0]), int(panel_size[1])
    if looks_like_png(raw):
        # The PNG is drawn at the dashboard's size and the hints fit it to the
        # panel, so its size is checked after the transforms, not before.
        if hints is None:
            return decode_frame(raw, (width, height))
        img = decode_frame(raw)
        if width <= 0 or height <= 0:
            return img
        return apply_transforms(
            img, (width, height), rotate=hints.rotate, scale=hints.scale, bg=hints.bg
        )
    if width > 0 and height > 0 and len(raw) == packed_size(width, height):
        return PackedFrame(bytes(raw), width, height)
    raise FrameRejected(
        f"frame is neither a PNG nor a packed buffer for this {width}x{height} panel "
        f"({len(raw)} bytes; a packed frame would be {packed_size(width, height)}; "
        f"first bytes {raw[:8]!r})"
    )


__all__ = ["NO_HINTS", "FrameRejected", "Hints", "PackedFrame", "Paintable", "load", "packed_size"]
