"""The tracks Mooxr has already made: listing them, changing their tags,
and sending them on to Spotify.

Spotify has no way for another app to hand it a local file: its Web API
can't add local files to a library or playlist. What it does do is watch
folders. Any MP3 in a folder listed under Settings -> Your Library ->
Local Files shows up in Spotify's "Local Files" by itself.

So "send to Spotify" means: copy the chosen tracks into that folder. Copies
appear there complete and tagged, the same way moox() saves (a hidden
".part" file renamed into place), so Spotify never sees half a file.

    from mooxr.library import list_tracks, send_to
    spotify = Path.home() / "Music" / "Spotify"  # where Mooxr saves by default
    tracks = list_tracks(spotify)
    send_to([t.path for t in list_tracks(other_save_folder)], spotify)  # only if Save to was changed
    edit_track(tracks[0].path, title="New Name", artist="Band", cover="art.png")
    delete_track(tracks[0].path, spotify)
    mooxed_index(folder).get(source_key("https://youtu.be/..."))  # mooxed before?
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

from mutagen import MutagenError
from mutagen.id3 import APIC, TIT2, TPE1
from mutagen.mp3 import MP3

from .core import (
    DEFAULT_OUT_DIR,
    UNKNOWN_ARTIST,
    MooxError,
    SourceInfo,
    _clean,
    _free_path,
    _prepare_cover,
    _require_tools,
    is_link,
    safe_filename,
)

DEFAULT_SPOTIFY_DIR = DEFAULT_OUT_DIR

# on_progress(done, total), called after each file
SendProgressFn = Callable[[int, int], None]


@dataclass
class Track:
    """One MP3 that Mooxr made."""

    path: Path
    title: str
    artist: str
    duration: float  # seconds
    has_cover: bool
    source: str  # what it was mooxed from
    modified: float = 0.0  # file's last change, as a timestamp


def list_tracks(folder: Union[str, Path]) -> List[Track]:
    """Every MP3 in ``folder`` that Mooxr made, newest first.

    Mooxr's own files are told apart from any other MP3s by the
    MOOXR_SOURCE tag it writes into every one.
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    tracks = []
    for path in folder.iterdir():
        if path.suffix.lower() != ".mp3" or path.name.startswith(".") or not path.is_file():
            continue
        track = _read(path)
        if track:
            tracks.append(track)
    tracks.sort(key=lambda t: t.modified, reverse=True)
    return tracks


# YouTube's many link shapes (watch?v=, youtu.be/, shorts/, music.youtube.com)
# all carry the same 11-character video id.
_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})",
    re.IGNORECASE,
)


def source_key(source: Union[str, Path]) -> str:
    """What two sources are compared by to spot a repeat.

    YouTube links compare by video id, so a youtu.be link matches the
    youtube.com one. Other links compare without "https://", "www." or a
    trailing slash. Files compare by file name alone, because that is all the
    MOOXR_SOURCE tag keeps for a file.
    """
    text = str(source).strip()
    if is_link(text):
        found = _YOUTUBE_ID.search(text)
        if found:
            return "youtube:" + found.group(1)
        text = re.sub(r"^https?://(www\.|m\.)?", "", text, flags=re.IGNORECASE)
        return "link:" + text.split("#")[0].rstrip("/").lower()
    return "file:" + Path(text).name.lower()


def mooxed_index(folder: Union[str, Path]) -> Dict[str, Track]:
    """Every mooxed track in ``folder``, keyed by source_key() of its source.

    If the same source was mooxed more than once, the newest track wins.
    """
    index: Dict[str, Track] = {}
    for track in list_tracks(folder):  # newest first
        if track.source:
            index.setdefault(source_key(track.source), track)
    return index


def _read(path: Path) -> Optional[Track]:
    try:
        audio = MP3(path)
    except (MutagenError, OSError):
        return None  # unreadable or still being written by someone else
    tags = audio.tags
    if tags is None or "TXXX:MOOXR_SOURCE" not in tags:
        return None

    def text(key: str) -> str:
        frame = tags.get(key)
        return str(frame.text[0]) if frame and frame.text else ""

    return Track(
        path=path,
        title=text("TIT2") or path.stem,
        artist=text("TPE1") or UNKNOWN_ARTIST,
        duration=audio.info.length,
        has_cover=bool(tags.getall("APIC")),
        source=text("TXXX:MOOXR_SOURCE"),
        modified=_modified(path),
    )


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def already_sent(track: Union[Track, Path], dest: Union[str, Path]) -> bool:
    """True if ``dest`` already holds an up-to-date copy of this track."""
    path = track.path if isinstance(track, Track) else Path(track)
    if _same_folder(path.parent, dest):
        return path.is_file()  # Spotify watches the track's own folder
    copy = sent_copy(path, dest)
    try:
        return copy is not None and copy.stat().st_size == path.stat().st_size
    except OSError:
        return False


