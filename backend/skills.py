"""Skills: reusable working instructions ("so arbeitest du") the user gives the model.

A skill is a Markdown file in ``<home>/skills`` (optionally with a small front matter
``name:`` / ``description:``) plus a JSON file with its metadata. Skills are only ever used as
*instructions in the prompt* – nothing is executed.

* **Own skills** (written in the Studio) are active right away.
* **Imported skills** (a ``.md`` file chosen by the user, or downloaded with the command line from
  an approved raw GitHub host) start **inactive**: the user reads them and switches them on. The
  checksum of the reviewed text is kept – a file changed afterwards is not used again until it was
  reviewed again.

A skill chosen in the chat or for a document is resolved on the server (:func:`enabled_skill`);
the request never carries instructions itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from . import artifacts, config, db
from .web import HTTPError, Request, Response, route

MAX_SKILL_BYTES = 64 * 1024
APPROVED_HOSTS = frozenset({"raw.githubusercontent.com", "gist.githubusercontent.com"})
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TIMEOUT_S = 20


class SkillError(RuntimeError):
    """A skill could not be installed; the message says why."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        raise SkillError(f"Redirects are not followed (server redirected to {newurl})")


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ".." in name:
        raise SkillError(
            "Skill name must be 1-64 characters: lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def validate_url(url: object) -> str:
    if not isinstance(url, str):
        raise SkillError("URL must be a string")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise SkillError("Only https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise SkillError("URLs with credentials are not allowed")
    if (parsed.hostname or "").lower() not in APPROVED_HOSTS:
        raise SkillError(f"Host not approved. Allowed hosts: {', '.join(sorted(APPROVED_HOSTS))}")
    if parsed.port not in (None, 443):
        raise SkillError("Custom ports are not allowed")
    if not parsed.path.lower().endswith(".md"):
        raise SkillError("Only Markdown files (.md) can be installed as skills")
    return parsed.geturl()


def _download(url: str, opener: urllib.request.OpenerDirector | None) -> bytes:
    opener = opener or urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "LowRAM-AI-skill-installer"})
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_SKILL_BYTES:
                raise SkillError(f"Skill is larger than the {MAX_SKILL_BYTES // 1024} KB limit")
            data = response.read(MAX_SKILL_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SkillError(f"Download failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, SkillError):
            raise exc.reason from exc
        raise SkillError(f"Download failed: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SkillError(f"Download failed: {exc}") from exc
    if len(data) > MAX_SKILL_BYTES:
        raise SkillError(f"Skill is larger than the {MAX_SKILL_BYTES // 1024} KB limit")
    return data


def install_skill(
    url: str,
    name: str,
    *,
    overwrite: bool = False,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict:
    """Download a Markdown skill (max 64 KB) and store it untrusted on disk.

    Returns ``{"name", "path", "sha256", "bytes", "url", "trusted": False}``.
    """
    name = validate_name(name)
    url = validate_url(url)
    folder = config.skills_dir()
    target = folder / f"{name}.md"
    if target.exists() and not overwrite:
        raise SkillError(f"Skill {name!r} already exists; pass overwrite=True to replace it")
    data = _download(url, opener)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("Skill is not valid UTF-8 text") from exc
    if b"\x00" in data:
        raise SkillError("Skill contains binary data")
    digest = hashlib.sha256(data).hexdigest()
    artifacts.write_bytes_atomic(target, data)
    meta = {
        "name": name,
        "url": url,
        "sha256": digest,
        "bytes": len(data),
        "installed_at": db.utcnow(),
        "trusted": False,
        "source": "url",
        "enabled": False,  # read it first, then switch it on in the Studio
    }
    artifacts.write_json_atomic(folder / f"{name}.json", meta)
    return {**meta, "path": str(target)}


def list_skills() -> list[dict]:
    """Installed skills; broken entries are listed with an ``error`` instead of vanishing."""
    skills = []
    for meta_path in sorted(config.skills_dir().glob("*.json")):
        md = meta_path.with_suffix(".md")
        broken = {"name": meta_path.stem, "url": "", "checksum_ok": False, "path": str(md)}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            skills.append({**broken, "error": f"metadata unreadable: {exc}"})
            continue
        if not isinstance(meta, dict):
            skills.append({**broken, "error": "metadata is not a JSON object"})
            continue
        if not md.exists():
            skills.append({**broken, **meta, "checksum_ok": False, "error": "Markdown file is missing"})
            continue
        actual = hashlib.sha256(md.read_bytes()).hexdigest()
        meta["checksum_ok"] = actual == meta.get("sha256")
        meta["path"] = str(md)
        skills.append(meta)
    return skills


def skill_path(name: str) -> Path:
    return config.skills_dir() / f"{validate_name(name)}.md"


# --------------------------------------------------------------------------- skills in the Studio

MAX_PROMPT_CHARS = 8000  # what a skill may add to a prompt (~2500 tokens)
MAX_TITLE_CHARS = 80
MAX_DESCRIPTION_CHARS = 300
# "import" is a route; the others are device names Windows does not allow as file names (con.md …)
RESERVED_NAMES = frozenset({"import", "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                            *(f"lpt{i}" for i in range(1, 10))})
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def parse_markdown(text: str) -> tuple[dict, str]:
    """``({"name", "title", "description"}, body)`` – a small front matter is optional."""
    meta: dict = {}
    match = _FRONT.match(text)
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            key = key.strip().lower()
            if sep and key in ("name", "title", "description"):
                meta[key] = value.strip().strip("\"'")
        text = text[match.end():]
    return meta, text.strip()


def _clean(value: object, limit: int, what: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{what} muss Text sein.")
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value).strip()
    if required and not value:
        raise ValueError(f"Bitte gib {what} an.")
    if len(value) > limit:
        raise ValueError(f"{what} darf höchstens {limit} Zeichen lang sein.")
    return value


def _slug(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower().translate(str.maketrans("äöüß", "aous"))).strip("-")[:48] or "skill"
    if not base[0].isalnum():
        base = "s" + base
    name, number = base, 2
    while (config.skills_dir() / f"{name}.json").exists() or name in RESERVED_NAMES:
        name, number = f"{base}-{number}", number + 1
    return name


def _paths(name: str) -> tuple[Path, Path]:
    name = validate_name(name)
    folder = config.skills_dir()
    return folder / f"{name}.md", folder / f"{name}.json"


def _load(name: str) -> tuple[dict, str]:
    md, meta_path = _paths(name)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise db.NotFound(f"Den Skill {name!r} gibt es nicht (mehr).") from exc
    if not isinstance(meta, dict):
        raise db.NotFound(f"Der Skill {name!r} ist beschädigt.")
    return meta, text


def _view(meta: dict, text: str | None, *, with_text: bool = False) -> dict:
    front, body = parse_markdown(text or "")
    sha = hashlib.sha256((text or "").encode("utf-8")).hexdigest() if text is not None else None
    source = meta.get("source") or ("url" if meta.get("url") else "own")
    view = {
        "name": meta.get("name"),
        "title": meta.get("title") or front.get("title") or front.get("name") or meta.get("name"),
        "description": meta.get("description") or front.get("description") or "",
        "source": source,
        "enabled": bool(meta.get("enabled", source == "own")),
        "reviewed": bool(meta.get("reviewed_sha256")) and meta.get("reviewed_sha256") == sha,
        "changed_since_review": bool(meta.get("reviewed_sha256")) and meta.get("reviewed_sha256") != sha,
        "chars": len(body),
        "too_long": len(body) > MAX_PROMPT_CHARS,
        "url": meta.get("url") or None,
        "updated_at": meta.get("updated_at") or meta.get("installed_at"),
    }
    view["usable"] = view["enabled"] and view["reviewed"] and not view["too_long"] and bool(body)
    if with_text:
        view["instructions"] = body
        view["text"] = text
    return view


def all_skills() -> list[dict]:
    """Every skill for the Studio (broken entries are left out; ``list_skills`` shows them)."""
    out = []
    for meta_path in sorted(config.skills_dir().glob("*.json")):
        try:
            meta, text = _load(meta_path.stem)
        except (db.NotFound, SkillError):
            continue
        out.append(_view(meta, text))
    return sorted(out, key=lambda item: (not item["usable"], str(item["title"]).lower()))


def get_skill(name: str) -> dict:
    meta, text = _load(name)
    return _view(meta, text, with_text=True)


def _write(name: str, meta: dict, text: str) -> dict:
    md, meta_path = _paths(name)
    data = text.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    meta.update(name=name, sha256=sha, bytes=len(data), updated_at=db.utcnow())
    artifacts.write_bytes_atomic(md, data)
    artifacts.write_json_atomic(meta_path, meta)
    return _view(meta, text, with_text=True)


def _compose(title: str, description: str, instructions: str) -> str:
    head = f"---\nname: {title}\n" + (f"description: {description}\n" if description else "") + "---\n\n"
    return head + instructions.strip() + "\n"


def save_skill(data: dict, name: str | None = None) -> dict:
    """Create (``name`` None) or change a skill written in the Studio – it is active right away."""
    title = _clean(data.get("title"), MAX_TITLE_CHARS, "einen Namen", required=True)
    description = _clean(data.get("description"), MAX_DESCRIPTION_CHARS, "die Beschreibung")
    instructions = data.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Bitte schreib, wie die KI arbeiten soll (die Anleitung).")
    if len(instructions) > MAX_PROMPT_CHARS:
        raise ValueError(f"Die Anleitung darf höchstens {MAX_PROMPT_CHARS} Zeichen lang sein.")
    if name is None:
        name, meta = _slug(title), {"source": "own", "installed_at": db.utcnow()}
    else:
        meta, _ = _load(name)
    text = _compose(title, description, instructions)
    meta.update(title=title, description=description, enabled=bool(data.get("enabled", meta.get("enabled", True))),
                trusted=True, reviewed_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    return _write(name, meta, text)


def import_skill(filename: str, data: bytes) -> dict:
    """A ``.md`` skill file chosen by the user: stored inactive until it was read and switched on."""
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError(f"Die Skill-Datei ist größer als {MAX_SKILL_BYTES // 1024} KB.")
    if b"\x00" in data:
        raise ValueError("Das ist keine Textdatei.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Die Skill-Datei ist kein UTF-8-Text.") from exc
    front, body = parse_markdown(text)
    if not body:
        raise ValueError("Die Skill-Datei enthält keine Anleitung.")
    stem = re.sub(r"\.(md|markdown|txt)$", "", (filename or "").rsplit("/", 1)[-1], flags=re.I)
    title = _clean((front.get("title") or front.get("name") or stem or "Skill")[:MAX_TITLE_CHARS], MAX_TITLE_CHARS,
                   "einen Namen")
    description = (front.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    meta = {"source": "file", "title": title or "Skill", "description": description, "enabled": False,
            "trusted": False, "installed_at": db.utcnow()}
    return _write(_slug(title or "skill"), meta, text)


def set_enabled(name: str, enabled: bool) -> dict:
    """Switch a skill on (= its current text was read and is trusted) or off."""
    meta, text = _load(name)
    if enabled:
        _, body = parse_markdown(text)
        if not body:
            raise ValueError("Der Skill enthält keine Anleitung.")
        if len(body) > MAX_PROMPT_CHARS:
            raise ValueError(f"Der Skill ist zu lang für die KI (höchstens {MAX_PROMPT_CHARS} Zeichen Anleitung). "
                             "Kürze ihn zuerst.")
        meta.update(enabled=True, trusted=True, reviewed_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    else:
        meta["enabled"] = False
    artifacts.write_json_atomic(_paths(name)[1], meta)
    return _view(meta, text, with_text=True)


def delete_skill(name: str) -> None:
    for path in _paths(name):
        path.unlink(missing_ok=True)


def enabled_skill(name: object) -> dict:
    """``{"name", "instructions"}`` of a switched-on, reviewed skill – else ValueError (400)."""
    if not isinstance(name, str):
        raise ValueError("Unbekannter Skill.")
    try:
        meta, text = _load(name)
    except SkillError as exc:
        raise ValueError("Unbekannter Skill.") from exc
    view = _view(meta, text, with_text=True)
    if not view["enabled"]:
        raise ValueError(f"Der Skill „{view['title']}“ ist ausgeschaltet.")
    if not view["reviewed"]:
        raise ValueError(f"Der Skill „{view['title']}“ wurde geändert und muss erst wieder geprüft werden.")
    if view["too_long"] or not view["instructions"]:
        raise ValueError(f"Der Skill „{view['title']}“ ist leer oder zu lang.")
    return {"name": view["title"], "instructions": view["instructions"]}


def _skill_name(raw: str) -> str:
    try:
        return validate_name(raw)
    except SkillError as exc:
        raise db.NotFound("Unbekannter Skill.") from exc


@route("GET", r"/api/skills")
def get_skills(req: Request) -> Response:
    return Response({"skills": all_skills(), "max_chars": MAX_PROMPT_CHARS})


@route("POST", r"/api/skills")
def post_skill(req: Request) -> Response:
    return Response({"skill": save_skill(req.json())}, HTTPStatus.CREATED)


@route("POST", r"/api/skills/import")
def post_import(req: Request) -> Response:
    req.check_csrf()
    data = req.body(max_bytes=MAX_SKILL_BYTES + 1)
    try:
        skill = import_skill(req.headers.get("X-Filename") or "", data)
    except ValueError as exc:
        raise HTTPError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
    return Response({"skill": skill}, HTTPStatus.CREATED)


@route("GET", r"/api/skills/([a-z0-9][a-z0-9._-]{0,63})")
def get_one(req: Request, name: str) -> Response:
    return Response({"skill": get_skill(_skill_name(str(name)))})


@route("POST", r"/api/skills/([a-z0-9][a-z0-9._-]{0,63})")
def post_update(req: Request, name: str) -> Response:
    return Response({"skill": save_skill(req.json(), _skill_name(str(name)))})


@route("POST", r"/api/skills/([a-z0-9][a-z0-9._-]{0,63})/enabled")
def post_enabled(req: Request, name: str) -> Response:
    data = req.json()
    if not isinstance(data.get("enabled"), bool):
        raise ValueError("enabled must be true or false")
    return Response({"skill": set_enabled(_skill_name(str(name)), data["enabled"])})


@route("POST", r"/api/skills/([a-z0-9][a-z0-9._-]{0,63})/delete")
def post_delete(req: Request, name: str) -> Response:
    req.json()
    name = _skill_name(str(name))
    _load(name)  # 404 for unknown skills
    delete_skill(name)
    return Response({"deleted": True})
