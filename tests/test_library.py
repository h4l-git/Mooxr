"""Tests for listing mooxed tracks, editing their tags and sending them to the Spotify folder."""

import shutil
import subprocess

import pytest
from mutagen.id3 import ID3

from mooxr import MooxError, moox, tools
from mooxr.library import (
    already_sent,
    delete_track,
    edit_track,
    list_tracks,
    mooxed_index,
    resend,
    send_to,
    source_key,
)


@pytest.fixture(scope="module")
def mooxed(tmp_path_factory):
    """A folder with two mooxed tracks and some things that aren't."""
    work = tmp_path_factory.mktemp("work")
    out = tmp_path_factory.mktemp("mooxed")
    for name in ("Band A - One", "Band B - Two"):
        wav = work / f"{name}.wav"
        subprocess.run(
            [tools.find("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(wav)],
            check=True,
        )
        moox(wav, out)
    # An MP3 Mooxr didn't make: copy one and strip its MOOXR_SOURCE tag.
    stranger = out / "Someone Else - Song.mp3"
    shutil.copyfile(out / "Band A - One.mp3", stranger)
    tags = ID3(stranger)
    tags.delall("TXXX:MOOXR_SOURCE")
    tags.save(stranger)
    (out / "broken.mp3").write_bytes(b"not an mp3")
    (out / "notes.txt").write_text("hello")
    return out


def test_lists_only_mooxed_tracks(mooxed):
    tracks = list_tracks(mooxed)
    assert sorted((t.artist, t.title) for t in tracks) == [("Band A", "One"), ("Band B", "Two")]
    assert all(t.duration == pytest.approx(2.0, abs=0.2) for t in tracks)
    assert all(t.source.endswith(".wav") for t in tracks)


def test_missing_folder_lists_nothing(tmp_path):
    assert list_tracks(tmp_path / "nope") == []


def test_send_copies_into_the_spotify_folder(mooxed, tmp_path):
    spotify = tmp_path / "Spotify"
    tracks = list_tracks(mooxed)
    progress = []

    sent = send_to([t.path for t in tracks], spotify, on_progress=lambda d, n: progress.append((d, n)))

    assert sorted(p.name for p in spotify.iterdir()) == ["Band A - One.mp3", "Band B - Two.mp3"]
    assert sent == [spotify / t.path.name for t in tracks]
    assert progress == [(1, 2), (2, 2)]
    assert all(already_sent(t, spotify) for t in tracks)
    assert all(t.path.is_file() for t in tracks)  # originals stay put


def test_sending_twice_does_not_duplicate(mooxed, tmp_path):
    track = list_tracks(mooxed)[0]
    send_to([track.path], tmp_path)
    send_to([track.path], tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [track.path.name]


def test_a_different_file_with_the_same_name_is_kept(mooxed, tmp_path):
    track = list_tracks(mooxed)[0]
    (tmp_path / track.path.name).write_bytes(b"something else")
    sent = send_to([track.path], tmp_path)
    assert sent == [tmp_path / f"{track.path.stem} (2).mp3"]
    assert (tmp_path / track.path.name).read_bytes() == b"something else"


def test_no_part_files_are_left(mooxed, tmp_path):
    send_to([t.path for t in list_tracks(mooxed)], tmp_path)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_spotify_folder_is_created(mooxed, tmp_path):
    dest = tmp_path / "a" / "b"
    send_to([list_tracks(mooxed)[0].path], dest)
    assert dest.is_dir()


def test_missing_track_is_explained(tmp_path):
    with pytest.raises(MooxError, match="moved or deleted"):
        send_to([tmp_path / "gone.mp3"], tmp_path / "Spotify")


def test_unusable_spotify_folder_is_explained(mooxed, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("in the way")
    with pytest.raises(MooxError, match="Spotify folder"):
        send_to([list_tracks(mooxed)[0].path], blocker / "Spotify")


# -- editing ----------------------------------------------------------------


@pytest.fixture
def one_track(mooxed, tmp_path):
    """A fresh copy of one mooxed track, so edits don't leak between tests."""
    folder = tmp_path / "edit"
    folder.mkdir()
    shutil.copyfile(mooxed / "Band A - One.mp3", folder / "Band A - One.mp3")
    return folder / "Band A - One.mp3"


def _red_png(path, size=700):
    subprocess.run(
        [tools.find("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=red:s={size}x{size // 2}", "-frames:v", "1", str(path)],
        check=True,
    )
    return path


def test_edit_changes_tags_and_renames(one_track):
    track = edit_track(one_track, title="  New   Song ", artist="Other Band")
    assert (track.title, track.artist) == ("New Song", "Other Band")
    assert track.path == one_track.parent / "Other Band - New Song.mp3"
    assert not one_track.exists()
    tags = ID3(track.path)
    assert str(tags["TALB"]) == "Mooxed"  # untouched tags stay
    assert "TXXX:MOOXR_SOURCE" in tags
    assert [p.name for p in one_track.parent.iterdir()] == [track.path.name]  # no .part left


def test_edit_without_artist_names_by_title(one_track):
    track = edit_track(one_track, title="Solo", artist="")
    assert track.artist == "Unknown Artist"
    assert track.path.name == "Solo.mp3"


def test_edit_keeps_the_name_when_it_still_fits(one_track):
    numbered = one_track.with_name("Band A - One (2).mp3")
    one_track.rename(numbered)
    track = edit_track(numbered, title="One", artist="Band A", cover=None)
    assert track.path == numbered


def test_edit_does_not_overwrite_another_track(one_track):
    (one_track.parent / "Band B - Two.mp3").write_bytes(b"someone else")
    track = edit_track(one_track, title="Two", artist="Band B")
    assert track.path.name == "Band B - Two (2).mp3"
    assert (one_track.parent / "Band B - Two.mp3").read_bytes() == b"someone else"


def test_edit_replaces_cover_with_a_square(one_track, tmp_path):
    track = edit_track(one_track, title="One", artist="Band A", cover=_red_png(tmp_path / "art.png"))
    pictures = ID3(track.path).getall("APIC")
    assert len(pictures) == 1 and pictures[0].mime == "image/jpeg"
    jpg = tmp_path / "out.jpg"
    jpg.write_bytes(pictures[0].data)
    size = subprocess.run(
        [tools.find("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(jpg)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert size == "350,350"
    assert track.has_cover


def test_edit_needs_a_title(one_track):
    with pytest.raises(MooxError, match="title"):
        edit_track(one_track, title="   ", artist="X")
    assert one_track.exists()


def test_edit_bad_cover_leaves_the_track_alone(one_track, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    before = one_track.read_bytes()
    with pytest.raises(MooxError):
        edit_track(one_track, title="Changed", artist="X", cover=bad)
    assert one_track.read_bytes() == before


# -- keeping the Spotify folder up to date after an edit ----------------------


def test_resend_swaps_a_renamed_copy(one_track, tmp_path):
    spotify = tmp_path / "Spotify"
    send_to([one_track], spotify)
    track = edit_track(one_track, title="Renamed", artist="Band A")
    landed = resend(track.path, spotify, replaces=one_track)
    assert landed == spotify / "Band A - Renamed.mp3"
    assert [p.name for p in spotify.iterdir()] == ["Band A - Renamed.mp3"]
    assert already_sent(track, spotify)


def test_resend_replaces_a_same_name_copy(one_track, tmp_path):
    spotify = tmp_path / "Spotify"
    send_to([one_track], spotify)
    track = edit_track(one_track, title="One", artist="Band A", cover=_red_png(tmp_path / "art.png"))
    resend(track.path, spotify, replaces=one_track)
    assert [p.name for p in spotify.iterdir()] == ["Band A - One.mp3"]
    assert already_sent(track, spotify)
    assert ID3(spotify / "Band A - One.mp3").getall("APIC")[0].data == ID3(track.path).getall("APIC")[0].data


def test_resend_leaves_other_files_alone(one_track, tmp_path):
    spotify = tmp_path / "Spotify"
    spotify.mkdir()
    (spotify / "Band A - One.mp3").write_bytes(b"not ours")
    track = edit_track(one_track, title="Renamed", artist="Band A")
    resend(track.path, spotify, replaces=one_track)
    assert sorted(p.name for p in spotify.iterdir()) == ["Band A - One.mp3", "Band A - Renamed.mp3"]
    assert (spotify / "Band A - One.mp3").read_bytes() == b"not ours"


def test_resend_when_spotify_watches_the_output_folder(one_track):
    track = edit_track(one_track, title="Renamed", artist="Band A")
    assert resend(track.path, track.path.parent, replaces=one_track) == track.path
    assert [p.name for p in track.path.parent.iterdir()] == ["Band A - Renamed.mp3"]


# -- spotting repeats -------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?t=42"),
        ("https://www.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.example.com/song/", "http://example.com/song"),
        (r"C:\Videos\My Band - Song.mp4", "My Band - Song.mp4"),
        ("/home/me/clip.MP4", "clip.mp4"),
    ],
)
def test_same_source_gives_the_same_key(a, b):
    assert source_key(a) == source_key(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/aaaaaaaaaaa"),
        ("https://example.com/one", "https://example.com/two"),
        ("one.mp4", "two.mp4"),
        ("https://example.com/clip.mp4", "clip.mp4"),
    ],
)
def test_different_sources_give_different_keys(a, b):
    assert source_key(a) != source_key(b)


def test_index_finds_tracks_by_source(mooxed):
    index = mooxed_index(mooxed)
    assert set(index) == {source_key("Band A - One.wav"), source_key("Band B - Two.wav")}
    assert index[source_key(r"D:\elsewhere\Band A - One.wav")].path == mooxed / "Band A - One.mp3"


def test_tracks_know_when_they_changed(mooxed):
    for track in list_tracks(mooxed):
        assert track.modified == pytest.approx(track.path.stat().st_mtime)


# -- deleting ---------------------------------------------------------------


def test_delete_removes_the_track_and_its_spotify_copy(one_track, tmp_path):
    spotify = tmp_path / "Spotify"
    send_to([one_track], spotify)
    removed = delete_track(one_track, spotify)
    assert removed == [one_track, spotify / one_track.name]
    assert not one_track.exists()
    assert list(spotify.iterdir()) == []


def test_delete_leaves_a_different_file_with_the_same_name(one_track, tmp_path):
    spotify = tmp_path / "Spotify"
    spotify.mkdir()
    (spotify / one_track.name).write_bytes(b"someone else's song")
    assert delete_track(one_track, spotify) == [one_track]
    assert (spotify / one_track.name).read_bytes() == b"someone else's song"


def test_delete_finds_a_copy_sent_under_another_name(one_track, tmp_path):
    # "Band A - One.mp3" in the Spotify folder is someone else's, so the copy became "(2)".
    spotify = tmp_path / "Spotify"
    spotify.mkdir()
    (spotify / one_track.name).write_bytes(b"someone else's song")
    copy = send_to([one_track], spotify)[0]
    assert copy.name == "Band A - One (2).mp3"
    assert already_sent(one_track, spotify)
    assert send_to([one_track], spotify) == [copy]  # not copied a third time

    assert delete_track(one_track, spotify) == [one_track, copy]
    assert sorted(p.name for p in spotify.iterdir()) == [one_track.name]


def test_delete_leaves_the_copy_of_a_twin_track(one_track, tmp_path):
    # Mooxed twice from the same source: each copy belongs to its own track.
    twin = one_track.with_name("Band A - One (2).mp3")
    shutil.copyfile(one_track, twin)
    spotify = tmp_path / "Spotify"
    send_to([twin], spotify)
    assert delete_track(one_track, spotify) == [one_track]
    assert (spotify / twin.name).is_file()


def test_delete_when_spotify_watches_the_output_folder(one_track):
    assert delete_track(one_track, one_track.parent) == [one_track]
    assert not one_track.exists()


def test_delete_uses_the_given_discard(one_track, tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    delete_track(one_track, None, discard=lambda p: shutil.move(str(p), str(bin_ / p.name)))
    assert not one_track.exists()
    assert (bin_ / one_track.name).is_file()


def test_delete_failure_is_explained_and_keeps_the_track(one_track):
    def refuse(path):
        raise PermissionError("in use")

    with pytest.raises(MooxError, match="couldn't delete"):
        delete_track(one_track, None, discard=refuse)
    assert one_track.is_file()


def test_delete_missing_track_is_explained(tmp_path):
    with pytest.raises(MooxError, match="Can't find"):
        delete_track(tmp_path / "gone.mp3")
