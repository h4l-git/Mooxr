"""The Mooxr window.

Drop videos (or paste links) in, check the guessed title / artist, press
Moox. Each source becomes a row; rows convert one at a time on a worker
thread so the window never freezes.

    python -m mooxr          (no arguments opens the window)
    python -m mooxr.app
"""

from __future__ import annotations

import getpass
import itertools
import json
import queue
import subprocess
import sys
from html import escape
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from PyQt6.QtCore import QEvent, QFile, QSettings, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QActionGroup, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import art, frame, library, services, tools
from .core import DEFAULT_OUT_DIR, Moox, MooxCancelled, MooxError, check_art, guess_tags, is_link, moox
from .settings import SettingsPage, SpotifyPage
from .logo import LogoButton
from .theme import Appearance, icon_pixmap, logo_parts, service_pixmap

MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma",
}
VIDEO_FILTER = "Video and audio (" + " ".join(f"*{ext}" for ext in sorted(MEDIA_EXTENSIONS)) + ");;All files (*)"

# Row states
READY, WAITING, WORKING, DONE, FAILED = "ready", "waiting", "working", "done", "failed"


# --------------------------------------------------------------------------
# Look
# --------------------------------------------------------------------------

def _restyle(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class Worker(QThread):
    """Converts jobs from a queue, one at a time, until the queue is empty.

    A job whose id is put in ``cancelled_jobs`` is skipped if it hasn't
    started, or stopped part way if it has (the ``cancelled`` signal says so).
    """

    progress = pyqtSignal(int, str, float)   # row id, stage, fraction
    succeeded = pyqtSignal(int, object)      # row id, Moox
    failed = pyqtSignal(int, str)            # row id, message
    cancelled = pyqtSignal(int)              # row id
    sent = pyqtSignal(int, str)              # row id, "" if it reached the chosen service, else why not
    started_job = pyqtSignal(int)

    def __init__(self, jobs: "queue.Queue[dict]", cancelled_jobs: Set[int]) -> None:
        super().__init__()
        self.jobs = jobs
        self.cancelled_jobs = cancelled_jobs  # shared with the window

    def run(self) -> None:
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            row_id = job.pop("row_id")
            job_id = job.pop("job_id")
            spotify_dir = job.pop("spotify_dir")
            service = job.pop("service")
            if job_id in self.cancelled_jobs:
                continue  # cancelled while waiting; the row already says so
            self.started_job.emit(row_id)
            try:
                result = moox(
                    **job,
                    on_progress=lambda stage, fraction, r=row_id: self.progress.emit(r, stage, fraction),
                    cancelled=lambda j=job_id: j in self.cancelled_jobs,
                )
            except MooxCancelled:
                self.cancelled.emit(row_id)
            except MooxError as problem:
                self.failed.emit(row_id, str(problem))
            except Exception as problem:  # never let one bad file kill the thread
                self.failed.emit(row_id, f"Something unexpected went wrong: {problem}")
            else:
                self.succeeded.emit(row_id, result)
                # Straight on to the chosen app (Spotify, Apple Music, …).
                try:
                    services.deliver(result.path, service, spotify_dir)
                except MooxError as problem:
                    self.sent.emit(row_id, str(problem))
                except Exception as problem:
                    self.sent.emit(row_id, f"Something unexpected went wrong: {problem}")
                else:
                    self.sent.emit(row_id, "")


class _Cancelled(Exception):
    pass


class FfmpegInstaller(QThread):
    """Runs tools.install_ffmpeg() off the window's thread."""

    progress = pyqtSignal(float)
    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def _report(self, fraction: float) -> None:
        if self.cancelled:
            raise _Cancelled()
        self.progress.emit(fraction)

    def run(self) -> None:
        try:
            tools.install_ffmpeg(self._report)
        except _Cancelled:
            return
        except MooxError as problem:
            self.failed.emit(str(problem))
        except Exception as problem:
            self.failed.emit(f"Something unexpected went wrong while getting FFmpeg: {problem}")
        else:
            self.succeeded.emit()


class ToolsBanner(QFrame):
    """Shown while FFmpeg is missing, with a button to fetch it."""

    installed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("banner")
        self.installer: Optional[FfmpegInstaller] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        line = QHBoxLayout()
        self.text = QLabel()
        self.text.setObjectName("bannerText")
        self.text.setWordWrap(True)
        self.text.setOpenExternalLinks(True)
        self.button = QPushButton(f"Get FFmpeg (about {tools.FFMPEG_DOWNLOAD_MB} MB)")
        self.button.setObjectName("primary")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.install)
        self.button.setVisible(tools.can_install())
        line.addWidget(self.text, 1)
        line.addWidget(self.button)
        layout.addLayout(line)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        layout.addWidget(self.bar)

    def show_missing(self, missing: List[str]) -> None:
        if self.installer and self.installer.isRunning():
            return
        if tools.can_install():
            self._say("Mooxr needs FFmpeg to convert audio. It's free; Mooxr can download it and "
                      "keep its own copy, so there's nothing for you to install.")
        else:
            self._say(f"Mooxr needs {' and '.join(missing)} (part of FFmpeg). Install it from "
                      '<a href="https://ffmpeg.org/download.html">ffmpeg.org</a> and restart Mooxr.')
        self.show()

    def install(self) -> None:
        if self.installer and self.installer.isRunning():
            return
        self.button.setEnabled(False)
        self.button.setText("Downloading…")
        self.bar.setValue(0)
        self.bar.show()
        self._say("Getting FFmpeg. You can keep adding videos meanwhile.")
        self.installer = FfmpegInstaller()
        self.installer.progress.connect(self._progress)
        self.installer.succeeded.connect(self._succeeded)
        self.installer.failed.connect(self._failed)
        self.installer.start()

    def cancel(self) -> None:
        if self.installer and self.installer.isRunning():
            self.installer.cancelled = True
            self.installer.wait()

    def _progress(self, fraction: float) -> None:
        self.bar.setValue(int(fraction * 1000))
        if fraction >= 1.0:
            self.button.setText("Unpacking…")
        else:
            self.button.setText(f"Downloading… {fraction:.0%}")

    def _succeeded(self) -> None:
        self.hide()
        self.installed.emit()

    def _failed(self, message: str) -> None:
        self.bar.hide()
        self.button.setEnabled(True)
        self.button.setText("Try again")
        self._say(message, failed=True)

    def _say(self, text: str, failed: bool = False) -> None:
        self.text.setText(text)
        self.text.setProperty("failed", "true" if failed else "false")
        _restyle(self.text)


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class DropZone(QFrame):
    browse = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("drop")
        self.setMinimumHeight(130)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        text = QLabel("Drop videos here")
        text.setObjectName("dropText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = text
        hint = QLabel("or")
        hint.setProperty("muted", True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button = QPushButton("Choose files…")
        button.clicked.connect(self.browse)
        button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.hint = hint
        layout.addWidget(text)
        layout.addWidget(hint)
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_compact(self, compact: bool) -> None:
        """Smaller once there is a queue to look at."""
        self.hint.setVisible(not compact)
        self.text.setText("Drop more videos here" if compact else "Drop videos here")
        self.setMinimumHeight(0 if compact else 130)

    def set_hot(self, hot: bool) -> None:
        self.setProperty("hot", "true" if hot else "false")
        _restyle(self)


_job_ids = itertools.count()


class Row(QFrame):
    """One source in the queue."""

    remove = pyqtSignal(int)
    cancel = pyqtSignal(int)

    def __init__(self, row_id: int, source: str) -> None:
        super().__init__()
        self.row_id = row_id
        self.source = source
        self.state = READY
        self.result: Optional[Moox] = None
        self.repeat: Optional[library.Track] = None  # mooxed from this same source before
        self.job_id = -1  # set each time it's queued
        self.service = services.get(None)  # set when queued
        self._done_text = ""
        self.setObjectName("row")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        link = is_link(source)
        name = source.strip() if link else Path(source).name
        if link:
            guess_artist, guess_title = None, None
        else:
            guess_artist, guess_title = guess_tags(Path(source).stem)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 8, 12)
        outer.setSpacing(8)

        # Top line: source name and remove button
        top = QHBoxLayout()
        kind = QLabel("🔗" if link else "🎬")
        self.name = QLabel()
        self.name.setObjectName("rowSource")
        self.name.setToolTip(source)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._full_name = name
        self.remove_button = QPushButton("✕")
        self.remove_button.setObjectName("remove")
        self.remove_button.setToolTip("Remove from the list")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(lambda: self.remove.emit(self.row_id))
        top.addWidget(kind)
        top.addWidget(self.name, 1)
        top.addWidget(self.remove_button)
        outer.addLayout(top)

        # Tag fields. Left empty, Mooxr works the tags out itself; the
        # placeholder shows what it will probably pick.
        fields = QHBoxLayout()
        self.title_edit = QLineEdit()
        self.artist_edit = QLineEdit()
        auto = "from the link" if link else None
        self.title_edit.setPlaceholderText(f"Title: {guess_title or auto or 'auto'}")
        self.artist_edit.setPlaceholderText(f"Artist: {guess_artist or auto or 'auto'}")
        self.title_edit.setToolTip("Leave empty to let Mooxr decide")
        self.artist_edit.setToolTip("Leave empty to let Mooxr decide")
        fields.addWidget(self.title_edit, 3)
        fields.addWidget(self.artist_edit, 2)
        outer.addLayout(fields)

        # Cover art: what Spotify will show, checked against its spec (art.py)
        self.art = art.ArtPanel(source)
        self.art.tags_found.connect(self._link_guess)
        outer.addWidget(self.art)

        # Status line
        bottom = QHBoxLayout()
        self.status = QLabel("Ready")
        self.status.setObjectName("status")
        self.status.setProperty("muted", True)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.show_button = QPushButton("Show in folder")
        self.show_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_button.clicked.connect(self._reveal)
        self.show_button.hide()
        self.upload_button = QPushButton("Upload")
        self.upload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_button.clicked.connect(self.open_upload)
        self.upload_button.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Stop mooxing this one")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(lambda: self.cancel.emit(self.row_id))
        self.cancel_button.hide()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.upload_button)
        bottom.addWidget(self.show_button)
        outer.addLayout(bottom)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        outer.addWidget(self.bar)

    def resizeEvent(self, event) -> None:  # keep long names on one line
        metrics = self.name.fontMetrics()
        self.name.setText(metrics.elidedText(self._full_name, Qt.TextElideMode.ElideMiddle, max(self.name.width(), 50)))
        super().resizeEvent(event)

    # -- state changes ----------------------------------------------------

    def job(self, out_dir: Path, spotify_dir: Path, service: services.Service) -> dict:
        self.service = service
        self.job_id = next(_job_ids)
        return {
            "row_id": self.row_id,
            "job_id": self.job_id,
            "spotify_dir": spotify_dir,
            "service": service.key,
            "source": self.source,
            "out_dir": out_dir,
            "title": self.title_edit.text().strip() or None,
            "artist": self.artist_edit.text().strip() or None,
            "cover": self.art.cover,
        }

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        editable = state in (READY, FAILED)
        self.title_edit.setEnabled(editable)
        self.artist_edit.setEnabled(editable)
        self.art.set_editable(editable)
        self.remove_button.setEnabled(state not in (WAITING, WORKING))
        self.bar.setVisible(state == WORKING)
        self.cancel_button.setVisible(state in (WAITING, WORKING))
        self.cancel_button.setEnabled(True)
        # Before mooxing, "Show in folder" shows the earlier copy of a repeat.
        self.show_button.setVisible(state == DONE or (state == READY and self.repeat is not None))
        if state != DONE:
            self.upload_button.hide()
        self.status.setText(text)
        self.status.setToolTip("")
        self.status.setProperty("state", state)
        _restyle(self.status)

    def note_repeat(self, track: library.Track) -> None:
        """This source was mooxed before: say so, before it's mooxed again."""
        self.repeat = track
        self.set_state(READY, f"Already mooxed as {track.path.name}. Moox it again to make a second copy.")
        self.status.setProperty("state", "warn")
        _restyle(self.status)
        self.status.setToolTip(f"{track.path}\nMooxed from: {track.source}")

    def set_stopping(self) -> None:
        """Cancel was pressed while it's working; the worker stops it shortly."""
        self.cancel_button.setEnabled(False)
        self.status.setText("Stopping…")

    def set_progress(self, stage: str, fraction: float) -> None:
        if self.state != WORKING or not self.cancel_button.isEnabled():  # not while stopping
            return
        self.bar.setValue(int(fraction * 1000))
        word = "Downloading" if stage == "fetching" else "Converting"
        self.status.setText(f"{word}… {fraction:.0%}")

    def set_done(self, result: Moox) -> None:
        self.result = result
        minutes, seconds = divmod(int(round(result.duration)), 60)
        extras = f"{minutes}:{seconds:02d}" + ("" if result.has_cover else " · no cover art")
        self.title_edit.setText(result.title)
        self.artist_edit.setText(result.artist)
        self.art.show_track(result.path)
        self._done_text = f"Saved as {result.path.name}  ({extras})"
        self.set_state(DONE, self._done_text + f" · sending to {self.service.name}…")

    def set_sent(self, problem: str) -> None:
        """After handing it to the chosen service: problem is "" if it worked."""
        if self.state != DONE:
            return
        name = self.service.name
        if problem:
            self.status.setText(f"{self._done_text}, but it didn't reach {name}: {problem}")
            self.status.setProperty("state", "warn")
            _restyle(self.status)
        elif self.service.kind == services.UPLOAD:
            self.status.setText(self._done_text + f" · ready to upload to {name}")
            self.status.setToolTip(self.service.hint)
            self.upload_button.setText(f"Upload to {name}")
            self.upload_button.setToolTip(self.service.hint)
            self.upload_button.show()
        else:
            self.status.setText(self._done_text + f" · sent to {name}")
            self.status.setToolTip(self.service.hint)

    def open_upload(self) -> None:
        """Open the service's upload page and show the file, ready to drag in."""
        if self.result and self.service.upload_url:
            QDesktopServices.openUrl(QUrl(self.service.upload_url))
            reveal(self.result.path)

    def _reveal(self) -> None:
        if self.result:
            reveal(self.result.path)
        elif self.repeat:
            reveal(self.repeat.path)

    def _link_guess(self, title: str, artist: str) -> None:
        """A link's lookup found its title / artist: show them as the guess."""
        if title:
            self.title_edit.setPlaceholderText(f"Title: {title}")
        if artist:
            self.artist_edit.setPlaceholderText(f"Artist: {artist}")


