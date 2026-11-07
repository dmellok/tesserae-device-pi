"""One client, both frame formats: recognise, prepare and paint."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest
from PIL import Image

from tesserae_pi_client import frame as frames
from tesserae_pi_client.frame import FrameRejected, Hints, PackedFrame, load, packed_size
from tesserae_pi_client.mqtt_loop import is_frame_topic
from tesserae_pi_client.paint import SPECTRA6_RGB, packed_to_image, paint, unpack_to_buf


def _png(w: int, h: int, colour: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def _packed(w: int, h: int, left: int, right: int) -> bytes:
    """Every byte holds (left, right) nibbles: even columns left, odd right."""
    return bytes([(left << 4) | right]) * packed_size(w, h)


# --- recognising -------------------------------------------------------------


def test_a_packed_frame_of_exactly_the_panel_size_is_packed() -> None:
    raw = _packed(8, 4, 2, 5)
    got = load(raw, (8, 4))
    assert got == PackedFrame(raw, 8, 4)


def test_a_png_with_hints_is_fitted_to_the_panel() -> None:
    got = load(_png(4, 2), (8, 8), Hints(rotate=0, scale="fit", bg="white", saturation=0.5))
    assert isinstance(got, Image.Image) and got.size == (8, 8)


def test_a_png_without_hints_must_already_be_panel_sized() -> None:
    assert load(_png(8, 4), (8, 4)).size == (8, 4)
    with pytest.raises(FrameRejected, match="refusing"):
        load(_png(4, 2), (8, 4))


@pytest.mark.parametrize(
    "raw",
    [b"", b"\x00" * (packed_size(8, 4) - 1), b"\x00" * (packed_size(8, 4) + 1), b"GIF89a...."],
)
def test_anything_else_is_refused(raw: bytes) -> None:
    with pytest.raises(FrameRejected):
        load(raw, (8, 4))


# --- painting ----------------------------------------------------------------


class _BufPanel:
    """An Impression driver: show() paints from buf."""

    def __init__(self) -> None:
        self.buf: Any = None
        self.shown = 0
        self.images: list[Any] = []

    def set_image(self, image: Any, saturation: float = 0.5) -> None:
        self.images.append((image, saturation))

    def show(self) -> None:
        self.shown += 1


class _NoBufPanel:
    def __init__(self) -> None:
        self.images: list[Any] = []
        self.shown = 0

    def set_image(self, image: Any, saturation: float = 0.5) -> None:
        self.images.append((image, saturation))

    def show(self) -> None:
        self.shown += 1


def test_unpack_puts_the_high_nibble_on_even_columns() -> None:
    buf = unpack_to_buf(_packed(4, 2, 3, 6), 4, 2)
    assert buf.shape == (2, 4)
    assert buf.tolist() == [[3, 6, 3, 6], [3, 6, 3, 6]]


def test_a_packed_frame_is_painted_straight_into_buf() -> None:
    panel = _BufPanel()
    paint(panel, PackedFrame(_packed(4, 2, 1, 0), 4, 2), 0.5)
    assert panel.shown == 1 and panel.images == []
    assert np.array_equal(panel.buf, np.array([[1, 0, 1, 0], [1, 0, 1, 0]], np.uint8))


def test_without_buf_a_packed_frame_is_painted_via_set_image() -> None:
    panel = _NoBufPanel()
    paint(panel, PackedFrame(_packed(4, 2, 3, 5), 4, 2), 0.5)
    ((img, _sat),) = panel.images
    assert panel.shown == 1 and img.size == (4, 2)
    assert img.getpixel((0, 0)) == SPECTRA6_RGB[3] and img.getpixel((1, 0)) == SPECTRA6_RGB[5]


def test_an_image_goes_through_set_image() -> None:
    panel = _BufPanel()
    img = Image.new("RGB", (4, 2))
    paint(panel, img, 0.3)
    assert panel.images == [(img, 0.3)] and panel.shown == 1 and panel.buf is None


def test_packed_to_image_maps_every_index() -> None:
    img = packed_to_image(PackedFrame(_packed(2, 1, 0, 6), 2, 1))
    assert [img.getpixel((x, 0)) for x in (0, 1)] == [SPECTRA6_RGB[0], SPECTRA6_RGB[6]]


# --- MQTT topics ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "ok"),
    [
        ("tesserae/kitchen/frame/png", True),
        ("tesserae/kitchen/frame/bin", True),
        ("tesserae/kitchen/frame/jpg", False),
        ("tesserae/other/frame/png", False),
        ("tesserae/kitchen/status", False),
    ],
)
def test_frames_arrive_on_either_format_topic(topic: str, ok: bool) -> None:
    assert is_frame_topic(topic, "kitchen") is ok


def test_no_hints_are_neutral() -> None:
    assert frames.NO_HINTS == Hints(0, "fit", "white", 0.5)
