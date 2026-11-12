# LowRAM AI

A **local, low-memory task runner** for small language models on your own machine
(built for macOS, also runs on Linux and Windows with Python 3.10+).

It gives you a browser dashboard and a CLI on top of any local
OpenAI-compatible inference server (for example `llama-server` from llama.cpp):

* a persistent **SQLite task queue** – one task per line, processed strictly
  **one inference at a time**;
* an adjustable **20–80 % RAM planning budget** with a live RAM check that
  **pauses** when free memory is too low;
* a **bounded loop** that works through the queue unattended and stops on its
  own (empty queue, task limit, failure, low RAM or your stop button);
* an experimental **coding agent** that plans small steps, proposes file
  changes for review and only touches your repository after **explicit human
  approval** – in single steps or as an interactive review loop;
* results stored as plain files on disk, with **explicit failure recording** –
  nothing is ever silently reported as a success.

The model is **not** downloaded or bundled. You bring your own GGUF model.

> Status: prototype (v0.2). Local, single-user use only. Read [Security](#security)
> before you use it with anything important.

---

## Quick start (macOS)

Requirements: Python 3.10+ and a running OpenAI-compatible endpoint.

```bash
git clone https://github.com/YOUR_USERNAME/LowRAM-AI.git
cd LowRAM-AI
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

In a second terminal, start a model server, e.g. with llama.cpp
(`brew install llama.cpp`):

```bash
llama-server -m /path/to/your/model.gguf --host 127.0.0.1 --port 8080 -c 4096
```

Then start the dashboard:

```bash
python -m backend.app
```

Open <http://127.0.0.1:8765>, create a project with one task per line, and
either click **Run next task** or start a **bounded loop**.

---

## Dashboard

* **RAM card** – total / available memory, the budget derived from your slider
  and a *Ready* / *Paused – low RAM* indicator.
* **Planning budget (20–80 %)** – sizes the prompt: how much carry-over
  context from earlier results is included and the `max_tokens` limit. It is a
  *scheduling hint*, not a hard memory cap (the inference server owns its memory).
* **Projects** – create a project with one task per line; see each task's
  status, attempts, errors and stored result.
* **Run next task** – runs exactly one task and shows the result.
* **Run loop** – runs pending tasks one after another in the background and
  stops automatically when:
  `queue_empty` · `max_tasks` reached (1–100) · a task `failure` ·
  `paused_low_ram` · `stop_requested` (finishes the current task first) ·
  `busy` (another inference holds the lock).
* **Retry** a failed task, **Recover stuck tasks** after a crash.

Each task receives a limited **carry-over context**: the newest results of
earlier tasks in the same project, truncated to the budget.

---

## CLI

```bash
python3 lowram.py memory --ram 40                 # RAM + derived limits
python3 lowram.py serve                           # same as python -m backend.app

# General task queue
python3 lowram.py new "Research" --task "Summarise X" --task "List risks of X"
python3 lowram.py new "Batch" --file tasks.txt    # one task per line
python3 lowram.py run PROJECT_ID --ram 40 --max-tasks 10   # bounded loop
python3 lowram.py tasks PROJECT_ID
python3 lowram.py result TASK_ID
python3 lowram.py retry TASK_ID
python3 lowram.py recover                         # crash recovery
python3 lowram.py projects
```

Add `--json` before the command for machine-readable output
(`python3 lowram.py --json tasks 1`).

---

## Coding agent (experimental)

The coding agent indexes a local repository, asks the model for a small plan,
and then proposes **complete file contents** for one step at a time. Proposals
are written to disk; your repository is only changed by `approve`.

```bash
python3 lowram.py create /absolute/path/to/project "Add tests for the API"
python3 lowram.py plan PROJECT_ID --ram 40
python3 lowram.py step PROJECT_ID --ram 40        # writes a proposal, changes nothing
python3 lowram.py show PROJECT_ID TASK_ID         # summary + unified diff
python3 lowram.py approve PROJECT_ID TASK_ID      # apply after review
python3 lowram.py reject PROJECT_ID TASK_ID --reason "too broad"
python3 lowram.py add-step PROJECT_ID "Fix typo in README" --file README.md
```

**Review loop** – propose → show diff → you answer `y` / `n` / `q` → next step:

```bash
python3 lowram.py loop PROJECT_ID --ram 40 --max-steps 5
```

The loop requires an interactive terminal and never approves anything on its own.

From Python:

```python
from backend.agent import create_coding_project, plan, execute_coding_step, approve, reject

pid = create_coding_project("Add a documented helper function", "/absolute/path/to/your/repository")
plan(pid)
proposal = execute_coding_step(pid, percent=40)
print(proposal["diff"])            # also on disk: proposal["proposal_path"], proposal["diff_path"]
# approve(pid, proposal["task_id"])   # only after review
```

Safety rules enforced by the agent:

* The model **cannot run shell commands or code**. Proposals are syntax-checked
  by parsing (Python via `ast`, JSON, and TOML on Python 3.11+) – never executed.
* Paths must stay inside the repository: no absolute paths, `..`, symlinks,
  `.git`, hidden directories, dependency/build folders, secret-looking files
  (`.env`, keys, credentials) or non-text files.
* Files that run automatically are **never written**: `.github/` (CI
  workflows), `.pre-commit-config.yaml`, `sitecustomize.py`. Files that run
  code on build/test/install (`Makefile`, `setup.py`, `conftest.py`,
  `package.json`, `*.sh`, …) can be changed but are flagged **ATTENTION** in
  the review.
* Content with invisible or control characters (ANSI escapes, bidi overrides,
  zero-width characters) is rejected, so the diff you review is the file you
  get. Windows (CRLF) line endings of existing files are preserved.
* An existing file can only be replaced if the model was shown its full
  content (no blind overwrites); a step may change at most 10 files of ≤ 256 KiB.
* Only one proposal may await review per project.
* `approve` refuses if the proposal file was modified after it was generated
  (SHA-256) or if a target file changed in the meantime, keeps backups of
  overwritten files under `~/.lowram-ai/artifacts/project_N/backups/` and rolls
  back if a write fails.
* Files are never deleted by the agent.

---

## Skills (download only)

```bash
python3 lowram.py skill install https://raw.githubusercontent.com/OWNER/REPO/main/SKILL.md my-skill
python3 lowram.py skill list
```

Downloads up to **64 KB of Markdown** from `raw.githubusercontent.com` or
`gist.githubusercontent.com` (https only, no redirects), stores it under
`~/.lowram-ai/skills/` and records its SHA-256 checksum. Skills are **not
trusted, executed or injected into prompts**; a reviewed skill loader is planned.

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOWRAM_LLM_URL` | `http://127.0.0.1:8080/v1/chat/completions` | OpenAI-compatible chat endpoint |
| `LOWRAM_LLM_MODEL` | `local-model` | `model` field sent to the endpoint |
| `LOWRAM_LLM_API_KEY` | – | optional bearer token for the endpoint |
| `LOWRAM_LLM_TIMEOUT` | `600` | seconds to wait for one answer |
| `LOWRAM_MIN_FREE_MB` | `1024` | tasks pause below this much available RAM |
| `LOWRAM_HOME` | `~/.lowram-ai` | database, artifacts and skills |
| `LOWRAM_PORT` | `8765` | dashboard port (always bound to 127.0.0.1) |

Data layout:

```
~/.lowram-ai/
├── lowram.db                         # SQLite: projects, tasks, locks
├── artifacts/project_N/
│   ├── task_ID.md / task_ID.json     # result + metadata (general tasks)
│   ├── index.json, plan.json         # coding agent: repo index and plan
│   ├── task_ID.proposal.json / .diff # coding agent: proposal for review
│   └── backups/task_ID/…             # files overwritten by approve
└── skills/NAME.md / NAME.json
```

---

## How it works

```
backend/
├── config.py     environment configuration
├── memory.py     psutil RAM check + 20–80 % budget → context/max_tokens
├── db.py         SQLite queue, atomic claims, global inference lock, crash recovery
├── llm.py        stdlib client for /v1/chat/completions (loopback bypasses proxies)
├── artifacts.py  atomic writes of results, proposals and backups
├── runner.py     run_next() and the bounded run_loop()
├── agent.py      coding agent: index → plan → propose → approve/reject
├── skills.py     restricted Markdown skill downloader
├── app.py        http.server dashboard + JSON API + background loop controller
├── cli.py        argparse CLI (lowram.py)
└── static/       dashboard (vanilla HTML/CSS/JS, no external resources)
```

* **Locking:** a task is claimed with a `BEGIN IMMEDIATE` transaction. While
  any task is `running` (or the planning lock is held) no other inference can
  start – across projects, browser tabs, the CLI and the dashboard.
* **Crash recovery:** every running task records its owner (`host:pid:start-time`).
  If that process is gone (or the PID was reused by another process), the task
  is marked `failed` with an explanation the next time anything touches the
  queue (and when the dashboard starts). `lowram.py retry TASK_ID` queues it
  again. Nothing is retried automatically. `lowram.py recover --force` clears
  everything that is still marked running – only use it when no LowRAM AI
  process is working.
* **Exit codes (CLI):** `0` success, `1` error or failed task, `2` usage error,
  `3` nothing ran (low RAM or busy).

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite uses a fake OpenAI-compatible server, so no model is needed.
GitHub Actions runs it on Ubuntu and macOS with Python 3.10–3.13.

---

## Security

* The dashboard binds to **127.0.0.1 only**. **Do not expose port 8765** (no
  port forwarding, no reverse proxy, no `0.0.0.0`).
* There is **no authentication**. Basic protections exist: requests with a
  non-loopback `Host` header are rejected (DNS rebinding), every POST needs a
  per-process token embedded in the page (CSRF), and a strict
  Content-Security-Policy is sent. Model output is rendered as plain text.
* Treat model output as **untrusted**. Review every coding proposal before you
  approve it, and do not use the prototype with sensitive repositories.
* Do not commit private projects, API keys, credentials or proprietary skills.

## Not implemented yet

Autonomous task decomposition for general projects, automatic test execution /
verification, automatic retries, ETA, fine-tuning, multi-device coordination,
web browsing, hard RAM enforcement or swap management, sandboxed code
execution, an MLX engine, native desktop packaging, and authentication for the
web UI.

## Roadmap

1. Test-based verification for coding steps (run the project's tests in a sandbox).
2. Resource measurement of the inference process itself (not just system RAM).
3. Reviewed skill loader with pinned checksums.
4. Tauri desktop shell.
5. Distributed workers with mutual authentication.