class TrackCard(QFrame):
    """One mooxed track, with its title, artist and cover art editable."""

    saved = pyqtSignal()
    deleted = pyqtSignal(object)  # this card

    def __init__(self, track: library.Track, spotify_dir: Callable[[], Path]) -> None:
        super().__init__()
        self.setObjectName("row")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.track = track
        self.spotify_dir = spotify_dir
        self.in_spotify = False
        self.new_cover: Optional[Path] = None  # picked but not saved yet
        self._info_text = ""
        self._note, self._note_state = "", ""  # the last message, shown under the fields

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(14)

        # Cover art, with a button underneath to change it
        left = QVBoxLayout()
        left.setSpacing(6)
        self.thumb = QLabel("♪")
        self.thumb.setFixedSize(art.ART_PX, art.ART_PX)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            "QLabel { background: palette(button); border: 1px solid palette(mid); border-radius: 8px;"
            " color: palette(placeholder-text); font-size: 18pt; }"
        )
        self.cover_button = QPushButton("Change…")
        self.cover_button.setToolTip("Pick a new cover image for this track")
        self.cover_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_button.clicked.connect(self.choose_cover)
        left.addWidget(self.thumb, alignment=Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.cover_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        left.addStretch(1)
        outer.addLayout(left)

        # Title, artist, file details and actions
        right = QVBoxLayout()
        right.setSpacing(8)
        fields = QHBoxLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Title")
        self.title_edit.setToolTip("Title")
        self.artist_edit = QLineEdit()
        self.artist_edit.setPlaceholderText(f"Artist: {library.UNKNOWN_ARTIST}")
        self.artist_edit.setToolTip("Artist")
        self.title_edit.textChanged.connect(self._changed)
        self.artist_edit.textChanged.connect(self._changed)
        self.title_edit.returnPressed.connect(self.save)
        self.artist_edit.returnPressed.connect(self.save)
        fields.addWidget(self.title_edit, 3)
        fields.addWidget(self.artist_edit, 2)
        right.addLayout(fields)

        self.info = QLabel()
        self.info.setProperty("muted", True)
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        right.addWidget(self.info)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setProperty("muted", True)
        self.status.setWordWrap(True)
        show = QPushButton("Show in folder")
        show.setCursor(Qt.CursorShape.PointingHandCursor)
        show.clicked.connect(lambda: reveal(self.track.path))
        self.send_button = QPushButton("Send to Spotify")
        self.send_button.setToolTip("Copy this track into the folder Spotify watches")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.clicked.connect(self.send)
        self.undo_button = QPushButton("Undo changes")
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.clicked.connect(lambda: self.show_track(self.track))
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self.save)
        self.delete_button = QPushButton("Delete…")
        self.delete_button.setObjectName("danger")
        self.delete_button.setToolTip(f"Move this track to the {TRASH_NAME}, and take it out of Spotify")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self.delete)
        bottom.addWidget(self.delete_button)
        bottom.addStretch(1)
        bottom.addWidget(show)
        bottom.addWidget(self.send_button)
        bottom.addWidget(self.undo_button)
        bottom.addWidget(self.save_button)
        right.addLayout(bottom)
        right.addWidget(self.status)  # own line, so long messages wrap in full
        outer.addLayout(right, 1)

        self.show_track(track)

    # -- what the page calls -----------------------------------------------

    @property
    def dirty(self) -> bool:
        return (
            self.new_cover is not None
            or self.title_edit.text().strip() != self.track.title
            or self.artist_edit.text().strip() != _shown_artist(self.track)
        )

    def show_track(self, track: library.Track) -> None:
        """Show the track as it is on disk, dropping unsaved changes."""
        self.track = track
        self.new_cover = None
        for edit, text in ((self.title_edit, track.title), (self.artist_edit, _shown_artist(track))):
            edit.blockSignals(True)
            edit.setText(text)
            edit.blockSignals(False)
        self._show_info()
        data = art.embedded_art(track.path)
        image = art.read_art(data=data)[0] if data else None
        if image is None or image.isNull():
            self._clear_thumb()
        else:
            self._set_thumb(image)
        self._say("")

    # -- editing -----------------------------------------------------------

    def choose_cover(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose cover art", art.pictures_dir(), art.IMAGE_FILTER)
        if not chosen:
            return
        image, fmt = art.read_art(Path(chosen))
        if image.isNull():
            self._say(f"Mooxr can't read {Path(chosen).name} as an image.", failed=True)
            return
        self.new_cover = Path(chosen)
        self._set_thumb(image, tooltip_path=chosen)
        fits, text = check_art(image.width(), image.height(), fmt)
        self._say(f"New cover, not saved yet: {text}", failed=None if fits else True)

    def save(self) -> None:
        if not self.dirty:
            return
        if not self.title_edit.text().strip():
            self._say("A track needs a title.", failed=True)
            self.title_edit.setFocus()
            return
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        before = self.track.path
        try:
            track = library.edit_track(
                before,
                title=self.title_edit.text(),
                artist=self.artist_edit.text(),
                cover=self.new_cover,
            )
        except MooxError as problem:
            QApplication.restoreOverrideCursor()
            self.save_button.setText("Save")
            self._say(str(problem), failed=True)
            return
        self.show_track(track)
        self.saved.emit()
        # Then swap the copy in the Spotify folder for the edited one.
        try:
            library.resend(track.path, self.spotify_dir(), replaces=before)
        except MooxError as problem:
            self._say(f"Saved, but Spotify still has the old version. {problem}", failed=True)
        else:
            self._say("Saved and updated in Spotify.", failed=False)
        finally:
            QApplication.restoreOverrideCursor()
            self.save_button.setText("Save")
            self._show_info()

    def send(self) -> None:
        """Copy the track into the Spotify folder (e.g. one mooxed before sending was automatic)."""
        try:
            library.send_to([self.track.path], self.spotify_dir())
        except MooxError as problem:
            self._say(str(problem), failed=True)
        else:
            self._say("Sent to Spotify.", failed=False)
        self._show_info()

    def delete(self) -> None:
        """Ask, then move the track (and its Spotify copy) to the Recycle Bin."""
        name = self.track.title if self.track.artist == library.UNKNOWN_ARTIST else (
            f"{self.track.artist} - {self.track.title}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete track")
        box.setText(f"Delete “{name}”?")
        detail = f"{self.track.path.name} goes to the {TRASH_NAME}"
        if self.in_spotify:
            detail += ", and its copy leaves your Spotify folder, so Spotify drops it from Local Files and playlists"
        box.setInformativeText(detail + ".")
        delete = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(box.addButton("Keep it", QMessageBox.ButtonRole.RejectRole))
        box.exec()
        if box.clickedButton() is not delete:
            return
        try:
            library.delete_track(self.track.path, self.spotify_dir(), discard=move_to_trash)
        except MooxError as problem:
            if self.track.path.exists():
                self._say(str(problem), failed=True)
                return
            # The track went but its Spotify copy stayed: say so, then drop the card.
            QMessageBox.warning(self, "Delete track", str(problem))
        self.deleted.emit(self)

    # -- internals ---------------------------------------------------------

    def _changed(self) -> None:
        dirty = self.dirty
        self.save_button.setEnabled(dirty)
        self.undo_button.setVisible(dirty)
        # Spotify knows a local track by its title, artist, album and length,
        # so a new title or artist makes it a different song to Spotify.
        renamed = (self.title_edit.text().strip() != self.track.title
                   or self.artist_edit.text().strip() != _shown_artist(self.track))
        if renamed and self._note_state != "failed":
            text, state = NEW_SONG_WARNING, "warn"
        else:
            text, state = self._note, self._note_state
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.status.setProperty("state", state)
        _restyle(self.status)

    def _show_info(self) -> None:
        track = self.track
        minutes, seconds = divmod(int(round(track.duration)), 60)
        in_spotify = self.in_spotify = library.already_sent(track, self.spotify_dir())
        self._info_text = f"{track.path.name}  ·  {minutes}:{seconds:02d}" + ("  ·  ✓ in Spotify" if in_spotify else "")
        self._elide_info()
        self.info.setToolTip(f"{track.path}\nMooxed from: {track.source}")
        self.send_button.setVisible(not in_spotify)

    def resizeEvent(self, event) -> None:  # keep long names on one line
        self._elide_info()
        super().resizeEvent(event)

    def _elide_info(self) -> None:
        metrics = self.info.fontMetrics()
        width = max(self.info.width(), 50)
        self.info.setText(metrics.elidedText(self._info_text, Qt.TextElideMode.ElideMiddle, width))

    def _set_thumb(self, image, tooltip_path: Optional[str] = None) -> None:
        dpr = max(self.devicePixelRatioF() or 1.0, 2.0)  # stays sharp on a 2x screen
        self.thumb.setText("")
        self.thumb.setPixmap(art.rounded_thumbnail(image, art.ART_PX, dpr))
        if tooltip_path:
            url = QUrl.fromLocalFile(tooltip_path).toString()
            self.thumb.setToolTip(f'<img src="{escape(url)}" width="240" height="240">')
        else:
            self.thumb.setToolTip("")

    def _clear_thumb(self) -> None:
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText("♪")
        self.thumb.setToolTip("No cover art")

    def _say(self, text: str, failed: Optional[bool] = None) -> None:
        """failed=True red, False green, None plain grey."""
        self._note = text
        self._note_state = {True: "failed", False: "done"}.get(failed, "")
        self._changed()


NEW_SONG_WARNING = (
    "Spotify will treat this as a different song: after saving, add it to your playlists "
    "and Liked Songs again."
)


def _shown_artist(track: library.Track) -> str:
    """What goes in the artist box: empty for "Unknown Artist", so saving
    keeps naming the file by title alone, as moox() does."""
    return "" if track.artist == library.UNKNOWN_ARTIST else track.artist


LIBRARY_SORTS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "title": "Title A–Z",
    "artist": "Artist A–Z",
    "longest": "Longest first",
}
LIBRARY_FILTERS = {
    "all": "All tracks",
    "not_sent": "Not in Spotify",
    "no_cover": "No cover art",
}


