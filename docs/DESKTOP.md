# LowRAM AI Studio – desktop app

Double-click, and everything starts: the local server, the AI engine (once a
model is installed) and the Studio window. Closing the window stops all of it
again, including a llama-server that LowRAM AI started.

## Install the app (recommended)

1. Open the project's **Releases** page on GitHub and download
   * `LowRAM-AI-<version>-macOS-arm64.zip` for a Mac with Apple Silicon (M1 or newer, macOS 12+), or
   * `LowRAM-AI-<version>-Windows-x64.zip` for Windows 10/11.
2. **macOS:** double-click the zip, then drag **LowRAM AI** into **Applications**.
   **Windows:** unzip the folder and start **LowRAM AI.exe**.
3. **First start only – the app is not signed by Apple/Microsoft yet:**
   * macOS 12–14: right-click (or Control-click) **LowRAM AI** → **Open** → **Open**.
   * macOS 15 and newer: double-click once and close the warning, then open
     **System Settings → Privacy & Security**, scroll down and click **Open Anyway**.
   * Or, in Terminal: `xattr -dr com.apple.quarantine "/Applications/LowRAM AI.app"`
   * Windows SmartScreen: **More info → Run anyway**.

After that, LowRAM AI opens like any other app (Dock, Launchpad, Spotlight).
Opening it while it already runs simply shows the running Studio – a second
server is never started.

## Run from the source folder (double-click)

Needs Python 3.10 or newer from <https://www.python.org/downloads/>.

| System | Double-click |
| --- | --- |
| macOS | `LowRAM AI.command` (first time: right-click → Open) |
| Windows | `start.bat` |
| Linux | `./start.sh` in a terminal |

The first start creates a private Python environment in `.venv` and installs
the components (about a minute); later starts open the window right away and
reinstall only after `requirements*.txt` changed. Keep the Terminal window
open while you work; closing the LowRAM AI window (or pressing Ctrl+C in the
terminal) stops everything. On Linux the native window needs GTK or Qt for
pywebview; without them the Studio opens in the default browser.

Command line equivalents:

```bash
python -m desktop.launcher               # window (browser fallback)
python -m desktop.launcher --no-window   # default browser, Ctrl+C to stop
python -m desktop.launcher --port 8800   # exact port instead of 8765–8785
python lowram.py app [--browser]         # same via the CLI
```

## What the launcher does

* Looks for a running LowRAM AI on ports 8765–8785 (`GET /api/status`) and
  opens it instead of starting another one.
* Otherwise starts the server on **127.0.0.1 only**, on 8765 or the next free
  port up to 8785 (`--port` or `LOWRAM_PORT` select one exact port), and
  waits until `/api/status` answers (20 s).
* Shows a native window (pywebview, 1280×820, minimum 900×600) with a short
  start screen; if the server cannot start, the window explains why and where
  the log is. Without a native web view it opens the default browser.
* Closing the window, Ctrl+C, SIGTERM or closing the terminal runs a clean
  shutdown (stops the task loop and a managed llama-server).

## Where things are

| What | Where |
| --- | --- |
| Models, chats, projects, settings | `~/.lowram-ai/` (change with `LOWRAM_HOME`) |
| Log file (send it with bug reports) | `~/.lowram-ai/logs/app.log` (rotating, 5 × 1 MB) |
| Window data (theme, drafts) | `~/.lowram-ai/webview/` |
| App (macOS) | `/Applications/LowRAM AI.app` – deleting it keeps your data |

To remove everything, delete the app and the `~/.lowram-ai` folder.

## Build the app yourself

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-desktop.txt
bash desktop/build_macos.sh          # macOS → dist/LowRAM AI.app + dist/LowRAM-AI-<v>-macOS-<arch>.zip
python -m PyInstaller --noconfirm --clean desktop/LowRAM-AI.spec   # Windows/Linux → dist/LowRAM AI/
```

* `desktop/LowRAM-AI.spec` bundles `backend/` (including the feature
  modules that `backend/app.py` imports by name – add new ones to
  `FEATURE_MODULES` in the spec), `backend/static/`, the splash icon and
  certifi's CA bundle (HTTPS downloads in the frozen app).
* `desktop/build_macos.sh` turns `desktop/assets/icon-*.png` into an `.icns`
  with `sips`/`iconutil`, runs PyInstaller, checks the bundle
  (`ai.lowram.studio`, ad-hoc signature) and zips it with `ditto`.
  `LOWRAM_TARGET_ARCH=universal2` builds for Intel Macs too (needs a universal2 Python).
* Smoke test without a window: `"dist/LowRAM AI.app/Contents/MacOS/LowRAM AI" --headless --port 8799`
  then `curl http://127.0.0.1:8799/api/status`.
* `.github/workflows/release.yml` does all of this on `macos-latest` and
  `windows-latest` for every `v*` tag (and on demand), runs the tests and a
  headless smoke test, and attaches both zips to the GitHub release.

### Icon

`desktop/icon.svg` is the master (1024×1024 grid; below 48 px a media query
switches to a bolder variant with fewer pins). `desktop/assets/icon-<N>.png`
(16–1024 px, transparent) were rendered from it with Playwright/Chromium by
loading the SVG in an `<img>` of the target size and taking an element
screenshot with `omitBackground: true`. Re-render them after changing the SVG.

### Signing (later)

The release builds are ad-hoc signed only. For a warning-free start, sign
with a Developer ID certificate and notarize (`codesign --deep --options
runtime`, `xcrun notarytool submit --wait`, `xcrun stapler staple`), and sign
the Windows build with an Authenticode certificate.
