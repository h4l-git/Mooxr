#!/usr/bin/env python
"""Double-click to open Mooxr without a console window.

The line above makes the Windows launcher (pyw.exe) use the Python on PATH,
the one `python` runs in a terminal, rather than whichever it likes best.
It still starts the windowless pythonw.exe.
"""

import sys
import traceback

try:
    from mooxr.app import main
except Exception:
    # A .pyw has no console, so without this a startup error is invisible.
    message = (
        f"Mooxr couldn't start with the Python at:\n{sys.executable}\n\n"
        f"{traceback.format_exc(limit=1)}\n"
        "Install what it needs with:\n"
        f'"{sys.executable}" -m pip install -r requirements.txt'
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Mooxr", 0x10)
    except Exception:
        print(message, file=sys.stderr)
    raise SystemExit(1)

raise SystemExit(main())
