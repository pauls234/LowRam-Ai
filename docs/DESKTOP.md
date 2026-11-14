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

Turning on **Devices** can bring up two macOS prompts – allow both:

1. **Local Network** (macOS 15 and newer): *"LowRAM AI" would like to find and
   connect to devices on your local network* (with the text "LowRAM AI finds
   and connects to your other computers on the same network."). Needed for
   finding other computers (UDP broadcast/multicast on port 8767) and for
   connecting to them. If you denied it: System Settings → Privacy & Security
   → Local Network → turn on LowRAM AI.
2. **Incoming connections** (only if the macOS firewall is on): *"Do you want
   the application "LowRAM AI" to accept incoming network connections?"* –
   click **Allow** so other computers can connect. If you clicked *Deny*:
   System Settings → Network → Firewall → Options → LowRAM AI → Allow
   incoming connections.

LowRAM AI listens on your local network only while Devices is on, with TLS
and per-device keys. Windows shows a similar firewall prompt – allow it for
*private* networks only. Because the app is only ad-hoc signed, macOS may ask
again after you install a new version.

### MacBook: long jobs, battery and notifications

* **Stays awake while it works.** During the task queue, image/video jobs and
  helping another computer, LowRAM AI runs macOS's own `caffeinate -i -w
  <its process id>`, so the Mac does not fall asleep in the middle of a job.
  It ends as soon as the last job finishes (and by itself if LowRAM AI quits
  or crashes). Closing the lid still puts a MacBook to sleep unless an
  external display is connected. Setting: *Keep the computer awake*
  (`power.keep_awake`). On Windows the same is done with
  `SetThreadExecutionState`; on Linux nothing is changed.
* **Pauses on low battery.** On battery below 20 % (setting
  `power.battery_min_percent`, 0 = never), long jobs wait: the task queue
  stops between two tasks ("On battery at 15 % – long jobs pause below 20 %.
  Plug in to continue."), image/video jobs stay queued with that message, and
  a helper computer stops taking new tasks. Plug in and they continue (the
  task queue needs one click on *Run*). Chat is never paused.
* **Notifications** when the task queue finishes, an image or video is ready
  or failed, a computer asks to join (with the number to compare), or a
  combined result of a distributed project is ready. They are sent with
  `osascript` (`display notification`); macOS shows them as coming from
  *Script Editor*, and the first one may ask for permission – if you see
  nothing, allow notifications for Script Editor in System Settings →
  Notifications. Setting: `power.notifications`. At most one notification per
  kind every 5 seconds.
* `GET /api/power` shows the battery, the active keep-awake reasons and the
  settings.

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
  with `sips`/`iconutil`, runs PyInstaller, checks the Info.plist
  (`ai.lowram.studio`, version, macOS 12+, `NSLocalNetworkUsageDescription`,
  `NSAllowsLocalNetworking` for the window's `http://127.0.0.1`), signs the
  whole bundle ad hoc (`codesign --force --deep --sign -`), zips it with
  `ditto` (keeps symlinks and permissions) and verifies the signature of the
  unzipped copy. No `NSBonjourServices`: discovery uses plain UDP
  broadcast/multicast, not Bonjour.
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
