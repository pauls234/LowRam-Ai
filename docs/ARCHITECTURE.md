# LowRAM AI Studio – architecture and API contract (v0.3)

LowRAM AI Studio is a local, free, "slower but private" alternative to a cloud
assistant: chat, coding, image/video generation and a task queue, all running
on the user's own machine(s). This document is the contract every module is
built against. **If something here is ambiguous, choose the safer option and
note it in your report.**

## Principles

1. **Local first.** The UI server listens on `127.0.0.1` only. The only LAN
   listener is the device (cluster) server, and only when the user enables it;
   it uses TLS with a pinned self-signed certificate plus per-device HMAC keys.
2. **One heavy job per machine.** LLM inference, image and video generation
   share the global slot: `db.inference_slot(label)` or a task lease. A
   machine never runs two heavy jobs at once (RAM).
3. **Slow is fine, silent is not.** Every long operation shows progress, can
   be cancelled, and ends as an explicit success or an explicit failure with a
   human-readable reason. Never report success that did not happen.
4. **No new runtime dependencies** in `backend/` (stdlib + psutil). The desktop
   build may use `pywebview` and `pyinstaller` (in `requirements-desktop.txt`).
5. **Python 3.10+** compatible (`tomllib` is 3.11+, guard it).
6. **Security rules**
   * `subprocess` only with argument lists, never `shell=True`; never pass
     user/model text through a shell; set timeouts; kill the process group on
     cancel.
   * Files are written only below `LOWRAM_HOME` (except approved coding
     changes). Validate every id/path; reject `..`, absolute paths, symlinks.
   * Downloads only over HTTPS from allow-listed hosts (see `downloads.py`),
     redirects only to allow-listed hosts, SHA-256 verified when a digest is
     known, archives extracted with zip-slip/symlink protection.
   * Model/remote output is untrusted text: the UI renders it with
     `textContent`/DOM building only (no `innerHTML`), strict CSP stays.
   * Secrets (device keys, API keys) are never logged or returned by the API
     after creation.

## Shared building blocks (integrator-owned – use them, do not edit them)

| Module | What you get |
| --- | --- |
| `web.py` | `@route(method, regex, scope="ui")`, `Request` (`.json()` enforces JSON + CSRF token, `.arg()`, `.body()`, `.server`, `.headers`), `Response(data, status)`, `StreamResponse(events)` (SSE, one JSON object per `data:` line), `FileResponse(path, content_type, download_name)`, `HTTPError(status, msg)`, `@on_startup`, `@on_shutdown` |
| `app.py` | Imports feature modules listed in `FEATURE_MODULES` (`api_coding`, `chat`, `engine`, `media`, `cluster`) so their routes register. Error mapping: `HTTPError`→status, `db.NotFound`→404, `db.Busy`→409, `db.InvalidTransition`/`ArtifactError`→409, `ValueError`→400, `ConfigError`→500, anything else→500 (logged). `start_background(port)` / `shutdown(server)` |
| `db.py` | `connect()`, `transaction(conn)` (BEGIN IMMEDIATE), `register_schema(sql)`, `task_dict(row)`, `inference_slot(label)`, `busy_reason()`, `claim_next_task`, `finish_task`, `register_owner_check(prefix, fn)`, `REMOTE_OWNER_PREFIX = "device:"` (tasks leased to other devices do not block this machine) |
| `settings.py` | `load()`, `get(section, key)`, `update(section, values)`; sections `engine`, `media`, `cluster`, `ui`, `budget_percent` (see `DEFAULTS`) |
| `config.py` | `home_dir()`, `models_dir()`, `bin_dir()`, `artifacts_dir()`, `set_runtime_llm_url(url)`, `llm_url()` (env `LOWRAM_LLM_URL` > managed engine > default) |
| `memory.py` | `plan(percent)` → `context_chars`, `max_tokens`, `can_run`, `reason` |
| `artifacts.py` | atomic writes, `project_dir(id)`, `resolve_inside(path)` |
| `llm.py` | `chat(messages, max_tokens, temperature)` (non-streaming), `LLMError`, `redact_url` |
| `runner.py` / `agent.py` | task queue and coding agent (see README) |

Shared files – **only the integrator edits them**: `app.py`, `web.py`,
`db.py`, `config.py`, `settings.py`, `api_core.py`, `cli.py`, `README.md`,
`requirements*.txt`, `.github/workflows/ci.yml`. If you need a change there,
describe it precisely (file, function, diff) in your final report.

## Module ownership

