"""Big jobs on small RAM: the model splits a big goal into independent subtasks.

``POST /api/autosplit {goal, percent?, max_tasks? (2..12, default 8), distribute?: bool}``
→ 202 ``{project, tasks, merge: bool, distributed: bool, host_loop: str}``

* One planning call while holding the inference slot ("planning big job"):
  the model answers ``{"tasks": ["…"]}`` – independent subtasks whose results
  can be merged, in the goal's language. They are cleaned (control
  characters, list markers), shortened to 400 characters, deduplicated and
  capped at ``max_tasks``; fewer than two usable ones → 502, and the raw
  output is kept under ``<artifacts>/autosplit/``.
* A general project named after the goal gets one task per subtask,
  ``"<subtask>\\n\\nGesamtziel: <goal>"`` (the goal shortened to the runner's
  task limit), and this machine's loop starts working on it.
* The results are combined by the Devices merge (``cluster.distribute`` with
  ``merge=True``; ``GET /api/cluster/projects/<id>/final``), which also works
  without other devices. ``distribute`` only decides whether paired helper
  devices may claim tasks: without it the project is registered for the merge
  only once no task is pending or running (remembered in ``autosplit_deferred``,
  so this survives a restart), so helpers never see one of its tasks.
* Without the Devices module (or when registering fails) the local loop runs
  alone and ``merge`` is false.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import agent, artifacts, config, db, llm, memory, runner, settings, web
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.autosplit")

MAX_GOAL_CHARS = 4000
MIN_TASKS = 2
MAX_TASKS = 12
DEFAULT_TASKS = 8
MAX_SUBTASK_CHARS = 400
MAX_NAME_CHARS = 80
PLAN_MIN_TOKENS = 1024
PLAN_TEMPERATURE = 0.2
SLOT_LABEL = "planning big job"
GOAL_LABEL = "Gesamtziel"
DEFAULT_NAME = "Großer Auftrag"
WATCH_INTERVAL_S = 5.0

PLAN_SYSTEM_PROMPT = (
    "You are the planner of LowRAM AI. A small local model with little memory will work on a big job one "
    "subtask at a time, and all results are merged into one final result at the end. Split the user's goal "
    "into independent subtasks: each one can be done on its own without the results of the others, together "
    "they cover the whole goal, and their results can be merged. Every subtask is one self-contained "
    f"instruction of at most {MAX_SUBTASK_CHARS} characters. Write the subtasks in the language of the goal. "
    'Reply with JSON only, exactly in this shape: {"tasks": ["first subtask", "second subtask"]}'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS autosplit_deferred (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    percent INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""
db.register_schema(SCHEMA)

_LIST_MARK = re.compile(r"^(?:[-*•]\s+|\d{1,2}[.):]\s+)")


# --------------------------------------------------------------------------- validation and parsing


def validate_goal(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("'goal' must be text describing the big job")
    goal = agent.clean_text(value).strip()
    if not goal:
        raise ValueError("Describe the big job in 'goal'")
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"'goal' must be at most {MAX_GOAL_CHARS} characters")
    return goal


def validate_max_tasks(value: object) -> int:
    if value is None:
        return DEFAULT_TASKS
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_TASKS <= value <= MAX_TASKS:
        raise ValueError(f"'max_tasks' must be a whole number between {MIN_TASKS} and {MAX_TASKS}")
    return value


def shorten(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, at a word boundary when possible, with "…"."""
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip() + "…"


def project_name(goal: str) -> str:
    return shorten(" ".join(goal.split()), MAX_NAME_CHARS) or DEFAULT_NAME


def clean_subtask(value: str) -> str:
    text = " ".join(agent.clean_text(value).split())
    return shorten(_LIST_MARK.sub("", text).strip(), MAX_SUBTASK_CHARS)


def task_text(subtask: str, goal: str) -> str:
    prefix = f"{subtask}\n\n{GOAL_LABEL}: "
    return prefix + shorten(goal, runner.MAX_TASK_CHARS - len(prefix))


def _task_items(raw: str) -> list:
    try:
        data = agent.extract_json_object(raw)
    except ValueError:
        text = llm.strip_reasoning(raw)  # tolerate a bare JSON list
        start = text.find("[")
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start) if start >= 0 else (None, 0)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return data
        raise
    for key in ("tasks", "subtasks", "steps"):
        if isinstance(data.get(key), list):
            return data[key]
    raise ValueError('the reply has no "tasks" list')


