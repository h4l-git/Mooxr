"""Mooxr's look: colour themes, light/dark mode and the stylesheet.

A theme is a gradient (like the logo's mint-to-emerald) plus a hue that
tints the backgrounds. `colours()` turns a theme and a mode into the colours
the stylesheet uses, so a new theme is one line in THEMES.
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import QByteArray, QObject, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

ASSETS = Path(__file__).resolve().parent / "assets"
WORDMARK = ASSETS / "logo" / "mooxr-wordmark-dark.svg"
MARK = ASSETS / "logo" / "mooxr-mark.svg"
CHECK = ASSETS / "check.svg"

# Colours baked into the logo SVGs; swapped for the theme's when drawn.
_LOGO_FROM, _LOGO_TO, _LOGO_LETTERS = "#5EEB8E", "#14B866", "#F2F7F3"


@dataclass(frozen=True)
class Theme:
    name: str
    hue: int          # tints backgrounds and borders
    grad_from: str
    grad_to: str


THEMES: Dict[str, Theme] = {
    "mint": Theme("Mint", 145, "#5EEB8E", "#14B866"),
    "ocean": Theme("Ocean", 210, "#6FD3FF", "#2A8CF0"),
    "grape": Theme("Grape", 265, "#D0A8FF", "#9B6CFF"),
    "rose": Theme("Rose", 335, "#FFA3CF", "#F0508F"),
    "sunset": Theme("Sunset", 15, "#FFB36B", "#FF6A5C"),
    "gold": Theme("Gold", 42, "#FFE27A", "#F5A623"),
}
DEFAULT_THEME = "mint"

MODES = {"dark": "Dark", "light": "Light", "system": "System"}
DEFAULT_MODE = "dark"


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------


def _hsl(hue: int, s: float, l: float) -> str:
    return QColor.fromHslF(hue / 360, s, l).name()


def _lightness(colour: str, factor: float) -> str:
    h, s, l, _ = QColor(colour).getHslF()
    return QColor.fromHslF(max(h, 0.0), s, min(max(l * factor, 0.0), 1.0)).name()


def _mix(a: str, b: str) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor((ca.red() + cb.red()) // 2, (ca.green() + cb.green()) // 2, (ca.blue() + cb.blue()) // 2).name()


def colours(theme: Theme, dark: bool) -> Dict[str, str]:
    h = theme.hue
    ink = _hsl(h, 0.22, 0.065)
    if dark:
        c = {
            "bg": ink, "surface": _hsl(h, 0.20, 0.10), "raised": _hsl(h, 0.19, 0.13),
            "border": _hsl(h, 0.18, 0.18), "text": _hsl(h, 0.25, 0.96), "muted": _hsl(h, 0.10, 0.59),
            "drop": _hsl(h, 0.43, 0.12), "accent": _mix(theme.grad_from, theme.grad_to),
            "strong": theme.grad_from, "good": "#5EEB8E", "bad": "#FF7B7B", "warn": "#FFC66B",
        }
    else:
        c = {
            "bg": _hsl(h, 0.25, 0.965), "surface": "#FFFFFF", "raised": _hsl(h, 0.22, 0.93),
            "border": _hsl(h, 0.16, 0.85), "text": ink, "muted": _hsl(h, 0.08, 0.40),
            "drop": _hsl(h, 0.55, 0.93), "accent": theme.grad_to,
            "strong": _lightness(theme.grad_to, 0.72), "good": "#1E8E4E", "bad": "#D03A3A", "warn": "#A86400",
        }
    c.update({
        "accent_text": ink,
        "grad_from": theme.grad_from, "grad_to": theme.grad_to,
        "grad_hover_from": _lightness(theme.grad_from, 1.08), "grad_hover_to": _lightness(theme.grad_to, 1.12),
    })
    return c


def _gradient(c: Dict[str, str], hover: bool = False) -> str:
    a, b = (c["grad_hover_from"], c["grad_hover_to"]) if hover else (c["grad_from"], c["grad_to"])
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {a}, stop:1 {b})"


def _tinted(name: str, colour: str) -> Path:
    """A copy of assets/<name>.svg (drawn in #000000) in ``colour``, for the
    stylesheet, which can only point at image files. Falls back to the original."""
    folder = Path(tempfile.gettempdir()) / "mooxr-icons"
    dest = folder / f"{name}-{colour.lstrip('#')}.svg"
    try:
        if not dest.is_file():
            folder.mkdir(exist_ok=True)
            svg = (ASSETS / f"{name}.svg").read_text(encoding="utf-8")
            dest.write_text(svg.replace("#000000", colour), encoding="utf-8")
        return dest
    except OSError:
        return ASSETS / f"{name}.svg"


def stylesheet(c: Dict[str, str]) -> str:
    CHEVRON = _tinted("chevron-down", c["muted"])
    return f"""
    QWidget {{ color: {c['text']}; font-size: 10pt; }}
    QMainWindow, QDialog, #page {{ background: {c['bg']}; }}
    QScrollArea, #queue {{ background: transparent; border: none; }}
    #title {{ font-size: 20pt; font-weight: 700; }}
    #subtitle, #byline, *[muted="true"] {{ color: {c['muted']}; }}
    #byline {{ font-size: 8pt; }}
    #drop {{
        background: {c['surface']}; border: 2px dashed {c['border']}; border-radius: 18px;
    }}
    #drop[hot="true"] {{ background: {c['drop']}; border-color: {c['accent']}; }}
    #dropText {{ font-size: 12pt; font-weight: 600; }}
    #row {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px; }}
    #rowSource {{ font-weight: 600; }}
    #banner {{ background: {c['drop']}; border: 1px solid {c['accent']}; border-radius: 14px; }}
    #bannerText[failed="true"] {{ color: {c['bad']}; }}
    QLineEdit {{
        background: {c['bg']}; border: 1px solid {c['border']}; border-radius: 10px; padding: 6px 10px;
        selection-background-color: {c['accent']}; selection-color: {c['accent_text']};
    }}
    QLineEdit:focus {{ border-color: {c['accent']}; }}
    QLineEdit[invalid="true"] {{ border-color: {c['bad']}; }}
    QLineEdit:disabled {{ color: {c['muted']}; }}
    QPushButton {{
        background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 15px; padding: 6px 16px;
    }}
    QPushButton:hover {{ border-color: {c['accent']}; color: {c['strong']}; }}
    QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
    QPushButton#primary {{
        background: {_gradient(c)}; color: {c['accent_text']}; border: none; font-weight: 700;
        padding: 9px 28px; font-size: 11pt; border-radius: 18px;
    }}
    QPushButton#primary:hover {{ background: {_gradient(c, hover=True)}; color: {c['accent_text']}; }}
    QPushButton#primary:disabled {{ background: {c['raised']}; color: {c['muted']}; }}
    /* Moox and its "to Spotify ▾" half, drawn as one pill */
    QPushButton#primary[joined="true"] {{
        border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 9px 14px 9px 24px;
    }}
    QPushButton#sendTo {{
        border-top-left-radius: 0; border-bottom-left-radius: 0;
        border-top-right-radius: 18px; border-bottom-right-radius: 18px;
        padding: 0; font-size: 10pt; font-weight: 600;
    }}
    QPushButton#sendTo::menu-indicator {{ image: none; width: 0; }}
    QToolTip {{
        background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px 6px;
    }}
    QMenu {{ background: {c['raised']}; border: 1px solid {c['border']}; padding: 4px; }}
    QMenu::item {{ padding: 5px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {c['drop']}; color: {c['strong']}; }}
    QMenu::item:disabled {{ color: {c['muted']}; }}
    QCheckBox::indicator, QListWidget::indicator {{
        width: 14px; height: 14px; border: 1px solid {c['muted']}; border-radius: 4px; background: {c['bg']};
    }}
    QCheckBox::indicator:checked, QListWidget::indicator:checked {{
        background: {c['accent']}; border-color: {c['accent']}; image: url("{CHECK.as_posix()}");
    }}
    QPushButton#danger:hover {{ border-color: {c['bad']}; color: {c['bad']}; }}
    QComboBox {{
        background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 15px; padding: 5px 12px;
    }}
    QComboBox:hover, QComboBox:focus {{ border-color: {c['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox::down-arrow {{ image: url("{CHEVRON.as_posix()}"); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{
        background: {c['raised']}; border: 1px solid {c['border']}; padding: 4px; outline: none;
        selection-background-color: {c['drop']}; selection-color: {c['strong']};
    }}
    QPushButton#remove {{ border: none; background: transparent; color: {c['muted']}; font-size: 13pt; padding: 0 6px; }}
    QPushButton#remove:hover {{ color: {c['bad']}; }}
    QListWidget {{
        background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px; padding: 4px;
        outline: none;
    }}
    QListWidget::item {{ padding: 7px 6px; border-radius: 8px; }}
    QListWidget::item:hover {{ background: {c['raised']}; }}
    QListWidget::item:selected {{ background: {c['drop']}; color: {c['text']}; }}
    QProgressBar {{ background: {c['raised']}; border: none; border-radius: 3px; max-height: 6px; }}
    QProgressBar::chunk {{ background: {_gradient(c)}; border-radius: 3px; }}
    #status[state="done"] {{ color: {c['good']}; }}
    #status[state="failed"] {{ color: {c['bad']}; }}
    #status[state="warn"] {{ color: {c['warn']}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; height: 0;
    }}

    /* Settings window */
    #sidebar {{ background: {c['surface']}; border-right: 1px solid {c['border']}; }}
    #sidebarTitle {{ font-size: 15pt; font-weight: 700; }}
    QListWidget#nav {{ background: transparent; border: none; padding: 0; }}
    QListWidget#nav::item {{ padding: 9px 12px; border-radius: 10px; margin-bottom: 2px; }}
    QListWidget#nav::item:hover {{ background: {c['raised']}; }}
    QListWidget#nav::item:selected {{ background: {c['drop']}; color: {c['strong']}; font-weight: 600; }}
    #pageTitle {{ font-size: 15pt; font-weight: 700; }}
    #settingLabel {{ font-weight: 600; }}
    #segmentBar {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 18px; }}
    QPushButton#segment {{
        background: transparent; border: none; border-radius: 14px; padding: 6px 20px; color: {c['muted']};
    }}
    QPushButton#segment:hover {{ color: {c['text']}; }}
    QPushButton#segment:checked {{ background: {_gradient(c)}; color: {c['accent_text']}; font-weight: 700; }}
    QPushButton#swatch {{
        background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px;
        padding: 10px 14px; text-align: left; font-weight: 600;
    }}
    QPushButton#swatch:hover {{ border-color: {c['accent']}; color: {c['text']}; }}
    QPushButton#swatch:checked {{ background: {c['drop']}; border: 2px solid {c['accent']}; }}
    #swatchName {{ font-weight: 600; background: transparent; }}
    #swatchCaption {{ color: {c['muted']}; font-size: 8pt; background: transparent; }}
    """


def _qpalette(c: Dict[str, str]) -> QPalette:
    """For the bits the stylesheet doesn't reach (native menus, dialogs)."""
    palette = QPalette()
    R = QPalette.ColorRole
    for role, key in (
        (R.Window, "bg"), (R.Base, "bg"), (R.AlternateBase, "surface"), (R.Button, "raised"),
        (R.WindowText, "text"), (R.Text, "text"), (R.ButtonText, "text"), (R.ToolTipBase, "raised"),
        (R.ToolTipText, "text"), (R.PlaceholderText, "muted"), (R.Highlight, "accent"),
        (R.HighlightedText, "accent_text"), (R.Link, "strong"), (R.Mid, "border"),
    ):
        palette.setColor(role, QColor(c[key]))
    return palette


