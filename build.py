"""Build dist/Mooxr.exe: one file with Python, Mooxr and its packages inside.

    pip install pyinstaller
    python build.py

FFmpeg is not packed in; the app fetches its own copy the first time it is
needed (see mooxr/tools.py). To ship FFmpeg inside anyway, put ffmpeg.exe
and ffprobe.exe in an "ffmpeg" folder here before building.
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "mooxr" / "assets"


def make_icon() -> None:
    """Render the app icon (logo/mooxr-icon.svg) as PNG and multi-size ICO."""
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QGuiApplication, QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(exist_ok=True)
    svg = QSvgRenderer(str(ASSETS / "logo" / "mooxr-icon.svg"))
    if not svg.isValid():
        raise SystemExit(f"Can't read {ASSETS / 'logo' / 'mooxr-icon.svg'}")

    def draw(size: int) -> QImage:
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        svg.render(p, QRectF(0, 0, size, size))
        p.end()
        return image

    draw(256).save(str(ASSETS / "icon.png"))
    # Qt's ICO writer stores one image per file, so assemble the sizes by hand.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = []
    for size in sizes:
        tmp = ASSETS / f"_{size}.png"
        draw(size).save(str(tmp))
        pngs.append(tmp.read_bytes())
        tmp.unlink()
    header = (0).to_bytes(2, "little") + (1).to_bytes(2, "little") + len(sizes).to_bytes(2, "little")
    offset = 6 + 16 * len(sizes)
    entries = b""
    for size, data in zip(sizes, pngs):
        dim = 0 if size == 256 else size
        entries += bytes([dim, dim, 0, 0]) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
        entries += len(data).to_bytes(4, "little") + offset.to_bytes(4, "little")
        offset += len(data)
    (ASSETS / "icon.ico").write_bytes(header + entries + b"".join(pngs))
    del app


def main() -> int:
    make_icon()
    sep = ";" if sys.platform == "win32" else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", "Mooxr",
        "--icon", str(ASSETS / "icon.ico"),
        "--add-data", f"{ASSETS}{sep}mooxr/assets",
        # Links are optional in the code (imported lazily), so make sure
        # yt-dlp and all its site extractors go in.
        "--collect-submodules", "yt_dlp",
        # Qt pieces Mooxr never uses; leaving them out keeps the exe small.
        *[arg for mod in ("PyQt6.QtNetwork", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtPdf",
                          "PyQt6.QtMultimedia", "PyQt6.QtWebEngineCore", "tkinter", "pytest")
          for arg in ("--exclude-module", mod)],
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
    ]
    bundled = HERE / "ffmpeg"
    if (bundled / "ffmpeg.exe").exists() and (bundled / "ffprobe.exe").exists():
        args += ["--add-binary", f"{bundled / 'ffmpeg.exe'}{sep}ffmpeg",
                 "--add-binary", f"{bundled / 'ffprobe.exe'}{sep}ffmpeg"]
        print("Packing FFmpeg from", bundled)
    args.append(str(HERE / "Mooxr.pyw"))

    result = subprocess.run(args, cwd=HERE)
    if result.returncode == 0:
        exe = HERE / "dist" / ("Mooxr.exe" if sys.platform == "win32" else "Mooxr")
        print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.0f} MB)")
    return result.returncode


if __name__ == "__main__":
    if "--icon-only" in sys.argv:
        make_icon()
        raise SystemExit(0)
    shutil.rmtree(HERE / "build", ignore_errors=True)
    raise SystemExit(main())
