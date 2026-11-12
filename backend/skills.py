"""Download Markdown "skills" from approved raw GitHub hosts.

Downloaded skills are stored on disk with their SHA-256 checksum and are
**not** trusted, executed or injected into prompts. A reviewed loader is
planned for a later version.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import artifacts, config, db

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
