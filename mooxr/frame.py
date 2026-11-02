"""The Windows frame around the Mooxr window, in the theme's colours.

Windows 11 draws an accent-coloured border (and title bar) around the
active window. This swaps them for a thin border in the theme's border
colour and a title bar the same colour as the app, so the frame blends in
the way Claude's does. It keeps the native frame, so snapping, resizing,
shadows and rounded corners all still work.

Does nothing on other systems or Windows 10, which don't support it.
"""

from __future__ import annotations

import sys
from typing import Dict

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget

# DwmSetWindowAttribute attributes (Windows 11, build 22000+)
_CORNER_PREFERENCE = 33
_BORDER_COLOR = 34
_CAPTION_COLOR = 35
_TEXT_COLOR = 36
_ROUND = 2


def _colorref(colour: str) -> int:
    c = QColor(colour)
    return c.red() | (c.green() << 8) | (c.blue() << 16)  # 0x00BBGGRR


def style(window: QWidget, c: Dict[str, str]) -> None:
    """Colour the window's frame to match the colours `c` from theme.colours()."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        dwm = ctypes.windll.dwmapi
        hwnd = wintypes.HWND(int(window.winId()))

        def set_attr(attr: int, value: int) -> None:
            data = wintypes.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(data), ctypes.sizeof(data))

        set_attr(_CORNER_PREFERENCE, _ROUND)
        set_attr(_BORDER_COLOR, _colorref(c["border"]))
        set_attr(_CAPTION_COLOR, _colorref(c["bg"]))
        set_attr(_TEXT_COLOR, _colorref(c["text"]))
    except Exception:
        pass  # older Windows: keep the default frame
