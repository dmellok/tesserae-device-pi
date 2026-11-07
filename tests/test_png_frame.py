"""Structural verification of frame bytes that arrive without a render payload.

This is the load-bearing divergence from the .bin sibling. That client refuses
any relay/cache frame whose length isn't exactly `panel_w * panel_h / 2`; a PNG
has no fixed length, so these three checks — signature, real decode, dimensions
against the live panel — are what stands in for it. Weakening them would leave
the relay and cache paths trusting bytes nothing has validated.
"""

from __future__ import annotations

import io

import pytest
from PIL.Image import new as new_image

from tesserae_pi_client.png_frame import (
    PNG_MAGIC,
    FrameRejected,
    decode_frame,
    looks_like_png,
)

PANEL = (600, 448)


def _png(width: int = PANEL[0], height: int = PANEL[1]) -> bytes:
    buf = io.BytesIO()
    new_image("RGB", (width, height), (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def test_a_panel_sized_png_decodes() -> None:
    img = decode_frame(_png(), PANEL)
    assert img.size == PANEL


def test_magic_is_the_rfc_signature() -> None:
    assert PNG_MAGIC == b"\x89PNG\r\n\x1a\n"
    assert looks_like_png(_png())
    assert not looks_like_png(b"<html>502 Bad Gateway</html>")


def test_an_html_error_page_is_rejected_before_the_decoder() -> None:
    """The common failure: a proxy's error page fetched in place of a frame."""
    with pytest.raises(FrameRejected, match="PNG signature"):
        decode_frame(b"<html>502 Bad Gateway</html>", PANEL)


def test_empty_bytes_are_rejected() -> None:
    with pytest.raises(FrameRejected, match="empty"):
        decode_frame(b"", PANEL)


def test_valid_header_with_corrupt_body_is_rejected() -> None:
    """A truncated download keeps the signature but cannot be decoded — the
    forced load() is what catches it here rather than mid-refresh."""
    truncated = _png()[: len(PNG_MAGIC) + 40]
    with pytest.raises(FrameRejected, match="not a decodable PNG"):
        decode_frame(truncated, PANEL)


def test_wrong_dimensions_are_refused() -> None:
    """The PNG analogue of the bin client's length check. These frames are
    pre-rendered for this panel, so a different size means the two ends
    disagree about the hardware."""
    with pytest.raises(FrameRejected, match="refusing to paint"):
        decode_frame(_png(640, 400), PANEL)


def test_a_transposed_frame_is_also_refused() -> None:
    """Same pixel count, wrong orientation — painting it would be garbage."""
    with pytest.raises(FrameRejected, match="refusing to paint"):
        decode_frame(_png(PANEL[1], PANEL[0]), PANEL)


def test_dimension_check_is_skipped_when_no_panel_is_known() -> None:
    assert decode_frame(_png(10, 10), None).size == (10, 10)


def test_unknown_panel_geometry_does_not_reject() -> None:
    """A zero-sized panel means detection hasn't populated Status yet; that
    is a reason to skip the comparison, not to refuse every frame."""
    assert decode_frame(_png(10, 10), (0, 0)).size == (10, 10)


def test_frame_rejected_is_a_value_error() -> None:
    """Paint paths already catch ValueError; the subclass must not slip past."""
    assert issubclass(FrameRejected, ValueError)
