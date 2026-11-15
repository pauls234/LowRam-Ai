"""General task queue: run one task at a time, or a bounded loop of tasks.

Every task outcome is recorded explicitly (``done`` with a result artifact, or
``failed`` with an error). Resilience on weak hardware ("hold on instead of
giving up"):

* while a task streams, its partial answer is checkpointed (atomically, every
  ~15 s, plus a final one on error) to ``task_ID.partial.md``; on success the
  result file replaces it;
* after a transient model error (engine crash/restart, dropped or stalled
  connection) the task waits for the model (up to 10 min) and continues ONCE
  from the checkpoint – if that fails too, it fails with the partial text kept;
* a context overflow is retried once with half the carry-over context
  (:func:`call_with_overflow_retry` offers the same to other modules);
* :func:`run_loop` waits for free RAM between tasks (``memory.guard_wait``).

Nothing else is retried automatically.
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import artifacts, config, db, llm, memory, power, settings

log = logging.getLogger("lowram.runner")

CHECKPOINT_INTERVAL_S = 15.0  # spare the SSD; a final checkpoint is always written on error
ENGINE_WAIT_S = 600.0  # how long a task waits for the model after a transient error
ENGINE_STEP_S = 30.0  # one engine.ensure_running() call; the stop request is checked in between
ENGINE_POLL_S = 5.0
ENGINE_READY_POLL_S = 1.0  # the engine says "ready" but its URL does not answer yet (stale state after a crash)
RESUME_PROMPT = (
    "Deine Antwort oben wurde unterbrochen. Setze exakt dort fort, wo sie aufhört, ohne Wiederholung."
)
WAITING_FOR_MODEL = "Warte auf das Modell (Verbindung unterbrochen, es wird neu geladen)…"

MAX_TASKS_PER_PROJECT = 200
MAX_TASK_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_LOOP_TASKS = 100
DEFAULT_LOOP_TASKS = 5

STOP_REASONS = (
    "queue_empty", "max_tasks", "failure", "paused_low_ram", "no_model", "stop_requested", "busy", "error",
    "on_battery",
)

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


def model_problem() -> str | None:
    """``None`` when a model endpoint is usable, else why not.

    Starts the managed engine when needed (same rules as chat). Without the
    chat/engine modules, or with an external ``LOWRAM_LLM_URL``, nothing is checked.
    """
    try:
        from . import chat  # lazy: chat imports this module
    except ImportError:
        return None
    try:
        chat.ensure_model()
    except Exception as exc:  # HTTPError(409) with a user-facing reason
        return getattr(exc, "message", None) or describe_error(exc)
    return None


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


# --------------------------------------------------------------------------- resilience


def resilience() -> dict:
    """``settings.resilience`` with the defaults for anything missing or invalid."""
    defaults = settings.DEFAULTS["resilience"]
    try:
        stored = settings.get("resilience")
    except Exception:  # noqa: BLE001 - a broken settings file must not stop the queue
        log.warning("Could not read the resilience settings; using defaults", exc_info=True)
        stored = {}
    values = dict(defaults)
    for key, default in defaults.items():
        value = stored.get(key, default) if isinstance(stored, dict) else default
        if isinstance(default, bool):
            values[key] = value if isinstance(value, bool) else default
        elif (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(value) and value >= 0):
            values[key] = value
    return values


def partial_path(project_id: int, task_id: int) -> Path:
    """Checkpoint of a task's streaming answer: ``artifacts/project_N/task_ID.partial.md``."""
    return artifacts.project_dir(project_id) / f"task_{int(task_id)}.partial.md"


def join_continuation(prefix: str, continuation: str) -> str:
    """``prefix`` (an interrupted answer) followed by the model's continuation of it."""
    if not prefix.strip():
        return continuation
    if not continuation:
        return prefix
    if prefix[-1].isspace() or continuation[0].isspace():
        return prefix + continuation
    if prefix[-1] in ".!?:;,)" and continuation[0].isalnum():
        return f"{prefix} {continuation}"
    return prefix + continuation


def resume_messages(messages: list[dict], partial: str) -> list[dict]:
    """The original request plus the interrupted answer and the instruction to continue it."""
    return [*messages, {"role": "assistant", "content": partial}, {"role": "user", "content": RESUME_PROMPT}]


