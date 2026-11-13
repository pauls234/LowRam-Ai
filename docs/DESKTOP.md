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

* Opens the LowRAM AI that is already running for this user instead of
  starting another one – but only if it can prove it is ours:
  `~/.lowram-ai/instance.json` (mode 0600, written atomically, removed on
  clean shutdown) records port, pid, process start time and the SHA-256 of
  the server's per-process page token. The file must be owned by you and not
  writable by others, the process must still be alive with the same start
  time, and the server on that port must serve the matching token. Anything
  else listening on 127.0.0.1 (another user's LowRAM AI, a stale or foreign
  program) is never shown in the window; LowRAM AI then starts its own
  server on the next free port.
* Otherwise starts the server on **127.0.0.1 only**, on 8765 or the next free
  port up to 8785 (`--port` or `LOWRAM_PORT` select one exact port), and
  waits until `/api/status` answers (20 s).
* Shows a native window (pywebview, 1280×820, minimum 900×600) with a short
  start screen; if the server cannot start, the window explains why and where
  the log is. Without a native web view it opens the default browser.
* Closing the window, Ctrl+C, SIGTERM or closing the terminal (SIGHUP) runs a
  clean shutdown (stops the task loop and a managed llama-server). With the
  window open, a watcher thread reacts to these signals immediately (Python's
  wakeup fd), shuts the server down first and then closes the window, so
  llama-server is never left running. Only a force quit (SIGKILL) skips this.
* The window's JavaScript can call exactly two functions, nothing else:
  `window.pywebview.api.pick_folder(title)` and
  `window.pywebview.api.pick_file(title, ["*.gguf"])`. Both open a native
  dialog and return an absolute path or `null` (cancelled). In the browser
  `window.pywebview` does not exist; the UI falls back to a text field.

## Where things are

| What | Where |
| --- | --- |
| Chats, projects, settings | `~/.lowram-ai/` (change with `LOWRAM_HOME`) |
| Models | `~/.lowram-ai/models/` by default – see below |
| Running instance (port, pid) | `~/.lowram-ai/instance.json` |
| Log file (send it with bug reports) | `~/.lowram-ai/logs/app.log` (rotating, 5 × 1 MB) |
| Window data (theme, drafts) | `~/.lowram-ai/webview/` |
| App (macOS) | `/Applications/LowRAM AI.app` – deleting it keeps your data |

To remove everything, delete the app and the `~/.lowram-ai` folder (and a
custom models folder, if you chose one).

### Models on an external drive

Models are large (2–10 GB each). In **Models & Settings** you can choose a
different models folder – for example `/Volumes/MySSD/LowRAM models` on an
external SSD (in the app window a native folder dialog opens) – and let
LowRAM AI move the models that are already installed. You can also import a
`.gguf` file you already have; it is registered, not copied. Keep the drive connected while you use those
models – when it is missing, LowRAM AI says so instead of failing silently.
`LOWRAM_MODELS_DIR` overrides the setting.

### Devices (other computers)

The first time you turn on **Devices**, macOS asks *"Do you want the
application "LowRAM AI" to accept incoming network connections?"* – click
**Allow**. Other computers on your network need this to connect; LowRAM AI
then listens on your local network with TLS and per-device keys, only while
Devices is on. Windows shows a similar firewall prompt – allow it for
*private* networks only. If you clicked *Deny* by mistake: System Settings →
Network → Firewall → Options → LowRAM AI → Allow incoming connections.

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
