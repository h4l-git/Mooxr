"""Tests for the Mooxr conversion core.

These run real conversions: ffmpeg generates small sample videos, Mooxr
converts them, and the results are read back with ffprobe and mutagen.

    pip install pytest
    pytest
"""

import functools
import http.server
import json
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from mooxr import MooxCancelled, MooxError, guess_tags, is_link, moox, safe_filename, tools
from mooxr.core import check_art, preview_cover


def ffmpeg(*args):
    subprocess.run([tools.find("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def ffprobe(path):
    out = subprocess.run(
        [tools.find("ffprobe") or "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(out)


def audio_stream(path):
    return next(s for s in ffprobe(path)["streams"] if s["codec_type"] == "audio")


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """A folder of small generated media files."""
    folder = tmp_path_factory.mktemp("samples")
    video = ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=4"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]

    ffmpeg(*video, *tone, "-ac", "2", *h264, "-c:a", "aac", "-shortest",
           str(folder / "My Band - First Song (Official Video).mp4"))
    ffmpeg(*video, *tone, "-ac", "2", *h264, "-c:a", "aac", "-shortest",
           str(folder / "holiday_clip.mp4"))
    ffmpeg(*video, *tone, "-ac", "2", *h264, "-c:a", "aac", "-shortest",
           "-metadata", "title=Tagged Title", "-metadata", "artist=Tagged Artist",
           str(folder / "tagged.mp4"))
    ffmpeg(*video, *h264, str(folder / "silent.mp4"))
    ffmpeg(*tone, str(folder / "Solo Act - Demo.wav"))
    ffmpeg(*video, *tone, "-ac", "6", *h264, "-c:a", "aac", "-shortest",
           str(folder / "surround.mp4"))
    ffmpeg("-f", "lavfi", "-i", "color=c=red:size=800x400", "-frames:v", "1", "-update", "1",
           str(folder / "art.png"))
    (folder / "notes.txt").write_text("not media")
    return folder


SONG = "My Band - First Song (Official Video).mp4"


# ---- the conversion itself ------------------------------------------------


def test_video_becomes_a_spotify_friendly_mp3(samples, tmp_path):
    result = moox(samples / SONG, tmp_path)

    assert result.path == tmp_path / "My Band - First Song.mp3"
    assert result.path.is_file()
    stream = audio_stream(result.path)
    assert stream["codec_name"] == "mp3"
    assert stream["sample_rate"] == "44100"
    assert stream["channels"] == 2
    assert int(stream["bit_rate"]) == 320_000
    assert result.duration == pytest.approx(4.0, abs=0.2)


def test_output_has_no_video_other_than_the_cover(samples, tmp_path):
    result = moox(samples / SONG, tmp_path)
    video = [s for s in ffprobe(result.path)["streams"] if s["codec_type"] == "video"]
    assert len(video) == 1
    assert video[0]["disposition"]["attached_pic"] == 1


def test_surround_is_folded_down_to_stereo(samples, tmp_path):
    result = moox(samples / "surround.mp4", tmp_path)
    assert audio_stream(result.path)["channels"] == 2


def test_audio_only_input_works_and_has_no_cover(samples, tmp_path):
    result = moox(samples / "Solo Act - Demo.wav", tmp_path)
    assert result.path.name == "Solo Act - Demo.mp3"
    assert result.has_cover is False
    assert not ID3(result.path).getall("APIC")


def test_bitrate_can_be_changed(samples, tmp_path):
    result = moox(samples / SONG, tmp_path, bitrate="192k")
    assert int(audio_stream(result.path)["bit_rate"]) == 192_000


# ---- tags -----------------------------------------------------------------


def test_tags_come_from_the_file_name(samples, tmp_path):
    result = moox(samples / SONG, tmp_path)
    tags = ID3(result.path)

    assert (result.title, result.artist, result.album) == ("First Song", "My Band", "Mooxed")
    assert tags["TIT2"].text == ["First Song"]
    assert tags["TPE1"].text == ["My Band"]
    assert tags["TALB"].text == ["Mooxed"]
    assert tags["TXXX:MOOXR_SOURCE"].text == [SONG]
    assert tags.version[:2] == (2, 3)


def test_given_tags_beat_everything_else(samples, tmp_path):
    result = moox(samples / "tagged.mp4", tmp_path, title="Chosen", artist="Me", album="Demos")
    tags = ID3(result.path)
    assert result.path.name == "Me - Chosen.mp3"
    assert (tags["TIT2"].text, tags["TPE1"].text, tags["TALB"].text) == (["Chosen"], ["Me"], ["Demos"])


def test_tags_inside_the_file_beat_the_file_name(samples, tmp_path):
    result = moox(samples / "tagged.mp4", tmp_path)
    assert (result.title, result.artist) == ("Tagged Title", "Tagged Artist")


def test_blank_text_boxes_count_as_not_given(samples, tmp_path):
    result = moox(samples / SONG, tmp_path, title="   ", artist="")
    assert (result.title, result.artist) == ("First Song", "My Band")


def test_no_artist_anywhere(samples, tmp_path):
    result = moox(samples / "holiday_clip.mp4", tmp_path)
    assert result.path.name == "holiday clip.mp3"
    assert result.artist == "Unknown Artist"
    assert ID3(result.path)["TPE1"].text == ["Unknown Artist"]


def test_non_english_tags_survive(samples, tmp_path):
    result = moox(samples / SONG, tmp_path, title="Café del Mar ☀", artist="Björk & 坂本")
    tags = ID3(result.path)
    assert tags["TIT2"].text == ["Café del Mar ☀"]
    assert tags["TPE1"].text == ["Björk & 坂本"]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("My Band - First Song", ("My Band", "First Song")),
        ("My Band - First Song (Official Video)", ("My Band", "First Song")),
        ("My Band - First Song [Official Music Video] [HD]", ("My Band", "First Song")),
        ("My Band – First Song (Lyrics)", ("My Band", "First Song")),
        ("My_Band_-_First_Song", ("My Band", "First Song")),
        ("My Band - First Song (feat. Someone)", ("My Band", "First Song (feat. Someone)")),
        ("My Band - First Song (Live)", ("My Band", "First Song (Live)")),
        ("My Band - Song - Part 2", ("My Band", "Song - Part 2")),
        ("Well-Known Tune", (None, "Well-Known Tune")),
        ("holiday_clip", (None, "holiday clip")),
        ("(Official Video)", (None, "(Official Video)")),
    ],
)
def test_guess_tags(name, expected):
    assert guess_tags(name) == expected


# ---- cover art ------------------------------------------------------------


def cover_of(path):
    pictures = ID3(path).getall("APIC")
    assert len(pictures) == 1
    return pictures[0]


def test_cover_is_grabbed_from_the_video(samples, tmp_path):
    result = moox(samples / SONG, tmp_path)
    picture = cover_of(result.path)

    assert result.has_cover is True
    assert picture.mime == "image/jpeg"
    assert picture.type == 3  # front cover
    assert picture.data[:2] == b"\xff\xd8"  # JPEG magic number
    art = tmp_path / "art.jpg"
    art.write_bytes(picture.data)
    stream = ffprobe(art)["streams"][0]
    assert (stream["width"], stream["height"]) == (720, 720)  # square, at the video's own resolution


def test_a_given_cover_image_is_used_and_squared(samples, tmp_path):
    result = moox(samples / "Solo Act - Demo.wav", tmp_path, cover=samples / "art.png")
    art = tmp_path / "art.jpg"
    art.write_bytes(cover_of(result.path).data)
    stream = ffprobe(art)["streams"][0]
    assert (stream["width"], stream["height"]) == (400, 400)  # never blown up


def test_huge_art_is_shrunk_to_spotifys_best_size(samples, tmp_path):
    big = tmp_path / "big.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=3600x3200", "-frames:v", "1", "-update", "1", str(big))
    result = moox(samples / "Solo Act - Demo.wav", tmp_path, cover=big)
    art = tmp_path / "art.jpg"
    art.write_bytes(cover_of(result.path).data)
    stream = ffprobe(art)["streams"][0]
    assert (stream["width"], stream["height"]) == (3000, 3000)


def test_preview_matches_what_gets_embedded(samples, tmp_path):
    dest = tmp_path / "preview.jpg"
    assert preview_cover(samples / SONG, dest) == "a frame of the video"
    assert (ffprobe(dest)["streams"][0]["width"], ffprobe(dest)["streams"][0]["height"]) == (720, 720)
    assert preview_cover(samples / "Solo Act - Demo.wav", tmp_path / "none.jpg") is None
    assert preview_cover(samples / "Solo Act - Demo.wav", tmp_path / "own.jpg", cover=samples / "art.png") == "your image"
    with pytest.raises(MooxError, match="image"):
        preview_cover(samples / SONG, tmp_path / "bad.jpg", cover=samples / "notes.txt")


@pytest.mark.parametrize(
    "size, fits, words",
    [
        ((3000, 3000, "jpeg"), True, "ideal for Spotify"),
        ((720, 720, "jpeg"), True, "fits Spotify"),
        ((640, 640, "png"), True, "fits Spotify"),
        ((1280, 720, "png"), True, "crops the middle 720 × 720"),
        ((400, 400, "jpeg"), False, "640 × 640 minimum"),
        ((4000, 4000, "jpeg"), True, "shrinks it to 3000 × 3000"),
        ((1000, 1000, "webp"), True, "saves it as JPEG"),
    ],
)
def test_check_art_against_spotifys_spec(size, fits, words):
    ok, text = check_art(*size)
    assert ok is fits
    assert words in text


def test_embedded_cover_is_carried_over(samples, tmp_path):
    first = moox(samples / SONG, tmp_path)  # an MP3 with artwork inside
    second = moox(first.path, tmp_path / "again")
    assert second.has_cover is True
    assert cover_of(second.path).data[:2] == b"\xff\xd8"


def test_missing_cover_file_is_an_error(samples, tmp_path):
    with pytest.raises(MooxError, match="cover"):
        moox(samples / SONG, tmp_path, cover=samples / "nope.png")


def test_unreadable_cover_file_is_an_error(samples, tmp_path):
    with pytest.raises(MooxError, match="image"):
        moox(samples / SONG, tmp_path, cover=samples / "notes.txt")


# ---- saving ---------------------------------------------------------------


def test_same_name_twice_keeps_both(samples, tmp_path):
    first = moox(samples / SONG, tmp_path)
    second = moox(samples / SONG, tmp_path)
    third = moox(samples / SONG, tmp_path)
    assert first.path.name == "My Band - First Song.mp3"
    assert second.path.name == "My Band - First Song (2).mp3"
    assert third.path.name == "My Band - First Song (3).mp3"
    assert first.path.is_file() and second.path.is_file() and third.path.is_file()


def test_overwrite_replaces_the_file(samples, tmp_path):
    moox(samples / SONG, tmp_path)
    again = moox(samples / SONG, tmp_path, overwrite=True)
    assert again.path.name == "My Band - First Song.mp3"
    assert [p.name for p in tmp_path.iterdir()] == ["My Band - First Song.mp3"]


def test_nothing_but_the_mp3_is_left_in_the_folder(samples, tmp_path):
    moox(samples / SONG, tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["My Band - First Song.mp3"]


def test_output_folder_is_created(samples, tmp_path):
    result = moox(samples / SONG, tmp_path / "Music" / "Mooxed")
    assert result.path.parent == tmp_path / "Music" / "Mooxed"


def test_awkward_characters_in_tags_make_a_safe_file_name(samples, tmp_path):
    result = moox(samples / SONG, tmp_path, title='What?! <Live> "2/2"', artist="AC/DC")
    assert result.path.name == "AC DC - What ! Live 2 2.mp3"
    assert ID3(result.path)["TIT2"].text == ['What?! <Live> "2/2"']  # tag keeps the real title


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AC/DC", "AC DC"),
        ("a:b*c?d", "a b c d"),
        ("  trailing dots... ", "trailing dots"),
        ("???", "untitled"),
        ("CON", "CON_"),
        ("x" * 300, "x" * 120),
    ],
)
def test_safe_filename(text, expected):
    assert safe_filename(text) == expected


# ---- things going wrong ---------------------------------------------------


def test_video_without_audio_is_refused_cleanly(samples, tmp_path):
    with pytest.raises(MooxError, match="no audio track"):
        moox(samples / "silent.mp4", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_file(tmp_path):
    with pytest.raises(MooxError, match="Can't find"):
        moox(tmp_path / "nope.mp4", tmp_path)


def test_file_that_is_not_media(samples, tmp_path):
    with pytest.raises(MooxError):
        moox(samples / "notes.txt", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_failed_encode_leaves_no_part_file(samples, tmp_path):
    with pytest.raises(MooxError, match="ffmpeg"):
        moox(samples / SONG, tmp_path, bitrate="not-a-bitrate")
    assert list(tmp_path.iterdir()) == []


def test_unusable_output_folder_is_explained(samples, tmp_path):
    blocker = tmp_path / "actually-a-file"
    blocker.write_text("x")
    with pytest.raises(MooxError, match="output folder"):
        moox(samples / SONG, blocker / "Mooxed")


def test_save_failure_is_explained_and_cleaned_up(samples, tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr("mooxr.core.os.replace", refuse)
    with pytest.raises(MooxError, match="couldn't save"):
        moox(samples / SONG, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_ffmpeg_is_explained(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("mooxr.tools.search_dirs", lambda: [])
    with pytest.raises(MooxError, match="FFmpeg"):
        moox(samples / SONG, tmp_path)


# ---- progress -------------------------------------------------------------


def test_progress_only_goes_forward_and_ends_at_one(samples, tmp_path):
    seen = []
    moox(samples / SONG, tmp_path, on_progress=lambda stage, fraction: seen.append((stage, fraction)))

    fractions = [fraction for _, fraction in seen]
    assert {stage for stage, _ in seen} == {"mooxing"}
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0
    assert all(0.0 <= fraction <= 1.0 for fraction in fractions)


# ---- links ----------------------------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        ("https://example.com/watch?v=1", True),
        ("http://example.com/a.mp4", True),
        ("  HTTPS://EXAMPLE.COM/a", True),
        ("C:\\Users\\me\\clip.mp4", False),
        ("/home/me/clip.mp4", False),
        ("https - the movie.mp4", False),
        (Path("clip.mp4"), False),
    ],
)
def test_is_link(source, expected):
    assert is_link(source) is expected


@pytest.fixture
def local_site(samples, monkeypatch):
    """Serve the samples folder over HTTP so the link path can be run for real."""
    pytest.importorskip("yt_dlp")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(samples)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_link_is_downloaded_and_mooxed(local_site, tmp_path):
    seen = []
    link = f"{local_site}/{quote(SONG)}"
    result = moox(link, tmp_path, on_progress=lambda stage, fraction: seen.append(stage))

    assert result.path == tmp_path / "My Band - First Song.mp3"
    assert (result.title, result.artist) == ("First Song", "My Band")
    assert result.source == link
    assert result.has_cover is True
    assert MP3(result.path).info.length == pytest.approx(4.0, abs=0.2)
    assert ID3(result.path)["TXXX:MOOXR_SOURCE"].text == [link]
    assert "fetching" in seen and "mooxing" in seen
    assert [p.name for p in tmp_path.iterdir()] == ["My Band - First Song.mp3"]


def test_dead_link_is_explained(local_site, tmp_path):
    with pytest.raises(MooxError, match="couldn't fetch"):
        moox(f"{local_site}/nothing-here.mp4", tmp_path)
    assert list(tmp_path.iterdir()) == []


# ---- cancelling -----------------------------------------------------------


def test_cancel_before_starting_leaves_nothing(samples, tmp_path):
    with pytest.raises(MooxCancelled):
        moox(samples / SONG, tmp_path, cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_cancel_part_way_through_the_encode(samples, tmp_path):
    seen = []
    with pytest.raises(MooxCancelled):
        moox(
            samples / SONG, tmp_path,
            on_progress=lambda stage, fraction: seen.append(fraction),
            cancelled=lambda: bool(seen),  # stop once the encode has started
        )
    assert seen and max(seen) < 1.0
    assert list(tmp_path.iterdir()) == []


def test_a_cancelled_moox_is_still_a_mooxerror(samples, tmp_path):
    with pytest.raises(MooxError, match="Cancelled"):
        moox(samples / SONG, tmp_path, cancelled=lambda: True)


def test_cancel_during_a_download(local_site, tmp_path):
    fetched = []
    with pytest.raises(MooxCancelled):
        moox(
            f"{local_site}/{quote(SONG)}", tmp_path,
            on_progress=lambda stage, fraction: fetched.append(stage),
            cancelled=lambda: "fetching" in fetched,
        )
    assert "mooxing" not in fetched
    assert list(tmp_path.iterdir()) == []


def test_link_preview_reports_the_title_and_artist(local_site, tmp_path):
    found = []
    preview_cover(f"{local_site}/{quote(SONG)}", tmp_path / "art.jpg",
                  on_tags=lambda title, artist: found.append((title, artist)))
    assert found == [("First Song", "My Band")]