class Checkpoint:
    """Keeps a streaming answer in ``task_ID.partial.md`` (atomic writes, at most every ~15 s).

    ``prefix`` is the answer saved before a resume, ``current`` the visible
    text of the request that is running now; :attr:`text` combines both.
    Write errors are logged, never raised: a checkpoint must not fail a task.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.prefix = ""
        self.current = ""
        self.saved = False
        self._last_write = time.monotonic()

    @property
    def text(self) -> str:
        return join_continuation(self.prefix, self.current)

    def on_text(self, visible: str) -> None:
        """``llm.chat(on_text=…)`` callback: remember the text, write it every CHECKPOINT_INTERVAL_S."""
        self.current = visible
        if time.monotonic() - self._last_write >= CHECKPOINT_INTERVAL_S:
            self.flush()

    def flush(self) -> bool:
        """Write the current text now; True if a checkpoint with text exists afterwards."""
        self._last_write = time.monotonic()
        text = self.text
        if not text.strip():
            return self.saved
        try:
            artifacts.write_text_atomic(self.path, text)
            self.saved = True
        except (OSError, artifacts.ArtifactError) as exc:
            log.warning("Could not write the checkpoint %s: %s", self.path.name, exc)
        return self.saved

    def interrupted(self, partial: str = "") -> None:
        """The running request failed: everything received so far becomes the prefix."""
        if partial:
            self.current = partial
        self.prefix, self.current = self.text, ""
        self.flush()

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove the checkpoint %s: %s", self.path.name, exc)
        self.saved = False


def _managed_engine():
    """``backend.engine`` (lazy, optional) when LowRAM AI runs llama-server itself, else ``None``
    (``LOWRAM_LLM_URL`` or a chosen local AI server)."""
    if os.environ.get("LOWRAM_LLM_URL", "").strip() or config.local_server() is not None:
        return None
    name = f"{__package__}.engine"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            log.warning("The engine module could not be loaded: %s", exc)
        return None


def await_model(timeout: float | None = None, *, stop_event: threading.Event | None = None,
                on_wait: Callable[[str], None] | None = None) -> tuple[str | None, str | None]:
    """Wait until the model endpoint answers again after a transient error.

    Returns ``(url, None)`` with the URL to use from now on, or ``(None,
    reason)`` after ``ENGINE_WAIT_S`` or when ``stop_event`` is set (checked
    at least every ``ENGINE_STEP_S``). Managed engine: the URL returned by
    ``engine.ensure_running`` (restarts/loads it if needed) is only trusted
    once ``llm.endpoint_ready`` confirms it – right after a crash the engine
    may still report the old, dead port as ready; then it is asked again
    every ``ENGINE_READY_POLL_S``. External endpoint or no engine module: the
    configured URL is polled.
    """
    limit = ENGINE_WAIT_S if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + limit
    engine = _managed_engine()
    if on_wait is not None:
        on_wait(WAITING_FOR_MODEL)
    while True:
        if stop_event is not None and stop_event.is_set():
            return None, "stopped while waiting for the model"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, f"the model was not ready again within {limit / 60:g} min"
        pause = ENGINE_POLL_S
        if engine is not None:
            try:
                url = engine.ensure_running(timeout=min(remaining, ENGINE_STEP_S))
            except Exception as exc:  # noqa: BLE001 - EngineError (loading, paused, …): keep waiting
                log.info("Waiting for the model: %s", exc)
            else:
                url = url if isinstance(url, str) and url else config.llm_url()
                if llm.endpoint_ready(url, timeout=min(5.0, max(0.1, deadline - time.monotonic()))):
                    return url, None
                log.info("The engine reports %s as ready, but it does not answer yet", llm.redact_url(url))
                pause = ENGINE_READY_POLL_S
        else:
            url = config.llm_url()
            if llm.endpoint_ready(url, timeout=min(5.0, remaining)):
                return url, None
        pause = min(pause, max(0.0, deadline - time.monotonic()))
        if stop_event is not None:
            stop_event.wait(pause)
        else:
            time.sleep(pause)


def wait_for_model(timeout: float | None = None, *, stop_event: threading.Event | None = None,
                   on_wait: Callable[[str], None] | None = None) -> str | None:
    """Like :func:`await_model`; returns ``None`` when the model answers again, else the reason."""
    return await_model(timeout, stop_event=stop_event, on_wait=on_wait)[1]


def call_with_overflow_retry(
    build_messages: Callable[[int], list[dict]],
    context_chars: int,
    *,
    info: dict | None = None,
    **chat_kwargs,
) -> str:
    """``llm.chat(build_messages(context_chars), **chat_kwargs)`` with ONE retry on a context overflow.

    ``build_messages(budget)`` must build the request with at most ``budget``
    characters of carry-over context; after an ``LLMError`` with
    ``context_overflow`` it is called again with half the budget. Other errors
    (and a second overflow) are raised unchanged. ``info`` (optional dict)
    receives ``attempts``, ``context_retry`` and ``context_chars`` (the budget
    of the last request).
    """
    budget = max(0, int(context_chars))
    attempts = 0
    while True:
        attempts += 1
        if info is not None:
            info.update(attempts=attempts, context_retry=attempts > 1, context_chars=budget)
        try:
            return llm.chat(build_messages(budget), **chat_kwargs)
        except llm.LLMError as exc:
            if not exc.context_overflow or attempts > 1 or budget <= 0:
                raise
            log.info("Context overflow (%s); retrying once with %s instead of %s characters of context",
                     exc, budget // 2, budget)
            budget //= 2


def _generate(project_id: int, task: dict, mem: memory.MemoryPlan, checkpoint: Checkpoint, skipped: list[int],
              info: dict, stop_event: threading.Event | None, on_wait: Callable[[str], None] | None) -> str:
    """The task's answer: checkpointed, resumed once after a transient error, overflow retried once."""
    rules = resilience()
    budget = mem.context_chars
    context = build_context(project_id, task, budget, skipped)
    info.update(attempts=0, resumed_from_checkpoint=False, transient_retries=0, context_retry=False,
                context_chars=len(context))
    url: str | None = None  # None = the configured URL at call time; after a crash: the one confirmed ready
    while True:
        messages = _messages(task, context)
        if checkpoint.prefix:
            messages = resume_messages(messages, checkpoint.prefix)
        info["attempts"] += 1
        checkpoint.current = ""
        try:
            answer = llm.chat(messages, max_tokens=mem.max_tokens, url=url, on_text=checkpoint.on_text)
        except llm.LLMError as exc:
            if exc.context_overflow and not info["context_retry"] and context:
                info["context_retry"] = True
                budget //= 2
                skipped.clear()
                context = build_context(project_id, task, budget, skipped)
                info["context_chars"] = len(context)
                log.info("Task #%s: context overflow, retrying with %s characters of context", task["id"], budget)
                continue
            checkpoint.interrupted(exc.partial)
            if not (exc.transient and rules["retry_transient"] and not info["transient_retries"]):
                raise
            info["transient_retries"] = 1
            log.warning("Task #%s: transient model error (%s); waiting for the model", task["id"], exc)
            url, problem = await_model(stop_event=stop_event, on_wait=on_wait)
            if problem is not None:
                raise llm.LLMError(f"{exc} ({problem})") from exc
            info["url"] = url  # never the URL from before the crash: the engine may have a new port
            info["resumed_from_checkpoint"] = bool(checkpoint.prefix.strip())
            continue
        return join_continuation(checkpoint.prefix, answer).strip()


