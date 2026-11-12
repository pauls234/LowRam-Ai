"""Disk artifacts: task results, coding proposals, plans and backups.

All files live below ``<LOWRAM_HOME>/artifacts/project_<id>/`` and are
written atomically (temporary file + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import config


class ArtifactError(RuntimeError):
    """An artifact is missing or outside the artifact directory."""


def project_dir(project_id: int) -> Path:
    path = config.artifacts_dir() / f"project_{int(project_id)}"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_bytes_atomic(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def write_text_atomic(path: Path, text: str, mode: int = 0o600) -> Path:
    return write_bytes_atomic(path, text.encode("utf-8"), mode)


def write_json_atomic(path: Path, data: object) -> Path:
    return write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_task_result(project_id: int, task_id: int, text: str, metadata: dict) -> Path:
    """Store a general task's answer as Markdown plus a JSON sidecar."""
    folder = project_dir(project_id)
    result = write_text_atomic(folder / f"task_{int(task_id)}.md", text.rstrip() + "\n")
    write_json_atomic(folder / f"task_{int(task_id)}.json", metadata)
    return result


def resolve_inside(path_text: str | Path | None) -> Path:
    """Resolve a stored artifact path, refusing anything outside the artifact directory."""
    if not path_text:
        raise ArtifactError("This task has no stored result")
    root = config.artifacts_dir().resolve()
    resolved = Path(path_text).resolve()
    if root not in resolved.parents:
        raise ArtifactError("Refusing to read a file outside the artifact directory")
    return resolved


def read_result(path_text: str | None, max_chars: int | None = None) -> str:
    """Read a stored artifact (only from inside the artifact directory)."""
    path = resolve_inside(path_text)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars if max_chars is not None else -1)
    except FileNotFoundError as exc:
        raise ArtifactError(f"Result file is missing on disk: {path}") from exc
