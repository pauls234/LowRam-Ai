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

**Own models (v0.3.1).** `CatalogModel` gains `source: "catalog"|"custom"|"local"`,
`path: str|null`, `available: bool` (file present – false when the external
drive is unplugged), `fits_ram: bool`.

* `POST /api/engine/custom/resolve {source}` – `source` is `owner/repo`, a
  Hugging Face repo URL or a direct `…/resolve/<rev>/<file>.gguf` URL →
  `{repo, revision, files: [{filename, size_bytes, quant, fits_ram, recommended}]}`
  (GGUF files only, split `-00001-of-0000N` files grouped as one entry).
* `POST /api/engine/custom/install {repo, filename, name?, purpose?}` → 202 `{download, model_id}`
  (size + SHA-256 from the Hugging Face API; unverified downloads refused).
* `POST /api/engine/custom/import {path, name?, purpose?}` → 201 `{model}` – registers an
  existing local `.gguf` (e.g. on an external drive) without copying it.
  Deleting an imported model only unregisters it; the file is never removed.
* `GET /api/engine/storage` → `{models_dir, default_dir, custom: bool, available: bool,
  writable: bool, free_bytes, total_bytes, moving: {state, bytes_done, bytes_total, error}|null}`
* `POST /api/engine/storage {models_dir: str|null, move_existing: bool}` → 202 `{storage}` –
  `null` resets to the default folder. The folder must be absolute, exist, be
  writable and not be a system location. Moving runs in the background (copy,
  verify size, then delete the source); the engine must be stopped meanwhile.
* `config.models_dir()` resolves `LOWRAM_MODELS_DIR` > `settings.engine.models_dir` >
  `<home>/models` and never creates a custom folder. Downloads may write below
  `LOWRAM_HOME` **or** below `config.models_dir()`.

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
**Easy pairing (v0.3.1) – like Bluetooth.** While Devices is enabled and
`cluster.discoverable` is true, the host announces itself on the LAN (UDP
beacon, see `cluster.py`). Any LowRAM AI (even with Devices off) listens and
lists nearby hosts. Joining sends a pairing *request*; both screens show the
same 6-digit verification number derived from the host certificate
fingerprint and both nonces; the host user clicks Allow. A man-in-the-middle
would have a different certificate and therefore a different number.

* `GET /api/cluster/nearby` → `{hosts: [{id, name, addresses: [str], port, fingerprint_short, last_seen}]}`
* `POST /api/cluster/join {host_id, name?, percent?}` (discovered host) or `{code, name?, percent?}`
  (manual code, unchanged) → 202 `{worker}`; `worker.state` may be `"awaiting_approval"` with
  `worker.verification_code` ("123 456") to show on the helper.
* Host: `GET /api/cluster` adds `requests: [{id, name, platform, ram_total_bytes, address,
  verification_code, expires_at}]`; `POST /api/cluster/requests/<id>/approve {}` / `…/deny {}`.
  Requests expire after 2 minutes, at most 3 pending, rate-limited per address.
  The helper first sends only a commitment `sha256(client_nonce)`; it reveals
  the nonce on its first status poll, after it has the host nonce. The host
  shows the number (and accepts Allow) only after the reveal, so an attacker
  cannot try host nonces until the numbers happen to match.
  LAN calls: `POST /cluster/v1/pair-request {name, platform, ram_total_bytes, client_commit}`
  → `{request_id, host_nonce, expires_in}`; `POST /cluster/v1/pair-status {request_id, client_nonce}`
  → `{state: pending|approved|denied|expired, device_id?, device_key?}` (key returned once).
* Desktop window: `window.pywebview.api.pick_folder(title)` → absolute path or `null`
  (only inside the app window; the browser UI falls back to a text field).
* LAN API (`/cluster/v1/pair|heartbeat|claim|leave`, `/cluster/v1/tasks/<id>/result|fail`): see the `backend/cluster.py` docstring. Device leases are requeued after 90 s without a heartbeat (max 3 attempts) – the only automatic retry in LowRAM AI.
* LAN API (`scope="cluster"`, served only by the TLS device server): designed by the devices owner, all requests HMAC-signed except the one-time pairing call.

