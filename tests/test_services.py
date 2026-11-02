"""Tests for handing finished tracks to Spotify, Apple Music and the upload-only apps."""

from pathlib import Path

import pytest

from mooxr import services
from mooxr.core import MooxError


def _track(tmp_path: Path) -> Path:
    track = tmp_path / "out" / "Band - Song.mp3"
    track.parent.mkdir()
    track.write_bytes(b"not really an mp3")
    return track


def test_unknown_or_missing_choice_falls_back_to_spotify():
    assert services.get(None).key == "spotify"
    assert services.get("tidal").key == "spotify"
    assert services.get("deezer").name == "Deezer"


def test_upload_services_have_a_page_to_open():
    for service in services.SERVICES.values():
        assert (service.kind == services.UPLOAD) == bool(service.upload_url)


def test_spotify_gets_a_copy(tmp_path):
    spotify = tmp_path / "Spotify"
    services.deliver(_track(tmp_path), "spotify", spotify)
    assert (spotify / "Band - Song.mp3").is_file()


def test_upload_services_copy_nothing(tmp_path):
    spotify = tmp_path / "Spotify"
    services.deliver(_track(tmp_path), "soundcloud", spotify)
    assert not spotify.exists()


def test_apple_music_folder_is_found_and_preferred_over_itunes(tmp_path):
    itunes = tmp_path / "iTunes" / "iTunes Media" / "Automatically Add to iTunes"
    apple = tmp_path / "Apple Music" / "Media" / "Automatically Add to Music"
    itunes.mkdir(parents=True)
    assert services.find_apple_music_dir(tmp_path) == itunes
    apple.mkdir(parents=True)
    assert services.find_apple_music_dir(tmp_path) == apple


def test_apple_music_gets_a_copy(tmp_path, monkeypatch):
    folder = tmp_path / "Automatically Add to Music"
    folder.mkdir()
    monkeypatch.setattr(services, "find_apple_music_dir", lambda: folder)
    services.deliver(_track(tmp_path), "apple_music", tmp_path / "Spotify")
    assert (folder / "Band - Song.mp3").is_file()


def test_missing_apple_music_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "find_apple_music_dir", lambda: None)
    with pytest.raises(MooxError, match="Apple Music"):
        services.deliver(_track(tmp_path), "apple_music", tmp_path / "Spotify")
