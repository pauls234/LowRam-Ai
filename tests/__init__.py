"""Tests for LowRAM AI.

pyobjc (installed with the desktop app's requirements on the Mac) cannot be imported twice in one
process ("Reload of objc._objc detected"). Some tests swap ``sys.modules`` for a moment, which drops
modules first imported meanwhile – so pyobjc is loaded once here, before any test runs.
"""

import importlib

for _name in ("objc", "Foundation", "AppKit", "Quartz"):
    try:
        importlib.import_module(_name)
    except Exception:  # noqa: BLE001 - not on this computer (Linux, Windows, Mac without pyobjc)
        pass
