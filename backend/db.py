"""SQLite persistence: projects, tasks and the global inference lock.

Design notes
------------
* Every public function opens its own short-lived connection, so the module
  is safe to use from several threads (web server + background loop).
* Writes that must be atomic use ``BEGIN IMMEDIATE`` transactions.
* Only one inference may run at a time across *all* projects and processes:
  a task in status ``running`` or a row in ``locks`` blocks new work.
* Each running task records its owner (``hostname:pid``). When that process is
  gone, the task is marked ``failed`` automatically (crash recovery) instead of
  blocking the queue forever.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import psutil

from . import config

log = logging.getLogger("lowram.db")

SCHEMA_VERSION = 1
STATUSES = ("pending", "running", "done", "failed", "proposed", "approved", "rejected")
KINDS = ("general", "coding")
INFERENCE_LOCK = "inference"
MAX_ERROR_CHARS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('general', 'coding')),
    goal TEXT NOT NULL DEFAULT '',
    repo_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'proposed', 'approved', 'rejected')),
    result_path TEXT,
    proposal_sha256 TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_position ON tasks(project_id, position);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    acquired_at TEXT NOT NULL
);
"""


_EXTRA_SCHEMAS: list[str] = []
_OWNER_CHECKS: dict[str, Callable[[str], bool]] = {}
REMOTE_OWNER_PREFIX = "device:"


def register_schema(sql: str) -> None:
    """Feature modules add their own ``CREATE TABLE IF NOT EXISTS`` statements."""
    if sql not in _EXTRA_SCHEMAS:
        _EXTRA_SCHEMAS.append(sql)


def register_owner_check(prefix: str, check: Callable[[str], bool]) -> None:
    """Liveness check for non-process lease owners, e.g. ``device:<id>`` leases."""
    _OWNER_CHECKS[prefix] = check


class NotFound(LookupError):
    """A project or task id does not exist."""


class Busy(RuntimeError):
    """Another inference is already in progress."""