def parse_tasks(raw: str, max_tasks: int) -> list[str]:
    """Clean, deduplicated subtasks from the model's reply (raises ``ValueError`` if < 2)."""
    tasks: list[str] = []
    seen: set[str] = set()
    for item in _task_items(raw):
        if isinstance(item, dict):
            item = next((item[k] for k in ("task", "title", "description") if isinstance(item.get(k), str)), None)
        if not isinstance(item, str):
            continue
        text = clean_subtask(item)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        tasks.append(text)
        if len(tasks) == max_tasks:
            break
    if len(tasks) < MIN_TASKS:
        raise ValueError(f"it contains fewer than {MIN_TASKS} usable subtasks")
    return tasks


# --------------------------------------------------------------------------- planning


def save_raw(raw: str) -> Path:
    folder = config.artifacts_dir() / "autosplit"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    return artifacts.write_text_atomic(folder / f"plan_{stamp}.raw.txt", raw)


def plan_tasks(goal: str, max_tasks: int, plan: memory.MemoryPlan) -> tuple[list[str], str]:
    """One model call inside the inference slot. 409 when busy / no model, 502 on unusable output."""
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Goal:\n{goal}\n\nSplit this goal into {MIN_TASKS} to {max_tasks} independent subtasks. "
            "Reply with the JSON only."
        )},
    ]
    with db.inference_slot(SLOT_LABEL):
        problem = runner.model_problem()  # starts the managed engine if needed
        if problem:
            raise HTTPError(HTTPStatus.CONFLICT, problem)
        try:
            raw = llm.chat(messages, max_tokens=max(plan.max_tokens, PLAN_MIN_TOKENS), temperature=PLAN_TEMPERATURE)
        except llm.LLMError as exc:
            raise HTTPError(HTTPStatus.BAD_GATEWAY, f"The model could not plan the job: {exc}") from exc
    try:
        return parse_tasks(raw, max_tasks), raw
    except ValueError as exc:
        path = save_raw(raw)
        raise HTTPError(
            HTTPStatus.BAD_GATEWAY, f"The model did not return usable subtasks ({exc}). Raw output: {path}"
        ) from exc


def create_project(goal: str, tasks: list[str], raw: str) -> int:
    name = runner.validate_name(project_name(goal))
    project_id = db.create_project(name, "general", goal=goal, tasks=[(task_text(t, goal), "") for t in tasks])
    artifacts.write_json_atomic(
        artifacts.project_dir(project_id) / "autosplit.json",
        {"goal": goal, "tasks": tasks, "raw_output": raw, "created_at": db.utcnow()},
    )
    return project_id


# --------------------------------------------------------------------------- running and merging


def _cluster() -> Any:
    try:
        from . import cluster
    except Exception:  # optional module (ImportError or a broken dependency)
        log.warning("The Devices module is not available; big jobs run without combining", exc_info=True)
        return None
    return cluster


def _start_local_loop(server: Any, project_id: int, percent: int) -> str:
    loop = getattr(server, "loop", None)
    if loop is None:
        return "not started: no local loop available"
    try:
        loop.start(project_id, percent, runner.MAX_LOOP_TASKS)
    except db.Busy as exc:
        return f"not started: {exc}"
    return "started"


def launch(server: Any, project_id: int, percent: int, distribute: bool) -> dict:
    """Enable the merge (now, or deferred without ``distribute``) and start this machine's loop."""
    cluster = _cluster()
    merge = False
    if cluster is not None:
        try:
            if distribute:
                cluster.distribute(project_id, percent, True)
            else:
                _defer_merge(project_id, percent)
            merge = True
        except Exception as exc:
            log.warning("Project #%s: combining the results is not available: %s", project_id, exc)
    if merge:
        host_loop = cluster._start_host_loop(server, project_id, percent)  # resumed after a busy stop
    else:
        host_loop = _start_local_loop(server, project_id, percent)
    return {"merge": merge, "distributed": merge and distribute, "host_loop": host_loop}


