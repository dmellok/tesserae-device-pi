"""Tesserae Raspberry Pi client."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tesserae-device-pi").split("+", 1)[0]
except PackageNotFoundError:  # running from a tree that was never installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
