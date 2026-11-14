"""Core dashboard API: memory, task-queue projects, results and the bounded loop."""

from __future__ import annotations

from http import HTTPStatus

from . import artifacts, db, memory, runner, settings
from .web import Request, Response, route

MAX_RESULT_CHARS = 2_000_000


@route("GET", r"/api/status")
def get_status(req: Request) -> Response:
    """Version, which feature modules are loaded and any startup problems."""
    from . import __version__, app  # local import: app imports this module

    return Response(
        {
            "version": __version__,
            "features": list(app.LOADED_FEATURES),
            "startup_errors": list(getattr(req.server, "startup_errors", [])),
            "busy": db.busy_reason(),
        }
    )


@route("GET", r"/api/settings")
def get_settings(req: Request) -> Response:
    return Response({"settings": settings.load()})


@route("POST", r"/api/settings")
def post_settings(req: Request) -> Response:
    data = req.json()
    section = data.get("section")
    if not isinstance(section, str):
        raise ValueError("'section' is required")
    if section == "budget_percent":
        value = memory.validate_percent(data.get("values"))
    else:
        value = data.get("values")
    return Response({"section": section, "values": settings.update(section, value)})


@route("GET", r"/api/memory")
def get_memory(req: Request) -> Response:
    return Response(memory.plan(req.arg("percent", str(memory.DEFAULT_PERCENT))).to_dict())


@route("GET", r"/api/projects")
def get_projects(req: Request) -> Response:
    return Response({"projects": db.list_projects()})


@route("GET", r"/api/projects/(\d+)")
def get_project(req: Request, project_id: int) -> Response:
    return Response({"project": db.get_project(project_id), "tasks": db.list_tasks(project_id)})


@route("GET", r"/api/tasks/(\d+)/result")
def get_result(req: Request, task_id: int) -> Response:
    task = db.get_task(task_id)
    path = artifacts.resolve_inside(task["result_path"])
    content = artifacts.read_result(str(path), max_chars=MAX_RESULT_CHARS + 1)
    return Response(
        {
            "task_id": task_id,
            "path": str(path),
            "content": content[:MAX_RESULT_CHARS],
            "truncated": len(content) > MAX_RESULT_CHARS,
        }
    )


@route("GET", r"/api/loop")
def get_loop(req: Request) -> Response:
    return Response({"loop": req.server.loop.status()})


@route("POST", r"/api/projects")
def post_project(req: Request) -> Response:
    data = req.json()
    project_id = runner.create_project(data.get("name"), data.get("tasks"))
    return Response({"project": db.get_project(project_id)}, HTTPStatus.CREATED)


@route("POST", r"/api/projects/(\d+)/run")
def post_run(req: Request, project_id: int) -> Response:
    data = req.json()
    if req.server.loop.is_active():
        raise db.Busy("A loop is running. Stop it before running single tasks.")
    with req.server.track_run():
        outcome = runner.run_next(project_id, data.get("percent", memory.DEFAULT_PERCENT))
    return Response(outcome)


@route("POST", r"/api/projects/(\d+)/loop")
def post_loop(req: Request, project_id: int) -> Response:
    data = req.json()
    percent = memory.validate_percent(data.get("percent", memory.DEFAULT_PERCENT))
    max_tasks = runner.validate_max_tasks(data.get("max_tasks", runner.DEFAULT_LOOP_TASKS))
    runner.require_general(project_id)
    return Response({"loop": req.server.loop.start(project_id, percent, max_tasks)}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/loop/stop")
def post_loop_stop(req: Request) -> Response:
    req.json()
    return Response({"loop": req.server.loop.stop()})


@route("POST", r"/api/tasks/(\d+)/retry")
def post_retry(req: Request, task_id: int) -> Response:
    req.json()
    return Response({"task": db.retry_task(task_id)})


@route("POST", r"/api/recover")
def post_recover(req: Request) -> Response:
    force = req.json().get("force") is True
    if force and (req.server.loop.is_active() or req.server.runs_in_flight()):
        raise db.Busy("A task is running in this dashboard; force recovery is only allowed when idle.")
    return Response({"recovered": db.recover_stale(force=force)})