def sent_copy(track: Union[str, Path], dest: Union[str, Path]) -> Optional[Path]:
    """This track's copy in ``dest`` (the Spotify folder), or None.

    Usually the copy has the track's own name. But send_to() names it
    "Song (2).mp3" when "Song.mp3" there is a different file, so failing
    that, look for an exact copy (same source, same size) under another name.
    A file named after another track in the track's own folder is that
    track's copy, so it's skipped.
    """
    path = Path(track).expanduser()
    dest = Path(dest).expanduser()
    if _same_folder(path.parent, dest):
        return None
    same_name = dest / path.name
    if _older_copy(same_name, path):
        return same_name
    try:
        size = path.stat().st_size
        candidates = [
            p for p in dest.iterdir()
            if p.suffix.lower() == ".mp3" and not p.name.startswith(".") and p.name != path.name
            and p.is_file() and p.stat().st_size == size  # cheap check before reading tags
        ]
    except OSError:
        return None
    for candidate in sorted(candidates):
        if not (path.parent / candidate.name).exists() and _older_copy(candidate, path):
            return candidate
    return None


def send_to(
    paths: Iterable[Union[str, Path]],
    dest: Union[str, Path] = DEFAULT_SPOTIFY_DIR,
    on_progress: Optional[SendProgressFn] = None,
) -> List[Path]:
    """Copy tracks into ``dest`` (the folder Spotify watches).

    Returns where each one ended up, in order. A track that is already
    there is left alone rather than copied twice; a different file that
    happens to share its name is kept, and the copy gets " (2)" added.

    Raises MooxError with a message fit to show the user.
    """
    paths = [Path(p).expanduser() for p in paths]
    dest = Path(dest).expanduser()
    report = on_progress or (lambda done, total: None)

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MooxError(f"Mooxr can't use {dest} as the Spotify folder. {exc.strerror or exc}") from exc

    sent: List[Path] = []
    for number, path in enumerate(paths, 1):
        if not path.is_file():
            raise MooxError(f"Can't find {path.name} any more. Has it been moved or deleted?")
        if path.parent.resolve() == dest.resolve():
            sent.append(dest / path.name)
            report(number, len(paths))
            continue
        if already_sent(path, dest):
            sent.append(sent_copy(path, dest) or dest / path.name)
            report(number, len(paths))
            continue

        part = dest / f".mooxr-{uuid.uuid4().hex}.part"
        try:
            shutil.copyfile(path, part)
            existing = dest / path.name
            # An older version of this same track (e.g. before its tags were
            # edited) is replaced; anything else with the name is kept.
            final = existing if _older_copy(existing, path) else _free_path(dest, path.stem)
            os.replace(part, final)
        except OSError as exc:
            raise MooxError(
                f"Mooxr couldn't copy {path.name} to {dest}. {exc.strerror or exc}"
            ) from exc
        finally:
            if part.exists():
                part.unlink()
        sent.append(final)
        report(number, len(paths))
    return sent


