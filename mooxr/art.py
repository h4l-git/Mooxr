"""The cover art section of a queue card.

Shows the picture Spotify will display for the track, holds it up against
Spotify's cover art spec, and lets you swap in your own image.

    panel = ArtPanel(source)        # starts building a preview at once
    job["cover"] = panel.cover      # your image, or None for automatic
    panel.show_track(result.path)   # after mooxing: the art really embedded

Previews are built on one background thread shared by every card, using
core.preview_cover(), so they match what moox() will embed.
"""

from __future__ import annotations

import itertools
import queue
import shutil
import tempfile
import threading
from html import escape
from pathlib import Path
from typing import Optional, Tuple

from mutagen.id3 import ID3, ID3NoHeaderError
from PyQt6.QtCore import QBuffer, QByteArray, QObject, QStandardPaths, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import tools
from .core import SPOTIFY_ART_SPEC, MooxError, check_art, is_link, preview_cover

ART_PX = 64  # thumbnail size on a card
IMAGE_FILTER = "Images (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff);;All files (*)"


# --------------------------------------------------------------------------
# Background previews
# --------------------------------------------------------------------------


class _Previewer(QObject):
    """One background thread that makes previews for every card in turn.

    A plain daemon thread rather than a QThread: fetching a link's thumbnail
    can be slow, and closing the window shouldn't have to wait for it.
    """

    ready = pyqtSignal(int, int, str, str)  # panel id, request, jpeg path, where it came from
    missing = pyqtSignal(int, int, str)     # panel id, request, message
    tags = pyqtSignal(int, str, str)        # panel id, title, artist (links only)

    def __init__(self) -> None:
        super().__init__()
        self.folder = Path(tempfile.mkdtemp(prefix="mooxr-art-"))
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def request(self, panel_id: int, request: int, source: str, cover: Optional[Path]) -> None:
        self.jobs.put((panel_id, request, source, cover))
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="mooxr-art", daemon=True)
                self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                panel_id, request, source, cover = self.jobs.get(timeout=1.0)
            except queue.Empty:
                return
            dest = self.folder / f"{panel_id}-{request}.jpg"
            try:
                origin = preview_cover(
                    source, dest, cover,
                    on_tags=lambda title, artist, p=panel_id: self.tags.emit(p, title or "", artist or ""),
                )
            except MooxError as problem:
                self.missing.emit(panel_id, request, str(problem))
            except Exception as problem:  # never let one bad file kill the thread
                self.missing.emit(panel_id, request, f"Mooxr couldn't make a cover preview: {problem}")
            else:
                if origin:
                    self.ready.emit(panel_id, request, str(dest), origin)
                elif is_link(source):
                    self.missing.emit(panel_id, request, "No thumbnail at this link. Mooxr will use a frame "
                                                         "from the video once it's downloaded, if it has one.")
                else:
                    self.missing.emit(panel_id, request, "Nothing to make art from, so Spotify will show its "
                                                         "plain placeholder. Use your own image to fix that.")

    def close(self) -> None:
        """Drop waiting previews and delete the preview pictures."""
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        shutil.rmtree(self.folder, ignore_errors=True)


_previewer: Optional[_Previewer] = None


def previewer() -> _Previewer:
    global _previewer
    if _previewer is None:
        _previewer = _Previewer()
    return _previewer


def close() -> None:
    """Call when the window closes."""
    if _previewer is not None:
        _previewer.close()


# --------------------------------------------------------------------------
# Pictures
# --------------------------------------------------------------------------


def pictures_dir() -> str:
    """The user's Pictures folder, where image pickers open.

    Asks Windows for it rather than guessing ~/Pictures, so a Pictures folder
    moved into OneDrive is still found.
    """
    found = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    return found if found and Path(found).is_dir() else str(Path.home())


def read_art(path: Optional[Path] = None, data: Optional[bytes] = None) -> Tuple[QImage, str]:
    """Load a picture from a file or from bytes. Returns (image, format such as "jpeg")."""
    if data is not None:
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QBuffer.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        fmt = bytes(reader.format()).decode("ascii", errors="replace")
        return reader.read(), fmt
    reader = QImageReader(str(path))
    fmt = bytes(reader.format()).decode("ascii", errors="replace")
    return reader.read(), fmt


def embedded_art(mp3: Path) -> Optional[bytes]:
    """The front cover inside an MP3, or None."""
    try:
        pictures = ID3(mp3).getall("APIC")
    except (ID3NoHeaderError, OSError):
        return None
    front = [p for p in pictures if p.type == 3] or pictures
    return front[0].data if front else None