# --------------------------------------------------------------------------
# Logo in theme colours
# --------------------------------------------------------------------------


def logo_pixmap(c: Dict[str, str], height: int, mark_only: bool = False) -> Optional[QPixmap]:
    """The wordmark (or just the headphones) recoloured for the theme, or None."""
    try:
        svg = (MARK if mark_only else WORDMARK).read_text(encoding="utf-8")
    except OSError:
        return None
    svg = svg.replace(_LOGO_FROM, c["grad_from"]).replace(_LOGO_TO, c["grad_to"]).replace(_LOGO_LETTERS, c["text"])
    return _render(svg, height)


# The wordmark's pieces, by the ids in its SVG. Each is drawn on its own
# full-size canvas so they stack back into the whole logo.
_LOGO_PARTS = {"letters": {"letters"}, "band": {"band"}, "ear_l": {"cup-l", "pad-l"}, "ear_r": {"cup-r", "pad-r"}}
_SVG_NS = "http://www.w3.org/2000/svg"


def logo_parts(c: Dict[str, str], height: int) -> Optional[Dict[str, QPixmap]]:
    """The wordmark in theme colours, split into letters, headband and each ear, or None."""
    try:
        svg = WORDMARK.read_text(encoding="utf-8")
    except OSError:
        return None
    svg = svg.replace(_LOGO_FROM, c["grad_from"]).replace(_LOGO_TO, c["grad_to"]).replace(_LOGO_LETTERS, c["text"])
    every_id = set().union(*_LOGO_PARTS.values())
    ET.register_namespace("", _SVG_NS)
    parts: Dict[str, QPixmap] = {}
    for name, keep in _LOGO_PARTS.items():
        try:
            root = ET.fromstring(svg)
        except ET.ParseError:
            return None
        found = set()
        for parent in list(root.iter()):
            for child in list(parent):
                part_id = child.get("id")
                if part_id in every_id:
                    found.add(part_id)
                    if part_id not in keep:
                        parent.remove(child)
        if found != every_id:  # an older SVG without the ids
            return None
        pixmap = _render(ET.tostring(root, encoding="unicode"), height)
        if pixmap is None:
            return None
        parts[name] = pixmap
    return parts