class LibraryPage(QWidget):
    """Every track Mooxr has made, shown in the main window in place of the
    queue. Each one's title, artist and cover art can be changed."""

    done = pyqtSignal()  # back to the queue

    def __init__(self, spotify_dir: Callable[[], Path]) -> None:
        super().__init__()
        self.setObjectName("page")
        self.spotify_dir = spotify_dir
        self.out_dir = DEFAULT_OUT_DIR
        self.cards: Dict[Path, TrackCard] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        words = QVBoxLayout()
        words.setSpacing(2)
        heading = QLabel("Mooxed files")
        heading.setObjectName("pageTitle")
        self.from_label = QLabel()
        self.from_label.setProperty("muted", True)
        self.from_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        words.addWidget(heading)
        words.addWidget(self.from_label)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Read the folder again")
        refresh.clicked.connect(lambda: self.load(self.out_dir))
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self.open_folder)
        header.addLayout(words, 1)
        header.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignTop)
        header.addWidget(open_folder, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        # Search, sort and filter
        self.settings = QSettings("Mooxr", "Mooxr")
        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist or file name")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.sort_box = QComboBox()
        self.sort_box.setToolTip("Sort")
        for key, name in LIBRARY_SORTS.items():
            self.sort_box.addItem(name, key)
        self._select(self.sort_box, self.settings.value("library/sort", "newest"))
        self.sort_box.currentIndexChanged.connect(self._sort_changed)
        self.filter_box = QComboBox()
        self.filter_box.setToolTip("Show")
        for key, name in LIBRARY_FILTERS.items():
            self.filter_box.addItem(name, key)
        self.filter_box.currentIndexChanged.connect(self._filter)
        tools_row.addWidget(self.search, 1)
        tools_row.addWidget(self.sort_box)
        tools_row.addWidget(self.filter_box)
        layout.addLayout(tools_row)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_search)

        self.empty = QLabel("No mooxed files in this folder yet.")
        self.empty.setProperty("muted", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        list_widget = QWidget()
        list_widget.setObjectName("queue")
        self.list_layout = QVBoxLayout(list_widget)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addWidget(self.empty)
        self.list_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(list_widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        back = QPushButton("Done")
        back.setObjectName("primary")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.done)
        actions.addWidget(back)
        layout.addLayout(actions)

    def load(self, out_dir: Path) -> None:
        """(Re)read the output folder. Cards with unsaved changes keep them."""
        self.out_dir = out_dir
        self.from_label.setToolTip(str(out_dir))
        old = self.cards
        self.cards = {}
        for track in library.list_tracks(out_dir):
            card = old.pop(track.path, None)
            if card is None:
                card = TrackCard(track, self.spotify_dir)
                card.saved.connect(self._card_saved)
                card.deleted.connect(self._card_deleted)
            elif not card.dirty:
                card.show_track(track)
            else:
                card._show_info()  # keeps the edits; the Spotify folder may have changed
            self.cards[track.path] = card
        for card in old.values():  # gone from the folder
            self.list_layout.removeWidget(card)
            card.deleteLater()
        self._sort()
        self._filter()

    # -- search, sort and filter ---------------------------------------------

    def _sort(self) -> None:
        """Put the cards in the chosen order, just under the "empty" label."""
        key = self.sort_box.currentData()
        cards = list(self.cards.values())
        if key == "title":
            cards.sort(key=lambda c: (c.track.title.casefold(), c.track.artist.casefold()))
        elif key == "artist":
            cards.sort(key=lambda c: (c.track.artist.casefold(), c.track.title.casefold()))
        elif key == "longest":
            cards.sort(key=lambda c: c.track.duration, reverse=True)
        else:
            cards.sort(key=lambda c: c.track.modified, reverse=(key != "oldest"))
        for card in cards:
            self.list_layout.removeWidget(card)
        for index, card in enumerate(cards):
            self.list_layout.insertWidget(1 + index, card)

    def _filter(self) -> None:
        """Show only the cards that match the search box and the Show choice."""
        words = self.search.text().casefold().split()
        show = self.filter_box.currentData()
        shown = 0
        for card in self.cards.values():
            track = card.track
            text = f"{track.title} {track.artist} {track.path.name}".casefold()
            visible = all(word in text for word in words) and (
                show == "all"
                or (show == "not_sent" and not card.in_spotify)
                or (show == "no_cover" and not track.has_cover)
            )
            card.setVisible(visible)
            shown += visible
        total = len(self.cards)
        tracks = "1 track" if total == 1 else f"{total} tracks"
        counted = tracks if shown == total else f"{shown} of {tracks}"
        self.from_label.setText(f"{counted} in {self.out_dir}")
        if not total:
            self.empty.setText("No mooxed files in this folder yet.")
        else:
            self.empty.setText("No tracks match.")
        self.empty.setVisible(shown == 0)

    def _sort_changed(self) -> None:
        self.settings.setValue("library/sort", self.sort_box.currentData())
        self._sort()

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    @staticmethod
    def _select(box: QComboBox, key: str) -> None:
        index = box.findData(key)
        box.setCurrentIndex(max(index, 0))

    # -- card changes ----------------------------------------------------------

    def _card_saved(self) -> None:
        # Saving can rename the file, so re-key the cards by their new paths.
        self.cards = {card.track.path: card for card in self.cards.values()}
        self._sort()
        self._filter()

    def _card_deleted(self, card: "TrackCard") -> None:
        self.cards = {path: c for path, c in self.cards.items() if c is not card}
        self.list_layout.removeWidget(card)
        card.hide()  # gone at once, not when Qt gets round to deleting it
        card.deleteLater()
        self._filter()

    def open_folder(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir)))