| Area | Files | Tests |
| --- | --- | --- |
| Engine | `backend/engine.py`, `backend/downloads.py` | `tests/test_engine.py`, `tests/test_downloads.py` |
| Chat + coding API | `backend/chat.py`, `backend/api_coding.py`, `llm.stream_chat` (append to `llm.py`, do not change existing functions) | `tests/test_chat.py`, `tests/test_api_coding.py` |
| Media | `backend/media.py` | `tests/test_media.py` |
| Devices | `backend/cluster.py`, `backend/worker.py` | `tests/test_cluster.py` |
| Desktop app | `desktop/**`, `.github/workflows/release.yml`, `LowRAM AI.command`, `start.sh`, `start.bat`, `requirements-desktop.txt` | `tests/test_desktop.py` |
| Frontend | `backend/static/**` | browser checks |

## API contract

All bodies are JSON. Errors: `{"error": "message"}`. POSTs need
`Content-Type: application/json` and `X-LowRAM-Token`. Ids in paths are
integers unless stated (catalog ids are strings matching
`[a-z0-9][a-z0-9._-]{0,63}`).

### Core (exists)

* `GET /api/status` → `{version, features: [str], startup_errors: [str], busy: str|null}`
* `GET /api/settings` → `{settings}`; `POST /api/settings {section, values}` → `{section, values}`
* `GET /api/memory?percent=40` → memory plan
* Task queue: `GET/POST /api/projects`, `GET /api/projects/<id>`,
  `POST /api/projects/<id>/run|loop`, `GET /api/loop`, `POST /api/loop/stop`,
  `GET /api/tasks/<id>/result`, `POST /api/tasks/<id>/retry`, `POST /api/recover`

### Engine and downloads (`engine.py`, `downloads.py`)

```
Download = {id: str, label: str, kind: "runtime"|"model"|"media-runtime"|"media-model",
            state: "queued"|"downloading"|"verifying"|"extracting"|"done"|"failed"|"cancelled",
            bytes_done: int, bytes_total: int|null, error: str|null,
            started_at: str|null, finished_at: str|null}
CatalogModel = {id, name, family, purpose: "chat"|"code", params: "3B", quant: "Q4_K_M",
                size_bytes: int, min_ram_gb: number, recommended: bool, installed: bool,
                license: str, description: str}
EngineServer = {state: "stopped"|"starting"|"ready"|"error", model_id: str|null,
                port: int|null, pid: int|null, ctx_size: int|null, error: str|null,
                started_at: str|null}
```

* `GET /api/engine/status` → `{runtime: {installed, path, version, source: "path"|"homebrew"|"managed"|null}, server: EngineServer, external: bool, llm_url: str}`
  (`external` = `LOWRAM_LLM_URL` is set; the app then never starts its own server)
* `GET /api/engine/catalog` → `{models: [CatalogModel], ram_total_bytes}`
* `POST /api/engine/runtime/install {}` → 202 `{download}`
* `POST /api/engine/models/<id>/install {}` → 202 `{download}`
* `POST /api/engine/models/<id>/delete {}` → `{deleted: true}` (409 while loaded)
* `POST /api/engine/start {model_id?}` → 202 `{server}`; `POST /api/engine/stop {}` → `{server}`
* `GET /api/downloads` → `{downloads: [Download]}`; `POST /api/downloads/<id>/cancel {}` → `{download}` (download ids are strings)

Python API for other modules:
`engine.status()`, `engine.ensure_running(timeout=...) -> str` (LLM url, raises `engine.EngineError`),
`engine.stop()`, `engine.pause_for_heavy_job()` (context manager: stop the managed
llama-server if running, restart it afterwards; no-op if external),
`downloads.start(job_id, url, dest, *, sha256=None, size=None, label, kind, extract=None) -> dict`,
`downloads.get(id)`, `downloads.list_jobs()`, `downloads.cancel(id)`, `downloads.wait(id, timeout)`.

### Chat (`chat.py`)

```
Conversation = {id, title, mode: "chat"|"code", created_at, updated_at, message_count}
Message = {id, conversation_id, role: "user"|"assistant", content,
           status: "complete"|"streaming"|"stopped"|"error", error: str|null,
           model: str|null, duration_s: number|null, created_at}
```

* `GET /api/chats` → `{chats: [Conversation]}` (newest first)
* `POST /api/chats {title?, mode?}` → 201 `{chat}`
* `GET /api/chats/<id>` → `{chat, messages: [Message]}`
* `POST /api/chats/<id>/rename {title}` → `{chat}`; `POST /api/chats/<id>/delete {}` → `{deleted: true}`
* `POST /api/chats/<id>/messages {content, percent?}` → `text/event-stream`:
  `{"type":"start","user_message":Message,"assistant_message":Message}`,
  then many `{"type":"token","text":"…"}`, then `{"type":"done","message":Message}`
  or `{"type":"error","error":"…","message":Message|null}`.
  Busy / invalid input is answered with normal JSON 409 / 400 *before* any stream starts.
* `POST /api/chats/<id>/stop {}` → `{stopped: bool}` (partial answer is kept, status `stopped`)