def icon_pixmap(name: str, colour: str, size: int) -> Optional[QPixmap]:
    """A line icon from assets/<name>.svg (drawn in #000000) in the given colour."""
    try:
        svg = (ASSETS / f"{name}.svg").read_text(encoding="utf-8")
    except OSError:
        return None
    return _render(svg.replace("#000000", colour), size)


def service_pixmap(key: str, size: int) -> Optional[QPixmap]:
    """A music app's logo from assets/services/<key>.svg, in its own colours."""
    try:
        svg = (ASSETS / "services" / f"{key}.svg").read_text(encoding="utf-8")
    except OSError:
        return None
    return _render(svg, size)


def _render(svg: str, height: int) -> Optional[QPixmap]:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return None
    size = renderer.defaultSize().scaled(10_000, height, Qt.AspectRatioMode.KeepAspectRatio)
    scale = 2.0  # drawn at 2x so it stays sharp on scaled screens
    pixmap = QPixmap(int(size.width() * scale), int(size.height() * scale))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


# --------------------------------------------------------------------------
# The current choice
# --------------------------------------------------------------------------


class Appearance(QObject):
    """Holds the chosen theme and mode, saves them, and restyles the app."""

    changed = pyqtSignal()

    def __init__(self, app: QApplication, settings: QSettings) -> None:
        super().__init__(app)
        self.app = app
        self.settings = settings
        theme = settings.value("appearance/theme", DEFAULT_THEME)
        mode = settings.value("appearance/mode", DEFAULT_MODE)
        self.theme_key = theme if theme in THEMES else DEFAULT_THEME
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.colours: Dict[str, str] = {}
        self._applying = False
        try:
            app.styleHints().colorSchemeChanged.connect(self._system_changed)
        except AttributeError:
            pass
        self.apply()

    @property
    def theme(self) -> Theme:
        return THEMES[self.theme_key]

    def is_dark(self) -> bool:
        if self.mode == "system":
            try:
                return self.app.styleHints().colorScheme() != Qt.ColorScheme.Light
            except AttributeError:
                return True
        return self.mode == "dark"

    def set_theme(self, key: str) -> None:
        if key in THEMES and key != self.theme_key:
            self.theme_key = key
            self.settings.setValue("appearance/theme", key)
            self.apply()

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self.mode:
            self.mode = mode
            self.settings.setValue("appearance/mode", mode)
            self.apply()

    def reload(self) -> None:
        """Pick up a theme or mode saved elsewhere (the Chrome extension's Settings)."""
        self.settings.sync()
        theme = self.settings.value("appearance/theme", DEFAULT_THEME)
        mode = self.settings.value("appearance/mode", DEFAULT_MODE)
        theme = theme if theme in THEMES else DEFAULT_THEME
        mode = mode if mode in MODES else DEFAULT_MODE
        if (theme, mode) != (self.theme_key, self.mode):
            self.theme_key, self.mode = theme, mode
            self.apply()

    def apply(self) -> None:
        # Telling Qt the scheme also colours the Windows title bar; for
        # "system" it goes back to following Windows.
        scheme = {"dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light}.get(self.mode, Qt.ColorScheme.Unknown)
        self._applying = True
        try:
            self.app.styleHints().setColorScheme(scheme)
        except AttributeError:  # Qt older than 6.8
            pass
        finally:
            self._applying = False
        self.colours = colours(self.theme, self.is_dark())
        self.app.setPalette(_qpalette(self.colours))
        self.app.setStyleSheet(stylesheet(self.colours))
        self.changed.emit()

    def _system_changed(self, *_args) -> None:
        if self.mode == "system" and not self._applying:
            self.apply()
