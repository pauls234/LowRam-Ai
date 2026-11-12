"""General task queue: run one task at a time, or a bounded loop of tasks.

Every task outcome is recorded explicitly (``done`` with a result artifact, or
``failed`` with an error). Nothing is retried automatically.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from . import artifacts, config, db, llm, memory

log = logging.getLogger("lowram.runner")

MAX_TASKS_PER_PROJECT = 200
MAX_TASK_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_LOOP_TASKS = 100
DEFAULT_LOOP_TASKS = 5

STOP_REASONS = ("queue_empty", "max_tasks", "failure", "paused_low_ram", "stop_requested", "busy", "error")

SYSTEM_PROMPT = (
    "You are LowRAM AI, a careful assistant running on a local model. You work through a "
    "queue of tasks one at a time. Answer the current task directly and concisely. Use the "
    "results of earlier tasks only as context. If you are unsure or lack information, say so "
    "explicitly instead of guessing. Never claim to have run code, browsed the web or "
    "modified files."
)


# --------------------------------------------------------------------------- validation


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Project name is required")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"Project name must be at most {MAX_NAME_CHARS} characters")
    return name


def parse_task_lines(text: object) -> list[str]:
    """Split text into tasks: one per non-empty line."""
    if not isinstance(text, str):
        raise ValueError("Tasks must be text with one task per line")
    tasks = [line.strip() for line in text.splitlines() if line.strip()]
    if not tasks:
        raise ValueError("Add at least one task (one per line)")
    if len(tasks) > MAX_TASKS_PER_PROJECT:
        raise ValueError(f"At most {MAX_TASKS_PER_PROJECT} tasks per project")
    for task in tasks:
        if len(task) > MAX_TASK_CHARS:
            raise ValueError(f"Each task must be at most {MAX_TASK_CHARS} characters: {task[:40]}…")
    return tasks


def validate_max_tasks(value: object) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("max_tasks must be a whole number")
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_tasks must be a whole number") from exc
    if not 1 <= count <= MAX_LOOP_TASKS:
        raise ValueError(f"max_tasks must be between 1 and {MAX_LOOP_TASKS}")
    return count


def require_general(project_id: int) -> dict:
    project = db.get_project(project_id)
    if project["kind"] != "general":
        raise ValueError(
            f"Project #{project_id} is a coding project; use the coding agent (plan / step / approve)."
        )
    return project


# --------------------------------------------------------------------------- projects


def create_project(name: object, tasks_text: object) -> int:
    clean_name = validate_name(name)
    tasks = parse_task_lines(tasks_text)
    return db.create_project(clean_name, "general", tasks=[(task, "") for task in tasks])


def add_tasks(project_id: int, tasks_text: object) -> list[int]:
    project = require_general(project_id)
    tasks = parse_task_lines(tasks_text)
    if project["total"] + len(tasks) > MAX_TASKS_PER_PROJECT:
        raise ValueError(f"At most {MAX_TASKS_PER_PROJECT} tasks per project")
    return db.add_tasks(project_id, [(task, "") for task in tasks])


# --------------------------------------------------------------------------- running


def build_context(project_id: int, task: dict, char_budget: int, skipped: list[int] | None = None) -> str:
    """Carry-over context: newest earlier results first, cut to ``char_budget``.

    Ids of earlier tasks whose result file could not be read are appended to
    ``skipped`` so the caller can record the gap instead of hiding it.
    """
    previous = [
        t for t in db.tasks_with_status(project_id, ["done"]) if t["position"] < task["position"]
    ]
    pieces: list[str] = []
    remaining = char_budget
    for prev in reversed(previous):
        header = f"Task {prev['position']}: {prev['title']}\nResult:\n"
        room = remaining - len(header)
        if room < 200:
            break
        try:
            text = artifacts.read_result(prev["result_path"]).strip()
        except artifacts.ArtifactError as exc:
            log.warning("Context for task #%s skips task #%s: %s", task["id"], prev["id"], exc)
            if skipped is not None:
                skipped.append(int(prev["id"]))
            continue
        if len(text) > room:
            text = text[: room - 16].rstrip() + "\n[…truncated]"
        pieces.append(header + text)
        remaining -= len(header) + len(text)
    return "\n\n".join(reversed(pieces))


def _messages(task: dict, context: str) -> list[dict]:
    parts = []
    if context:
        parts.append("Results of earlier tasks in this project (context, possibly truncated):\n\n" + context)
    parts.append(f"Current task ({task['position']}): {task['title']}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, (llm.LLMError, artifacts.ArtifactError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def record_failure(task_id: int, message: str, cause: BaseException | None = None) -> dict:
    """Mark a running task failed; if even that fails, raise with both errors attached."""
    try:
        return db.finish_task(task_id, "failed", error=message)
    except Exception as exc:
        log.exception("Could not record the failure of task #%s (%s)", task_id, message)
        raise RuntimeError(
            f"Task #{task_id} failed ({message}) and the failure could not be saved: {exc}. "
            "Restart LowRAM AI; the task will then be recovered as failed."
        ) from (cause or exc)


def run_next(project_id: int, percent: object = memory.DEFAULT_PERCENT) -> dict:
    """Run the next pending task of a general project.

    Returns a dict with ``status`` one of ``done``, ``failed``, ``paused``
    (not enough free RAM) or ``empty`` (no pending task). Raises ``db.Busy``
    when another inference is in progress.
    """
    pct = memory.validate_percent(percent)
    require_general(project_id)
    mem = memory.plan(pct)
    outcome: dict = {"task": None, "result": None, "error": None, "reason": None, "memory": mem.to_dict()}
    if not mem.can_run:
        return {**outcome, "status": "paused", "reason": mem.reason}
    task = db.claim_next_task(project_id)
    if task is None:
        return {**outcome, "status": "empty", "reason": "No pending tasks in this project."}
    started = time.monotonic()
    skipped: list[int] = []
    try:
        context = build_context(project_id, task, mem.context_chars, skipped)
        answer = llm.chat(_messages(task, context), max_tokens=mem.max_tokens)
        metadata = {
            "project_id": project_id,
            "task_id": task["id"],
            "position": task["position"],
            "title": task["title"],
            "model": config.llm_model(),
            "endpoint": llm.redact_url(config.llm_url()),
            "percent": pct,
            "max_tokens": mem.max_tokens,
            "context_chars": len(context),
            "context_skipped_task_ids": skipped,
            "duration_s": round(time.monotonic() - started, 2),
            "created_at": db.utcnow(),
        }
        path = artifacts.write_task_result(project_id, task["id"], answer, metadata)
        done = db.finish_task(task["id"], "done", result_path=str(path))
        return {**outcome, "status": "done", "task": done, "result": answer}
    except Exception as exc:  # every failure is recorded on the task, never swallowed
        failed = record_failure(task["id"], describe_error(exc), exc)
        return {**outcome, "status": "failed", "task": failed, "error": failed["error"]}
    except BaseException:
        try:
            db.finish_task(task["id"], "failed", error="Interrupted before the task finished")
        except Exception:  # keep the original KeyboardInterrupt/SystemExit
            log.exception("Could not record the interruption of task #%s", task["id"])
        raise


def run_loop(
    project_id: int,
    percent: object = memory.DEFAULT_PERCENT,
    max_tasks: object = DEFAULT_LOOP_TASKS,
    *,
    stop_event: threading.Event | None = None,
    on_step: Callable[[dict], None] | None = None,
    stop_on_failure: bool = True,
) -> dict:
    """Run pending tasks one after another until a stop condition is met.

    Stop conditions: queue empty, ``max_tasks`` executed, a task failed
    (``stop_on_failure``), RAM too low, another inference busy, or
    ``stop_event`` set (checked between tasks; a running inference is never
    killed mid-way).
    """
    pct = memory.validate_percent(percent)
    limit = validate_max_tasks(max_tasks)
    require_general(project_id)
    summary: dict = {
        "project_id": project_id,
        "percent": pct,
        "max_tasks": limit,
        "completed": 0,
        "failed": 0,
        "steps": [],
        "stopped_because": None,
        "message": "",
    }
    while True:
        if stop_event is not None and stop_event.is_set():
            summary.update(stopped_because="stop_requested", message="Stopped on request.")
            break
        if len(summary["steps"]) >= limit:
            summary.update(stopped_because="max_tasks", message=f"Reached the limit of {limit} task(s).")
            break
        try:
            outcome = run_next(project_id, pct)
        except db.Busy as exc:
            summary.update(stopped_because="busy", message=str(exc))
            break
        if on_step is not None:
            on_step(outcome)
        status = outcome["status"]
        if status in ("done", "failed"):
            summary["steps"].append(
                {"task_id": outcome["task"]["id"], "status": status, "error": outcome["error"]}
            )
        if status == "done":
            summary["completed"] += 1
        elif status == "failed":
            summary["failed"] += 1
            if stop_on_failure:
                summary.update(stopped_because="failure", message=outcome["error"] or "Task failed.")
                break
        elif status == "paused":
            summary.update(stopped_because="paused_low_ram", message=outcome["reason"])
            break
        elif status == "empty":
            summary.update(stopped_because="queue_empty", message="All tasks processed.")
            break
    return summary
