"""Mooxr conversion core.

One job: take a video file (or a link), pull the audio out, and leave a
correctly tagged MP3 in the Spotify folder, where Spotify's Local Files can
see it.

    from mooxr import moox
    result = moox("My Band - First Song.mp4")
    print(result.path)

The pipeline, in order:

    1. fetch   (links only) download the media with yt-dlp
    2. probe   ask ffprobe what is in the file
    3. tags    decide title / artist from what we know
    4. cover   make a square JPEG from an image or a video frame
    5. encode  ffmpeg -> MP3, written to a hidden ".part" file
    6. tag     write ID3v2.3 tags and cover art with mutagen
    7. save    rename the ".part" file to its final name, in one step

Step 7 is what keeps Spotify happy: a file only ever appears in the folder
complete and tagged, and Mooxr never moves or renames it afterwards.

Needs ffmpeg and ffprobe (see tools.py for where Mooxr looks, and how it
fetches its own copy) and the mutagen package. Links also need yt-dlp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TXXX
from mutagen.mp3 import MP3

from . import tools

DEFAULT_ALBUM = "Mooxed"
DEFAULT_OUT_DIR = Path.home() / "Music" / "Spotify"  # the folder Spotify watches
UNKNOWN_ARTIST = "Unknown Artist"

# Spotify's cover art spec (what it asks distributors for): a square JPEG or
# PNG in RGB, 3000 x 3000 px ideally and never under 640 x 640. Mooxr crops
# to a square and keeps the picture's own resolution, shrinking anything
# bigger than 3000 px. It never blows small pictures up: that only adds blur.
SPOTIFY_ART_BEST = 3000  # pixels, square
SPOTIFY_ART_MIN = 640
SPOTIFY_ART_SPEC = (
    f"Spotify wants: square (1:1) · JPEG or PNG · RGB · "
    f"{SPOTIFY_ART_BEST} × {SPOTIFY_ART_BEST} px best, {SPOTIFY_ART_MIN} × {SPOTIFY_ART_MIN} px minimum"
)

# on_progress(stage, fraction): stage is "fetching" or "mooxing", fraction 0..1
ProgressFn = Callable[[str, float], None]
# cancelled() -> True to stop a moox part way through
CancelFn = Callable[[], bool]

# Stops a console window flashing up for every ffmpeg call on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MooxError(Exception):
    """Something went wrong that the user can understand and act on.

    The message is written to be shown as-is in the app.
    """


class MooxCancelled(MooxError):
    """moox() stopped because its ``cancelled`` callback said so.

    Nothing is left behind: no download, no half-written file.
    """


@dataclass
class Moox:
    """One finished conversion."""

    path: Path
    title: str
    artist: str
    album: str
    duration: float  # seconds
    has_cover: bool
    source: str  # original file name, or the link


@dataclass
class SourceInfo:
    """What ffprobe found in the input."""

    duration: float = 0.0
    has_audio: bool = False
    audio_channels: int = 0
    has_video: bool = False
    video_is_still: bool = False  # e.g. cover art embedded in an audio file
    tags: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def moox(
    source: Union[str, Path],
    out_dir: Union[str, Path] = DEFAULT_OUT_DIR,
    *,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: str = DEFAULT_ALBUM,
    cover: Union[str, Path, None] = None,
    bitrate: str = "320k",
    overwrite: bool = False,
    on_progress: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> Moox:
    """Convert one video file or link into a tagged MP3 in ``out_dir``.

    ``title`` / ``artist`` win if given. Otherwise Mooxr uses, in order: the
    link's own metadata, tags already inside the file, then the file name
    ("Artist - Title.mp4").

    ``cover`` is an image file to use as artwork. Without it, Mooxr uses the
    link's thumbnail or grabs a frame from the video.

    If "Artist - Title.mp3" already exists the new file is saved as
    "Artist - Title (2).mp3", unless ``overwrite`` is true.

    ``cancelled`` is asked every so often while it works; once it returns
    True, moox() cleans up and raises MooxCancelled.

    Raises MooxError with a message fit to show the user.
    """
    _require_tools()
    report = on_progress or (lambda stage, fraction: None)
    stop = cancelled or (lambda: False)

    out_dir = Path(out_dir).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MooxError(f"Mooxr can't use {out_dir} as its output folder. {exc.strerror or exc}") from exc

    with tempfile.TemporaryDirectory(prefix="mooxr-") as work_name:
        work = Path(work_name)

        # 1. fetch
        hint_title = hint_artist = None
        thumbnail: Optional[Path] = None
        if is_link(source):
            link = str(source).strip()
            media, hint_title, hint_artist, thumbnail = fetch_link(link, work, report, stop)
            origin = link
        else:
            media = Path(source).expanduser()
            if not media.is_file():
                raise MooxError(f"Can't find a file at {media}")
            origin = media.name

        # 2. probe
        _check(stop)
        info = probe(media)
        if not info.has_audio:
            raise MooxError(f"There is no audio track in {origin}, so there is nothing to moox.")

        # 3. tags
        final_title, final_artist = _decide_tags(
            title, artist, hint_title, hint_artist, info.tags, media.stem
        )
        final_album = _clean(album) or DEFAULT_ALBUM

        # 4. cover
        cover_jpg = work / "cover.jpg"
        has_cover = _prepare_cover(cover, thumbnail, media, info, cover_jpg)
        _check(stop)

        # 5-7. encode, tag, save
        part = out_dir / f".mooxr-{uuid.uuid4().hex}.part"
        try:
            _encode(media, part, info, bitrate, report, stop)
            _write_tags(
                part,
                title=final_title,
                artist=final_artist or UNKNOWN_ARTIST,
                album=final_album,
                cover_jpg=cover_jpg if has_cover else None,
                origin=origin,
            )
            duration = MP3(part).info.length
            _check(stop)  # last chance: after this the file is in place

            stem = f"{final_artist} - {final_title}" if final_artist else final_title
            stem = safe_filename(stem)
            final = out_dir / f"{stem}.mp3" if overwrite else _free_path(out_dir, stem)
            try:
                os.replace(part, final)
            except OSError as exc:
                # On Windows this is what happens when the file is playing.
                raise MooxError(
                    f"Mooxr couldn't save {final.name}. If it is open in another app, close it and try again."
                ) from exc
        finally:
            if part.exists():
                part.unlink()

    report("mooxing", 1.0)
    return Moox(
        path=final,
        title=final_title,
        artist=final_artist or UNKNOWN_ARTIST,
        album=final_album,
        duration=duration,
        has_cover=has_cover,
        source=origin,
    )


def _check(cancelled: CancelFn) -> None:
    if cancelled():
        raise MooxCancelled("Cancelled.")


def is_link(source: Union[str, Path]) -> bool:
    """True for http(s) links, false for anything that should be a file path."""
    return isinstance(source, str) and re.match(r"\s*https?://", source, re.I) is not None


# --------------------------------------------------------------------------
# Tags and file names
# --------------------------------------------------------------------------

# Bracketed clutter that video titles pick up: "(Official Video)", "[HD]",
# "(Lyrics)". Only whole bracket groups containing one of these words go, so
# "(feat. X)", "(Live)" and "(Acoustic)" survive.
_JUNK = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*\b"
    r"(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|1080p|720p|mv)"
    r"\b[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")  # " - ", en dash, em dash


def guess_tags(name: str) -> Tuple[Optional[str], str]:
    """Turn a file name or video title into (artist, title).

    "My Band - First Song (Official Video)" -> ("My Band", "First Song")
    "holiday_clip"                          -> (None, "holiday clip")

    Artist is None when there is no " - " to split on.
    """
    text = name.replace("_", " ")
    text = _JUNK.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—")
    if not text:  # the whole name was junk, e.g. "(Official Video)"
        text = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    parts = _DASH_SPLIT.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None, text


def _decide_tags(
    title: Optional[str],
    artist: Optional[str],
    hint_title: Optional[str],
    hint_artist: Optional[str],
    embedded: Dict[str, str],
    stem: str,
) -> Tuple[str, Optional[str]]:
    """Pick the title and artist. First non-empty source wins:
    explicit argument, link metadata, tags inside the file, file name."""
    guessed_artist, guessed_title = guess_tags(stem)
    final_title = (
        _clean(title)
        or _clean(hint_title)
        or _clean(embedded.get("title"))
        or guessed_title
        or "Untitled"
    )
    final_artist = (
        _clean(artist)
        or _clean(hint_artist)
        or _clean(embedded.get("artist"))
        or _clean(embedded.get("album_artist"))
        or guessed_artist
    )
    return final_title, final_artist


def _clean(text: Optional[str]) -> Optional[str]:
    """Collapse whitespace; treat blank as missing (an empty text box)."""
    if text is None:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text or None


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}


def safe_filename(text: str, max_len: int = 120) -> str:
    """Make text safe as a file name on Windows, macOS and Linux."""
    text = _BAD_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:max_len].rstrip(" .")
    if not text:
        text = "untitled"
    if text.upper() in _RESERVED:
        text += "_"
    return text


def _free_path(folder: Path, stem: str) -> Path:
    """First of "stem.mp3", "stem (2).mp3", "stem (3).mp3"... not yet taken."""
    candidate = folder / f"{stem}.mp3"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}).mp3"
        number += 1
    return candidate


def _write_tags(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    cover_jpg: Optional[Path],
    origin: str,
) -> None:
    """Replace whatever tags ffmpeg wrote with ours, as ID3v2.3.

    v2.3 rather than v2.4 because it is the version every player reads,
    Spotify's local files included.
    """
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TPE1(encoding=3, text=[artist]))
    audio.tags.add(TALB(encoding=3, text=[album]))
    # Where this track came from, so the app can later spot repeats.
    audio.tags.add(TXXX(encoding=3, desc="MOOXR_SOURCE", text=[origin]))
    if cover_jpg is not None:
        audio.tags.add(
            APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_jpg.read_bytes())
        )
    audio.save(v2_version=3, v1=0)


# --------------------------------------------------------------------------
# ffmpeg / ffprobe
# --------------------------------------------------------------------------


def _tool(name: str) -> Optional[str]:
    return tools.find(name)


def _require_tools() -> None:
    missing = tools.missing()
    if missing:
        fix = (
            "Mooxr can download it for you."
            if tools.can_install()
            else "Install FFmpeg from https://ffmpeg.org/download.html and try again."
        )
        raise MooxError(f"Mooxr needs {' and '.join(missing)} (part of FFmpeg) to convert audio, and can't find it. {fix}")


def probe(path: Path) -> SourceInfo:
    """Ask ffprobe what streams and tags a media file has."""
    done = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    if done.returncode != 0:
        raise MooxError(f"Mooxr can't read {path.name} as audio or video.")
    data = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")

    info = SourceInfo()
    container = data.get("format", {})
    try:
        info.duration = float(container.get("duration", 0) or 0)
    except ValueError:
        info.duration = 0.0

    tags: Dict[str, str] = {}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_channels = int(stream.get("channels", 0) or 0)
            tags.update(stream.get("tags", {}))  # ogg/opus keep tags here
        elif kind == "video" and not info.has_video:
            info.has_video = True
            info.video_is_still = stream.get("disposition", {}).get("attached_pic") == 1
    tags.update(container.get("tags", {}))
    info.tags = {str(key).lower(): str(value) for key, value in tags.items()}
    return info


def _encode(
    media: Path,
    dest: Path,
    info: SourceInfo,
    bitrate: str,
    report: ProgressFn,
    cancelled: CancelFn = lambda: False,
) -> None:
    """Run ffmpeg: first audio track -> 44.1 kHz MP3 at ``dest``."""
    cmd: List[str] = [
        _tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(media),
        "-map", "0:a:0",                 # first audio track only
        "-vn", "-sn", "-dn",             # no video, subtitles or data
        "-map_metadata", "-1",           # drop source tags; we write our own
        "-map_chapters", "-1",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-ar", "44100",
    ]
    if info.audio_channels > 2:
        cmd += ["-ac", "2"]              # fold surround down to stereo
    cmd += [
        "-f", "mp3",                     # needed: dest ends in .part, not .mp3
        "-progress", "pipe:1", "-nostats",
        str(dest),
    ]

    # stderr goes to a temp file so a chatty ffmpeg can never fill a pipe and hang.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=errors,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
        assert process.stdout is not None
        # ffmpeg writes a progress block about twice a second, so checking
        # once per line stops it promptly.
        for line in process.stdout:
            if cancelled():
                process.kill()
                process.wait()
                raise MooxCancelled("Cancelled.")
            key, _, value = line.strip().partition("=")
            # Both keys are microseconds, despite the "ms" name.
            if key in ("out_time_us", "out_time_ms") and value.isdigit() and info.duration > 0:
                report("mooxing", min(int(value) / 1_000_000 / info.duration, 0.99))
        if process.wait() != 0:
            errors.seek(0)
            detail = errors.read().decode("utf-8", errors="replace").strip().splitlines()[-3:]
            raise MooxError("ffmpeg couldn't convert this file: " + " | ".join(detail))


def _prepare_cover(
    cover: Union[str, Path, None],
    thumbnail: Optional[Path],
    media: Path,
    info: SourceInfo,
    dest: Path,
) -> bool:
    """Make the cover JPEG. Returns False if there is nothing to make one from.

    Artwork is a nice-to-have, so apart from a cover file the user named that
    doesn't exist, failures here are swallowed rather than failing the moox.
    """
    if cover is not None:
        cover = Path(cover).expanduser()
        if not cover.is_file():
            raise MooxError(f"Can't find the cover image at {cover}")
        if _square_jpeg(cover, dest):
            return True
        raise MooxError(f"Mooxr can't read {cover.name} as an image.")

    if thumbnail is not None and _square_jpeg(thumbnail, dest):
        return True

    if info.has_video:
        if info.video_is_still:
            return _square_jpeg(media, dest)
        # Skip a little way in, then let ffmpeg pick the most representative
        # of the next 50 frames. Avoids the black frame most videos open on.
        seek = min(info.duration * 0.1, 30.0)
        return _square_jpeg(media, dest, seek=seek, pick_frame=True) or _square_jpeg(media, dest)

    return False


def preview_cover(
    source: Union[str, Path],
    dest: Union[str, Path],
    cover: Union[str, Path, None] = None,
    *,
    on_tags: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
) -> Optional[str]:
    """Make the cover moox() would embed, without converting anything.

    Writes a square JPEG to ``dest`` and says where it came from ("your
    image", "the link's thumbnail", ...), or returns None when there is
    nothing to make one from yet. Links only fetch their thumbnail, not the
    media, so a link with no thumbnail returns None even though moox() may
    still take a frame from the video once it's downloaded.

    For a link, ``on_tags(title, artist)`` is called with what moox() would
    probably name the track, from the same lookup that finds the thumbnail.

    Raises MooxError for a ``cover`` that is missing or not an image.
    """
    _require_tools()
    dest = Path(dest)
    if cover is not None:
        _prepare_cover(cover, None, Path(), SourceInfo(), dest)
        return "your image"
    if is_link(source):
        with tempfile.TemporaryDirectory(prefix="mooxr-art-") as work_name:
            thumbnail, title, artist = _link_preview(str(source).strip(), Path(work_name))
            if on_tags and (title or artist):
                on_tags(title, artist)
            if thumbnail is not None and _square_jpeg(thumbnail, dest):
                return "the link's thumbnail"
        return None
    media = Path(source).expanduser()
    if not media.is_file():
        return None
    try:
        info = probe(media)
    except MooxError:
        return None
    if not _prepare_cover(None, None, media, info, dest):
        return None
    return "the art inside the file" if info.video_is_still else "a frame of the video"


def check_art(width: int, height: int, fmt: str = "") -> Tuple[bool, str]:
    """Hold a picture up against Spotify's cover art spec.

    Returns (fits, text), e.g. (True, "720 × 720 JPEG · fits Spotify").
    Not square isn't a failure (Mooxr crops it); too small is.
    """
    side = min(width, height)
    fmt = fmt.upper().replace("JPG", "JPEG")
    label = f"{width} × {height}" + (f" {fmt}" if fmt else "")
    notes = []
    if width != height:
        notes.append(f"not square, so Mooxr crops the middle {min(side, SPOTIFY_ART_BEST)} × {min(side, SPOTIFY_ART_BEST)}")
    elif side > SPOTIFY_ART_BEST:
        notes.append(f"Mooxr shrinks it to {SPOTIFY_ART_BEST} × {SPOTIFY_ART_BEST}")
    if fmt and fmt not in ("JPEG", "PNG"):
        notes.append("Mooxr saves it as JPEG")
    if side < SPOTIFY_ART_MIN:
        notes.append(f"under Spotify's {SPOTIFY_ART_MIN} × {SPOTIFY_ART_MIN} minimum, so it may look blurry")
    elif not notes:
        notes.append("ideal for Spotify" if side >= SPOTIFY_ART_BEST else "fits Spotify")
    return side >= SPOTIFY_ART_MIN, f"{label} · " + "; ".join(notes)


def _square_jpeg(src: Path, dest: Path, seek: float = 0.0, pick_frame: bool = False) -> bool:
    """Centre-crop the first picture in ``src`` to a square JPEG."""
    best = SPOTIFY_ART_BEST
    filters = f"crop='min(iw,ih)':'min(iw,ih)',scale='min(iw,{best})':'min(ih,{best})'"
    if pick_frame:
        filters = "thumbnail=50," + filters
    cmd = [_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += [
        "-i", str(src), "-map", "0:v:0", "-frames:v", "1",
        "-vf", filters, "-c:v", "mjpeg", "-q:v", "3", "-update", "1", "-f", "image2",
        str(dest),
    ]
    done = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    return done.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def fetch_link(
    url: str,
    work: Path,
    report: Optional[ProgressFn] = None,
    cancelled: Optional[CancelFn] = None,
) -> Tuple[Path, Optional[str], Optional[str], Optional[Path]]:
    """Download a link's audio into ``work``.

    Returns (media file, title, artist, thumbnail file). Any of the last
    three can be None. Raises MooxCancelled if ``cancelled`` says to stop.
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise MooxError(
            "Links need the yt-dlp package. Install it with: pip install yt-dlp"
        ) from exc

    stop = cancelled or (lambda: False)

    def hook(status: dict) -> None:
        if stop():
            raise yt_dlp.utils.DownloadCancelled()
        if report and status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                report("fetching", min(status.get("downloaded_bytes", 0) / total, 1.0))

    options = {
        "format": "bestaudio/best",
        "outtmpl": {
            "default": str(work / "source.%(ext)s"),
            "thumbnail": str(work / "thumb.%(ext)s"),
        },
        "writethumbnail": True,
        "noplaylist": True,       # a video inside a playlist: just the video
        "playlist_items": "1",    # a bare playlist link: just its first video
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            data = downloader.extract_info(url, download=True)
            if data.get("_type") == "playlist":
                entries = [entry for entry in data.get("entries") or [] if entry]
                if not entries:
                    raise MooxError("Mooxr found nothing to download at that link.")
                data = entries[0]
            downloads = data.get("requested_downloads") or []
            media = Path(downloads[0]["filepath"] if downloads else downloader.prepare_filename(data))
    except yt_dlp.utils.DownloadCancelled as exc:
        raise MooxCancelled("Cancelled.") from exc
    except yt_dlp.utils.DownloadError as exc:
        raise MooxError(f"Mooxr couldn't fetch that link. {exc}") from exc
    _check(stop)  # the lookup before the download can't be interrupted

    if not media.is_file():
        raise MooxError("Mooxr downloaded that link but can't find the result.")

    title, artist = _link_tags(data, media.stem)
    thumbnails = sorted(work.glob("thumb.*"))
    return media, title, artist, (thumbnails[0] if thumbnails else None)


def _link_tags(data: dict, fallback: str) -> Tuple[Optional[str], Optional[str]]:
    """(title, artist) from a link's metadata.

    Music sites give a proper track / artist; video sites give one title
    string, usually "Artist - Title", plus the uploader's name.
    """
    if data.get("track"):
        title, artist = data["track"], data.get("artist")
    else:
        artist, title = guess_tags(data.get("title") or fallback)
        artist = artist or data.get("artist")
    artist = artist or data.get("uploader") or data.get("channel")
    return _clean(title), _clean(artist)


def _link_preview(url: str, work: Path) -> Tuple[Optional[Path], Optional[str], Optional[str]]:
    """Fetch just a link's thumbnail into ``work``, without the media.

    Returns (thumbnail, title, artist); any can be None.
    """
    try:
        import yt_dlp
    except ImportError:
        return None, None, None
    options = {
        "skip_download": True,
        "writethumbnail": True,
        "outtmpl": {"default": str(work / "source.%(ext)s"), "thumbnail": str(work / "thumb.%(ext)s")},
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    title = artist = None
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            data = downloader.extract_info(url, download=True) or {}
        if data.get("_type") == "playlist":
            data = next((entry for entry in data.get("entries") or [] if entry), {})
        if data:
            title, artist = _link_tags(data, "")
    except Exception:  # a preview is a nice-to-have; moox() reports real errors
        pass
    thumbnails = sorted(work.glob("thumb.*"))
    return (thumbnails[0] if thumbnails else None), title, artist