### Smart features (v0.3.2)

**Chat (`chat.py`)**
* Custom instructions: `settings.chat.instructions` (≤ 4000 chars, set via `POST /api/settings {section:"chat"}`) are appended to the system prompt of every conversation.
* Attachments: `POST /api/chats/<id>/messages {content, percent?, attachments?: [{name, content}]}` – text files only (≤ 5 files, ≤ 200 KB each, UTF-8, no NUL); stored with the user message (`Message.attachments: [{name, chars}]`) and put into the prompt within the context budget (truncated with a visible note).
* Message stats: `Message.tokens`, `Message.tokens_per_s`, `Message.finish_reason` (`"stop"|"length"|null`); `truncated: true` when the answer hit `max_tokens`.
* `POST /api/chats/<id>/continue {percent?}` – SSE stream that appends to the last assistant message (only when it is `truncated`).
* `POST /api/chats/<id>/regenerate {percent?}` – SSE stream that replaces the last assistant answer.
* `GET /api/chats?q=text` – search titles and message contents (case-insensitive).
* `GET /api/chats/<id>/export` – Markdown download (`FileResponse`, `text/markdown`).

**Power (`power.py`, MacBook-friendly long jobs)**
* `power.keep_awake(reason)` – context manager; macOS: `caffeinate -i -w <pid>` while any long job runs (reference-counted), Windows: `SetThreadExecutionState`, Linux: best effort; respects `settings.power.keep_awake`.
* `power.battery()` → `{available, percent, plugged}`; `power.battery_block_reason()` → text when on battery below `settings.power.battery_min_percent`, else `None`.
* `power.notify(title, body)` – system notification (macOS via `osascript` with arguments, never string-built scripts), respects `settings.power.notifications`; never raises.
* `GET /api/power` → `{battery, keep_awake: {enabled, active: [reasons]}, settings}`.
* Integration: task loop (stop reason `"on_battery"`, notification when finished), media jobs (wait while on low battery, notification when done), device worker (no claims on low battery), cluster host (notification on pairing request and when a merged result is ready).

### Polish round v0.3.1 (binding)

**Diagnostics + updates (`diagnostics.py`)**
* `GET /api/diagnostics` -> `{checks: [{id, title, status: "ok"|"warn"|"fail", detail, hint}], report: str}`; checks: engine runtime installed/version, model available (external drive!), llama-server health, RAM/disk free, models folder writable, media runtime, LAN (Devices listener, discovery port bound, openssl present), Python/platform/app version. `report` = plain-text copyable summary WITHOUT secrets/paths of user content (home dir shown as `~`).
* `POST /api/diagnostics/selftest {}` -> runs a tiny chat completion against the running model ("Antworte mit OK") and returns `{ok, seconds, tokens_per_s, error}`.
* `GET /api/update` -> `{current, latest: str|null, url: str|null, available: bool, checked: bool, error: str|null}`: checks `https://api.github.com/repos/pauls234/LowRam-Ai/releases/latest` (https only, allow-list api.github.com, 5 s timeout, cached 6 h, `settings.ui.check_updates` default true, repo constant in one place). A private repo answers 404 unauthenticated -> `checked: false` and NO error shown to the user (silent); never send tokens.

**Knowledge base (`knowledge.py`, local documents for chat)**
* Tables via db.register_schema: collections(id,name,created_at), documents(id,collection_id,name,chars,created_at), chunks(id,document_id,position,text); pure-Python BM25 search (no new deps), chunks ~800 chars with 100 overlap.
* `GET /api/knowledge` -> `{collections: [{id, name, documents: n, chars}]}`; `POST /api/knowledge {name}` -> 201; `POST /api/knowledge/<id>/documents {name, content}` (text/markdown/code; also base64 `pdf` is NOT supported) <= 2 MB each, <= 200 documents per collection; `GET /api/knowledge/<id>`; `POST /api/knowledge/<id>/documents/<doc_id>/delete`; `POST /api/knowledge/<id>/delete`.
* `POST /api/knowledge/<id>/search {query, k?}` -> `{results: [{document, text, score}]}`.
* Chat: `POST /api/chats/<id>/messages` accepts `collection_id`; the top chunks (within the context budget) are added to the prompt as "Quellen" with document names; the assistant message stores `sources: [{document, snippet}]` (Message.sources) and the UI shows them under the answer.