### Coding (`api_coding.py`, wraps `backend.agent`)

* `GET /api/coding/projects` → `{projects}`; `POST /api/coding/projects {goal, repo_path}` → 201 `{project}`
* `GET /api/coding/projects/<pid>` → `{project, tasks}`
* `POST /api/coding/projects/<pid>/plan {percent?}` → `{tasks}`
* `POST /api/coding/projects/<pid>/steps {title, files?, detail?}` → 201 `{task_id}`
* `POST /api/coding/projects/<pid>/step {percent?}` → result of `execute_coding_step`
* `GET /api/coding/projects/<pid>/tasks/<tid>/proposal` → `{proposal}`
* `POST /api/coding/projects/<pid>/tasks/<tid>/approve {}` → approve result
* `POST /api/coding/projects/<pid>/tasks/<tid>/reject {reason?}` → `{task}`

### Media (`media.py`)

```
MediaJob = {id, kind: "image"|"video", prompt, negative_prompt, model_id,
            params: {width, height, steps, seed, cfg_scale, frames?, fps?},
            status: "queued"|"running"|"done"|"failed"|"cancelled",
            progress: number 0..1, message: str, error: str|null,
            output_url: str|null, output_type: str|null,
            created_at, started_at, finished_at, duration_s}
```

* `GET /api/media/status` → `{runtime: {installed, path, version}, image_models: [CatalogModel-like], video_models: [...], active_job_id}`
* `POST /api/media/runtime/install {}` → 202 `{download}`; `POST /api/media/models/<id>/install {}` → 202 `{downloads: [Download]}`
* `GET /api/media/jobs` → `{jobs}`; `POST /api/media/jobs {kind, prompt, negative_prompt?, width?, height?, steps?, seed?, frames?, model_id?}` → 202 `{job}`
* `GET /api/media/jobs/<id>` → `{job}`; `POST /api/media/jobs/<id>/cancel {}` → `{job}`; `POST /api/media/jobs/<id>/delete {}` → `{deleted: true}`
* `GET /api/media/jobs/<id>/file` → the image/video file

### Devices (`cluster.py`, `worker.py`)

```
Device = {id, name, status: "online"|"offline"|"busy", platform, ram_total_bytes,
          model: str|null, last_seen, tasks_done, current_task_id: int|null, paired_at}
```

* `GET /api/cluster` → `{enabled, listening: {addresses: [str], port}|null, fingerprint: str|null, devices: [Device], pairing: {code, expires_at}|null, error: str|null}`
* `POST /api/cluster/enable {}` / `POST /api/cluster/disable {}` → `{cluster}`
* `POST /api/cluster/pairing {}` → `{pairing: {code, expires_at}}` (one-time, 10 minutes)
* `POST /api/cluster/devices/<id>/revoke {}` → `{revoked: true}`
* `POST /api/cluster/projects/<id>/distribute {percent?, merge: bool}` → 202 `{project_id, distributed: true}` – helper devices may claim this project's pending tasks while this machine works on it too; with `merge` a final "combine the results" step runs locally when all tasks are done.
* `GET /api/cluster/projects/<id>/final` → `{content, path}` (404 until merged)
* `POST /api/cluster/join {code, name?, percent?}` → 202 `{worker}`; `POST /api/cluster/leave {}` → `{worker}`
* `GET /api/cluster` additionally returns `worker: {state: "idle"|"pairing"|"working"|"error"|"stopped", host, device_id, tasks_done, current_task: {id, title, project_id}|null, error, message}|null`, `projects: [{project_id, name, percent, merge, merge_state: "off"|"waiting"|"running"|"done"|"failed", merge_error, final_available, counts, total}]`, `pairing_error`, `host_name`
* LAN API (`/cluster/v1/pair|heartbeat|claim|leave`, `/cluster/v1/tasks/<id>/result|fail`): see the `backend/cluster.py` docstring. Device leases are requeued after 90 s without a heartbeat (max 3 attempts) – the only automatic retry in LowRAM AI.
* LAN API (`scope="cluster"`, served only by the TLS device server): designed by the devices owner, all requests HMAC-signed except the one-time pairing call.

### Desktop (`desktop/launcher.py`)

`main(argv) -> int`: pick a port (prefer 8765), `app.start_background(port)`,
open a native window via pywebview (fallback: default browser), single
instance (if LowRAM AI already answers on the port, just open it), on close
call `app.shutdown(server)`.

## UI direction

Calm, modern, Claude-like: left sidebar (new chat, chat history, sections:
Chat, Code, Images & Video, Task queue, Devices, Models & Settings), a
centered conversation column with streaming answers, a composer at the bottom,
a first-run setup that recommends a model for the machine's RAM and installs
everything with one click. Light and dark theme. Works offline (no CDNs).