def run_next(project_id: int, percent: object = memory.DEFAULT_PERCENT, *,
             stop_event: threading.Event | None = None,
             on_wait: Callable[[str], None] | None = None) -> dict:
    """Run the next pending task of a general project.

    Returns a dict with ``status`` one of ``done``, ``failed``, ``paused``
    (not enough free RAM) or ``empty`` (no pending task). Raises ``db.Busy``
    when another inference is in progress. ``on_wait(message)`` reports
    waits for the model after a transient error; ``stop_event`` ends such a
    wait early (the task then fails, its partial answer kept).
    """
    pct = memory.validate_percent(percent)
    require_general(project_id)
    mem = memory.plan(pct)
    outcome: dict = {"task": None, "result": None, "error": None, "reason": None, "memory": mem.to_dict()}
    if not mem.can_run:
        return {**outcome, "status": "paused", "reason": mem.reason}
    if not db.tasks_with_status(project_id, ["pending"]):
        return {**outcome, "status": "empty", "reason": "No pending tasks in this project."}
    problem = model_problem()
    if problem:  # nothing is claimed, so the task is not marked failed
        return {**outcome, "status": "no_model", "reason": problem}
    task = db.claim_next_task(project_id)
    if task is None:
        return {**outcome, "status": "empty", "reason": "No pending tasks in this project."}
    started = time.monotonic()
    skipped: list[int] = []
    info: dict = {}
    checkpoint = Checkpoint(partial_path(project_id, task["id"]))
    try:
        checkpoint.remove()  # a fresh run never mixes with leftovers of an earlier one
        answer = _generate(project_id, task, mem, checkpoint, skipped, info, stop_event, on_wait)
        metadata = {
            "project_id": project_id,
            "task_id": task["id"],
            "position": task["position"],
            "title": task["title"],
            "model": config.llm_model(),
            "endpoint": llm.redact_url(info.get("url") or config.llm_url()),
            "percent": pct,
            "max_tokens": mem.max_tokens,
            "context_chars": info.get("context_chars", 0),
            "context_skipped_task_ids": skipped,
            "attempts": info.get("attempts", 1),
            "resumed_from_checkpoint": info.get("resumed_from_checkpoint", False),
            "transient_retries": info.get("transient_retries", 0),
            "context_retry": info.get("context_retry", False),
            "duration_s": round(time.monotonic() - started, 2),
            "created_at": db.utcnow(),
        }
        path = artifacts.write_task_result(project_id, task["id"], answer, metadata)
        checkpoint.prefix = checkpoint.current = ""
        checkpoint.remove()  # the result file replaces the checkpoint
        done = db.finish_task(task["id"], "done", result_path=str(path))
        return {**outcome, "status": "done", "task": done, "result": answer}
    except Exception as exc:  # every failure is recorded on the task, never swallowed
        message = describe_error(exc)
        if checkpoint.flush():
            message += (f" The partial answer ({len(checkpoint.text.strip())} characters) was kept in "
                        f"{checkpoint.path.name}.")
        failed = record_failure(task["id"], message, exc)
        return {**outcome, "status": "failed", "task": failed, "error": failed["error"]}
    except BaseException:
        checkpoint.flush()
        try:
            db.finish_task(task["id"], "failed", error="Interrupted before the task finished")
        except Exception:  # keep the original KeyboardInterrupt/SystemExit
            log.exception("Could not record the interruption of task #%s", task["id"])
        raise