def rounded_thumbnail(image: QImage, size: int, dpr: float) -> QPixmap:
    """The image as a sharp square thumbnail with rounded corners."""
    px = max(int(size * dpr), 1)
    scaled = image.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, px, px, 7 * dpr, 7 * dpr)
    painter.setClipPath(clip)
    painter.drawImage((px - scaled.width()) // 2, (px - scaled.height()) // 2, scaled)
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


# --------------------------------------------------------------------------
# The card section
# --------------------------------------------------------------------------

_ids = itertools.count()


class ArtPanel(QWidget):
    """Thumbnail, spec check and "use my own image" for one queue card."""

    # For a link: the title and artist the preview's lookup found ("" if none).
    tags_found = pyqtSignal(str, str)

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.cover: Optional[Path] = None  # the user's own image; None = automatic
        self._id = next(_ids)
        self._request = 0
        self._done = False
        self._shown_path: Optional[str] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)

        # palette() follows whichever theme is active.
        self.thumb = QLabel("♪")
        self.thumb.setFixedSize(ART_PX, ART_PX)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            "QLabel { background: palette(button); border: 1px solid palette(mid); border-radius: 8px;"
            " color: palette(placeholder-text); font-size: 18pt; }"
        )
        layout.addWidget(self.thumb, alignment=Qt.AlignmentFlag.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(2)
        heading = QLabel("Cover art in Spotify")
        heading.setStyleSheet("font-weight: 600;")
        # Named "status" to pick up the theme's good / bad colours for it.
        self.info = QLabel("Finding cover art…")
        self.info.setObjectName("status")
        self.info.setProperty("muted", True)
        self.info.setWordWrap(True)
        self.spec = QLabel(SPOTIFY_ART_SPEC)
        self.spec.setProperty("muted", True)
        self.spec.setWordWrap(True)
        self.spec.setStyleSheet("font-size: 8.5pt;")
        texts.addWidget(heading)
        texts.addWidget(self.info)
        texts.addWidget(self.spec)
        texts.addStretch(1)
        layout.addLayout(texts, 1)

        self.button = QPushButton("Use my own image…")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setToolTip("Pick the picture Spotify shows for this track")
        self.button.clicked.connect(self._button_clicked)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignTop)

        shared = previewer()
        shared.ready.connect(self._ready)
        shared.missing.connect(self._missing)
        shared.tags.connect(self._tags)
        self.refresh()

    # -- what the card calls -------------------------------------------------

    def refresh(self) -> None:
        """(Re)build the preview, e.g. after the user picks an image or FFmpeg arrives."""
        if self._done:
            return
        self._request += 1
        if tools.missing() and self.cover is None:
            self._clear_thumb()
            self._say("The preview appears once FFmpeg is here.")
            return
        if self.cover is not None:
            # The user's own picture: show it straight away; the check is on
            # the original, since that's what they control.
            image, fmt = read_art(self.cover)
            if not image.isNull():
                self._set_thumb(image, tooltip_path=str(self.cover))
                fits, text = check_art(image.width(), image.height(), fmt)
                self._say(f"Your image: {text}", good=fits)
                return
            self._say("Checking your image…")
        else:
            self._say("Finding cover art…")
        previewer().request(self._id, self._request, self.source, self.cover)

    def set_editable(self, editable: bool) -> None:
        self.button.setEnabled(editable and not self._done)

    def show_track(self, mp3: Path) -> None:
        """After mooxing: show the art actually inside the MP3."""
        self._done = True
        self._request += 1  # ignore any preview still on its way
        self.button.hide()
        data = embedded_art(mp3)
        if data is None:
            self._clear_thumb()
            self._say("No cover art in this track, so Spotify shows its plain placeholder.", good=False)
            return
        image, fmt = read_art(data=data)
        if image.isNull():
            self._say("The cover art in this track can't be read.", good=False)
            return
        self._set_thumb(image)
        fits, text = check_art(image.width(), image.height(), fmt)
        self._say(f"In the MP3: {text}", good=fits)

    # -- internals -------------------------------------------------------------

    def _button_clicked(self) -> None:
        if self.cover is not None:
            self.cover = None
            self.refresh()
            return
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose cover art", pictures_dir(), IMAGE_FILTER)
        if chosen:
            self.cover = Path(chosen)
            self.refresh()

    def _ready(self, panel_id: int, request: int, path: str, origin: str) -> None:
        if panel_id != self._id or request != self._request:
            return
        image, fmt = read_art(Path(path))
        if image.isNull():
            self._missing(panel_id, request, "Mooxr made a cover preview but can't show it.")
            return
        self._set_thumb(image, tooltip_path=path)
        fits, text = check_art(image.width(), image.height(), fmt)
        self._say(f"From {origin}: {text}", good=fits)

    def _tags(self, panel_id: int, title: str, artist: str) -> None:
        if panel_id == self._id and not self._done:
            self.tags_found.emit(title, artist)

    def _missing(self, panel_id: int, request: int, message: str) -> None:
        if panel_id != self._id or request != self._request:
            return
        self._clear_thumb()
        self._say(message, good=False if self.cover is not None else None)

    def _set_thumb(self, image: QImage, tooltip_path: Optional[str] = None) -> None:
        dpr = max(self.devicePixelRatioF() or 1.0, 2.0)  # stays sharp on a 2x screen
        self.thumb.setText("")
        self.thumb.setPixmap(rounded_thumbnail(image, ART_PX, dpr))
        if tooltip_path:
            url = QUrl.fromLocalFile(tooltip_path).toString()
            self.thumb.setToolTip(f'<img src="{escape(url)}" width="240" height="240">')
        else:
            self.thumb.setToolTip("")
        self.button.setText("Back to automatic" if self.cover is not None else "Use my own image…")

    def _clear_thumb(self) -> None:
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText("♪")
        self.thumb.setToolTip("")
        self.button.setText("Back to automatic" if self.cover is not None else "Use my own image…")

    def _say(self, text: str, good: Optional[bool] = None) -> None:
        """good=True green, False red, None plain grey."""
        self.info.setText(text)
        self.info.setProperty("state", {True: "done", False: "failed"}.get(good, ""))
        self.info.style().unpolish(self.info)
        self.info.style().polish(self.info)
