"""Mooxr: video in, tagged MP3 out."""

from . import tools
from .core import (
    DEFAULT_ALBUM,
    DEFAULT_OUT_DIR,
    Moox,
    MooxCancelled,
    MooxError,
    guess_tags,
    is_link,
    moox,
    probe,
    safe_filename,
)

__all__ = [
    "DEFAULT_ALBUM",
    "DEFAULT_OUT_DIR",
    "Moox",
    "MooxCancelled",
    "MooxError",
    "guess_tags",
    "is_link",
    "moox",
    "probe",
    "safe_filename",
    "tools",
]
