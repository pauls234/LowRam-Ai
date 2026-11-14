<p align="center"><img src="desktop/assets/icon-128.png" width="96" alt=""></p>

# LowRAM AI Studio

**Your own AI assistant, running entirely on your computer – free, private,
offline.** Chat like with a cloud assistant, let it write and change code,
generate images and short videos, work through task lists, and team up
several of your computers on one project. It is slower than a data-center AI,
and that's the deal: everything stays on your machines and costs nothing.

Built for Macs with little RAM (Apple Silicon), also runs on Windows and Linux.

> **Kurz auf Deutsch:** App herunterladen (GitHub → Releases), Doppelklick,
> beim ersten Start empfohlenes Modell mit einem Klick installieren – fertig.
> Eigene Modelle per Hugging-Face-Link, Modelle auf externer Festplatte,
> Bilder/Videos und mehrere Geräte im WLAN koppeln (wie Bluetooth: gleiche
> 6-stellige Zahl auf beiden Bildschirmen, „Erlauben“ klicken).
> Erster Start der nicht signierten Mac-App: Rechtsklick → „Öffnen“.

---

## Get started

**App (recommended).** Download `LowRAM-AI-…-macOS-arm64.zip` (or the Windows
zip) from the [Releases](../../releases) page, move **LowRAM AI** to
Applications and double-click it. The app is not signed yet: on first start
right-click → **Open** (macOS 15: *System Settings → Privacy & Security → Open
Anyway*). Details: [docs/DESKTOP.md](docs/DESKTOP.md).

**From the source folder.** With Python 3.10+ installed, double-click
`LowRAM AI.command` (macOS), `start.bat` (Windows) or run `./start.sh`
(Linux). The first start sets up a private Python environment automatically.

**First run.** The Studio checks your RAM, recommends a model that fits,
and installs the llama.cpp engine plus the model with one click (downloads are
verified with SHA-256). Then just start chatting. Nothing is sent to the
internet afterwards – only model/engine downloads you start yourself.

## What you can do

| | |
| --- | --- |
| **Chat** | Claude-like conversations with streamed answers, Markdown and code blocks, a collapsible "thinking" section for reasoning models, stop, regenerate and *continue* (when an answer was cut off), speed shown per answer (tokens/s), custom instructions, text/code file attachments, chat search and Markdown export. |
| **MacBook-friendly** | Long jobs keep the Mac awake, pause on battery below a threshold you choose (default 20 %), and a notification tells you when a loop, an image or a merged result is ready – start it, do something else, come back later. |
| **Code** | A code chat mode, plus the coding agent: point it at a project folder, it plans small steps and proposes complete file changes as a diff. **Nothing changes on disk until you click Approve.** The model can never run commands. |
| **Images & video** | Local generation with [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) (SD-Turbo, SD 1.5, SDXL-Turbo, Wan 2.1 video). Jobs queue up, show progress and can be cancelled. Video is very slow on small machines. |
| **Task queue** | One task per line; run one, or a bounded loop that stops by itself (queue empty, limit, failure, low RAM, no model, stop button). |
| **Devices** | Let other computers on your Wi-Fi help with a project: they take tasks in parallel with their own local model, and the host combines the results. |
| **Your models** | Pick from a curated catalog with RAM-based recommendations, paste any Hugging Face GGUF repo/link, or use a `.gguf` file you already have. |
| **External drive** | Keep models on an external SSD; LowRAM AI runs them from there and says so clearly when the drive is unplugged. |

### Models

*Models & Settings* lists recommended models for your RAM (e.g. 8 GB Mac →
Qwen3 4B, 16 GB → Qwen2.5 7B). To use something else:

* **Add your own model:** paste `owner/repo` or a Hugging Face link
  (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`). The Studio lists the GGUF
  variants with size and a *fits your RAM* badge, recommends one, and downloads
  it with checksum verification.
* **Use a file you already have:** choose a `.gguf` file – it is used in place,
  never copied or deleted.
* **Models folder:** choose a folder (for example on an external SSD) and
  optionally move your existing models there.

### Connecting devices – as easy as Bluetooth

1. On the main computer: **Devices → Allow other devices** (macOS asks once to
   allow incoming connections – click *Allow*).
2. On the helper computer: open LowRAM AI → **Devices**. The main computer
   appears under *Nearby computers* → **Join**.
3. Both screens show the **same 6-digit number**. If it matches, click
   **Allow** on the main computer. Done – the helper reconnects automatically
   from now on.
4. In the *Task queue*, choose **Run on all devices**; enable *Combine results*
   to get one merged answer at the end.

No discovery on your network (some guest/office Wi-Fis block it)? Use *Use a
code instead* and type the pairing code, or on a terminal:
`python3 lowram.py worker --join LOWRAM1-…`.

The connection is TLS-encrypted with a pinned certificate, every request is
signed with a per-device key, and the 6-digit comparison protects the pairing
against anyone else on the network.

---

## Command line

```bash
python3 lowram.py app                      # the Studio window (same as double-click)
python3 lowram.py serve                    # web UI only, http://127.0.0.1:8765
python3 lowram.py memory --ram 40          # RAM and derived limits
python3 lowram.py new "Research" --task "Summarise X" --task "List risks of X"
python3 lowram.py run PROJECT_ID --max-tasks 10
python3 lowram.py create /abs/path/to/repo "Add tests for the API"
python3 lowram.py plan PROJECT_ID && python3 lowram.py step PROJECT_ID
python3 lowram.py show PROJECT_ID TASK_ID  # review the diff
python3 lowram.py approve PROJECT_ID TASK_ID
python3 lowram.py loop PROJECT_ID          # interactive review loop (y/n/q)
python3 lowram.py worker --join CODE       # help another computer
```

Exit codes: `0` success, `1` error/failed task, `2` usage error, `3` nothing ran
(low RAM, busy or no model).

## Configuration

Everything is configurable in the Studio. Environment variables override it:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOWRAM_LLM_URL` | managed engine | use your own OpenAI-compatible server instead of the built-in one |
| `LOWRAM_LLM_MODEL` / `LOWRAM_LLM_API_KEY` | – | model name / bearer token for that server |
| `LOWRAM_MODELS_DIR` | `~/.lowram-ai/models` | models folder (also settable in the Studio) |
| `LOWRAM_HOME` | `~/.lowram-ai` | database, settings, results, logs |
| `LOWRAM_PORT` | `8765` | Studio port (always 127.0.0.1) |
| `LOWRAM_MIN_FREE_MB` | `1024` | tasks pause below this much free RAM |

## How it works

* `backend/` – stdlib Python + psutil: `app.py`/`web.py` (local HTTP server,
  routing, SSE), `engine.py` (llama.cpp runtime and models), `downloads.py`
  (verified, resumable downloads), `chat.py`, `api_coding.py` + `agent.py`
  (coding agent), `media.py` (image/video), `cluster.py` + `worker.py`
  (devices), `runner.py` + `db.py` (SQLite queue, one heavy job per machine,
  crash recovery).
* `backend/static/` – the Studio UI (vanilla JS, no external resources, strict CSP).
* `desktop/` – the app launcher (pywebview window) and PyInstaller build.
* Contract and design rules: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Security & trust

What LowRAM AI does – and does not do – on your Mac:

* **Nothing is reachable from outside by default.** The Studio listens on
  `127.0.0.1` only (Host-header check, per-process CSRF token, strict CSP);
  the managed llama-server also listens on `127.0.0.1` and only answers with a
  random key that changes on every start. The only listener on your network is
  the optional **Devices** server (TLS with a pinned certificate, per-device
  HMAC keys, replay protection, rate limits) – it is **off** until you turn it
  on. A small UDP listener only *receives* discovery announcements (bounded,
  never replies). When Devices is on and "discoverable", your device name and
  certificate fingerprint are announced on the local network – turn
  discoverable off on public Wi-Fi.
* **No telemetry, no accounts, no cloud.** Outbound connections happen only
  for what you start: model/engine downloads and searches (Hugging Face,
  GitHub) and an update check against GitHub (at most every 6 h; switch it off
  in Settings). HTTPS only, allow-listed hosts, redirects checked on every hop.
* **Downloads are verified.** Programs (llama.cpp, stable-diffusion.cpp) are
  only installed from their official GitHub releases with the SHA-256 published
  there; models only with the SHA-256 that Hugging Face reports. This protects
  against corrupted or tampered transfers – it cannot make a malicious model
  repository trustworthy. **Only download models from publishers you trust**
  (the catalog lists well-known ones); model files are parsed by llama.cpp /
  stable-diffusion.cpp, and a deliberately crafted file could target a bug in
  those parsers.
* **Your files stay yours.** LowRAM AI writes only inside `~/.lowram-ai`, the
  models folder you choose, and – after you click *Approve* – the project
  folder you gave the coding agent. The model can never run commands; your
  test command runs only when you click *Tests ausführen*.
* **Gentle on the hardware.** One heavy job at a time, RAM guard and waiting
  instead of overloading, model unloaded when idle, battery pause, and the Mac
  is kept awake only while a long job runs.
* A paired device is trusted to return honest answers – only pair your own computers.

## Tests

```bash
python3 -m unittest discover -s tests
```

About 560 tests with fake model/image servers – no model or network needed.
CI runs them on Ubuntu and macOS with Python 3.10–3.13; the release workflow
builds the macOS and Windows apps on every `v*` tag.

## Not yet

Signed/notarized app builds, test-based verification of coding steps
(sandboxed test runs), web browsing, fine-tuning, and devices across different
networks (internet relay).
