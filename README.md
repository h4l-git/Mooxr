# Mooxr conversion core

Video file or link in, tagged MP3 out, saved where Spotify's Local Files can see it.

## Setup

1. Install Python 3.9 or newer.
2. In this folder: `pip install -r requirements.txt`

That's it. FFmpeg, which does the actual converting, is fetched by Mooxr
itself: the first time you open the window it offers a "Get FFmpeg" button
(about 115 MB, from gyan.dev, checked against its published SHA-256). Mooxr
keeps that copy in `%LOCALAPPDATA%\Mooxr\ffmpeg` and uses it from then on.

Where Mooxr looks for `ffmpeg` and `ffprobe`, first match wins:

1. the folder in `MOOXR_FFMPEG_DIR`, if set
2. an `ffmpeg` folder next to Mooxr (for shipping them with a packaged app)
3. its own downloaded copy
4. PATH

## Open the window

Double-click `Mooxr.pyw`, or run `python -m mooxr` with no arguments.

Drop videos (or a folder of them) onto the window, paste a link, or press
Ctrl+O. Each one gets a row where you can type a title / artist; leave them
empty and Mooxr works them out (the grey text shows its guess). Press Moox.
For a link, the grey text fills in with the link's own title and artist as
soon as Mooxr has looked it up. Files convert one at a time in the
background; Cancel stops one (or takes it out of the line), and failed rows
keep their error and can be retried. Ctrl+V anywhere adds links or files
from the clipboard. The output folder is remembered between runs.

Anything already mooxed into the output folder is flagged when you add it
("Already mooxed as …"), using the `MOOXR_SOURCE` tag. YouTube links match
whatever shape they come in (youtu.be, watch?v=, Shorts, YouTube Music);
files match by file name. Moox it anyway to get a second copy.

Closing the window while tracks are converting asks first; stopping leaves
no half-written files.

Settings (top right, or Ctrl+,) has an Appearance section: Dark, Light or
System mode, and a choice of colour themes. Changes apply straight away
and are remembered.

## Mooxed files

Press "Mooxed files" to see every track you've mooxed (anything in the
output folder carrying Mooxr's `MOOXR_SOURCE` tag), in the same window.
Each one shows its cover art, title and artist. Change the title or artist,
or press "Change…" under the cover to pick a new image, then press Save (or
Enter). Mooxr rewrites the tags, crops the new cover to a square the same
way it does when mooxing, and renames the file to `Artist - Title.mp3` to
match. Its copy in the Spotify folder is swapped for the edited one. "Undo
changes" drops anything not yet saved. Done goes back to the queue.

Search (Ctrl+F) by title, artist or file name; sort by newest, oldest,
title, artist or length (remembered); and show only tracks not yet in
Spotify or without cover art. "Delete…" moves a track to the Recycle Bin
and removes its copy from the Spotify folder, after asking.

Spotify knows a local track by its title, artist, album and length, so a new
title or artist makes it a different song to Spotify: it has to be added to
playlists and Liked Songs again. The page warns about this before you save.
A new cover is fine.

From code: `mooxr.library.edit_track(path, title=..., artist=..., cover=...)`,
then `mooxr.library.resend(new_path, spotify_folder, replaces=old_path)`.
`mooxr.library.delete_track(path, spotify_folder)` deletes one (for good,
unless you pass `discard=`), and `mooxed_index(folder)` /
`source_key(source)` spot repeats.

## Spotify

Every track is saved straight into a Spotify folder (`Music\Spotify` unless
you change it in Settings → Spotify). Spotify won't let other apps hand it
songs; it only watches folders. Set that up once in Spotify: Settings → Your
Library → turn on Show Local Files → Add a source → choose that folder.
Tracks then appear under Your Library → Local Files by themselves.

If you pick a different Save to folder on the main screen, tracks are saved
there and copied into the Spotify folder. Add only the Spotify folder to
Spotify, not the Save to folder as well, or every song shows up twice.

Tracks mooxed before this was automatic have a "Send to Spotify" button on
the Mooxed files page.

## Other apps

The "to Spotify ▾" half of the Moox button picks where finished tracks go
(remembered between runs). Only apps that accept your own MP3s are listed:

- **Spotify**: copied into the Spotify folder, as above.
- **Apple Music**: copied into its "Automatically Add to Music" folder
  (Mooxr looks for it under `Music`; an old iTunes one works too), and
  Apple Music adds it to your library.
- **YouTube Music, Deezer, SoundCloud**: these only take uploads through
  their websites. When the batch finishes, Mooxr opens the upload page and
  shows the file so you can drag it in; each row also gets an "Upload to …"
  button.

Tidal and Amazon Music have no way to add your own files.

## Build Mooxr.exe

```
pip install pyinstaller
python build.py
```

This makes `dist\Mooxr.exe` (about 50 MB): one file with Python, Mooxr,
PyQt6, mutagen and yt-dlp inside, so it runs on a Windows PC with nothing
installed. Copy it anywhere and double-click it. FFmpeg isn't packed in; the
exe fetches it the first time it's needed, like the window does. To pack it
in anyway, put `ffmpeg.exe` and `ffprobe.exe` in an `ffmpeg` folder here
before building.

The first launch takes a few seconds while Windows unpacks the exe. Some
antivirus tools are wary of new, unsigned exes and may ask before running it.

## Command line

```
python -m mooxr "My Band - First Song.mp4"
python -m mooxr clip.mov --title "First Song" --artist "My Band"
python -m mooxr one.mp4 two.mp4 --out "D:\Music\Spotify"
```

Output goes to `Music/Spotify` in your home folder unless you pass `--out`.

## Use it from code

```python
from mooxr import moox, MooxError

try:
    result = moox(
        "My Band - First Song.mp4",
        title=None,            # None or "" means "work it out"
        artist=None,
        on_progress=lambda stage, fraction: print(stage, fraction),
        cancelled=None,        # a function; return True to stop (raises MooxCancelled)
    )
    print(result.path, result.title, result.artist, result.has_cover)
except MooxError as problem:
    print(problem)             # written to be shown to the user as-is
```

`moox()` is the only call the app window needs. Everything else in
`mooxr/core.py` is a step inside it.

## What it decides for you

| Thing | Rule |
|---|---|
| Format | MP3, 320 kbps, 44.1 kHz. Surround is folded down to stereo. |
| Title / artist | First of: what you pass in, the link's metadata, tags inside the file, the file name split on `Artist - Title`. |
| Clutter | Bracketed `(Official Video)`, `[HD]`, `(Lyrics)` and similar are removed. `(feat. X)` and `(Live)` stay. |
| No artist found | Tagged `Unknown Artist`; the file is named by title alone. |
| Album | `Mooxed` on every track, so they group together. |
| Cover | Your image if given, else the link's thumbnail, else a frame from the video (skipping the opening). Centre-cropped to a square JPEG at its own resolution, shrunk to 3000x3000 if bigger (Spotify's spec: square, JPEG or PNG, RGB, 3000x3000 best, 640x640 minimum). Each card in the window previews it and says whether it fits. |
| File name | `Artist - Title.mp3`, with characters Windows rejects replaced by spaces. |
| Name already taken | Saved as `Artist - Title (2).mp3`. Pass `overwrite=True` to replace. |
| Half-written files | Never visible. Work happens in a hidden `.part` file that is renamed into place when finished. |
| Source | Stored in a `MOOXR_SOURCE` tag so the app can spot repeats later. |

## Tests

```
pytest
```

The tests generate small sample videos with FFmpeg, convert them, and read the
results back with ffprobe and mutagen. The link test downloads from a web
server it starts on your own machine, so it needs no internet.
