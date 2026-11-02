"""The Settings page, shown inside the main window in place of the queue:
a list of sections on the left, the chosen one on the right.

To add a section later, build its page as a QWidget and call
`SettingsPage.add_section("Name", page)`.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import library
from .theme import DEFAULT_THEME, MODES, THEMES, Appearance, colours, logo_pixmap


class SettingsPage(QWidget):
    done = pyqtSignal()  # the user wants to go back to the main view

    def __init__(self, appearance: Appearance) -> None:
        super().__init__()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left: section list
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(180)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 18)
        side.setSpacing(12)
        heading = QLabel("Settings")
        heading.setObjectName("sidebarTitle")
        heading.setContentsMargins(6, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setCursor(Qt.CursorShape.PointingHandCursor)
        side.addWidget(heading)
        side.addWidget(self.nav, 1)
        root.addWidget(sidebar)

        # Right: the chosen section, with Done underneath
        right = QVBoxLayout()
        right.setContentsMargins(26, 20, 22, 18)
        right.setSpacing(12)
        self.pages = QStackedWidget()
        right.addWidget(self.pages, 1)
        done_row = QHBoxLayout()
        done_row.addStretch(1)
        done_button = QPushButton("Done")
        done_button.setObjectName("primary")
        done_button.setCursor(Qt.CursorShape.PointingHandCursor)
        done_button.clicked.connect(self.done)
        done_row.addWidget(done_button)
        right.addLayout(done_row)
        root.addLayout(right, 1)

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.add_section("Appearance", AppearancePage(appearance))
        self.nav.setCurrentRow(0)

    def add_section(self, name: str, page: QWidget) -> None:
        self.nav.addItem(name)
        self.pages.addWidget(page)


class AppearancePage(QWidget):
    def __init__(self, appearance: Appearance) -> None:
        super().__init__()
        self.appearance = appearance

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel("Appearance")
        title.setObjectName("pageTitle")
        intro = QLabel("Changes apply straight away.")
        intro.setProperty("muted", True)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addSpacing(14)

        # Mode: one pill bar with a segment per choice
        mode_label = QLabel("Mode")
        mode_label.setObjectName("settingLabel")
        layout.addWidget(mode_label)
        bar = QFrame()
        bar.setObjectName("segmentBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(3, 3, 3, 3)
        bar_layout.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for key, name in MODES.items():
            button = QPushButton(name)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: appearance.set_mode(k))
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
            bar_layout.addWidget(button)
        bar_row = QHBoxLayout()
        bar_row.addWidget(bar)
        bar_row.addStretch(1)
        layout.addLayout(bar_row)
        hint = QLabel("System follows the light or dark setting in Windows.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(16)

        # Colour theme: a card per theme showing the headphones in its colours
        theme_label = QLabel("Colour theme")
        theme_label.setObjectName("settingLabel")
        layout.addWidget(theme_label)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.theme_group = QButtonGroup(self)
        self.theme_buttons = {}
        self.theme_icons = {}
        for i, (key, theme) in enumerate(THEMES.items()):
            button = QPushButton()
            button.setObjectName("swatch")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(60)
            button.setAccessibleName(theme.name)
            button.clicked.connect(lambda _=False, k=key: appearance.set_theme(k))
            # Card contents: headphones icon, then the name with an optional caption under it.
            inner = QHBoxLayout(button)
            inner.setContentsMargins(14, 8, 14, 8)
            inner.setSpacing(10)
            icon = QLabel()
            icon.setFixedSize(40, 40)
            words = QVBoxLayout()
            words.setSpacing(0)
            labels = [icon, QLabel(theme.name)]
            labels[1].setObjectName("swatchName")
            words.addStretch(1)
            words.addWidget(labels[1])
            if key == DEFAULT_THEME:
                caption = QLabel("(Default)")
                caption.setObjectName("swatchCaption")
                labels.append(caption)
                words.addWidget(caption)
            words.addStretch(1)
            for label in labels:  # let clicks reach the card
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            inner.addWidget(icon)
            inner.addLayout(words, 1)
            self.theme_group.addButton(button)
            self.theme_buttons[key] = button
            self.theme_icons[key] = icon
            grid.addWidget(button, i // 3, i % 3)
        layout.addLayout(grid)
        layout.addStretch(1)

        appearance.changed.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        a = self.appearance
        self.mode_buttons[a.mode].setChecked(True)
        self.theme_buttons[a.theme_key].setChecked(True)
        dark = a.is_dark()
        for key, icon in self.theme_icons.items():
            pixmap = logo_pixmap(colours(THEMES[key], dark), 40, mark_only=True)
            if pixmap is not None:
                icon.setPixmap(pixmap)


class SpotifyPage(QWidget):
    """Where finished tracks are copied so Spotify lists them."""

    changed = pyqtSignal()  # the folder changed

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self.settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel("Spotify")
        title.setObjectName("pageTitle")
        intro = QLabel("Every track you moox is saved straight into this folder.")
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addSpacing(14)

        label = QLabel("Spotify folder")
        label.setObjectName("settingLabel")
        layout.addWidget(label)
        row = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        change = QPushButton("Change…")
        change.setCursor(Qt.CursorShape.PointingHandCursor)
        change.clicked.connect(self.choose_folder)
        open_folder = QPushButton("Open folder")
        open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        open_folder.clicked.connect(self.open_folder)
        row.addWidget(self.folder_label, 1)
        row.addWidget(change)
        row.addWidget(open_folder)
        layout.addLayout(row)
        layout.addSpacing(16)

        setup = QLabel("Set up Spotify once")
        setup.setObjectName("settingLabel")
        layout.addWidget(setup)
        steps = QLabel(
            "Spotify can't be handed songs by other apps; it watches folders instead. In Spotify: "
            "Settings → Your Library → turn on Show Local Files → Add a source → pick the folder above. "
            "Mooxed tracks then appear under Your Library → Local Files by themselves.\n\n"
            "If you pick a different Save to folder on the main screen, tracks are saved there and "
            "copied here. Add only this folder to Spotify, not that one as well, or every song shows up twice."
        )
        steps.setProperty("muted", True)
        steps.setWordWrap(True)
        layout.addWidget(steps)
        layout.addStretch(1)

        self._show()

    @property
    def folder(self) -> Path:
        return Path(self.settings.value("spotify_dir", str(library.DEFAULT_SPOTIFY_DIR)))

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "The folder Spotify watches", str(self.folder))
        if chosen:
            self.settings.setValue("spotify_dir", chosen)
            self._show()
            self.changed.emit()

    def open_folder(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def refresh(self) -> None:
        """Show the folder as saved now (it may have changed from Chrome)."""
        self._show()

    def _show(self) -> None:
        self.folder_label.setText(str(self.folder))
        self.folder_label.setToolTip(str(self.folder))
