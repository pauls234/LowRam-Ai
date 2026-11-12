"""Experimental coding agent (v0.2): index -> plan -> propose -> human approval.

Safety properties
-----------------
* The model never executes anything: no shell commands, no code execution.
* Proposals are written to disk for review; repository files change only in
  :func:`approve`, which a human must call explicitly.
* Every path is validated to stay inside the repository: no absolute paths, no
  ``..``, no symlinks, no ``.git``, no hidden directories (except ``.github``),
  no secret-looking files, text files only.
* Python, JSON and TOML proposals are syntax-checked without executing them.
* An existing file may only be replaced if the model was shown its full
  content (no blind overwrites).
* :func:`approve` refuses when the proposal file or a target file changed
  since the proposal was generated, keeps backups of overwritten files and
  rolls back on a failed write.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import logging
import time

from . import artifacts, db, llm, memory
from .runner import describe_error, record_failure

log = logging.getLogger("lowram.agent")

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None  # type: ignore[assignment]

MAX_PLAN_STEPS = 8
MAX_CHANGES_PER_STEP = 10
MAX_FILE_BYTES = 256 * 1024
MAX_INDEX_FILES = 2000
MAX_GOAL_CHARS = 2000
MAX_PATH_CHARS = 300
MAX_STEP_TOKENS = 4096

SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", ".idea", ".vscode", ".next",
        "target", ".gradle", "Pods", "DerivedData", ".eggs",
    }
)
ALLOWED_HIDDEN_DIRS = frozenset({".github"})
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".sh", ".rs", ".go",
        ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".sql", ".vue",
        ".svelte", ".lua", ".r", ".m", ".cs", ".fs", ".dart", ".scala", ".ex", ".exs", ".zig",
    }
)
TEXT_NAMES = frozenset(
    {"Makefile", "Dockerfile", "LICENSE", "README", "CHANGELOG", ".gitignore", ".editorconfig", ".dockerignore"}
)
SENSITIVE_NAMES = frozenset(
    {
        ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa",
        "id_ed25519", "credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml",
    }
)
SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".crt", ".der"})
# Readable for context, but never writable: CI workflows and hooks run automatically.
WRITE_BLOCKED_DIRS = frozenset({".github"})
WRITE_BLOCKED_NAMES = frozenset(
    {".pre-commit-config.yaml", ".pre-commit-config.yml", "sitecustomize.py", "usercustomize.py"}
)
# Writable, but flagged loudly in the review: these run code on build/test/install.
ATTENTION_NAMES = frozenset(
    {
        "Makefile", "Dockerfile", "setup.py", "setup.cfg", "pyproject.toml", "conftest.py", "noxfile.py",
        "tox.ini", "package.json", "Cargo.toml", "build.gradle", "pom.xml", "Gemfile", "Rakefile",
        "requirements.txt", "go.mod", "Package.swift", ".gitignore",
    }
)
ATTENTION_SUFFIXES = frozenset({".sh"})
_ALLOWED_CONTROL = frozenset({"\n", "\t"})

PLAN_SYSTEM = (
    "You are a careful senior software engineer planning small, safe code changes. "
    "Reply with a single JSON object and nothing else."
)
STEP_SYSTEM = (
    "You are a careful coding agent. You propose minimal, correct changes for exactly one step. "
    "You cannot run commands, tests or code. Reply with a single JSON object and nothing else."
)


class AgentError(RuntimeError):
    """The coding agent cannot continue; the message says why."""


class ReviewPending(AgentError):
    """A proposal must be approved or rejected before the next step."""


class ProposalError(AgentError):
    """A proposed change violates a safety rule."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_sensitive(name: str) -> bool:
    lower = name.lower()
    if lower in SENSITIVE_NAMES:
        return True
    if lower.startswith(".env.") and not lower.endswith((".example", ".sample", ".template")):
        return True
    return PurePosixPath(lower).suffix in SENSITIVE_SUFFIXES


