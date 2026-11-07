"""Verification for frame bytes that arrive without a render payload.

This module exists because of the one structural difference between this
client and its `.bin` sibling. That client paints a headerless packed 4-bpp
buffer whose length is *exactly* `panel_w * panel_h / 2`, and nearly every
safety check it makes leans on that fact: a decrypted relay frame or a cached
deck page whose length doesn't match known geometry is refused, and the check
costs one comparison.

A PNG has no fixed length, so that check has no direct analogue — and dropping
it is not an option, because these are precisely the paths where the bytes did
NOT come with a trusted `{url, rotate, scale, bg, saturation}` announcement to
validate them against. The equally-real replacement is structural:

1. **Magic bytes.** Cheap, and it catches the common failure of an HTML error
   page or a truncated download being handed straight to the decoder.
2. **A real decode through PIL.** `load()` is forced, so a file that is a valid
   PNG header wrapped around corrupt IDAT data fails here rather than halfway
   through a panel refresh.
3. **Dimensions against the live panel.** `paint.panel_resolution()` is read
   off the device inky.auto() detected, so unlike the relay's plaintext
   `X-Tesserae-Panel-*` headers it is not something a compromised or buggy
   relay can alter.

The dimension check is deliberately exact, matching the strictness of the bin
client's length check. Frames on these paths are *pre-rendered for this panel*
— home's `pi_png` renderer rotates to the client's landscape-native pixel grid
using the geometry the panel itself self-reported at pairing — so anything else
means the two ends disagree about the hardware, which is worth surfacing rather
than papering over with a resize.

The normal REST/MQTT frame path does NOT come through here: it carries the
server's rotate/scale/bg hints, and `transforms.apply_transforms` is the
authority on turning an arbitrary image into a panel-sized one.
"""
from __future__ import annotations

import io

from PIL import Image as PILImage
from PIL.Image import Image

# The 8-byte PNG signature (RFC 2083 §3.1).
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FrameRejected(ValueError):
    """The bytes are not a PNG this panel should paint."""


def looks_like_png(raw: bytes) -> bool:
    """True when `raw` starts with the PNG signature."""
    return raw[: len(PNG_MAGIC)] == PNG_MAGIC


def decode_frame(raw: bytes, panel_size: tuple[int, int] | None = None) -> Image:
    """Magic-check, decode, and (optionally) size-check a frame.

    Raises FrameRejected — a ValueError subclass, so callers that already
    catch ValueError on the paint path keep working — with a message naming
    the specific rule that failed. Never returns a partially-decoded image.
    """
    if not raw:
        raise FrameRejected("frame is empty")
    if not looks_like_png(raw):
        raise FrameRejected(
            f"frame does not start with the PNG signature "
            f"(first bytes: {raw[:8]!r})"
        )
    try:
        img = PILImage.open(io.BytesIO(raw))
        img.load()
    except OSError as exc:
        # Covers PIL's UnidentifiedImageError and truncated-file errors alike.
        raise FrameRejected(f"frame is not a decodable PNG: {exc}") from exc

    if panel_size is not None:
        expected = (int(panel_size[0]), int(panel_size[1]))
        if expected[0] > 0 and expected[1] > 0 and img.size != expected:
            raise FrameRejected(
                f"frame decodes to {img.size[0]}x{img.size[1]} but this panel "
                f"is {expected[0]}x{expected[1]}; refusing to paint a frame "
                "rendered for different hardware"
            )
    return img


__all__ = ["PNG_MAGIC", "FrameRejected", "decode_frame", "looks_like_png"]