**German UI + menu bar**: UI strings in German (`backend/static/i18n.js`: `LR.t(key)` with de/en dictionaries, language from `settings.ui.language` = "auto"|"de"|"en", default "auto" -> browser language, German fallback); system prompts tell the model to answer in the user's language, default German. Menu-bar/tray icon in `desktop/` (open window, status, quit) must be optional (never crash the app if the tray library is missing).

**Models**: catalog gets `default_for: ["chat"|"code"|"general"]` hints; on first run with purpose "coding" the wizard recommends a Coder model that fits; catalog install failure "file not found" triggers self-healing: look up the repo tree via the HF API and offer the best matching Q4_K_M GGUF file (`POST /api/engine/models/<id>/install` returns 409 `{suggestion: {filename, size_bytes}}`, UI offers one-click retry with `{filename}`).

### Low-RAM power round v0.4 (binding)

**Engine turbo (`engine.py`)**
* Detect the flags the installed llama-server supports (parse `llama-server --help` once per binary, cached like the version) and only pass supported ones.
* Profiles `settings.engine.profile`: `"auto"` (default) | `"saver"` | `"fast"`. auto/saver: flash attention on (`-fa on` or `--flash-attn` depending on the help text), KV cache `q8_0` for K and V (`--cache-type-k/--cache-type-v`, only with flash attention), `--cache-reuse 256` (prompt prefix reuse across chat turns); context size derived from free RAM after the model file size (KV q8_0 ≈ half of f16), clamped 4096..32768 (saver: ≤8192); fast: f16 KV, larger `-b/-ub` batch if supported. Never pass a flag the binary lacks.
* `GET /api/engine/status` → `server.tuning: {profile, flash_attn, kv_cache, ctx_size, cache_reuse}` and `server.restarts`.
* Supervisor: if the managed llama-server crashes after being ready, restart it automatically with back-off (max 3 restarts per 10 minutes, then state error); never during shutdown or while paused.
* Interrupted model downloads (a `.part` file plus a known pending install recorded in `<home>/pending_installs.json`) resume automatically at startup.
* `GET /api/engine/search?q=text&limit=20` → `{models: [{repo, downloads, likes, updated, license|null}]}` via `https://huggingface.co/api/models?search=…&filter=gguf&sort=downloads&limit=…` (https, allow-list, 8 s timeout, cache 10 min); then `custom/resolve` lists the files with fits_ram.

**Long-chat memory (`chat.py`)**
* When a conversation no longer fits the context budget, older turns are summarised by the model (≤ 1200 chars, German if the chat is German) instead of being dropped; stored as `conversations.summary` + `summarized_until` (message id) and put into the system prompt as "Zusammenfassung des bisherigen Gesprächs". The summarisation runs inside the same stream before the answer and emits `{"type":"status","text":"Fasse ältere Nachrichten zusammen…"}`. `GET /api/chats/<id>` includes `summary`. Failure to summarise falls back to dropping (never blocks the answer).

