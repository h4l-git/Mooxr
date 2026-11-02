"""Finding FFmpeg, and fetching Mooxr's own copy when there isn't one.

Mooxr needs ffmpeg and ffprobe. It looks, in order:

    1. MOOXR_FFMPEG_DIR, if set
    2. an "ffmpeg" folder shipped next to Mooxr (for a packaged app)
    3. Mooxr's own copy, which install_ffmpeg() puts in the user's app data
    4. PATH

so a user never has to install FFmpeg themselves: the window offers to run
install_ffmpeg() when nothing is found.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

TOOLS = ("ffmpeg", "ffprobe")

# gyan.dev's "release essentials" Windows build: a stable release with MP3
# (libmp3lame) support. The link always points at the newest release and has
# a matching .sha256 file next to it.
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA256_URL = FFMPEG_URL + ".sha256"
FFMPEG_DOWNLOAD_MB = 115  # roughly; shown to the user before downloading

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Mooxr"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "mooxr"


# Where install_ffmpeg() puts Mooxr's own copy.
OWN_DIR = _data_dir() / "ffmpeg"


def _bundled_dir() -> Path:
    """The "ffmpeg" folder beside the app: next to the .exe when packaged,
    else in the project folder."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ffmpeg"
    return Path(__file__).resolve().parent.parent / "ffmpeg"


def search_dirs() -> List[Path]:
    """Folders searched before PATH, most preferred first."""
    dirs = []
    if os.environ.get("MOOXR_FFMPEG_DIR"):
        dirs.append(Path(os.environ["MOOXR_FFMPEG_DIR"]))
    dirs += [_bundled_dir(), OWN_DIR]
    return dirs


def find(name: str) -> Optional[str]:
    """Full path to ffmpeg or ffprobe, or None if Mooxr can't find it."""
    for folder in search_dirs():
        found = shutil.which(name, path=str(folder))
        if found:
            return found
    return shutil.which(name)


def missing() -> List[str]:
    return [name for name in TOOLS if find(name) is None]


def can_install() -> bool:
    """install_ffmpeg() only knows a Windows download."""
    return sys.platform == "win32"


def install_ffmpeg(
    on_progress: Optional[Callable[[float], None]] = None,
    url: str = FFMPEG_URL,
    sha256_url: Optional[str] = FFMPEG_SHA256_URL,
    dest: Optional[Path] = None,
) -> Path:
    """Download FFmpeg, check it, and unpack ffmpeg + ffprobe into ``dest``.

    ``on_progress(fraction)`` is called as the download goes. Returns the
    folder. Raises MooxError with a message fit to show the user. Nothing
    is left half-installed: the tools only appear in ``dest`` once both have
    been unpacked and run.
    """
    from .core import MooxError

    dest = Path(dest or OWN_DIR)
    report = on_progress or (lambda fraction: None)
    exe = ".exe" if sys.platform == "win32" else ""

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MooxError(f"Mooxr can't create {dest.parent} to keep FFmpeg in. {exc.strerror or exc}") from exc

    with tempfile.TemporaryDirectory(prefix="mooxr-ffmpeg-", dir=dest.parent) as work_name:
        work = Path(work_name)
        archive = work / "ffmpeg.zip"

        # Download, hashing as we go.
        digest = hashlib.sha256()
        try:
            expected = (_fetch_text(sha256_url).split() or [""])[0].lower() if sha256_url else None
            request = urllib.request.Request(url, headers={"User-Agent": "Mooxr"})
            with urllib.request.urlopen(request, timeout=30) as response, open(archive, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        report(min(done / total, 1.0))
        except OSError as exc:  # URLError, timeouts and disk errors are all OSErrors
            raise MooxError(
                "Mooxr couldn't download FFmpeg. Check your internet connection and try again. "
                f"({getattr(exc, 'reason', None) or exc})"
            ) from exc

        if expected and digest.hexdigest() != expected:
            raise MooxError("The FFmpeg download was damaged on the way (its checksum didn't match). Please try again.")

        # Unpack just the two programs Mooxr uses; the archive holds docs and
        # a third program (ffplay) Mooxr doesn't need.
        staged = work / "ffmpeg"
        staged.mkdir()
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = {Path(m).name.lower(): m for m in bundle.namelist() if not m.endswith("/")}
                for name in TOOLS:
                    member = members.get(f"{name}{exe}")
                    if not member:
                        raise MooxError(f"The FFmpeg download doesn't contain {name}{exe}, so Mooxr can't use it.")
                    with bundle.open(member) as src, open(staged / f"{name}{exe}", "wb") as out:
                        shutil.copyfileobj(src, out)
                    if not exe:
                        (staged / name).chmod(0o755)
        except zipfile.BadZipFile as exc:
            raise MooxError("The FFmpeg download wasn't a usable zip file. Please try again.") from exc

        for name in TOOLS:
            try:
                subprocess.run(
                    [str(staged / f"{name}{exe}"), "-version"],
                    check=True, capture_output=True, timeout=30, creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise MooxError(f"The downloaded {name} won't run on this computer.") from exc

        # Swap the new folder into place.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            os.replace(staged, dest)
        except OSError as exc:
            raise MooxError(f"Mooxr couldn't put FFmpeg in {dest}. {exc.strerror or exc}") from exc

    report(1.0)
    return dest


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mooxr"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(4096).decode("ascii", "replace")