def _emit_waiting(on_step: Callable[[dict], None] | None, message: str) -> None:
    """Report a wait (RAM, model restart) as a ``waiting`` step: shown as the loop's status text."""
    if on_step is not None:
        on_step({"status": "waiting", "task": None, "result": None, "error": None, "reason": message,
                 "memory": None})


def _hold_for_ram(stop_event: threading.Event | None,
                  on_step: Callable[[dict], None] | None) -> tuple[str, str] | None:
    """Between tasks: wait up to ``wait_for_ram_minutes`` for free RAM.

    ``None`` = go on; else ``(stopped_because, message)``.
    """
    if memory.plan().can_run:
        return None
    minutes = resilience()["wait_for_ram_minutes"]
    if memory.guard_wait("task loop", minutes * 60, stop_event, lambda message: _emit_waiting(on_step, message)):
        return None
    if stop_event is not None and stop_event.is_set():
        return "stop_requested", "Stopped on request."
    return "paused_low_ram", f"{memory.plan().reason} (Waited {minutes:g} min for free RAM.)"


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
    (``stop_on_failure``), RAM too low, on battery below the minimum
    (``power.battery_block_reason``), another inference busy, or
    ``stop_event`` set (checked between tasks; a running inference is never
    killed mid-way).

    RAM: too little free RAM before the first task stops the loop at once
    (``paused_low_ram``, the user just started it); between tasks the loop
    waits up to ``settings.resilience.wait_for_ram_minutes`` for RAM to
    recover (``memory.guard_wait``), reporting ``{"status": "waiting",
    "reason": "Warte auf freien Arbeitsspeicher (… MB frei)…"}`` through
    ``on_step``; ``stop_event`` ends the wait promptly. Waits for the model
    after a transient error are reported the same way.
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
    with power.keep_awake("task loop"):
        while True:
            if stop_event is not None and stop_event.is_set():
                summary.update(stopped_because="stop_requested", message="Stopped on request.")
                break
            if len(summary["steps"]) >= limit:
                summary.update(stopped_because="max_tasks", message=f"Reached the limit of {limit} task(s).")
                break
            battery = power.battery_block_reason()  # MacBook on battery: stop between tasks
            if battery:
                summary.update(stopped_because="on_battery", message=battery)
                break
            if summary["steps"]:  # between tasks: hold on instead of giving up while RAM is short
                held = _hold_for_ram(stop_event, on_step)
                if held is not None:
                    summary.update(stopped_because=held[0], message=held[1])
                    break
            try:
                outcome = run_next(project_id, pct, stop_event=stop_event,
                                   on_wait=lambda message: _emit_waiting(on_step, message))
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
            elif status == "no_model":
                summary.update(stopped_because="no_model", message=outcome["reason"])
                break
            elif status == "empty":
                summary.update(stopped_because="queue_empty", message="All tasks processed.")
                break
    return summary