**Big jobs on small RAM (`autosplit.py`)**
* `POST /api/autosplit {goal (≤4000 chars), percent?, max_tasks? (2..12, default 8), distribute?: bool}` → 202 `{project, tasks}`: the model plans independent subtasks (JSON), a general project is created with them plus merge enabled (`cluster` distribute with `merge: true`; works without other devices – the host's own loop processes the tasks), and the loop starts. Each subtask gets the goal as context. `GET /api/cluster/projects/<id>/final` delivers the combined result as before.

**Coding like Claude Code (`agent.py`, `api_coding.py`)**
* Exploration: during a step the model may answer `{"need_files": ["rel/path", …]}` (max 2 extra rounds, max 6 files, same path validation, within the context budget) before it must return the proposal.
* Test & fix loop: `POST /api/coding/projects/<pid>/test-command {command}` → `{command}` (≤300 chars, `shlex.split`, first token must resolve via PATH or be a relative path inside the repo; stored per project). `POST /api/coding/projects/<pid>/run-tests {}` → 202 `{run}`; `GET /api/coding/projects/<pid>/test-runs/<rid>` → `{run: {id, state: running|passed|failed|error|timeout, exit_code, output_tail (≤20 000 chars), started_at, finished_at}}`; `POST /api/coding/projects/<pid>/fix-tests {run_id}` → 201 `{task_id}` adds a step "Fehlschlagende Tests beheben" with the output tail as detail. Commands run ONLY after an explicit user request, never from model output: argument list, no shell, cwd = repo, timeout 10 min, environment without secrets, process group killed on timeout, one run at a time per project. Also: `POST /api/coding/projects/<pid>/test-runs/<rid>/cancel {}` → `{run}`; `GET /api/coding/projects/<pid>` includes `test_command` and `last_test_run`; step results include `explored_files`; absolute program paths are refused.

### Resilience on weak hardware v0.4 (binding) – "hold on instead of giving up"

Settings `resilience`: `wait_for_ram_minutes` (30), `idle_unload_minutes` (15, 0 = never), `retry_transient` (true), `first_token_timeout_s` (1800), `idle_token_timeout_s` (300).

* **No timeouts on slow machines (`llm.py`)**: `llm.chat()` uses streaming internally (accumulating `stream_chat`) whenever the endpoint supports it, so a long answer keeps the connection alive token by token. Timeouts: until the first token `first_token_timeout_s` (prompt processing can take minutes), afterwards `idle_token_timeout_s` between tokens – never a fixed total. Errors are classified: `LLMError.transient` = connection refused/reset/closed or HTTP 502/503 (engine restarting / loading); `LLMError.context_overflow` = HTTP 400 whose body mentions the context size.
* **Checkpoints (`runner.py`)**: while a task streams, the partial answer is written every ~5 s (atomic) to `artifacts/project_N/task_ID.partial.md`. On success it is replaced by the result file. On a transient error with `retry_transient`, the runner waits for the engine to be ready again (`engine.ensure_running`, up to 10 min) and continues ONCE from the checkpoint (prompt: previous messages + partial answer + "setze exakt dort fort, ohne Wiederholung"), recording `resumed_from_checkpoint` in the metadata; if that fails too, the task fails with the partial text kept and the error says so. On a context overflow the carry-over context is halved and the request retried once. The coding agent and autosplit get the same overflow retry.
* **RAM guard (`memory.py`)**: `memory.guard_wait(reason, max_wait_s, stop_event, on_wait)` – waits (checking every 5 s) until `plan().can_run`; returns False on timeout/stop. `runner.run_loop` uses it between tasks instead of stopping immediately (`paused_low_ram` only after `wait_for_ram_minutes`), reporting "Warte auf freien Arbeitsspeicher (… MB frei)…" through `on_step`/loop status. A background sampler (every 3 s, started from an on_startup hook) keeps `memory.pressure()` → `{level: "ok"|"warn"|"critical", available_bytes, swap_used_bytes, swap_growth_mb_per_min}` for the UI (`GET /api/memory` includes `pressure`). On macOS `kern.memorystatus_vm_pressure_level` (via `sysctl -n`, cached 3 s) is used when available.
* **Free RAM when idle (`engine.py`)**: the managed llama-server is stopped after `idle_unload_minutes` without any inference (track last use in `ensure_running`/llm calls through a small `engine.touch()`), state `stopped` with `unloaded_idle: true`; the next request loads it again automatically (chat shows the loading status).

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
