# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for LowRAM AI Studio. Run from the repository root:

    python -m PyInstaller --noconfirm --clean desktop/LowRAM-AI.spec

* macOS:   dist/LowRAM AI.app         windowed app bundle (bundle id ai.lowram.studio)
* Windows: dist/LowRAM AI/LowRAM AI.exe  windowed, onedir
* Linux:   dist/LowRAM AI/LowRAM AI      onedir (used for smoke tests)

macOS icon: ``desktop/build_macos.sh`` builds an .icns with iconutil and passes
it as LOWRAM_ICNS; otherwise one is assembled here from desktop/assets/*.png
(PNG-based icns, macOS 10.7+). The Windows .ico is always assembled here.
Optional: LOWRAM_TARGET_ARCH (macOS: arm64, x86_64 or universal2).
"""

import os
import re
import struct
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is provided by PyInstaller
ASSETS = ROOT / "desktop" / "assets"
ICON_DIR = Path(workpath) / "icons"  # noqa: F821 - workpath is provided by PyInstaller
APP_NAME = "LowRAM AI"
BUNDLE_ID = "ai.lowram.studio"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "backend" / "__init__.py").read_text("utf-8")).group(1)

# Feature modules are imported by name (importlib) in backend/app.py, so the
# import scanner cannot see them. Modules that do not exist yet only produce an
# "ERROR: Hidden import ... not found" line; the build continues.
FEATURE_MODULES = ["api_coding", "chat", "engine", "downloads", "media", "cluster", "worker"]
hiddenimports = sorted(
    {f"backend.{p.stem}" for p in (ROOT / "backend").glob("*.py") if p.stem != "__init__"}
    | {f"backend.{name}" for name in FEATURE_MODULES}
    | {"desktop", "desktop.launcher", "certifi"}
)

datas = [
    (str(ROOT / "backend" / "static"), "backend/static"),
    (str(ROOT / "desktop" / "icon.svg"), "desktop"),  # splash screen
]


def _png(size):
    return (ASSETS / f"icon-{size}.png").read_bytes()


def _write_ico(dest):
    """Windows .ico with PNG-compressed entries (Vista+)."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    dest.write_bytes(header + entries + b"".join(images))
    return dest


def _write_icns(dest):
    """macOS .icns from PNG entries (same content iconutil produces from an .iconset)."""
    kinds = [("icp4", 16), ("icp5", 32), ("ic11", 32), ("icp6", 64), ("ic12", 64), ("ic07", 128),
             ("ic08", 256), ("ic13", 256), ("ic09", 512), ("ic14", 512), ("ic10", 1024)]
    body = b"".join(kind.encode("ascii") + struct.pack(">I", 8 + len(_png(s))) + _png(s) for kind, s in kinds)
    dest.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)
    return dest


ICON_DIR.mkdir(parents=True, exist_ok=True)
if sys.platform == "darwin":
    icns = os.environ.get("LOWRAM_ICNS") or str(_write_icns(ICON_DIR / "LowRAM-AI.icns"))
    exe_icon = None
elif sys.platform == "win32":
    icns = None
    exe_icon = str(_write_ico(ICON_DIR / "LowRAM-AI.ico"))
else:
    icns = exe_icon = None

a = Analysis(  # noqa: F821
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,  # windowed: no terminal window on macOS/Windows; logs go to app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.environ.get("LOWRAM_TARGET_ARCH") or None,
    codesign_identity=None,  # ad-hoc signature (unsigned for Gatekeeper, see docs/DESKTOP.md)
    entitlements_file=None,
    icon=exe_icon,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=icns,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow light/dark mode
            "LSApplicationCategoryType": "public.app-category.productivity",
            # The window loads the local server over plain http://127.0.0.1.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
