"""Coding agent API: thin JSON wrappers around :mod:`backend.agent`.

Flow: create a project for a local repository -> plan (or add steps by hand)
-> step (the model proposes complete files) -> read the proposal -> approve
(files are written, with backups) or reject (repository untouched).

Error mapping: ``agent.ProposalError`` (unsafe path) -> 400,
``agent.ReviewPending`` and other ``agent.AgentError`` (wrong state, low RAM,
unusable model output) -> 409, ``db.Busy`` -> 409 (by the app),
``llm.LLMError`` (model endpoint unreachable/failing) -> 502, except when the
managed model was paused/stopped (image or video job) during the call -> 409.

Repositories must be a folder inside the user's home directory, not in a
hidden folder or ``~/Library``, and not the LowRAM AI data folder.

Test & fix loop: the user saves a test command (``test-command``) and starts
it explicitly (``run-tests``, 202, runs in the background, poll
``test-runs/<rid>``); ``fix-tests`` turns a failed run into a step. Commands
are only ever started by these user requests, never from model output.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import agent, chat, config, db, llm, memory
from .web import HTTPError, Request, Response, on_shutdown, on_startup, route

MAX_REASON_CHARS = 500
PAUSED_MESSAGE = "The model was paused for an image/video job – try again when it finishes"


def _percent(data: dict) -> int:
    value = data.get("percent")
    return memory.validate_percent(chat.default_percent() if value is None else value)


def validate_repo(repo_path: object) -> Path:
    """``agent.validate_repo`` plus: inside the user's home, no hidden folder or ~/Library,
    and never the LowRAM AI data folder (or a folder containing it)."""
    repo = agent.validate_repo(repo_path)
    user_home = Path.home().resolve()
    try:
        inside = repo.relative_to(user_home)
    except ValueError:
        raise ValueError(f"The repository must be a folder inside your home directory ({user_home})") from None
    if any(part.startswith(".") for part in repo.parts):
        raise ValueError("The repository must not be inside a hidden folder (a folder whose name starts with '.')")
    if inside.parts and inside.parts[0] == "Library":
        raise ValueError("The repository must not be inside ~/Library")
    data = config.home_dir().resolve()
    if repo == data or data in repo.parents or repo in data.parents:
        raise ValueError("The repository must not be, contain or be inside the LowRAM AI data folder")
    return repo


def _coding_project(project_id: int) -> dict:
    project = db.get_project(project_id)
    if project["kind"] != "coding":
        raise HTTPError(HTTPStatus.NOT_FOUND, f"Coding project #{project_id} not found")
    return project


def _project_task(project_id: int, task_id: int) -> dict:
    _coding_project(project_id)
    task = db.get_task(task_id)
    if task["project_id"] != project_id:
        raise HTTPError(HTTPStatus.NOT_FOUND, f"Task #{task_id} not found in coding project #{project_id}")
    return task


def _task_view(task: dict) -> dict:
    """A task plus its step details (``files``, ``detail``) decoded from the stored JSON."""
    try:
        data = json.loads(task.get("detail") or "{}")
    except json.JSONDecodeError:
        data = {"detail": task.get("detail") or ""}
    if not isinstance(data, dict):
        data = {}
    files = data.get("files") if isinstance(data.get("files"), list) else []
    detail = data.get("detail") if isinstance(data.get("detail"), str) else ""
    return {**task, "step": {"files": [f for f in files if isinstance(f, str)], "detail": detail}}


def _call(func: Callable[..., Any], *args: Any) -> Any:
    try:
        return func(*args)
    except agent.ProposalError as exc:
        raise HTTPError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    except agent.AgentError as exc:  # includes ReviewPending
        raise HTTPError(HTTPStatus.CONFLICT, str(exc)) from exc
    except llm.LLMError as exc:
        if chat.engine_interrupted():
            raise HTTPError(HTTPStatus.CONFLICT, PAUSED_MESSAGE) from exc
        raise HTTPError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc


def _prepare_model() -> None:
    """Fail fast when busy, and start the managed model if one is needed (409 if none).

    Called right before the agent call, which takes the inference slot itself."""
    reason = db.busy_reason()
    if reason:
        raise db.Busy(reason)
    chat.ensure_model()


def _optional_text(data: dict, key: str, limit: int) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be text")
    if len(value) > limit:
        raise ValueError(f"'{key}' must be at most {limit} characters")
    return value


@route("GET", r"/api/coding/projects")
def get_projects(req: Request) -> Response:
    return Response({"projects": [p for p in db.list_projects() if p["kind"] == "coding"]})


@route("POST", r"/api/coding/projects")
def post_project(req: Request) -> Response:
    data = req.json()
    repo = validate_repo(data.get("repo_path"))
    project_id = agent.create_coding_project(data.get("goal"), str(repo))
    return Response({"project": db.get_project(project_id)}, HTTPStatus.CREATED)


@route("GET", r"/api/coding/projects/(\d+)")
def get_project(req: Request, project_id: int) -> Response:
    project = _coding_project(project_id)
    return Response(
        {
            "project": project,
            "tasks": [_task_view(t) for t in db.list_tasks(project_id)],
            "test_command": agent.get_test_command(project_id),
            "last_test_run": agent.latest_test_run(project_id),
        }
    )


@route("POST", r"/api/coding/projects/(\d+)/plan")
def post_plan(req: Request, project_id: int) -> Response:
    data = req.json()
    percent = _percent(data)
    _coding_project(project_id)
    _prepare_model()
    tasks = _call(agent.plan, project_id, percent)
    return Response({"tasks": [_task_view(t) for t in tasks]})


@route("POST", r"/api/coding/projects/(\d+)/steps")
def post_step_add(req: Request, project_id: int) -> Response:
    data = req.json()
    _coding_project(project_id)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A step title is required")
    if len(title) > 300:
        raise ValueError("The step title must be at most 300 characters")
    files = data.get("files")
    if files is None:
        files = []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("'files' must be a list of relative paths")
    if len(files) > agent.MAX_CHANGES_PER_STEP:
        raise ValueError(f"At most {agent.MAX_CHANGES_PER_STEP} files per step")
    detail = _optional_text(data, "detail", 2000)
    task_id = _call(agent.add_step, project_id, title, files, detail)
    return Response({"task_id": task_id}, HTTPStatus.CREATED)


@route("POST", r"/api/coding/projects/(\d+)/step")
def post_step_run(req: Request, project_id: int) -> Response:
    data = req.json()
    percent = _percent(data)
    _coding_project(project_id)
    _prepare_model()
    result = _call(agent.execute_coding_step, project_id, percent)
    # A model-call failure (not a validation failure, which has raw_output) while the
    # engine was paused for an image/video job: requeue the step and ask to retry later.
    if result.get("status") == "failed" and "raw_output" not in result and chat.engine_interrupted():
        if result.get("task_id"):
            db.retry_task(result["task_id"])
        raise HTTPError(HTTPStatus.CONFLICT, PAUSED_MESSAGE)
    return Response(result)


@route("GET", r"/api/coding/projects/(\d+)/tasks/(\d+)/proposal")
def get_proposal(req: Request, project_id: int, task_id: int) -> Response:
    _project_task(project_id, task_id)
    return Response({"proposal": _call(agent.load_proposal, project_id, task_id)})


@route("POST", r"/api/coding/projects/(\d+)/tasks/(\d+)/approve")
def post_approve(req: Request, project_id: int, task_id: int) -> Response:
    req.json()
    _project_task(project_id, task_id)
    return Response(_call(agent.approve, project_id, task_id))


@route("POST", r"/api/coding/projects/(\d+)/tasks/(\d+)/reject")
def post_reject(req: Request, project_id: int, task_id: int) -> Response:
    data = req.json()
    reason = _optional_text(data, "reason", MAX_REASON_CHARS)
    _project_task(project_id, task_id)
    return Response({"task": _call(agent.reject, project_id, task_id, reason)})


# --------------------------------------------------------------------------- test & fix loop


@route("POST", r"/api/coding/projects/(\d+)/test-command")
def post_test_command(req: Request, project_id: int) -> Response:
    data = req.json()
    _coding_project(project_id)
    return Response({"command": _call(agent.set_test_command, project_id, data.get("command"))})


@route("POST", r"/api/coding/projects/(\d+)/run-tests")
def post_run_tests(req: Request, project_id: int) -> Response:
    req.json()
    _coding_project(project_id)
    return Response({"run": _call(agent.start_test_run, project_id)}, HTTPStatus.ACCEPTED)


@route("GET", r"/api/coding/projects/(\d+)/test-runs/(\d+)")
def get_test_run(req: Request, project_id: int, run_id: int) -> Response:
    _coding_project(project_id)
    return Response({"run": agent.get_test_run(project_id, run_id)})


@route("POST", r"/api/coding/projects/(\d+)/test-runs/(\d+)/cancel")
def post_cancel_test_run(req: Request, project_id: int, run_id: int) -> Response:
    req.json()
    _coding_project(project_id)
    return Response({"run": _call(agent.cancel_test_run, project_id, run_id)})


@route("POST", r"/api/coding/projects/(\d+)/fix-tests")
def post_fix_tests(req: Request, project_id: int) -> Response:
    data = req.json()
    _coding_project(project_id)
    run_id = data.get("run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("'run_id' must be the id of a test run")
    task_id = _call(agent.add_fix_tests_step, project_id, run_id)
    return Response({"task_id": task_id}, HTTPStatus.CREATED)


@on_startup
def _recover_test_runs(server: object = None) -> None:
    agent.recover_test_runs()


@on_shutdown
def _stop_test_runs(server: object = None) -> None:
    agent.stop_test_runs()