class InvalidTransition(ValueError):
    """A task is not in the status the requested operation needs."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        return -1.0  # exists, but we may not inspect it


def owner_id() -> str:
    """``host:pid:process-start-time`` – the start time detects PID reuse."""
    return f"{socket.gethostname()}:{os.getpid()}:{_create_time(os.getpid()) or 0:.2f}"


def _owner_alive(owner: str | None) -> bool:
    """Best effort: is the process that owns a lease still running?

    The database is local to one machine, so the host name is informational
    only (it changes with the network on macOS). A lease is alive when its PID
    exists and – if recorded – the process start time still matches.
    """
    if not owner:
        return False
    for prefix, check in _OWNER_CHECKS.items():
        if owner.startswith(prefix):
            return check(owner)
    if owner.startswith(REMOTE_OWNER_PREFIX):
        return True  # no checker registered yet: never steal a remote lease
    parts = owner.rsplit(":", 2)
    try:
        pid = int(parts[1])
    except (IndexError, ValueError):
        return False
    started = _create_time(pid)
    if started is None:
        return False
    if len(parts) == 3 and started >= 0:
        try:
            return abs(started - float(parts[2])) < 1.0
        except ValueError:
            return False
    return True


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = config.db_path()
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for extra in _EXTRA_SCHEMAS:
            conn.executescript(extra)
        version =conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{version} is newer than this LowRAM AI (v{SCHEMA_VERSION}); please upgrade."
            )
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


transaction = _transaction  # public alias for feature modules (BEGIN IMMEDIATE … COMMIT)


def _task_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["has_result"] = bool(data.get("result_path"))
    return data


task_dict = _task_dict  # public alias


def _empty_counts() -> dict[str, int]:
    return {status: 0 for status in STATUSES}


def _project_dicts(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    projects = [dict(row) for row in rows]
    if not projects:
        return projects
    counts: dict[int, dict[str, int]] = {p["id"]: _empty_counts() for p in projects}
    marks = ",".join("?" for _ in projects)
    for row in conn.execute(
        f"SELECT project_id, status, COUNT(*) AS n FROM tasks WHERE project_id IN ({marks}) "
        "GROUP BY project_id, status",
        [p["id"] for p in projects],
    ):
        counts[row["project_id"]][row["status"]] = row["n"]
    for project in projects:
        project["counts"] = counts[project["id"]]
        project["total"] = sum(project["counts"].values())
    return projects


# --------------------------------------------------------------------------- projects


def _insert_tasks(conn: sqlite3.Connection, project_id: int, tasks: Iterable[tuple[str, str]]) -> list[int]:
    start = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM tasks WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    now = utcnow()
    ids = []
    for offset, (title, detail) in enumerate(tasks, start=1):
        cur = conn.execute(
            "INSERT INTO tasks (project_id, position, title, detail, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (project_id, start + offset, title, detail, now),
        )
        ids.append(int(cur.lastrowid))
    return ids


def create_project(
    name: str,
    kind: str,
    goal: str = "",
    repo_path: str | None = None,
    tasks: Iterable[tuple[str, str]] = (),
) -> int:
    if kind not in KINDS:
        raise ValueError(f"Unknown project kind {kind!r}")
    with connect() as conn, _transaction(conn):
        cur = conn.execute(
            "INSERT INTO projects (name, kind, goal, repo_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, kind, goal, repo_path, utcnow()),
        )
        project_id = int(cur.lastrowid)
        _insert_tasks(conn, project_id, tasks)
    return project_id


def add_tasks(project_id: int, tasks: Iterable[tuple[str, str]]) -> list[int]:
    with connect() as conn, _transaction(conn):
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFound(f"Project #{project_id} not found")
        return _insert_tasks(conn, project_id, tasks)


def get_project(project_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound(f"Project #{project_id} not found")
        return _project_dicts(conn, [row])[0]


def list_projects() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
        return _project_dicts(conn, rows)


# --------------------------------------------------------------------------- tasks


def list_tasks(project_id: int) -> list[dict]:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFound(f"Project #{project_id} not found")
        rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY position, id", (project_id,)
        ).fetchall()
        return [_task_dict(row) for row in rows]


def get_task(task_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"Task #{task_id} not found")
        return _task_dict(row)


def tasks_with_status(project_id: int, statuses: Iterable[str]) -> list[dict]:
    wanted = [s for s in statuses if s in STATUSES]
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE project_id = ? AND status IN ({marks}) ORDER BY position, id",
            [project_id, *wanted],
        ).fetchall()
        return [_task_dict(row) for row in rows]


def _busy_reason(conn: sqlite3.Connection) -> str | None:
    """Is THIS machine's inference slot taken? Tasks leased to other devices don't count."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE status = 'running' AND (lease_owner IS NULL OR lease_owner NOT LIKE ?) "
        "ORDER BY id LIMIT 1",
        (REMOTE_OWNER_PREFIX + "%",),
    ).fetchone()
    if row is not None:
        return f"Task #{row['id']} is already running. LowRAM AI runs one inference at a time."
    lock = conn.execute("SELECT owner, label FROM locks WHERE name = ?", (INFERENCE_LOCK,)).fetchone()
    if lock is not None:
        if _owner_alive(lock["owner"]):
            label = lock["label"] or "another inference"
            return f"Busy: {label} is in progress. LowRAM AI runs one inference at a time."
        conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (INFERENCE_LOCK, lock["owner"]))
    return None


def busy_reason() -> str | None:
    """Why no new inference can start right now, or ``None`` when idle."""
    recover_stale()
    with connect() as conn, _transaction(conn):
        return _busy_reason(conn)


def claim_next_task(project_id: int, owner: str | None = None) -> dict | None:
    """Atomically move the next pending task of a project to ``running``.

    Returns ``None`` when the project has no pending task and raises ``Busy``
    when any other inference is in progress.
    """
    owner = owner or owner_id()
    recover_stale()
    with connect() as conn:
        with _transaction(conn):
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise NotFound(f"Project #{project_id} not found")
            reason = _busy_reason(conn)
            if reason:
                raise Busy(reason)
            row = conn.execute(
                "SELECT id FROM tasks WHERE project_id = ? AND status = 'pending' ORDER BY position, id LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET status = 'running', lease_owner = ?, started_at = ?, finished_at = NULL, "
                "error = NULL, attempts = attempts + 1 WHERE id = ? AND status = 'pending'",
                (owner, utcnow(), row["id"]),
            )
        claimed = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return _task_dict(claimed)


