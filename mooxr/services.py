"""Where a finished track goes next: the music apps Mooxr can hand it to.

Only apps that accept your own MP3s are listed. Two kinds:

- "folder": the app watches a folder, so Mooxr copies the track in and the
  app picks it up by itself (Spotify, Apple Music).
- "upload": the app only takes files through its website, so Mooxr opens
  the upload page and shows the file, ready to drag in (YouTube Music,
  Deezer, SoundCloud).

Tidal and Amazon Music have no way to add your own files, so they're not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import library
from .core import MooxError

FOLDER, UPLOAD = "folder", "upload"


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    kind: str  # FOLDER or UPLOAD
    hint: str  # one line for the menu's tooltip
    upload_url: str = ""


SERVICES: Dict[str, Service] = {
    s.key: s
    for s in (
        Service("spotify", "Spotify", FOLDER,
                "Copied into your Spotify folder; it shows under Your Library → Local Files"),
        Service("apple_music", "Apple Music", FOLDER,
                "Copied into Apple Music's \"Automatically Add\" folder; it lands in your library"),
        Service("youtube_music", "YouTube Music", UPLOAD,
                "Opens YouTube Music; drag the file onto the page to upload it",
                "https://music.youtube.com/library/uploaded_songs"),
        Service("deezer", "Deezer", UPLOAD,
                "Opens Deezer; upload it under Favourites → My MP3s",
                "https://www.deezer.com/"),
        Service("soundcloud", "SoundCloud", UPLOAD,
                "Opens SoundCloud's upload page. Only upload music you have the rights to",
                "https://soundcloud.com/upload"),
    )
}
DEFAULT_SERVICE = "spotify"


def get(key: Optional[str]) -> Service:
    """The service for ``key``, or Spotify if it's unknown (e.g. an old setting)."""
    return SERVICES.get(key or "", SERVICES[DEFAULT_SERVICE])


def find_apple_music_dir(music: Optional[Path] = None) -> Optional[Path]:
    """Apple Music's (or iTunes') "Automatically Add to …" folder, if there is one.

    Its exact place depends on the app version and the media folder chosen in
    its settings, so look for it under the Music folder rather than guess.
    Apple Music's own folder wins over an old iTunes one.
    """
    music = music or Path.home() / "Music"
    if not music.is_dir():
        return None
    found = []
    for pattern in ("*/Automatically Add to *", "*/*/Automatically Add to *", "*/*/*/Automatically Add to *"):
        try:
            found += [p for p in music.glob(pattern) if p.is_dir()]
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=lambda p: ("itunes" in p.name.lower(), len(p.parts)))
    return found[0]


def deliver(track: Path, key: str, spotify_dir: Path) -> None:
    """Hand a finished track to the chosen service.

    Folder services get a copy now; upload services need the user to drag
    it into a web page, so there is nothing to do here.

    Raises MooxError with a message fit to show the user.
    """
    service = get(key)
    if service.kind == UPLOAD:
        return
    if service.key == "apple_music":
        folder = find_apple_music_dir()
        if folder is None:
            raise MooxError(
                "Mooxr can't find Apple Music's \"Automatically Add to Music\" folder. "
                "Open Apple Music (or iTunes) once so it makes the folder, then press Moox again."
            )
        library.send_to([track], folder)
    else:
        library.send_to([track], spotify_dir)