_WATCH_LOCK = threading.Lock()
_WAKE = threading.Event()
_watcher: tuple[threading.Thread, threading.Event] | None = None


def _defer_merge(project_id: int, percent: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO autosplit_deferred (project_id, percent, created_at) VALUES (?, ?, ?)",
            (project_id, percent, db.utcnow()),
        )
    _ensure_watcher()


def _deferred() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT project_id, percent FROM autosplit_deferred ORDER BY project_id")]


def _forget(project_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM autosplit_deferred WHERE project_id = ?", (project_id,))


def check_deferred() -> list[int]:
    """Register deferred projects whose tasks are all finished for the merge; returns their ids.

    Meanwhile a loop that stopped because the slot was busy is resumed, as the
    Devices coordinator does for shared projects.
    """
    cluster = _cluster()
    if cluster is None:
        return []
    registered = []
    for row in _deferred():
        pid, percent = row["project_id"], row["percent"]
        try:
            counts = db.get_project(pid)["counts"]
        except db.NotFound:
            _forget(pid)
            continue
        if counts["pending"] or counts["running"]:
            if counts["pending"] and pid in cluster._STATE.auto_loop:
                cluster._maybe_restart_host_loop(pid, percent)
            continue
        try:
            cluster.distribute(pid, percent, True)
        except Exception as exc:  # e.g. busy combining: try again on the next round
            log.warning("Project #%s: could not register the merge yet: %s", pid, exc)
            continue
        _forget(pid)
        registered.append(pid)
    return registered


def _watch(stop: threading.Event) -> None:
    global _watcher
    while True:
        _WAKE.wait(WATCH_INTERVAL_S)
        _WAKE.clear()
        if stop.is_set():
            return
        try:
            check_deferred()
        except Exception:
            log.exception("Checking deferred merges of big jobs failed")
        with _WATCH_LOCK:  # decide and unregister atomically, so _ensure_watcher never misses a row
            try:
                remaining = bool(_deferred())
            except Exception:  # database unavailable: the startup hook resumes the watch
                remaining = False
            if not remaining or stop.is_set():
                if _watcher is not None and _watcher[0] is threading.current_thread():
                    _watcher = None
                return


def _ensure_watcher() -> None:
    global _watcher
    with _WATCH_LOCK:
        if _watcher is not None and _watcher[0].is_alive():
            return
        stop = threading.Event()
        thread = threading.Thread(target=_watch, args=(stop,), name="lowram-autosplit-merge", daemon=True)
        _watcher = (thread, stop)
        thread.start()


def stop_watcher() -> None:
    """Stop the deferred-merge watcher (shutdown and tests); deferred rows stay for the next start."""
    global _watcher
    with _WATCH_LOCK:
        current, _watcher = _watcher, None
    if current is not None:
        current[1].set()
        _WAKE.set()
        if current[0] is not threading.current_thread():
            current[0].join(5)


@web.on_startup
def on_app_startup(server: Any) -> None:
    if _deferred():
        _ensure_watcher()


@web.on_shutdown
def on_app_shutdown(server: Any) -> None:
    stop_watcher()


# --------------------------------------------------------------------------- route


@route("POST", r"/api/autosplit")
def post_autosplit(req: Request) -> Response:
    data = req.json()
    goal = validate_goal(data.get("goal"))
    raw_percent = data.get("percent")
    percent = memory.validate_percent(settings.get("budget_percent") if raw_percent is None else raw_percent)
    max_tasks = validate_max_tasks(data.get("max_tasks"))
    distribute = data.get("distribute", False)
    if not isinstance(distribute, bool):
        raise ValueError("'distribute' must be true or false")
    loop = getattr(req.server, "loop", None)
    if loop is not None and loop.is_active():
        raise db.Busy("A task loop is running. Stop it or let it finish, then start the big job.")
    plan = memory.plan(percent)
    if not plan.can_run:
        raise HTTPError(HTTPStatus.CONFLICT, plan.reason)
    tasks, raw = plan_tasks(goal, max_tasks, plan)
    project_id = create_project(goal, tasks, raw)
    started = launch(req.server, project_id, percent, distribute)
    return Response(
        {"project": db.get_project(project_id), "tasks": db.list_tasks(project_id), **started}, HTTPStatus.ACCEPTED
    )
