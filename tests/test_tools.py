"""Tests for finding FFmpeg and fetching Mooxr's own copy.

The download is served by a web server on this machine from a small fake
FFmpeg zip, so these need neither internet nor a real FFmpeg.
"""

import functools
import hashlib
import http.server
import sys
import threading
import zipfile

import pytest

from mooxr import MooxError, tools

EXE = ".exe" if sys.platform == "win32" else ""


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A folder served over HTTP; returns (folder, base url)."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "site"
    folder.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield folder, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def runs(monkeypatch):
    """Pretend every downloaded program runs; record what was run."""
    ran = []
    monkeypatch.setattr("mooxr.tools.subprocess.run", lambda cmd, **kw: ran.append(cmd))
    return ran


def make_zip(path, names=("ffmpeg", "ffprobe", "ffplay")):
    """Laid out like gyan.dev's: ffmpeg-x.y-essentials_build/bin/ffmpeg.exe."""
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("ffmpeg-9.0-essentials_build/README.txt", "docs")
        for name in names:
            bundle.writestr(f"ffmpeg-9.0-essentials_build/bin/{name}{EXE}", f"fake {name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_unpacks_just_the_two_tools(site, runs, tmp_path):
    folder, url = site
    digest = make_zip(folder / "ffmpeg.zip")
    (folder / "ffmpeg.zip.sha256").write_text(digest + "\n")
    dest = tmp_path / "Mooxr" / "ffmpeg"
    seen = []

    result = tools.install_ffmpeg(seen.append, url=f"{url}/ffmpeg.zip", sha256_url=f"{url}/ffmpeg.zip.sha256", dest=dest)

    assert result == dest
    assert sorted(p.name for p in dest.iterdir()) == [f"ffmpeg{EXE}", f"ffprobe{EXE}"]
    assert (dest / f"ffmpeg{EXE}").read_text() == "fake ffmpeg"
    assert [cmd[-1] for cmd in runs] == ["-version", "-version"]
    assert seen[-1] == 1.0 and seen == sorted(seen)
    # nothing but the finished folder is left behind
    assert [p.name for p in dest.parent.iterdir()] == ["ffmpeg"]


def test_install_replaces_an_old_copy(site, runs, tmp_path):
    folder, url = site
    make_zip(folder / "ffmpeg.zip")
    dest = tmp_path / "ffmpeg"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")

    tools.install_ffmpeg(url=f"{url}/ffmpeg.zip", sha256_url=None, dest=dest)

    assert not (dest / "stale.txt").exists()
    assert (dest / f"ffprobe{EXE}").exists()


def test_damaged_download_is_refused(site, runs, tmp_path):
    folder, url = site
    make_zip(folder / "ffmpeg.zip")
    (folder / "ffmpeg.zip.sha256").write_text("0" * 64)
    dest = tmp_path / "Mooxr" / "ffmpeg"

    with pytest.raises(MooxError, match="checksum"):
        tools.install_ffmpeg(url=f"{url}/ffmpeg.zip", sha256_url=f"{url}/ffmpeg.zip.sha256", dest=dest)
    assert list(dest.parent.iterdir()) == []


def test_zip_without_ffprobe_is_refused(site, runs, tmp_path):
    folder, url = site
    make_zip(folder / "ffmpeg.zip", names=("ffmpeg",))
    dest = tmp_path / "ffmpeg"

    with pytest.raises(MooxError, match="ffprobe"):
        tools.install_ffmpeg(url=f"{url}/ffmpeg.zip", sha256_url=None, dest=dest)
    assert not dest.exists()


def test_not_a_zip_is_refused(site, runs, tmp_path):
    folder, url = site
    (folder / "ffmpeg.zip").write_text("<html>error page</html>")

    with pytest.raises(MooxError, match="zip"):
        tools.install_ffmpeg(url=f"{url}/ffmpeg.zip", sha256_url=None, dest=tmp_path / "ffmpeg")


def test_unreachable_download_is_explained(site, tmp_path):
    _, url = site
    with pytest.raises(MooxError, match="couldn't download"):
        tools.install_ffmpeg(url=f"{url}/missing.zip", sha256_url=None, dest=tmp_path / "ffmpeg")


def test_own_copy_is_found_before_path(tmp_path, monkeypatch):
    own = tmp_path / "own"
    on_path = tmp_path / "path"
    for folder in (own, on_path):
        folder.mkdir()
        for name in tools.TOOLS:
            program = folder / f"{name}{EXE}"
            program.write_text("")
            program.chmod(0o755)
    monkeypatch.setattr("mooxr.tools.search_dirs", lambda: [own])
    monkeypatch.setenv("PATH", str(on_path))

    assert tools.find("ffmpeg").lower() == str(own / f"ffmpeg{EXE}").lower()
    assert tools.missing() == []


def test_missing_lists_what_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("mooxr.tools.search_dirs", lambda: [tmp_path])
    monkeypatch.setenv("PATH", str(tmp_path))
    assert tools.missing() == ["ffmpeg", "ffprobe"]