def is_text_candidate(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES or name in TEXT_NAMES


def needs_attention(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name
    return name in ATTENTION_NAMES or PurePosixPath(name).suffix.lower() in ATTENTION_SUFFIXES


def _hidden_char(text: str) -> str | None:
    """First control/format/separator character that could disguise a diff, if any."""
    for char in text:
        if char in _ALLOWED_CONTROL:
            continue
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp"):
            return f"U+{ord(char):04X}"
    return None


def clean_text(text: str) -> str:
    """Strip control/format characters from model text shown to humans (titles, summaries)."""
    return "".join(
        char for char in text if char in _ALLOWED_CONTROL or unicodedata.category(char) not in ("Cc", "Cf", "Zl", "Zp")
    )


# --------------------------------------------------------------------------- repositories


def validate_repo(repo_path: object) -> Path:
    if not isinstance(repo_path, (str, os.PathLike)) or not str(repo_path).strip():
        raise ValueError("Repository path is required")
    raw = Path(repo_path).expanduser()
    if not raw.is_absolute():
        raise ValueError("Repository path must be absolute, e.g. /Users/you/code/project")
    repo = raw.resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {repo}")
    if repo == Path(repo.anchor) or repo == Path.home().resolve():
        raise ValueError("Refusing to use the filesystem root or your home directory as a repository")
    return repo


def create_coding_project(goal: object, repo_path: object) -> int:
    """Register a coding goal for a local repository. Returns the project id."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("A goal is required")
    goal = goal.strip()
    if len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"The goal must be at most {MAX_GOAL_CHARS} characters")
    repo = validate_repo(repo_path)
    name = f"{repo.name}: {goal[:60]}" + ("…" if len(goal) > 60 else "")
    return db.create_project(name, "coding", goal=goal, repo_path=str(repo))


def require_coding(project_id: int) -> tuple[dict, Path]:
    project = db.get_project(project_id)
    if project["kind"] != "coding":
        raise ValueError(f"Project #{project_id} is not a coding project")
    return project, validate_repo(project["repo_path"])


def _summarize(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    suffix = path.suffix.lower()
    if suffix in (".py", ".pyi"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return f"{lines} lines; python with syntax errors"
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if names:
            more = "…" if len(names) > 12 else ""
            return f"{lines} lines; defines {', '.join(names[:12])}{more}"
    elif suffix == ".md":
        for line in text.splitlines():
            if line.startswith("#"):
                return f"{lines} lines; {line.lstrip('#').strip()[:80]}"
    return f"{lines} lines"


def index_repository(repo_path: object, max_files: int = MAX_INDEX_FILES) -> list[dict]:
    """List readable text files (path, size, short summary) without following symlinks."""
    repo = Path(repo_path).resolve()  # type: ignore[arg-type]
    entries: list[dict] = []
    for root, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and (not d.startswith(".") or d in ALLOWED_HIDDEN_DIRS)
            and not os.path.islink(os.path.join(root, d))
        )
        for name in sorted(files):
            full = Path(root) / name
            if full.is_symlink() or is_sensitive(name) or not is_text_candidate(name):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            entries.append({"path": full.relative_to(repo).as_posix(), "bytes": size, "summary": _summarize(full)})
            if len(entries) >= max_files:
                return entries
    return entries


def render_index(entries: list[dict], char_budget: int) -> str:
    lines: list[str] = []
    used = 0
    for position, entry in enumerate(entries):
        line = f"- {entry['path']} ({entry['summary']})"
        if used + len(line) + 1 > char_budget:
            lines.append(f"- … {len(entries) - position} more file(s) not shown")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) or "(no text files found)"


# --------------------------------------------------------------------------- model output parsing


def extract_json_object(text: str) -> dict:
    """Find the first JSON object in model output (handles code fences and chatter)."""
    cleaned = llm.strip_reasoning(text)
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    decoder = json.JSONDecoder()
    for candidate in [*fenced, cleaned]:
        candidate = candidate.strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
        for attempt, match in enumerate(re.finditer(r"\{", candidate)):
            if attempt >= 50:
                break
            try:
                value, _ = decoder.raw_decode(candidate, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no JSON object found in the model output")


def parse_plan(raw: str) -> list[dict]:
    data = extract_json_object(raw)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("the plan contains no steps")
    clean: list[dict] = []
    for step in steps[:MAX_PLAN_STEPS]:
        if not isinstance(step, dict):
            continue
        title = step.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        files_raw = step.get("files")
        files = (
            [f.strip() for f in files_raw if isinstance(f, str) and f.strip()][:MAX_CHANGES_PER_STEP]
            if isinstance(files_raw, list)
            else []
        )
        detail = step.get("detail")
        title = " ".join(clean_text(title).split())[:300]
        if not title:
            continue
        clean.append(
            {
                "title": title,
                "files": [clean_text(f) for f in files],
                "detail": clean_text(detail).strip()[:2000] if isinstance(detail, str) else "",
            }
        )
    if not clean:
        raise ValueError("the plan contains no valid steps")
    return clean


# --------------------------------------------------------------------------- validation


def resolve_repo_path(repo: Path, rel: object, *, write: bool = False) -> Path:
    """Map a model-supplied relative path to a safe location inside ``repo``.

    ``write=True`` additionally blocks locations that execute automatically
    (``.github/`` workflows, pre-commit config, ``sitecustomize.py``).
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ProposalError("path must be a non-empty string")
    rel = rel.strip()
    if len(rel) > MAX_PATH_CHARS or "\x00" in rel or "\\" in rel:
        raise ProposalError(f"{rel[:60]!r}: invalid path")
    if (
        rel.startswith("~")
        or PurePosixPath(rel).is_absolute()
        or PureWindowsPath(rel).is_absolute()
        or PureWindowsPath(rel).drive
    ):
        raise ProposalError(f"{rel}: path must be relative to the repository")
    parts = PurePosixPath(rel).parts
    if not parts or any(part == ".." for part in parts):
        raise ProposalError(f"{rel}: '..' is not allowed")
    for part in parts[:-1]:
        if part in SKIP_DIRS or (part.startswith(".") and part not in ALLOWED_HIDDEN_DIRS):
            raise ProposalError(f"{rel}: writing into {part}/ is not allowed")
    if parts[-1].lower() == ".git" or is_sensitive(parts[-1]):
        raise ProposalError(f"{rel}: this file looks sensitive and cannot be read or written")
    if not is_text_candidate(parts[-1]):
        raise ProposalError(f"{rel}: only text/source files can be changed")
    if write and (parts[0] in WRITE_BLOCKED_DIRS or parts[-1].lower() in WRITE_BLOCKED_NAMES):
        raise ProposalError(f"{rel}: CI workflows and hook configuration run automatically and cannot be written")
    current = repo
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise ProposalError(f"{rel}: symlinks are not allowed")
        if index < len(parts) - 1 and current.exists() and not current.is_dir():
            raise ProposalError(f"{rel}: {part} is a file, not a directory")
    target = repo.joinpath(*parts)
    if repo not in target.resolve().parents:
        raise ProposalError(f"{rel}: path escapes the repository")
    if target.exists() and not target.is_file():
        raise ProposalError(f"{rel}: exists but is not a regular file")
    return target


def check_syntax(rel_path: str, content: str) -> str | None:
    """Parse (never execute) Python, JSON and TOML. Returns an error or ``None``."""
    suffix = PurePosixPath(rel_path).suffix.lower()
    try:
        if suffix in (".py", ".pyi"):
            ast.parse(content, filename=rel_path)
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml" and tomllib is not None:
            tomllib.loads(content)
    except SyntaxError as exc:
        return f"{rel_path}: Python syntax error on line {exc.lineno}: {exc.msg}"
    except json.JSONDecodeError as exc:
        return f"{rel_path}: invalid JSON ({exc.msg} at line {exc.lineno})"
    except ValueError as exc:  # includes tomllib.TOMLDecodeError
        return f"{rel_path}: invalid content ({exc})"
    except (RecursionError, MemoryError):
        return f"{rel_path}: content is too deeply nested to check"
    return None


def _diff_lines(text: str) -> list[str]:
    """Split on '\\n' only (unlike str.splitlines), keeping line endings."""
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _unified_diff(rel: str, old: str | None, new: str) -> str:
    out: list[str] = []
    for line in difflib.unified_diff(
        _diff_lines(old or ""),
        _diff_lines(new),
        fromfile=f"a/{rel}" if old is not None else "/dev/null",
        tofile=f"b/{rel}",
        lineterm="\n",
    ):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out).rstrip("\n")


def validate_changes(repo: Path, changes: object) -> tuple[list[dict], list[str]]:
    """Validate proposed full-file replacements. Returns ``(changes, errors)``."""
    if not isinstance(changes, list) or not changes:
        return [], ["the proposal must contain a non-empty 'changes' list"]
    errors: list[str] = []
    if len(changes) > MAX_CHANGES_PER_STEP:
        errors.append(f"at most {MAX_CHANGES_PER_STEP} files may change in one step")
    validated: list[dict] = []
    seen: set[str] = set()
    unchanged: list[str] = []
    for number, change in enumerate(changes[:MAX_CHANGES_PER_STEP], start=1):
        if not isinstance(change, dict):
            errors.append(f"change {number}: must be an object with 'path' and 'content'")
            continue
        try:
            target = resolve_repo_path(repo, change.get("path"), write=True)
        except ProposalError as exc:
            errors.append(f"change {number}: {exc}")
            continue
        rel = target.relative_to(repo).as_posix()
        if rel in seen:
            errors.append(f"{rel}: listed more than once")
            continue
        seen.add(rel)
        content = change.get("content")
        if not isinstance(content, str):
            errors.append(f"{rel}: 'content' must be the complete file content as a string")
            continue
        hidden = _hidden_char(content.replace("\r\n", "\n"))
        if hidden:
            errors.append(
                f"{rel}: content contains the invisible/control character {hidden}, which could hide code in the diff"
            )
            continue
        content = content.replace("\r\n", "\n")
        if content and not content.endswith("\n"):
            content += "\n"
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            errors.append(f"{rel}: content is not valid UTF-8 text")
            continue
        if len(encoded) > MAX_FILE_BYTES:
            errors.append(f"{rel}: content exceeds {MAX_FILE_BYTES // 1024} KiB")
            continue
        syntax_error = check_syntax(rel, content)
        if syntax_error:
            errors.append(syntax_error)
            continue
        old_text: str | None = None
        base_sha: str | None = None
        newline = "\n"
        if target.exists():
            raw = target.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                errors.append(f"{rel}: existing file is larger than {MAX_FILE_BYTES // 1024} KiB")
                continue
            try:
                old_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{rel}: existing file is not UTF-8 text")
                continue
            base_sha = _sha256(raw)
            if "\r\n" in old_text:  # keep Windows line endings; the model only ever sees '\n'
                newline = "\r\n"
                old_text = old_text.replace("\r\n", "\n")
            if old_text == content:
                unchanged.append(rel)
                continue
        validated.append(
            {
                "path": rel,
                "action": "modify" if base_sha else "create",
                "base_sha256": base_sha,
                "new_sha256": _sha256(content.replace("\n", newline).encode("utf-8")),
                "newline": "crlf" if newline == "\r\n" else "lf",
                "attention": needs_attention(rel),
                "content": content,
                "diff": _unified_diff(rel, old_text, content),
            }
        )
    if not validated and not errors:
        errors.append("the proposal does not change anything" + (f" ({', '.join(unchanged)} unchanged)" if unchanged else ""))
    return validated, errors


# --------------------------------------------------------------------------- planning


def _require_memory(percent: object) -> memory.MemoryPlan:
    mem = memory.plan(percent)
    if not mem.can_run:
        raise AgentError(mem.reason)
    return mem


def plan(project_id: int, percent: object = memory.DEFAULT_PERCENT) -> list[dict]:
    """Ask the model for a small plan and store its steps as pending tasks."""
    project, repo = require_coding(project_id)
    open_steps = db.tasks_with_status(project_id, ["pending", "running", "proposed"])
    if open_steps:
        raise AgentError(
            f"Project #{project_id} already has {len(open_steps)} open step(s). "
            "Finish, approve or reject them before planning again."
        )
    mem = _require_memory(percent)
    folder = artifacts.project_dir(project_id)
    entries = index_repository(repo)
    artifacts.write_json_atomic(
        folder / "index.json", {"repo": str(repo), "created_at": db.utcnow(), "files": entries}
    )
    prompt = (
        f"Goal: {project['goal']}\n\n"
        f"Repository files (path and summary):\n{render_index(entries, mem.context_chars)}\n\n"
        f"Break the goal into 1 to {MAX_PLAN_STEPS} small steps. Each step should change only a few files.\n"
        "Reply with JSON only, exactly in this shape:\n"
        '{"steps": [{"title": "short imperative title", "files": ["relative/path.py"], '
        '"detail": "what to change and why"}]}\n'
        "Rules: only relative paths inside the repository; new files are allowed; never touch .git, "
        "secrets or binary files; no shell commands."
    )
    messages = [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": prompt}]
    with db.inference_slot(f"planning for project #{project_id}"):
        raw = llm.chat(messages, max_tokens=mem.max_tokens, temperature=0.1)
    raw_path = artifacts.write_text_atomic(folder / f"plan_{_stamp()}.raw.txt", raw)
    try:
        steps = parse_plan(raw)
    except ValueError as exc:
        raise AgentError(f"The model did not return a usable plan ({exc}). Raw output: {raw_path}") from exc
    artifacts.write_json_atomic(
        folder / "plan.json",
        {"goal": project["goal"], "steps": steps, "created_at": db.utcnow(), "raw_output": str(raw_path)},
    )
    db.add_tasks(
        project_id,
        [(s["title"], json.dumps({"files": s["files"], "detail": s["detail"]})) for s in steps],
    )
    return db.list_tasks(project_id)


def add_step(project_id: int, title: object, files: list[str] | None = None, detail: str = "") -> int:
    """Add a hand-written step (useful when the model's plan is not good enough)."""
    _, repo = require_coding(project_id)
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A step title is required")
    clean_files = []
    for rel in files or []:
        target = resolve_repo_path(repo, rel)
        clean_files.append(target.relative_to(repo).as_posix())
    payload = json.dumps({"files": clean_files[:MAX_CHANGES_PER_STEP], "detail": detail.strip()[:2000]})
    return db.add_tasks(project_id, [(" ".join(title.split())[:300], payload)])[0]


# --------------------------------------------------------------------------- executing a step


def _step_detail(task: dict) -> dict:
    try:
        data = json.loads(task.get("detail") or "{}")
    except json.JSONDecodeError:
        data = {"detail": task.get("detail") or ""}
    if not isinstance(data, dict):
        data = {}
    files = data.get("files") if isinstance(data.get("files"), list) else []
    detail = data.get("detail") if isinstance(data.get("detail"), str) else ""
    return {"files": [f for f in files if isinstance(f, str)], "detail": detail}


def _file_context(repo: Path, rel_paths: list[str], char_budget: int) -> tuple[str, set[str]]:
    """Full contents of the step's files within ``char_budget``; never partial files."""
    parts: list[str] = []
    shown: set[str] = set()
    remaining = char_budget
    for rel in rel_paths:
        try:
            target = resolve_repo_path(repo, rel)
        except ProposalError as exc:
            parts.append(f"### {rel}\n(not available: {exc})")
            continue
        rel_norm = target.relative_to(repo).as_posix()
        if not target.exists():
            parts.append(f"### {rel_norm}\n(new file - does not exist yet)")
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parts.append(f"### {rel_norm}\n(binary or unreadable - cannot be edited)")
            continue
        if len(text) > remaining:
            parts.append(
                f"### {rel_norm}\n(not shown: {len(text)} characters exceed the remaining context budget. "
                "Do NOT modify this file.)"
            )
            continue
        parts.append(f"### {rel_norm}\n```\n{text}\n```")
        shown.add(rel_norm)
        remaining -= len(text)
    return "\n\n".join(parts) or "(no files listed for this step)", shown


def execute_coding_step(project_id: int, percent: object = memory.DEFAULT_PERCENT) -> dict:
    """Generate a proposal for the next pending step and store it for review.

    Returns a dict with ``status``: ``proposed`` (review the files on disk,
    then call :func:`approve` or :func:`reject`), ``failed`` (validation or
    model error, recorded on the task), ``paused`` (low RAM) or ``empty``.
    """
    pct = memory.validate_percent(percent)
    project, repo = require_coding(project_id)
    waiting = db.tasks_with_status(project_id, ["proposed"])
    if waiting:
        raise ReviewPending(
            f"Task #{waiting[0]['id']} has a proposal awaiting review. Approve or reject it first."
        )
    mem = memory.plan(pct)
    if not mem.can_run:
        return {"status": "paused", "task_id": None, "reason": mem.reason, "memory": mem.to_dict()}
    task = db.claim_next_task(project_id)
    if task is None:
        return {
            "status": "empty",
            "task_id": None,
            "reason": "No pending steps. Run plan first or add a step.",
            "memory": mem.to_dict(),
        }
    folder = artifacts.project_dir(project_id)
    try:
        step = _step_detail(task)
        entries = index_repository(repo)
        index_text = render_index(entries, max(1000, mem.context_chars // 4))
        file_text, shown = _file_context(repo, step["files"], mem.context_chars)
        history = [t["title"] for t in db.tasks_with_status(project_id, ["approved"])]
        prompt = (
            f"Overall goal: {project['goal']}\n\n"
            f"Current step: {task['title']}\n"
            f"Details: {step['detail'] or '-'}\n\n"
            f"Previously approved steps: {'; '.join(history) if history else 'none'}\n\n"
            f"Repository files:\n{index_text}\n\n"
            f"Files for this step (full content):\n{file_text}\n\n"
            "Reply with JSON only, exactly in this shape:\n"
            '{"summary": "one or two sentences describing the change", '
            '"changes": [{"path": "relative/path.py", "content": "the COMPLETE new file content"}]}\n'
            "Rules:\n"
            "- Each change replaces the whole file: give the complete file content, not a diff or excerpt.\n"
            "- Only modify existing files whose full content is shown above; new files are allowed.\n"
            f"- Keep the change minimal and focused on this step; at most {MAX_CHANGES_PER_STEP} files.\n"
            "- No shell commands. Never touch .git, secrets or binary files."
        )
        messages = [{"role": "system", "content": STEP_SYSTEM}, {"role": "user", "content": prompt}]
        raw = llm.chat(messages, max_tokens=min(MAX_STEP_TOKENS, mem.max_tokens * 3), temperature=0.1)
        raw_path = artifacts.write_text_atomic(folder / f"task_{task['id']}.raw.txt", raw)

        errors: list[str] = []
        summary = ""
        validated: list[dict] = []
        try:
            data = extract_json_object(raw)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            summary = clean_text(data.get("summary")) if isinstance(data.get("summary"), str) else ""
            validated, errors = validate_changes(repo, data.get("changes"))
            for change in validated:
                if change["action"] == "modify" and change["path"] not in shown:
                    errors.append(
                        f"{change['path']}: the model was not shown this file in full; refusing a blind overwrite"
                    )
        if errors:
            report = artifacts.write_json_atomic(
                folder / f"task_{task['id']}.rejected.json",
                {"task_id": task["id"], "errors": errors, "raw_output": str(raw_path), "created_at": db.utcnow()},
            )
            db.finish_task(
                task["id"], "failed", result_path=str(report),
                error="Proposal failed validation: " + "; ".join(errors),
            )
            return {
                "status": "failed",
                "task_id": task["id"],
                "errors": errors,
                "raw_output": str(raw_path),
                "memory": mem.to_dict(),
            }

        diff = "\n".join(change["diff"] for change in validated)
        proposal = {
            "version": 1,
            "project_id": project_id,
            "task_id": task["id"],
            "repo": str(repo),
            "step": task["title"],
            "summary": summary.strip()[:2000],
            "created_at": db.utcnow(),
            "changes": [{k: v for k, v in change.items() if k != "diff"} for change in validated],
            "diff": diff,
        }
        payload = (json.dumps(proposal, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        proposal_path = artifacts.write_bytes_atomic(folder / f"task_{task['id']}.proposal.json", payload)
        diff_path = artifacts.write_text_atomic(folder / f"task_{task['id']}.diff", diff + "\n")
        db.finish_task(task["id"], "proposed", result_path=str(proposal_path), proposal_sha256=_sha256(payload))
        earlier_problems = [
            t for t in db.tasks_with_status(project_id, ["failed", "rejected"]) if t["position"] < task["position"]
        ]
        return {
            "status": "proposed",
            "task_id": task["id"],
            "step": task["title"],
            "summary": proposal["summary"],
            "files": [{"path": c["path"], "action": c["action"], "attention": c["attention"]} for c in validated],
            "attention": [c["path"] for c in validated if c["attention"]],
            "warnings": [
                f"Earlier step #{t['id']} ({t['title']}) is {t['status']}; this step may depend on it."
                for t in earlier_problems
            ],
            "proposal_path": str(proposal_path),
            "diff_path": str(diff_path),
            "diff": diff,
            "memory": mem.to_dict(),
        }
    except Exception as exc:  # recorded on the task, never swallowed
        failed = record_failure(task["id"], describe_error(exc), exc)
        return {"status": "failed", "task_id": task["id"], "errors": [failed["error"]], "memory": mem.to_dict()}
    except BaseException:
        try:
            db.finish_task(task["id"], "failed", error="Interrupted before the step finished")
        except Exception:  # keep the original KeyboardInterrupt/SystemExit
            log.exception("Could not record the interruption of task #%s", task["id"])
        raise


# --------------------------------------------------------------------------- review


def _proposed_task(project_id: int, task_id: int) -> dict:
    task = db.get_task(task_id)
    if task["project_id"] != project_id:
        raise AgentError(f"Task #{task_id} does not belong to project #{project_id}")
    if task["status"] != "proposed":
        raise AgentError(f"Task #{task_id} is {task['status']}; only proposed tasks can be reviewed")
    return task


def load_proposal(project_id: int, task_id: int) -> dict:
    """Return the stored proposal after verifying it was not modified on disk."""
    task = _proposed_task(project_id, task_id)
    path = artifacts.resolve_inside(task["result_path"])
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise AgentError(f"Proposal file is missing: {path}") from exc
    if _sha256(payload) != task["proposal_sha256"]:
        raise AgentError(
            "The proposal file changed after it was generated. Refusing to use it; reject it and run the step again."
        )
    proposal = json.loads(payload)
    if not isinstance(proposal, dict) or not isinstance(proposal.get("changes"), list):
        raise AgentError("The proposal file is malformed")
    return proposal


def _apply(repo: Path, changes: list[dict], backup_dir: Path) -> list[str]:
    """Write all files atomically-ish: stage temp files, back up, replace, roll back on failure."""
    staged: list[tuple[Path, Path, dict]] = []
    try:
        for change in changes:
            # Re-check right before touching the disk (narrows the symlink-swap window).
            target = resolve_repo_path(repo, change["path"], write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if repo not in target.parent.resolve().parents and target.parent.resolve() != repo:
                raise ProposalError(f"{change['path']}: parent directory escapes the repository")
            fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.lowram-", suffix=".tmp", dir=target.parent)
            newline = "\r\n" if change.get("newline") == "crlf" else "\n"
            with os.fdopen(fd, "wb") as handle:
                handle.write(change["content"].replace("\n", newline).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                shutil.copymode(target, tmp)
            else:
                os.chmod(tmp, 0o644)
            staged.append((target, Path(tmp), change))
        for target, _, change in staged:
            if change["base_sha256"]:
                backup = backup_dir / change["path"]
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        replaced: list[tuple[Path, dict]] = []
        try:
            for target, tmp, change in staged:
                os.replace(tmp, target)
                replaced.append((target, change))
        except BaseException:
            for target, change in reversed(replaced):
                if change["base_sha256"]:
                    shutil.copy2(backup_dir / change["path"], target)
                else:
                    target.unlink(missing_ok=True)
            raise
    finally:
        for _, tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    return [change["path"] for change in changes]


def _mark_approved_or_rollback(repo: Path, task_id: int, changes: list[dict], backup_dir: Path) -> None:
    """Record the approval; if that is impossible, undo the writes so state stays consistent."""
    for attempt in range(3):
        try:
            db.transition(task_id, ["proposed"], "approved")
            return
        except Exception as exc:  # noqa: BLE001 - decide below whether to retry or roll back
            if isinstance(exc, sqlite3.OperationalError) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            log.exception("Could not mark task #%s approved; rolling back the written files", task_id)
            problems = []
            for change in changes:
                target = repo / change["path"]
                try:
                    if change["base_sha256"]:
                        shutil.copy2(backup_dir / change["path"], target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError as restore_exc:
                    problems.append(f"{change['path']}: {restore_exc}")
            if problems:
                raise AgentError(
                    f"Files were written but the approval could not be saved ({exc}); restoring failed for "
                    + "; ".join(problems) + f". Backups: {backup_dir}"
                ) from exc
            raise AgentError(
                f"The approval could not be saved ({exc}); the repository was restored to its previous state."
            ) from exc


def approve(project_id: int, task_id: int) -> dict:
    """Apply a reviewed proposal to the repository. Call only after reading it."""
    _, repo = require_coding(project_id)
    with db.inference_slot(f"applying task #{task_id}"):
        proposal = load_proposal(project_id, task_id)
        if Path(str(proposal.get("repo", ""))).resolve() != repo:
            raise AgentError("The proposal was generated for a different repository path")
        stored = {c.get("path"): c for c in proposal["changes"] if isinstance(c, dict)}
        validated, errors = validate_changes(
            repo, [{"path": c.get("path"), "content": c.get("content")} for c in proposal["changes"]]
        )
        for change in validated:
            original = stored.get(change["path"])
            if original is None or original.get("base_sha256") != change["base_sha256"]:
                errors.append(f"{change['path']}: the file changed since the proposal was generated")
        if len(validated) != len(stored) and not errors:
            errors.append("some files already contain the proposed content or changed since the proposal")
        if errors:
            raise AgentError("Cannot apply the proposal: " + "; ".join(errors))
        backup_dir = artifacts.project_dir(project_id) / "backups" / f"task_{task_id}"
        written = _apply(repo, validated, backup_dir)
        _mark_approved_or_rollback(repo, task_id, validated, backup_dir)
    return {
        "status": "approved",
        "task_id": task_id,
        "written": written,
        "backup_dir": str(backup_dir) if any(c["base_sha256"] for c in validated) else None,
    }


def reject(project_id: int, task_id: int, reason: str = "") -> dict:
    """Discard a proposal without touching the repository."""
    note = f"Rejected by user: {reason.strip()}" if reason and reason.strip() else "Rejected by user"
    with db.inference_slot(f"rejecting task #{task_id}"):  # serialised with approve()
        _proposed_task(project_id, task_id)
        return db.transition(task_id, ["proposed"], "rejected", error=note[:500])