def finish_task(
    task_id: int,
    status: str,
    *,
    result_path: str | None = None,
    error: str | None = None,
    proposal_sha256: str | None = None,
) -> dict:
    """Record the outcome of a running task (``done``, ``failed`` or ``proposed``)."""
    if status not in ("done", "failed", "proposed"):
        raise ValueError(f"Cannot finish a task with status {status!r}")
    if error is not None:
        error = error[:MAX_ERROR_CHARS]
    # Recording an outcome must not be lost to a transient "database is locked",
    # otherwise the task would stay 'running' under a live process.
    for attempt in range(3):
        try:
            return _finish_task_once(task_id, status, result_path, error, proposal_sha256)
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _finish_task_once(
    task_id: int, status: str, result_path: str | None, error: str | None, proposal_sha256: str | None
) -> dict:
    with connect() as conn:
        with _transaction(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = ?, result_path = COALESCE(?, result_path), error = ?, "
                "proposal_sha256 = COALESCE(?, proposal_sha256), finished_at = ?, lease_owner = NULL "
                "WHERE id = ? AND status = 'running'",
                (status, result_path, error, proposal_sha256, utcnow(), task_id),
            )
            if cur.rowcount != 1:
                raise InvalidTransition(f"Task #{task_id} is not running; its outcome was not recorded")
        return _task_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def transition(task_id: int, from_statuses: Iterable[str], to_status: str, *, error: str | None = None) -> dict:
    """Guarded status change used for retry / approve / reject."""
    allowed = tuple(from_statuses)
    if to_status not in STATUSES or not set(allowed) <= set(STATUSES):
        raise ValueError("Unknown task status")
    marks = ",".join("?" for _ in allowed)
    with connect() as conn:
        with _transaction(conn):
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise NotFound(f"Task #{task_id} not found")
            if row["status"] not in allowed:
                raise InvalidTransition(
                    f"Task #{task_id} is {row['status']}; expected {' or '.join(allowed)}"
                )
            if to_status == "pending":
                conn.execute(
                    "UPDATE tasks SET status = 'pending', error = NULL, result_path = NULL, "
                    "proposal_sha256 = NULL, started_at = NULL, finished_at = NULL, lease_owner = NULL "
                    f"WHERE id = ? AND status IN ({marks})",
                    (task_id, *allowed),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, error = COALESCE(?, error), finished_at = ? "
                    f"WHERE id = ? AND status IN ({marks})",
                    (to_status, error[:MAX_ERROR_CHARS] if error else None, utcnow(), task_id, *allowed),
                )
        return _task_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def retry_task(task_id: int) -> dict:
    """Put a failed (or rejected coding) task back into the queue."""
    return transition(task_id, ("failed", "rejected"), "pending")


def recover_stale(force: bool = False) -> list[int]:
    """Mark ``running`` tasks whose owning process is gone as ``failed``.

    With ``force=True`` every running task is marked failed (only use this when
    you are sure no LowRAM AI process is still working).
    """
    recovered: list[int] = []
    with connect() as conn, _transaction(conn):
        rows = conn.execute("SELECT id, lease_owner FROM tasks WHERE status = 'running'").fetchall()
        for row in rows:
            if force or not _owner_alive(row["lease_owner"]):
                conn.execute(
                    "UPDATE tasks SET status = 'failed', lease_owner = NULL, finished_at = ?, error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (
                        utcnow(),
                        f"Interrupted: the process running this task ({row['lease_owner'] or 'unknown'}) "
                        "stopped before it finished. Use retry to queue it again.",
                        row["id"],
                    ),
                )
                recovered.append(int(row["id"]))
        for lock in conn.execute("SELECT owner FROM locks").fetchall():
            if force or not _owner_alive(lock["owner"]):
                conn.execute("DELETE FROM locks WHERE owner = ?", (lock["owner"],))
    return recovered


@contextmanager
def inference_slot(label: str, owner: str | None = None) -> Iterator[None]:
    """Hold the global inference lock for work that is not a queued task (e.g. planning)."""
    owner = owner or owner_id()
    recover_stale()
    with connect() as conn, _transaction(conn):
        reason = _busy_reason(conn)
        if reason:
            raise Busy(reason)
        conn.execute(
            "INSERT INTO locks (name, owner, label, acquired_at) VALUES (?, ?, ?, ?)",
            (INFERENCE_LOCK, owner, label[:200], utcnow()),
        )
    try:
        yield
    finally:
        for attempt in range(6):
            try:
                with connect() as conn:
                    conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (INFERENCE_LOCK, owner))
                break
            except sqlite3.OperationalError:
                if attempt == 5:
                    log.error("Could not release the inference lock (%s); restart LowRAM AI to clear it", label)
                    raise
                time.sleep(0.5 * (attempt + 1))