TRASH_NAME = "Recycle Bin" if sys.platform == "win32" else "Trash"


def move_to_trash(path: Path) -> None:
    """Move a file to the Recycle Bin (Trash). Raises OSError if it can't."""
    moved, _ = QFile.moveToTrash(str(path))
    if not moved:
        raise OSError(f"Couldn't move {path.name} to the {TRASH_NAME}")


def reveal(path: Path) -> None:
    """Open the file's folder with the file selected, where the OS allows it."""
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin" and path.exists():
        subprocess.Popen(["open", "-R", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))


AUTHOR_SITE = "https://h4l-git.github.io/"


class BylineLabel(QLabel):
    """The "By h4l.dev" credit; clicking it opens the author's website."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(AUTHOR_SITE)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            QDesktopServices.openUrl(QUrl(AUTHOR_SITE))
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


class Window(QMainWindow):
    def __init__(self, appearance: Appearance) -> None:
        super().__init__()
        self.setWindowTitle("Mooxr")
        self.resize(720, 680)
        self.setMinimumSize(640, 480)  # wide enough for the Settings page
        self.setAcceptDrops(True)

        self.appearance = appearance
        self.settings = QSettings("Mooxr", "Mooxr")
        self.rows: Dict[int, Row] = {}
        self.next_id = 0
        self.jobs: "queue.Queue[dict]" = queue.Queue()
        self.cancelled_jobs: Set[int] = set()  # job ids; read by the worker thread
        self.worker: Optional[Worker] = None
        self._wants_start = False
        self.service = services.get(self.settings.value("send_service", services.DEFAULT_SERVICE))
        self._to_upload: Optional[Row] = None  # an upload-service row that finished this run

        # The main view and Settings share the window; only one shows at a time.
        self.views = QStackedWidget()
        self.setCentralWidget(self.views)
        page = QWidget()
        page.setObjectName("page")
        self.main_view = page
        self.views.addWidget(page)
        self.settings_view = SettingsPage(self.appearance)
        self.settings_view.done.connect(self.hide_settings)
        spotify_page = SpotifyPage(self.settings)
        self.spotify_page = spotify_page
        spotify_page.changed.connect(self._show_folder)  # the save folder follows it
        spotify_page.changed.connect(self._reload_library)
        self.settings_view.add_section("Spotify", spotify_page)
        self.views.addWidget(self.settings_view)
        self.library = LibraryPage(lambda: self.spotify_dir)
        self.library.done.connect(self.hide_settings)
        self.views.addWidget(self.library)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Header: logo and tagline, Settings on the right
        header = QHBoxLayout()
        words = QVBoxLayout()
        words.setSpacing(4)
        self.logo = LogoButton()  # opens the website; the headphones groove on hover
        self.logo.setObjectName("title")
        subtitle = QLabel("Video in, tagged MP3 out - ready for Spotify Local Files.")
        subtitle.setObjectName("subtitle")
        byline = BylineLabel("By h4l.dev")
        byline.setObjectName("byline")
        brand = QHBoxLayout()
        brand.setSpacing(8)
        brand.addWidget(self.logo, alignment=Qt.AlignmentFlag.AlignBottom)
        brand.addWidget(byline, alignment=Qt.AlignmentFlag.AlignBottom)
        brand.addStretch(1)
        words.addLayout(brand)
        words.addWidget(subtitle)
        settings_button = QPushButton("Settings")
        self.settings_button = settings_button
        settings_button.setToolTip("Settings (Ctrl+,)")
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_button.clicked.connect(self.show_settings)
        header.addLayout(words, 1)
        header.addWidget(settings_button, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.appearance.changed.connect(self._draw_theme_art)
        self.appearance.changed.connect(self._style_frame)
        self._draw_theme_art()

        # Missing FFmpeg warning
        self.banner = ToolsBanner()
        self.banner.installed.connect(self._tools_installed)
        self.banner.hide()
        layout.addWidget(self.banner)

        # Drop zone
        self.drop = DropZone()
        self.drop.browse.connect(self.choose_files)
        layout.addWidget(self.drop)

        # Link entry
        link_row = QHBoxLayout()
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("…or paste a link (YouTube, SoundCloud, Bandcamp…) and press Enter")
        self.link_edit.returnPressed.connect(self.add_link)
        add = QPushButton("Add link")
        add.clicked.connect(self.add_link)
        link_row.addWidget(self.link_edit, 1)
        link_row.addWidget(add)
        layout.addLayout(link_row)

        # Queue
        self.empty = QLabel("Nothing queued yet.")
        self.empty.setProperty("muted", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        queue_widget = QWidget()
        queue_widget.setObjectName("queue")
        self.queue_layout = QVBoxLayout(queue_widget)
        self.queue_layout.setContentsMargins(0, 0, 4, 0)
        self.queue_layout.setSpacing(8)
        self.queue_layout.addWidget(self.empty)
        self.queue_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(queue_widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll = scroll
        layout.addWidget(scroll, 1)

        # Footer: output folder and actions
        folder_row = QHBoxLayout()
        save_to = QLabel("Save to:")
        save_to.setProperty("muted", True)
        self.folder_label = QLabel()
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        change = QPushButton("Change…")
        change.clicked.connect(self.choose_folder)
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self.open_folder)
        folder_row.addWidget(save_to)
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(change)
        folder_row.addWidget(open_folder)
        layout.addLayout(folder_row)

        actions = QHBoxLayout()
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.clicked.connect(self.clear_finished)
        library_button = QPushButton("Mooxed files")
        library_button.setToolTip("See every track you've mooxed and change its title, artist or cover")
        library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        library_button.clicked.connect(self.show_library)
        self.moox_button = QPushButton("Moox")
        self.moox_button.setObjectName("primary")
        self.moox_button.setProperty("joined", "true")
        self.moox_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.moox_button.clicked.connect(self.start)
        # Joined to Moox: where finished tracks go ("to" + the app's logo).
        # Opens a menu of apps.
        self.send_button = QPushButton()
        self.send_button.setObjectName("sendTo")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setMenu(self._services_menu())
        # Same height as Moox so the pair reads as one button.
        self.send_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        inner = QHBoxLayout(self.send_button)
        inner.setContentsMargins(10, 0, 12, 0)
        inner.setSpacing(6)
        self.send_logo = QLabel()
        self.send_arrow = QLabel()  # chevron, drawn in _draw_send_arrow()
        self.send_arrow.setFixedSize(14, 14)
        labels = [QLabel("to"), self.send_logo, self.send_arrow]
        for label in labels:  # let clicks reach the button
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            inner.addWidget(label)
        self._draw_send_arrow()
        self.appearance.changed.connect(self._draw_send_arrow)
        moox_pair = QHBoxLayout()
        moox_pair.setSpacing(0)
        moox_pair.addWidget(self.moox_button)
        moox_pair.addWidget(self.send_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(library_button)
        actions.addStretch(1)
        actions.addLayout(moox_pair)
        layout.addLayout(actions)
        self._show_service()

        # Ctrl+V outside a text box adds links or file paths from the clipboard.
        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.choose_files)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.toggle_settings)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide_settings)

        self._show_folder()
        self._check_tools()
        self._refresh()

    # -- adding sources ----------------------------------------------------

    def add_sources(self, sources: List[str]) -> int:
        queued = {row.source for row in self.rows.values() if row.state != DONE}
        # What's been mooxed into the output folder before, to flag repeats.
        mooxed = library.mooxed_index(self.out_dir) if sources else {}
        added = 0
        for source in sources:
            if source in queued:
                continue
            queued.add(source)
            row = Row(self.next_id, source)
            row.remove.connect(self.remove_row)
            row.cancel.connect(self.cancel_row)
            earlier = mooxed.get(library.source_key(source))
            if earlier is not None:
                row.note_repeat(earlier)
            self.rows[self.next_id] = row
            self.next_id += 1
            self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)
            added += 1
        self._refresh()
        if added:
            self.hide_settings()  # show the new rows
            # Scroll to the newest row once the layout has caught up.
            bar = self.scroll.verticalScrollBar()
            bar.rangeChanged.connect(self._scroll_to_end)
        return added

    def _scroll_to_end(self, _min: int, maximum: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.disconnect(self._scroll_to_end)
        bar.setValue(maximum)

    def add_paths(self, paths: List[Path]) -> None:
        sources: List[str] = []
        for path in paths:
            if path.is_dir():
                sources += sorted(
                    str(p) for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
                )
            elif path.is_file():
                sources.append(str(path))
        self.add_sources(sources)

    def add_link(self) -> None:
        text = self.link_edit.text().strip()
        if not text:
            return
        links = [part for part in text.split() if is_link(part)]
        if not links:
            self.link_edit.setProperty("invalid", "true")
            _restyle(self.link_edit)
            self.link_edit.setToolTip("That doesn't look like a link. Links start with http:// or https://")
            return
        self.link_edit.setProperty("invalid", "false")
        _restyle(self.link_edit)
        self.link_edit.setToolTip("")
        self.add_sources(links)
        self.link_edit.clear()

    def paste(self) -> None:
        clip = QGuiApplication.clipboard().mimeData()
        if clip.hasUrls() and any(url.isLocalFile() for url in clip.urls()):
            self.add_paths([Path(url.toLocalFile()) for url in clip.urls() if url.isLocalFile()])
        elif clip.hasText():
            words = clip.text().split()
            links = [w for w in words if is_link(w)]
            if links:
                self.add_sources(links)
            else:
                self.add_paths([Path(line.strip().strip('"')) for line in clip.text().splitlines() if line.strip()])

    def choose_files(self) -> None:
        start = self.settings.value("last_browse", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose videos to moox", start, VIDEO_FILTER)
        if files:
            self.settings.setValue("last_browse", str(Path(files[0]).parent))
            self.add_paths([Path(f) for f in files])

    # -- drag and drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        data = event.mimeData()
        if data.hasUrls() or (data.hasText() and is_link(data.text().strip())):
            event.acceptProposedAction()
            self.drop.set_hot(True)

    def dragLeaveEvent(self, event) -> None:
        self.drop.set_hot(False)

    def dropEvent(self, event) -> None:
        self.drop.set_hot(False)
        data = event.mimeData()
        if data.hasUrls():
            local = [Path(url.toLocalFile()) for url in data.urls() if url.isLocalFile()]
            remote = [url.toString() for url in data.urls() if not url.isLocalFile() and is_link(url.toString())]
            self.add_paths(local)
            self.add_sources(remote)
        elif data.hasText():
            self.add_sources([w for w in data.text().split() if is_link(w)])
        event.acceptProposedAction()

    # -- queue -------------------------------------------------------------

    def remove_row(self, row_id: int) -> None:
        row = self.rows.pop(row_id, None)
        if row:
            row.deleteLater()
        self._refresh()

    def clear_finished(self) -> None:
        for row_id in [i for i, row in self.rows.items() if row.state == DONE]:
            self.remove_row(row_id)

    def cancel_row(self, row_id: int) -> None:
        """Stop one row: drop it from the queue, or stop it part way."""
        row = self.rows.get(row_id)
        if row is None or row.state not in (WAITING, WORKING):
            return
        self.cancelled_jobs.add(row.job_id)
        if row.state == WAITING:
            row.set_state(READY, "Cancelled.")  # the worker skips it
        else:
            row.set_stopping()  # the worker says when it has stopped
        self._refresh()

    def _job_started(self, row_id: int) -> None:
        row = self.rows.get(row_id)
        # A row cancelled just as the worker picked it up is stopped straight away.
        if row and row.job_id not in self.cancelled_jobs:
            row.set_state(WORKING, "Starting…")

    def _job_cancelled(self, row_id: int) -> None:
        self._row(row_id, lambda r: r.set_state(READY, "Cancelled."))
        self._refresh()

    def start(self) -> None:
        if not self._check_tools():
            # Pressing Moox before FFmpeg is here starts fetching it, then
            # carries on by itself.
            self._wants_start = True
            if tools.can_install():
                self.banner.install()
            return
        self._wants_start = False
        ready = [row for row in self.rows.values() if row.state in (READY, FAILED)]
        for row in ready:
            row.set_state(WAITING, "Waiting…")
            self.jobs.put(row.job(self.out_dir, self.spotify_dir, self.service))
        self._ensure_worker()
        self._refresh()

    def _ensure_worker(self) -> None:
        if self.jobs.empty() or (self.worker and self.worker.isRunning()):
            return
        self.worker = Worker(self.jobs, self.cancelled_jobs)
        self.worker.started_job.connect(self._job_started)
        self.worker.cancelled.connect(self._job_cancelled)
        self.worker.progress.connect(lambda i, s, f: self._row(i, lambda r: r.set_progress(s, f)))
        self.worker.succeeded.connect(lambda i, res: self._row(i, lambda r: r.set_done(res)))
        self.worker.failed.connect(lambda i, msg: self._row(i, lambda r: r.set_state(FAILED, msg)))
        self.worker.succeeded.connect(lambda *_: self._refresh())
        self.worker.sent.connect(lambda i, problem: self._row(i, lambda r: r.set_sent(problem)))
        self.worker.sent.connect(lambda *_: self._reload_library())
        self.worker.sent.connect(self._note_upload)
        self.worker.failed.connect(lambda *_: self._refresh())
        # Jobs queued just as the thread was finishing get a fresh worker.
        self.worker.finished.connect(self._ensure_worker)
        self.worker.finished.connect(self._refresh)
        self.worker.finished.connect(self._open_upload)
        self.worker.start()

    def _row(self, row_id: int, action) -> None:
        row = self.rows.get(row_id)
        if row:
            action(row)

    def _refresh(self) -> None:
        states = [row.state for row in self.rows.values()]
        ready = sum(s in (READY, FAILED) for s in states)
        only_failed = ready and all(s == FAILED for s in states if s in (READY, FAILED))
        busy = any(s in (WAITING, WORKING) for s in states)
        self.empty.setVisible(not self.rows)
        self.drop.set_compact(bool(self.rows))
        self.clear_button.setEnabled(DONE in states)
        self.moox_button.setEnabled(ready > 0)
        if ready == 0:
            self.moox_button.setText("Mooxing…" if busy else "Moox")
        elif only_failed and not busy:
            self.moox_button.setText("Try again")
        elif busy:
            self.moox_button.setText(f"Moox {ready} more")
        else:
            self.moox_button.setText("Moox" if ready == 1 else f"Moox all {ready}")

    # -- where tracks go ---------------------------------------------------

    def _services_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        group = QActionGroup(menu)
        self.service_actions = {}
        headings = {services.FOLDER: "Added for you", services.UPLOAD: "Opens the upload page"}
        last_kind = None
        for service in services.SERVICES.values():
            if service.kind != last_kind:
                if last_kind is not None:
                    menu.addSeparator()
                heading = menu.addAction(headings[service.kind])
                heading.setEnabled(False)
                last_kind = service.kind
            action = menu.addAction(service.name)
            logo = service_pixmap(service.key, 16)
            if logo is not None:
                action.setIcon(QIcon(logo))
            action.setCheckable(True)
            action.setToolTip(service.hint)
            action.triggered.connect(lambda _=False, k=service.key: self.choose_service(k))
            group.addAction(action)
            self.service_actions[service.key] = action
        return menu

    def choose_service(self, key: str) -> None:
        self.service = services.get(key)
        self.settings.setValue("send_service", self.service.key)
        self._show_service()

    def _show_service(self) -> None:
        logo = service_pixmap(self.service.key, 20)
        if logo is not None:
            self.send_logo.setPixmap(logo)
            self.send_logo.setFixedSize(20, 20)
        else:  # logo file missing: fall back to the name
            self.send_logo.setText(self.service.name)
            self.send_logo.setMinimumSize(0, 0)
            self.send_logo.setMaximumSize(16777215, 16777215)
        self.send_button.setMinimumWidth(self.send_button.layout().sizeHint().width())
        self.send_button.setAccessibleName(f"to {self.service.name}")
        self.send_button.setToolTip(f"Sending to {self.service.name}. {self.service.hint}.\nClick to pick another app.")
        # With logos in the menu Qt draws no tick, so mark the choice by hand.
        for key, action in self.service_actions.items():
            chosen = key == self.service.key
            action.setChecked(chosen)
            action.setText(services.get(key).name + ("   ✓" if chosen else ""))
            font = action.font()
            font.setBold(chosen)
            action.setFont(font)

    def _note_upload(self, row_id: int, problem: str) -> None:
        row = self.rows.get(row_id)
        if row and not problem and row.service.kind == services.UPLOAD:
            self._to_upload = row

    def _open_upload(self) -> None:
        """When a run ends, open the upload page once (not once per track)."""
        if self._to_upload is None or (self.worker and self.worker.isRunning()):
            return  # more to come; the next run's end opens it
        row, self._to_upload = self._to_upload, None
        if row.row_id in self.rows:
            row.open_upload()

    # -- mooxed tracks -----------------------------------------------------

    def _draw_theme_art(self) -> None:
        """Redraw the logo and icons in the current theme's colours."""
        c = self.appearance.colours
        self.logo.set_parts(logo_parts(c, 46), c["accent"])
        gear = icon_pixmap("gear", c["text"], 16)
        if gear is not None:
            self.settings_button.setIcon(QIcon(gear))

    def _draw_send_arrow(self) -> None:
        """The dropdown's chevron, in the theme's text colour (▾ as a fallback)."""
        arrow = icon_pixmap("chevron-down", self.appearance.colours["text"], 14)
        if arrow is None:
            self.send_arrow.setText("▾")
        else:
            self.send_arrow.setPixmap(arrow)

    def _style_frame(self) -> None:
        """Windows border and title bar in the theme's colours instead of the accent colour."""
        frame.style(self, self.appearance.colours)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._style_frame()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # The Chrome extension's Settings write the same settings; show any
        # change it made while this window was in the background.
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.settings.sync()
            self.appearance.reload()
            self.spotify_page.refresh()
            self._show_folder()

    def show_settings(self) -> None:
        self.views.setCurrentWidget(self.settings_view)

    def hide_settings(self) -> None:
        """Back to the queue, from Settings or Mooxed files."""
        self.views.setCurrentWidget(self.main_view)

    def toggle_settings(self) -> None:
        if self.views.currentWidget() is self.settings_view:
            self.hide_settings()
        else:
            self.show_settings()

    def show_library(self) -> None:
        self.library.load(self.out_dir)
        self.views.setCurrentWidget(self.library)

    @property
    def spotify_dir(self) -> Path:
        """The folder Spotify watches, where finished tracks are copied."""
        return Path(self.settings.value("spotify_dir", str(library.DEFAULT_SPOTIFY_DIR)))

    def bring_forward(self) -> None:
        """Show the window on top, e.g. when Mooxr is opened again while already open."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    @property
    def out_dir(self) -> Path:
        """Where MP3s are saved: straight into the Spotify folder, unless another was picked."""
        chosen = self.settings.value("out_dir", "")
        return Path(chosen) if chosen else self.spotify_dir

    def _reload_library(self) -> None:
        # New tracks appear in an open list as soon as they're saved.
        if self.views.currentWidget() is self.library:
            self.library.load(self.out_dir)

    # -- output folder -----------------------------------------------------

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save MP3s to", str(self.out_dir))
        if folder:
            if library._same_folder(folder, self.spotify_dir):
                self.settings.remove("out_dir")  # back to following the Spotify folder
            else:
                self.settings.setValue("out_dir", folder)
            self._show_folder()
            self._reload_library()

    def open_folder(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir)))

    def _show_folder(self) -> None:
        self.folder_label.setText(str(self.out_dir))
        self.folder_label.setToolTip(str(self.out_dir))

    def _check_tools(self) -> bool:
        missing = tools.missing()
        if missing:
            self.banner.show_missing(missing)
            return False
        self.banner.hide()
        return True

    def _tools_installed(self) -> None:
        # Cover previews were waiting for FFmpeg too.
        for row in self.rows.values():
            row.art.refresh()
        # Anything the user already pressed Moox on while FFmpeg was missing
        # is still "ready"; start it now.
        if self._wants_start:
            self.start()


    def closeEvent(self, event) -> None:
        busy = [row for row in self.rows.values() if row.state in (WAITING, WORKING)]
        if busy and self.worker and self.worker.isRunning():
            count = "a track" if len(busy) == 1 else f"{len(busy)} tracks"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Mooxr")
            box.setText(f"Mooxr is still working on {count}.")
            box.setInformativeText("Stop now and quit? Tracks already finished are kept.")
            stop = box.addButton("Stop and quit", QMessageBox.ButtonRole.DestructiveRole)
            keep = box.addButton("Keep mooxing", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is not stop:
                event.ignore()
                return
        self.banner.cancel()
        art.close()
        if self.worker and self.worker.isRunning():
            # Drop anything not started and stop the current file; moox()
            # cleans up after itself, so no half-written file is left behind.
            while not self.jobs.empty():
                try:
                    self.jobs.get_nowait()
                except queue.Empty:
                    break
            self.cancelled_jobs.update(row.job_id for row in busy)
            self.setWindowTitle("Mooxr - stopping…")
            self.worker.wait()
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        # Own taskbar icon/group instead of python.exe's.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Mooxr.Mooxr")
        except Exception:
            pass
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Mooxr")
    icon = Path(__file__).resolve().parent / "assets" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyle("Fusion")
    # Nunito: rounded like the logo's lettering. Fall back to the system font if it won't load.
    nunito = QFontDatabase.addApplicationFont(str(Path(__file__).resolve().parent / "assets" / "fonts" / "Nunito.ttf"))
    if nunito != -1:
        family = "Nunito"
    else:
        family = "Segoe UI" if sys.platform == "win32" else app.font().family()
    font = QFont(family)
    font.setPointSize(10)
    app.setFont(font)

    # Files passed on the command line (e.g. "Open with" or drag onto a shortcut).
    args = (sys.argv if argv is None else argv)[1:]
    sources = [a if is_link(a) else str(Path(a).resolve()) for a in args]
    # Already open? Hand the sources to that window and bring it forward instead.
    if _hand_to_open_window(sources):
        return 0

    appearance = Appearance(app, QSettings("Mooxr", "Mooxr"))
    window = Window(appearance)
    window.show()
    _listen_for_more(window)
    if sources:
        window.add_sources(sources)
    return app.exec()


# One Mooxr window per user: a second launch (from the Chrome extension, a
# shortcut or "Open with") knocks on the open one rather than opening another.
_INSTANCE = f"Mooxr-{getpass.getuser()}"


def _hand_to_open_window(sources: List[str]) -> bool:
    """Send ``sources`` to a Mooxr window that's already open. False if there isn't one."""
    socket = QLocalSocket()
    socket.connectToServer(_INSTANCE)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        # Windows only lets the app in front pass the focus on; this launch has it.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
    socket.write(json.dumps(sources).encode("utf-8"))
    socket.waitForBytesWritten(1000)
    socket.disconnectFromServer()
    return True


def _listen_for_more(window: "Window") -> None:
    server = QLocalServer(window)
    QLocalServer.removeServer(_INSTANCE)  # left behind if Mooxr crashed (macOS / Linux)
    if not server.listen(_INSTANCE):
        return  # can't listen: later launches just open their own window

    def knocked() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            data = b""
            while conn.waitForReadyRead(300):
                data += bytes(conn.readAll())
            conn.deleteLater()
            try:
                sources = [str(s) for s in json.loads(data.decode("utf-8") or "[]")]
            except ValueError:
                sources = []
            window.bring_forward()
            if sources:
                window.add_sources(sources)

    server.newConnection.connect(knocked)


if __name__ == "__main__":
    raise SystemExit(main())
