"""The header logo: a link to the Mooxr website.

Hovering it makes the headphones groove: they rock side to side, bob up and
down, and the ear cups thump on each beat. The letters stay still.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from PyQt6.QtCore import QAbstractAnimation, QEasingCurve, QRectF, QSize, Qt, QUrl, QVariantAnimation
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractButton, QWidget

WEBSITE = "https://h4l-git.github.io/Mooxr/"

# Points in the wordmark SVG's 470x140 viewBox.
_VIEW_HEIGHT = 140
_PIVOT = (231, 104)  # just under the headband: the headphones rock around here
_EARS = {"ear_l": (182, 96), "ear_r": (280, 96)}

_BEAT_MS = 900  # one rock left and right; the cups thump twice in it
_TILT = 6.0  # degrees at the far end of each rock
_LIFT = 5.0  # viewBox units the headphones rise at each end
_THUMP = 0.1  # how much bigger the cups get on a beat


class LogoButton(QAbstractButton):
    """The wordmark as a button that opens the website."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._parts: Optional[Dict[str, QPixmap]] = None
        self._focus_colour = QColor("#39d17a")
        self._phase = 0.0  # 0..1 through the current beat
        self._amount = 0.0  # 0 = still, 1 = full groove; eases in and out
        self.setText("Mooxr")
        self.setToolTip("Visit the Mooxr website")
        self.setAccessibleName("Mooxr website")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(WEBSITE)))

        self._beat = QVariantAnimation(self)
        self._beat.setStartValue(0.0)
        self._beat.setEndValue(1.0)
        self._beat.setDuration(_BEAT_MS)
        self._beat.setLoopCount(-1)
        self._beat.valueChanged.connect(self._set_phase)

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(260)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._set_amount)
        self._fade.finished.connect(self._fade_done)

    def set_parts(self, parts: Optional[Dict[str, QPixmap]], focus_colour: str) -> None:
        """Use these logo pieces (from theme.logo_parts); None shows the name as text."""
        self._parts = parts
        self._focus_colour = QColor(focus_colour)
        self.updateGeometry()
        self.update()

    # -- size ----------------------------------------------------------------

    def sizeHint(self) -> QSize:
        if self._parts:
            letters = self._parts["letters"]
            ratio = letters.devicePixelRatio()
            return QSize(round(letters.width() / ratio), round(letters.height() / ratio))
        return self.fontMetrics().size(0, self.text())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # -- hover animation -----------------------------------------------------

    def enterEvent(self, event) -> None:
        self._groove(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._groove(False)
        super().leaveEvent(event)

    def _groove(self, on: bool) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._amount)
        self._fade.setEndValue(1.0 if on else 0.0)
        self._fade.start()
        if on and self._beat.state() != QAbstractAnimation.State.Running:
            self._beat.start()

    def _fade_done(self) -> None:
        if self._amount == 0.0:
            self._beat.stop()
            self._phase = 0.0
            self.update()

    def _set_phase(self, value) -> None:
        self._phase = float(value)
        self.update()

    def _set_amount(self, value) -> None:
        self._amount = float(value)
        self.update()

    # -- drawing -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.hasFocus():
            pen = QPen(self._focus_colour, 1.5)
            painter.setPen(pen)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75), 6, 6)
        if not self._parts:
            painter.setPen(self.palette().color(self.foregroundRole()))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
            return

        parts = self._parts
        letters = parts["letters"]
        scale = letters.height() / letters.devicePixelRatio() / _VIEW_HEIGHT  # viewBox units -> pixels
        painter.drawPixmap(0, 0, letters)

        # Rock side to side, rise at each end, drop back to the middle on the beat.
        swing = math.sin(2 * math.pi * self._phase)
        tilt = _TILT * swing * self._amount
        lift = _LIFT * abs(swing) * self._amount * scale
        beat = ((math.cos(4 * math.pi * self._phase) + 1) / 2) ** 6  # sharp peak as they hit the middle
        thump = 1 + _THUMP * beat * self._amount

        px, py = _PIVOT[0] * scale, _PIVOT[1] * scale
        painter.translate(px, py - lift)
        painter.rotate(tilt)
        painter.translate(-px, -py)
        painter.drawPixmap(0, 0, parts["band"])
        for key, (ex, ey) in _EARS.items():
            painter.save()
            painter.translate(ex * scale, ey * scale)
            painter.scale(thump, thump)
            painter.translate(-ex * scale, -ey * scale)
            painter.drawPixmap(0, 0, parts[key])
            painter.restore()