def edit_track(
    path: Union[str, Path],
    *,
    title: str,
    artist: Optional[str],
    cover: Union[str, Path, None] = None,
) -> Track:
    """Change a mooxed track's title, artist and (optionally) cover art.

    The file is renamed to match ("Artist - Title.mp3", like moox() names
    it). ``cover`` is an image file; it is centre-cropped to a square JPEG
    the same way moox() does it. Leave it None to keep the current art.

    Like moox(), the change is made on a hidden copy that replaces the
    original in one step, so Spotify never sees a half-written file.

    The Spotify folder isn't touched; call resend() afterwards for that.

    Returns the track as it now is. Raises MooxError with a message fit to
    show the user.
    """
    path = Path(path).expanduser()
    title = _clean(title)
    artist = _clean(artist)
    if not title:
        raise MooxError("A track needs a title.")
    if not path.is_file():
        raise MooxError(f"Can't find {path.name} any more. Has it been moved or deleted?")

    folder = path.parent
    stem = safe_filename(f"{artist} - {title}" if artist else title)
    # Keep "Song (2).mp3" as it is rather than renaming it to "Song (3).mp3".
    if re.fullmatch(re.escape(stem) + r"( \(\d+\))?", path.stem, flags=re.IGNORECASE):
        final = path
    else:
        final = _free_path(folder, stem)

    with tempfile.TemporaryDirectory(prefix="mooxr-edit-") as work_name:
        cover_jpg: Optional[Path] = None
        if cover is not None:
            _require_tools()  # cropping uses ffmpeg
            cover_jpg = Path(work_name) / "cover.jpg"
            _prepare_cover(cover, None, Path(), SourceInfo(), cover_jpg)

        part = folder / f".mooxr-{uuid.uuid4().hex}.part"
        try:
            shutil.copyfile(path, part)
            audio = MP3(part)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
            audio.tags.setall("TPE1", [TPE1(encoding=3, text=[artist or UNKNOWN_ARTIST])])
            if cover_jpg is not None:
                audio.tags.delall("APIC")
                audio.tags.add(
                    APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_jpg.read_bytes())
                )
            audio.save(v2_version=3, v1=0)
            os.replace(part, final)
        except (OSError, MutagenError) as exc:
            raise MooxError(
                f"Mooxr couldn't save the changes to {path.name}. If it is open in another app, "
                "close it and try again."
            ) from exc
        finally:
            if part.exists():
                part.unlink()

    if final != path:
        try:
            path.unlink()
        except OSError as exc:
            raise MooxError(
                f"Saved as {final.name}, but Mooxr couldn't remove the old {path.name}. "
                "If it is open in another app, close it and delete it yourself."
            ) from exc

    track = _read(final)
    if track is None:
        raise MooxError(f"Mooxr saved {final.name} but can't read it back.")
    return track


def resend(
    track: Union[str, Path],
    dest: Union[str, Path] = DEFAULT_SPOTIFY_DIR,
    replaces: Union[str, Path, None] = None,
) -> Path:
    """Send an edited track to ``dest`` in place of the copy sent before.

    ``replaces`` is the track's path before the edit. If the edit renamed it,
    the copy under the old name is removed so Spotify doesn't list both; a
    copy under the same name is replaced by send_to() itself.

    Returns where the track ended up. Raises MooxError with a message fit to
    show the user.
    """
    track = Path(track).expanduser()
    dest = Path(dest).expanduser()
    if replaces is not None and Path(replaces).name != track.name and not _same_folder(track.parent, dest):
        old = dest / Path(replaces).name
        if _older_copy(old, track):
            try:
                old.unlink()
            except OSError as exc:
                raise MooxError(
                    f"Mooxr couldn't remove the old copy, {old.name}, from your Spotify folder. If Spotify "
                    "is playing it, stop it and press Send to Spotify on this track."
                ) from exc
    return send_to([track], dest)[0]


def delete_track(
    path: Union[str, Path],
    spotify_dir: Union[str, Path, None] = None,
    discard: Optional[Callable[[Path], None]] = None,
) -> List[Path]:
    """Delete a mooxed track, and its copy in ``spotify_dir`` if there is one.

    The copy is found by sent_copy(), so one saved as "Song (2).mp3" goes
    too; a different file that happens to share its name is left alone.

    ``discard(path)`` does the deleting and should raise OSError if it can't.
    By default files are removed for good; the window passes one that moves
    them to the Recycle Bin instead.

    Returns the files removed. Raises MooxError with a message fit to show
    the user.
    """
    path = Path(path).expanduser()
    remove = discard or (lambda p: p.unlink())
    if not path.is_file():
        raise MooxError(f"Can't find {path.name} any more. Has it been moved or deleted?")

    # Find the copy first: telling it apart means reading this track.
    copy = sent_copy(path, spotify_dir) if spotify_dir is not None else None

    try:
        remove(path)
    except OSError as exc:
        raise MooxError(
            f"Mooxr couldn't delete {path.name}. If it is open in another app, close it and try again."
        ) from exc
    removed = [path]

    if copy is not None:
        try:
            remove(copy)
        except OSError as exc:
            raise MooxError(
                f"Deleted {path.name}, but Mooxr couldn't remove its copy from your Spotify folder. "
                "If Spotify is playing it, stop it and delete it from that folder yourself."
            ) from exc
        removed.append(copy)
    return removed


def _older_copy(copy: Path, track: Path) -> bool:
    """True if ``copy`` is a Mooxr track mooxed from the same source as ``track``."""
    if not copy.is_file():
        return False
    old, new = _read(copy), _read(track)
    return bool(old and new and old.source == new.source)


def _same_folder(a: Union[str, Path], b: Union[str, Path]) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False
