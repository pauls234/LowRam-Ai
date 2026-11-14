"""Chat with the local model: conversations, context building and streamed answers.

Design notes
------------
* Conversations and messages live in SQLite (``conversations``/``messages``,
  plus ``message_attachments`` for attached text files; everything is deleted
  together via ``ON DELETE CASCADE``). Columns added after v0.3 are added to
  older databases with ``ALTER TABLE`` on first use.
* An answer is one heavy job: the global inference slot is taken *before* the
  stream starts (busy -> normal JSON 409) and released when the stream ends,
  is stopped, the client disconnects, or the stream object is dropped without
  ever being iterated (see :class:`ChatStream`).
* The model runs in a worker thread; the HTTP thread turns its text into SSE
  events: ``start``, ``status`` (``{"type":"status","text":…}`` right after
  ``start`` and every ~5 s while the managed model is still being started or
  loaded), ``reasoning`` (``<think>`` text, not stored), ``token``, ``ping``
  (keep-alive after 10 s of silence, e.g. while a slow model reads the prompt;
  also lets a closed browser tab be noticed), then ``done`` or ``error``.
  Problems found before the stream (busy, invalid input, no model installed,
  engine paused) are plain JSON errors; later ones are ``error`` events.
  ``POST …/messages``, ``…/continue`` and ``…/regenerate`` all stream this way.
* With the managed engine the prompt is sized to the real llama-server
  context (``engine.ctx_size()``), not only to the RAM budget.
* An assistant message is never left ``streaming``: it ends ``complete``,
  ``stopped`` (stop button / disconnect; partial text kept) or ``error`` (with
  the reason). Leftovers from a crash are marked ``stopped`` on startup.
* Custom instructions (``settings.chat.instructions``) are appended to the
  system prompt; attachments are put into the prompt as fenced blocks.
* Model output is untrusted text. It is stored and returned verbatim as JSON
  strings; the UI renders it as text only.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http import HTTPStatus

from . import artifacts, config, db, llm, memory, settings, web
from .web import FileResponse, HTTPError, Request, Response, StreamResponse, route

log = logging.getLogger("lowram.chat")

MODES = ("chat", "code")
MAX_CONTENT_CHARS = 20_000
MAX_TITLE_CHARS = 120
AUTO_TITLE_CHARS = 60
MAX_INSTRUCTIONS_CHARS = 4000
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 200 * 1024
MAX_ATTACHMENT_NAME_CHARS = 120
MAX_QUERY_CHARS = 200
MAX_MESSAGE_BODY_BYTES = 4 * 1024 * 1024  # 5 attachments of 200 KB plus JSON escaping
CONTEXT_FACTOR = 2  # conversations get twice the task-queue carry-over budget
MIN_MAX_TOKENS = {"chat": 512, "code": 1024}
TEMPERATURE = {"chat": 0.5, "code": 0.2}
MESSAGE_OVERHEAD_CHARS = 16  # charged per message for role markers of the chat template
MIN_TRUNCATED_CHARS = 400
HISTORY_LIMIT = 400  # newest stored messages considered when building the context
SAVE_INTERVAL_S = 1.0
PING_INTERVAL_S = 10.0
QUEUE_POLL_S = 0.2
STOP_GRACE_S = 3.0
WORKER_JOIN_S = 2.0
ENGINE_START_TIMEOUT_S = 300.0
STATUS_INTERVAL_S = 5.0
CHARS_PER_TOKEN = 2.5  # conservative: code and non-English text pack fewer characters per token
CTX_RESERVE_TOKENS = 256  # chat-template markup and safety margin
MIN_CONTEXT_CHARS = 1000
LOADING_TEXT = "Loading the model… (first answer after a start can take a minute)"

NO_MODEL_MESSAGE = "No model is running. Open Models to install and start one."
OMITTED_NOTE = "[Earlier messages of this conversation were left out to fit the memory budget.]"
INTERRUPTED_ERROR = "Interrupted: LowRAM AI was closed before this answer finished."
CONTINUE_NOTE = (
    "(System note: your previous answer was cut off because it reached the length limit. Continue "
    "exactly where you stopped – do not repeat anything and do not start over.)"
)
INSTRUCTIONS_HEADER = "\n\nThe user's custom instructions (follow them in every answer):\n"

CHAT_SYSTEM_PROMPT = (
    "You are LowRAM AI, a helpful, honest and concise assistant. You run entirely on the user's own "
    "computer, so nothing they write leaves this machine. Always answer in the language of the user's "
    "latest message. Use Markdown where it helps readability: short paragraphs, lists, tables and code "
    "blocks fenced with their language. Get to the point and give more detail when asked. If you are not "
    "sure or do not know something, say so plainly instead of guessing. You cannot browse the internet, "
    "run code or open files, so never claim that you did."
)
CODE_SYSTEM_PROMPT = (
    "You are LowRAM AI in code mode: an experienced senior software engineer helping the user on their "
    "own computer. Always answer in the language of the user's latest message, but keep code, "
    "identifiers and code comments consistent with the user's code. Put every piece of code in a "
    "Markdown code fence tagged with its language, for example ```python. Prefer simple, correct, "
    "complete and secure solutions; briefly explain what you changed and why, and point out edge cases "
    "and tests worth adding. You cannot run code, tests or commands: never claim that you ran or tested "
    "anything; tell the user how to verify it instead. If the request is ambiguous, state your "
    "assumptions or ask one short question."
)
SYSTEM_PROMPTS = {"chat": CHAT_SYSTEM_PROMPT, "code": CODE_SYSTEM_PROMPT}

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'chat' CHECK (mode IN ('chat', 'code')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete' CHECK (status IN ('complete', 'streaming', 'stopped', 'error')),
    error TEXT,
    model TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL,
    tokens INTEGER,
    tokens_per_s REAL,
    finish_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE TABLE IF NOT EXISTS message_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    chars INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_attachments_message ON message_attachments(message_id, position);
"""
db.register_schema(SCHEMA)
# Columns added in v0.3.2; databases created by v0.3 get them via ALTER TABLE.
_ADDED_COLUMNS = (("tokens", "INTEGER"), ("tokens_per_s", "REAL"), ("finish_reason", "TEXT"))
_MIGRATED: set[str] = set()
_MIGRATE_LOCK = threading.Lock()

_CONVERSATION_SQL = (
    "SELECT c.id, c.title, c.mode, c.created_at, c.updated_at, "
    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
    "FROM conversations c"
)
_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, status, error, model, duration_s, created_at, "
    "tokens, tokens_per_s, finish_reason"
)


def _migrate(conn: sqlite3.Connection) -> None:
    key = str(config.db_path())
    if key in _MIGRATED:
        return
    with _MIGRATE_LOCK:
        if key in _MIGRATED:
            return
        present = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        for name, kind in _ADDED_COLUMNS:
            if name not in present:
                try:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {kind}")
                except sqlite3.OperationalError as exc:  # another process was faster
                    if "duplicate column" not in str(exc).lower():
                        raise
        _MIGRATED.add(key)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    with db.connect() as conn:
        _migrate(conn)
        yield conn


# --------------------------------------------------------------------------- validation


def _strip_controls(text: str, keep: str = "") -> str:
    return "".join(ch for ch in text if ch in keep or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


def clean_title(value: object, *, required: bool = False) -> str:
    """Whitespace-collapsed title without control characters (at most 120 characters)."""
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("The title must be text")
    title = " ".join(_strip_controls(" ".join(value.split())).split())
    if required and not title:
        raise ValueError("A title is required")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"The title must be at most {MAX_TITLE_CHARS} characters")
    return title


def auto_title(content: str, attachments: list[dict] | None = None) -> str:
    """Title from the first user message (or its first file name): at most 60 characters."""
    title = " ".join(_strip_controls(" ".join(content.split())).split())
    if not title and attachments:
        title = attachments[0]["name"]
    if len(title) > AUTO_TITLE_CHARS:
        title = title[: AUTO_TITLE_CHARS - 1].rstrip() + "…"
    return title or "New chat"


def validate_mode(value: object) -> str:
    if value is None:
        return "chat"
    if value not in MODES:
        raise ValueError("mode must be 'chat' or 'code'")
    return str(value)


def validate_content(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ValueError("Write a message first (content must not be empty)")
    if len(value) > MAX_CONTENT_CHARS:
        raise ValueError(f"The message is too long: at most {MAX_CONTENT_CHARS} characters, got {len(value)}")
    if "\x00" in value:
        raise ValueError("The message contains a NUL character")
    return value


def clean_attachment_name(name: str, position: int) -> str:
    """File name only (no folders), no control characters or backticks, at most 120 characters."""
    base = re.split(r"[\\/]", name)[-1]
    base = " ".join(_strip_controls(base).replace("`", "'").split())
    if len(base) > MAX_ATTACHMENT_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            base = stem[: MAX_ATTACHMENT_NAME_CHARS - len(ext) - 2] + "…." + ext
        else:
            base = base[: MAX_ATTACHMENT_NAME_CHARS - 1] + "…"
    return base or f"attachment-{position}.txt"


def validate_attachments(value: object) -> list[dict]:
    """``[{name, content}]`` -> cleaned ``[{name, content, chars}]`` (text files only)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'attachments' must be a list of {name, content}")
    if len(value) > MAX_ATTACHMENTS:
        raise ValueError(f"At most {MAX_ATTACHMENTS} files can be attached to one message")
    clean: list[dict] = []
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content"), str):
            raise ValueError(f"Attachment {position} must be an object with text 'name' and 'content'")
        name = clean_attachment_name(item["name"], position)
        content = item["content"]
        if "\x00" in content:
            raise ValueError(f"{name} contains NUL bytes; only text files can be attached")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError(f"{name} is not valid UTF-8 text; only text files can be attached") from None
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{name} is larger than {MAX_ATTACHMENT_BYTES // 1024} KB ({size // 1024} KB)")
        content = content.replace("\r\n", "\n")
        clean.append({"name": name, "content": content, "chars": len(content)})
    return clean


def clean_instructions(value: object) -> str:
    """Validated custom instructions for ``POST /api/chats/instructions``."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Custom instructions must be text")
    text = _strip_controls(value.replace("\r\n", "\n"), keep="\n\t").strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(
            f"Custom instructions must be at most {MAX_INSTRUCTIONS_CHARS} characters, got {len(text)}"
        )
    return text


def current_instructions() -> str:
    """``settings.chat.instructions`` cleaned; an over-long value (generic settings API) is truncated."""
    try:
        value = settings.get("chat", "instructions")
    except Exception:  # unreadable settings must not block chatting
        log.exception("Could not read the custom instructions; continuing without them")
        return ""
    if not isinstance(value, str):
        return ""
    text = _strip_controls(value.replace("\r\n", "\n"), keep="\n\t").strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        log.warning("Custom instructions are %s characters; using the first %s", len(text), MAX_INSTRUCTIONS_CHARS)
        text = text[:MAX_INSTRUCTIONS_CHARS].rstrip()
    return text


def default_percent() -> object:
    try:
        return settings.get("budget_percent")
    except Exception:  # a broken settings file must not block chatting
        log.exception("Could not read budget_percent from the settings; using the default")
        return memory.DEFAULT_PERCENT


def clean_query(value: object) -> str:
    if value is None:
        return ""
    query = " ".join(str(value).split())
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"The search text must be at most {MAX_QUERY_CHARS} characters")
    return query


# --------------------------------------------------------------------------- storage


def _message_dict(row: sqlite3.Row, attachments: list[dict] | None = None) -> dict:
    data = dict(row)
    data["truncated"] = data.get("finish_reason") == "length"
    data["attachments"] = attachments or []
    return data


def _attachment_summaries(conn: sqlite3.Connection, message_ids: list[int]) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for start in range(0, len(message_ids), 500):
        chunk = message_ids[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT message_id, name, chars FROM message_attachments WHERE message_id IN ({marks}) "
            "ORDER BY message_id, position",
            chunk,
        ):
            found.setdefault(row["message_id"], []).append({"name": row["name"], "chars": row["chars"]})
    return found


def _messages_with_attachments(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    summaries = _attachment_summaries(conn, [row["id"] for row in rows if row["role"] == "user"])
    return [_message_dict(row, summaries.get(row["id"])) for row in rows]


def _get_message(conn: sqlite3.Connection, message_id: int) -> dict:
    row = conn.execute(f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise db.NotFound(f"Message #{message_id} not found")
    return _messages_with_attachments(conn, [row])[0]


def _get_conversation(conn: sqlite3.Connection, chat_id: int) -> dict:
    row = conn.execute(f"{_CONVERSATION_SQL} WHERE c.id = ?", (chat_id,)).fetchone()
    if row is None:
        raise db.NotFound(f"Chat #{chat_id} not found")
    return dict(row)


def get_conversation(chat_id: int) -> dict:
    with _connect() as conn:
        return _get_conversation(conn, chat_id)


def _like_pattern(query: str) -> str:
    escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _casefold(value: object) -> str | None:
    return value.casefold() if isinstance(value, str) else None


def list_conversations(query: object = None) -> list[dict]:
    """All chats, newest first; with ``query`` only those whose title or a message contains it."""
    text = clean_query(query)
    with _connect() as conn:
        if not text:
            rows = conn.execute(f"{_CONVERSATION_SQL} ORDER BY c.updated_at DESC, c.id DESC").fetchall()
        else:
            # Unicode-aware case folding (SQLite's LIKE only folds ASCII); wildcards are escaped.
            conn.create_function("lowram_fold", 1, _casefold, deterministic=True)
            pattern = _like_pattern(text)
            rows = conn.execute(
                f"{_CONVERSATION_SQL} WHERE lowram_fold(c.title) LIKE ? ESCAPE '\\' OR EXISTS ("
                "SELECT 1 FROM messages m WHERE m.conversation_id = c.id "
                "AND lowram_fold(m.content) LIKE ? ESCAPE '\\') "
                "ORDER BY c.updated_at DESC, c.id DESC",
                (pattern, pattern),
            ).fetchall()
        return [dict(row) for row in rows]


def create_conversation(title: object = None, mode: object = None) -> dict:
    clean = clean_title(title)
    clean_mode = validate_mode(mode)
    now = db.utcnow()
    with _connect() as conn:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO conversations (title, mode, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (clean, clean_mode, now, now),
            )
        return _get_conversation(conn, int(cur.lastrowid))


def rename_conversation(chat_id: int, title: object) -> dict:
    clean = clean_title(title, required=True)
    with _connect() as conn:
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (clean, db.utcnow(), chat_id)
            )
            if cur.rowcount != 1:
                raise db.NotFound(f"Chat #{chat_id} not found")
        return _get_conversation(conn, chat_id)


def delete_conversation(chat_id: int) -> None:
    request_stop(chat_id)  # a running answer ends as "stopped"; its rows go away with the chat
    with _connect() as conn, db.transaction(conn):
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (chat_id,))
        if cur.rowcount != 1:
            raise db.NotFound(f"Chat #{chat_id} not found")


def list_messages(chat_id: int) -> list[dict]:
    with _connect() as conn:
        _get_conversation(conn, chat_id)
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        return _messages_with_attachments(conn, rows)


def _recent_messages(chat_id: int, char_limit: int, before_id: int | None = None,
                     limit: int = HISTORY_LIMIT) -> list[dict]:
    """The newest ``limit`` messages (before ``before_id``) in chronological order, with
    attachment contents loaded up to ``char_limit`` characters each (``chars`` is the full size)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
            (chat_id, before_id if before_id is not None else 2**62, limit),
        ).fetchall()
        messages = [_message_dict(row) for row in reversed(rows)]
        user_ids = [m["id"] for m in messages if m["role"] == "user"]
        files: dict[int, list[dict]] = {}
        for start in range(0, len(user_ids), 500):
            chunk = user_ids[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT message_id, name, chars, substr(content, 1, ?) AS content FROM message_attachments "
                f"WHERE message_id IN ({marks}) ORDER BY message_id, position",
                (max(char_limit, 0) + 1, *chunk),
            ):
                files.setdefault(row["message_id"], []).append(
                    {"name": row["name"], "chars": row["chars"], "content": row["content"]}
                )
    for message in messages:
        message["attachments"] = files.get(message["id"], [])
    return messages


def _insert_turn(chat_id: int, content: str, attachments: list[dict], model: str | None) -> tuple[dict, dict, dict]:
    """Store the user message (with its files) and an empty ``streaming`` assistant message atomically."""
    now = db.utcnow()
    with _connect() as conn:
        with db.transaction(conn):
            _get_conversation(conn, chat_id)
            user_id = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, status, created_at) "
                "VALUES (?, 'user', ?, 'complete', ?)",
                (chat_id, content, now),
            ).lastrowid
            for position, item in enumerate(attachments):
                conn.execute(
                    "INSERT INTO message_attachments (message_id, position, name, content, chars) VALUES (?, ?, ?, ?, ?)",
                    (user_id, position, item["name"], item["content"], item["chars"]),
                )
            assistant_id = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, status, model, created_at) "
                "VALUES (?, 'assistant', '', 'streaming', ?, ?)",
                (chat_id, model, now),
            ).lastrowid
            conn.execute(
                "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                "WHERE id = ?",
                (now, auto_title(content, attachments), chat_id),
            )
        return _get_message(conn, user_id), _get_message(conn, assistant_id), _get_conversation(conn, chat_id)


def _last_turn(conn: sqlite3.Connection, chat_id: int) -> tuple[dict | None, dict | None]:
    """``(user_message, assistant_message)`` of the newest turn (either may be ``None``)."""
    rows = conn.execute(
        f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 2", (chat_id,)
    ).fetchall()
    if not rows:
        return None, None
    last = _messages_with_attachments(conn, rows)
    if last[0]["role"] == "user":
        return last[0], None
    user = last[1] if len(last) > 1 and last[1]["role"] == "user" else None
    return user, last[0]


@web.on_startup
def recover_interrupted(server: object = None) -> int:
    """Mark answers that were still ``streaming`` when LowRAM AI stopped as ``stopped``."""
    with _ACTIVE_LOCK:
        active = [answer.assistant_id for answer in _ACTIVE.values()]
    marks = ",".join("?" for _ in active)
    extra = f" AND id NOT IN ({marks})" if active else ""
    with _connect() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE messages SET status = 'stopped', error = COALESCE(error, ?) WHERE status = 'streaming'" + extra,
            (INTERRUPTED_ERROR, *active),
        )
    if cur.rowcount:
        log.warning("Marked %s interrupted chat answer(s) as stopped", cur.rowcount)
    return cur.rowcount


# --------------------------------------------------------------------------- export


def _export_filename(title: str, chat_id: int) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_title).strip("-._")[:80]
    return f"{slug or f'chat-{chat_id}'}.md"


def export_markdown(chat_id: int) -> tuple[str, str]:
    """``(markdown, download_name)`` of a conversation; reasoning is never stored, so never exported."""
    chat = get_conversation(chat_id)
    messages = list_messages(chat_id)
    title = chat["title"] or "Untitled chat"
    lines = [
        f"# {title}",
        "",
        f"*{'Code' if chat['mode'] == 'code' else 'Chat'} conversation from LowRAM AI · started "
        f"{chat['created_at']} · exported {db.utcnow()} · {len(messages)} messages*",
    ]
    for message in messages:
        if message["role"] == "user":
            heading = "## User"
        else:
            details = [message["model"]] if message["model"] else []
            if message["status"] != "complete":
                details.append(message["status"])
            if message["truncated"]:
                details.append("cut off at the length limit")
            heading = "## Assistant" + (f" ({', '.join(details)})" if details else "")
        lines += ["", heading, "", message["content"].rstrip() or "*(no text)*"]
        for item in message["attachments"]:
            lines += ["", f"*Attached file: {item['name']} ({item['chars']} characters)*"]
        if message["role"] == "assistant" and message["error"]:
            lines += ["", f"*Error: {message['error']}*"]
    return "\n".join(lines) + "\n", _export_filename(title, chat_id)


# --------------------------------------------------------------------------- context


def truncate_middle(text: str, limit: int) -> str:
    """Cut ``text`` to about ``limit`` characters, keeping its start and end with a marker."""
    if len(text) <= limit:
        return text
    marker = "\n\n[… {n} characters left out to fit the memory budget …]\n\n"
    keep = max(0, limit - len(marker.format(n=len(text))))
    head = keep * 2 // 3
    tail = keep - head
    omitted = len(text) - head - tail
    return text[:head] + marker.format(n=omitted) + (text[-tail:] if tail else "")


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _attachment_block(name: str, body: str, chars: int) -> str:
    fence = _fence(body)
    note = f"\n[…truncated – {chars - len(body)} characters omitted]" if chars > len(body) else ""
    return f"\n\nAttached file: {name}\n{fence}\n{body}\n{fence}{note}"


def render_user_message(content: str, attachments: list[dict], limit: int) -> str:
    """The user's text followed by fenced attachment blocks, within about ``limit`` characters.

    The typed text has priority (it keeps at least half the room); attachments
    share the rest fairly and are cut at the end with a visible note.
    """
    files = [(a["name"], a.get("content") or "", int(a.get("chars") or len(a.get("content") or ""))) for a in attachments]
    full = content + "".join(_attachment_block(n, b, c) for n, b, c in files)
    if len(full) <= limit:
        return full.lstrip("\n")
    if not files:
        return truncate_middle(content, limit)
    files_size = len(full) - len(content)
    room_for_text = max(limit - files_size, limit // 2)
    text = truncate_middle(content, room_for_text) if len(content) > room_for_text else content
    left = max(limit - len(text), 0)
    keep: dict[int, int] = {}
    order = sorted(range(len(files)), key=lambda i: len(files[i][1]))
    for position, index in enumerate(order):
        name, body, chars = files[index]
        overhead = len(_attachment_block(name, "", chars)) + len(_fence(body))
        share = left // (len(order) - position)
        keep[index] = max(0, min(len(body), share - overhead))
        left -= min(share, keep[index] + overhead)
    blocks = "".join(_attachment_block(n, b[: keep[i]], c) for i, (n, b, c) in enumerate(files))
    return (text + blocks).lstrip("\n")


def _answered_turns(history: list[dict]) -> list[dict]:
    """Chronological user/assistant messages that form complete turns.

    Failed or empty answers and the questions they belonged to are skipped, so
    roles always alternate (several chat templates reject anything else).
    """
    turns: list[dict] = []
    for index, message in enumerate(history):
        if message["role"] != "user" or index + 1 >= len(history):
            continue
        answer = history[index + 1]
        if (
            answer["role"] == "assistant"
            and answer["status"] in ("complete", "stopped")
            and (answer["content"] or "").strip()
        ):
            files = message.get("attachments") or []
            turns.append({
                "role": "user", "content": render_user_message(message["content"], files, 2**62),
                "_text": message["content"], "_files": files,
            })
            turns.append({"role": "assistant", "content": answer["content"]})
    return turns


def system_prompt(mode: str, instructions: str = "") -> str:
    base = SYSTEM_PROMPTS[mode]
    return base + INSTRUCTIONS_HEADER + instructions if instructions else base


def build_messages(
    mode: str,
    history: list[dict],
    new_content: str,
    char_budget: int,
    *,
    attachments: list[dict] | None = None,
    instructions: str = "",
    partial: str | None = None,
) -> list[dict]:
    """System prompt + the newest turns that fit ``char_budget`` + the new user message.

    The new message and its attachments have priority; the oldest messages
    are dropped first and messages are never split, except that the newest
    earlier message (or an older one's attachments) and the new message are
    shortened with a visible marker when they alone are larger than the
    budget. With ``partial`` (continue), the cut-off answer and a note asking
    to continue follow the new message.
    """
    system = system_prompt(mode, instructions)
    remaining = char_budget - len(system) - MESSAGE_OVERHEAD_CHARS
    tail: list[dict] = []
    if partial is not None:
        remaining -= len(CONTINUE_NOTE) + MESSAGE_OVERHEAD_CHARS
        partial_text = truncate_middle(partial, max(remaining // 2 - MESSAGE_OVERHEAD_CHARS, MIN_TRUNCATED_CHARS))
        remaining -= len(partial_text) + MESSAGE_OVERHEAD_CHARS
        tail = [{"role": "assistant", "content": partial_text}, {"role": "user", "content": CONTINUE_NOTE}]
    user_text = render_user_message(
        new_content, attachments or [], max(remaining - MESSAGE_OVERHEAD_CHARS, MIN_TRUNCATED_CHARS)
    )
    remaining -= len(user_text) + MESSAGE_OVERHEAD_CHARS
    remaining -= len(OMITTED_NOTE) + MESSAGE_OVERHEAD_CHARS  # reserved in case the note is needed
    picked: list[dict] = []
    for message in reversed(_answered_turns(history)):
        cost = len(message["content"]) + MESSAGE_OVERHEAD_CHARS
        if cost <= remaining:
            picked.append(message)
            remaining -= cost
            continue
        room = remaining - MESSAGE_OVERHEAD_CHARS
        if message.get("_files") and room - len(message["_text"]) >= MIN_TRUNCATED_CHARS:
            picked.append({**message, "content": render_user_message(message["_text"], message["_files"], room)})
        elif not picked and room >= MIN_TRUNCATED_CHARS:
            picked.append({**message, "content": truncate_middle(message["content"], room)})
        break
    picked.reverse()
    if picked and picked[0]["role"] == "assistant":
        picked.insert(0, {"role": "user", "content": OMITTED_NOTE})
    history_messages = [{"role": m["role"], "content": m["content"]} for m in picked]
    return [
        {"role": "system", "content": system}, *history_messages, {"role": "user", "content": user_text}, *tail,
    ]


# --------------------------------------------------------------------------- reasoning


class ThinkSplitter:
    """Split streamed text into ``("reasoning", …)`` and ``("token", …)`` parts.

    ``<think>``/``</think>`` tags may arrive split across chunks; a possible
    partial tag at the end of a chunk is held back until the next one. A
    closing tag without an opening one is dropped.
    """

    _TAG = re.compile(r"<(/?)think>", re.IGNORECASE)
    _TAGS = ("<think>", "</think>")

    def __init__(self) -> None:
        self.inside = False
        self._pending = ""

    def _emit(self, out: list[tuple[str, str]], text: str) -> None:
        if not text:
            return
        kind = "reasoning" if self.inside else "token"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))

    @classmethod
    def _partial_tag_len(cls, text: str) -> int:
        for size in range(min(len(text), max(map(len, cls._TAGS)) - 1), 0, -1):
            tail = text[-size:].lower()
            if any(tag.startswith(tail) for tag in cls._TAGS):
                return size
        return 0

    def feed(self, text: str) -> list[tuple[str, str]]:
        buffer = self._pending + text
        self._pending = ""
        out: list[tuple[str, str]] = []
        position = 0
        for match in self._TAG.finditer(buffer):
            self._emit(out, buffer[position : match.start()])
            self.inside = not match.group(1)
            position = match.end()
        rest = buffer[position:]
        keep = self._partial_tag_len(rest)
        self._emit(out, rest[: len(rest) - keep])
        self._pending = rest[len(rest) - keep :]
        return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._emit(out, self._pending)
        self._pending = ""
        return out


# --------------------------------------------------------------------------- model endpoint


def _engine_module():
    """``backend.engine`` if it exists (it is optional and built separately)."""
    name = f"{__package__}.engine"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


def _configured_model() -> str | None:
    return os.environ.get("LOWRAM_LLM_MODEL", "").strip() or None


def _managed_engine():
    """The engine module when LowRAM AI manages llama-server itself (no ``LOWRAM_LLM_URL``)."""
    if os.environ.get("LOWRAM_LLM_URL", "").strip():
        return None
    return _engine_module()


def _no_model_error(engine, exc: BaseException) -> HTTPError:
    """409 with the engine's own reason (not installed, drive unplugged, paused, …)."""
    engine_error = getattr(engine, "EngineError", None)
    if engine_error is None or not isinstance(exc, engine_error):
        log.error("The engine failed unexpectedly", exc_info=exc)
    detail = str(exc).strip()
    message = f"{NO_MODEL_MESSAGE} Details: {detail}" if detail else NO_MODEL_MESSAGE
    return HTTPError(HTTPStatus.CONFLICT, message[:1000])


def engine_interrupted() -> bool:
    """Is the managed model paused (image/video job) or not ready right now?"""
    engine = _managed_engine()
    if engine is None:
        return False
    try:
        info = engine.status()
        return bool(info.get("paused")) or (info.get("server") or {}).get("state") != "ready"
    except Exception:
        log.debug("Could not read the engine status", exc_info=True)
        return False


def ensure_model(timeout: float = ENGINE_START_TIMEOUT_S) -> tuple[str, str | None]:
    """``(llm_url, model_name)`` to use, starting the managed engine when needed.

    With ``LOWRAM_LLM_URL`` set (external server) or without an engine module,
    the configured URL is used as is. Otherwise ``engine.ensure_running`` must
    succeed, else ``HTTPError(409)`` tells the user to install/start a model.
    """
    engine = _managed_engine()
    if engine is None:
        return config.llm_url(), _configured_model()
    try:
        url = engine.ensure_running(timeout=timeout)
    except Exception as exc:  # EngineError or anything else: the user needs a reason, not a 500
        raise _no_model_error(engine, exc) from exc
    model = _configured_model()
    try:
        server = engine.status().get("server") or {}
        if isinstance(server.get("model_id"), str):
            model = server["model_id"]
    except Exception:  # the model name is informational only
        log.debug("Could not read the engine status", exc_info=True)
    return (url if isinstance(url, str) and url else config.llm_url()), model


def prepare_model() -> dict:
    """Before streaming: ``{url, wait, model, ctx_size}`` for the answer.

    External server / no engine: ``url`` is set, ``ctx_size`` unknown. Managed
    engine ready: ``url`` and ``ctx_size`` set. Managed engine stopped: it is
    started here (fast; raises 409 for no model, no runtime, unplugged drive or
    a pause) and ``wait`` is a callable that waits for it to finish loading –
    called inside the stream so the user sees ``status`` events meanwhile.
    """
    engine = _managed_engine()
    if engine is None:
        return {"url": config.llm_url(), "wait": None, "model": _configured_model(), "ctx_size": None}
    start = getattr(engine, "start", None)
    try:
        server = dict(engine.status().get("server") or {})
        if server.get("state") not in ("ready", "starting") and start is not None:
            server = dict(start(persist=False) or {})
    except Exception as exc:
        raise _no_model_error(engine, exc) from exc
    ctx = server.get("ctx_size")
    if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0:
        try:
            ctx = int(engine.ctx_size())
        except Exception:
            ctx = None
    model = server["model_id"] if isinstance(server.get("model_id"), str) else _configured_model()
    if server.get("state") != "starting":  # ready (or an engine without start()): no visible wait
        url, model = ensure_model()
        return {"url": url, "wait": None, "model": model, "ctx_size": ctx}

    def wait() -> str:
        url = engine.ensure_running(timeout=ENGINE_START_TIMEOUT_S)
        return url if isinstance(url, str) and url else config.llm_url()

    return {"url": None, "wait": wait, "model": model, "ctx_size": ctx}


def context_limits(plan_chars: int, plan_tokens: int, mode: str, ctx_size: int | None) -> tuple[int, int]:
    """``(char_budget, max_tokens)``: the RAM plan, capped so prompt + answer fit ``ctx_size``."""
    budget = plan_chars * CONTEXT_FACTOR
    max_tokens = max(plan_tokens, MIN_MAX_TOKENS[mode])
    if ctx_size:
        max_tokens = min(max_tokens, max(ctx_size // 2, 64))
        room = int((ctx_size - max_tokens - CTX_RESERVE_TOKENS) * CHARS_PER_TOKEN)
        budget = min(budget, max(room, MIN_CONTEXT_CHARS))
    return budget, max_tokens


# --------------------------------------------------------------------------- streaming

_ACTIVE: dict[int, "_Answer"] = {}
_ACTIVE_LOCK = threading.Lock()


def request_stop(chat_id: int) -> bool:
    """Ask the running answer of a chat to stop. Returns whether one was running."""
    with _ACTIVE_LOCK:
        answer = _ACTIVE.get(chat_id)
    if answer is None:
        return False
    answer.stop_event.set()
    return True


def is_streaming(chat_id: int) -> bool:
    with _ACTIVE_LOCK:
        return chat_id in _ACTIVE


class _Answer:
    """State of one streamed answer: worker thread, partial text, slot and persistence.

    Holds no reference to its :class:`ChatStream`, so dropping the stream
    object triggers ``ChatStream.__del__`` (which releases the slot) at once.
    For ``continue`` the answer starts with the stored text and ``restore``
    holds the previous state, written back if nothing new arrives.
    """

    def __init__(self, *, chat: dict, user_message: dict | None, assistant_message: dict, messages: list[dict],
                 max_tokens: int, temperature: float, url: str | None, slot: ExitStack,
                 wait=None, action: str = "message", extra: dict | None = None, restore: dict | None = None) -> None:
        self.chat_id = int(chat["id"])
        self.chat = chat
        self.user_message = user_message
        self.assistant = assistant_message
        self.assistant_id = int(assistant_message["id"])
        self.messages = messages
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.url = url
        self.wait = wait  # waits for the managed model to finish loading (worker thread)
        self.action = action
        self.extra = extra or {}
        self.restore = restore
        self.loading = False
        self.stats: dict = {}
        self.stop_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.final: dict | None = None
        self._slot = slot
        self._released = False
        self._lock = threading.RLock()
        self._initial = assistant_message["content"] if restore is not None else ""
        self._parts: list[str] = [self._initial] if self._initial else []
        self._started = time.monotonic()
        self._last_save = self._started

    # ---- model side (worker thread)

    def start_worker(self) -> None:
        self.loading = self.wait is not None
        self.worker = threading.Thread(target=self._produce, name=f"lowram-chat-{self.chat_id}", daemon=True)
        self.worker.start()

    def _produce(self) -> None:
        try:
            if self.wait is not None:
                try:
                    self.url = self.wait()
                except Exception as exc:
                    raise llm.LLMError(f"The model could not be loaded: {exc}") from exc
                finally:
                    self.loading = False
            for piece in llm.stream_chat(
                self.messages, max_tokens=self.max_tokens, temperature=self.temperature,
                url=self.url, stop_event=self.stop_event, stats=self.stats,
            ):
                self.queue.put(("text", piece))
            self.queue.put(("end", None))
        except Exception as exc:  # handed to the HTTP thread, reported to the user there
            self.queue.put(("error", exc))

    def stop_worker(self) -> None:
        self.stop_event.set()
        worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(WORKER_JOIN_S)

    # ---- HTTP side

    def start_event(self) -> dict:
        return {
            "type": "start", "action": self.action, "chat": self.chat,
            "user_message": self.user_message, "assistant_message": self.assistant, **self.extra,
        }

    def take(self, kind: str, text: str) -> dict | None:
        """Turn a split piece into an SSE event (leading blank space of the answer is dropped)."""
        if kind == "reasoning":
            return {"type": "reasoning", "text": text}
        if not self._parts:
            text = text.lstrip()
            if not text:
                return None
        self._parts.append(text)
        return {"type": "token", "text": text}

    def new_text(self) -> str:
        return "".join(self._parts)[len(self._initial):]

    def has_content(self) -> bool:
        return bool(self.new_text().strip())

    def save_progress(self) -> None:
        now = time.monotonic()
        if now - self._last_save < SAVE_INTERVAL_S:
            return
        self._last_save = now
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE messages SET content = ? WHERE id = ? AND status = 'streaming'",
                    ("".join(self._parts), self.assistant_id),
                )
        except sqlite3.Error:  # the final save still records everything
            log.warning("Could not save the partial answer of chat #%s", self.chat_id, exc_info=True)

    def _final_values(self, status: str, error: str | None) -> dict:
        if self.restore is not None and not self.has_content():  # continue produced nothing: keep the old answer
            return {**self.restore, "error": None}
        finish = self.stats.get("finish_reason") if status == "complete" else None
        before = self.restore or {}  # continue: add to the stored answer's numbers
        tokens = int(before.get("tokens") or 0) + int(self.stats.get("tokens") or 0)
        duration = round(time.monotonic() - self._started + float(before.get("duration_s") or 0), 2)
        return {
            "content": "".join(self._parts).rstrip(),
            "status": status,
            "error": error[: db.MAX_ERROR_CHARS] if error else None,
            "duration_s": duration,
            "tokens": tokens or None,
            "tokens_per_s": self.stats.get("tokens_per_s"),
            "finish_reason": None if not finish else ("length" if finish == "length" else "stop"),
        }

    def finalize(self, status: str, error: str | None) -> dict:
        """Persist the final state once; later calls return the first result."""
        with self._lock:
            if self.final is not None:
                return self.final
            values = self._final_values(status, error)
            now = db.utcnow()
            for attempt in range(3):
                try:
                    with _connect() as conn, db.transaction(conn):
                        conn.execute(
                            "UPDATE messages SET content = ?, status = ?, error = ?, duration_s = ?, tokens = ?, "
                            "tokens_per_s = ?, finish_reason = ? WHERE id = ?",
                            (values["content"], values["status"], values["error"], values["duration_s"],
                             values["tokens"], values["tokens_per_s"], values["finish_reason"], self.assistant_id),
                        )
                        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, self.chat_id))
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            self.final = {**self.assistant, **values, "truncated": values["finish_reason"] == "length"}
            self._unregister()
            return self.final

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._unregister()
            self._slot.close()

    def abandon(self) -> None:
        """End without a regular finish (stop, disconnect, never iterated): keep the text, free the slot."""
        try:
            self.stop_event.set()
            self.finalize("stopped", None)
        except Exception:
            log.exception("Could not save the stopped answer of chat #%s", self.chat_id)
        finally:
            self.release()

    def _unregister(self) -> None:
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self.chat_id) is self:
                del _ACTIVE[self.chat_id]


def _pump(answer: _Answer) -> Iterator[dict]:
    """Forward the worker's text as events. Returns ``(status, error)``."""
    answer.start_worker()
    splitter = ThinkSplitter()
    last_event = last_status = time.monotonic()
    stop_seen: float | None = None
    if answer.wait is not None:  # the managed model is still starting / loading
        yield {"type": "status", "text": LOADING_TEXT}
    while True:
        try:
            kind, value = answer.queue.get(timeout=QUEUE_POLL_S)
        except queue.Empty:
            now = time.monotonic()
            if answer.stop_event.is_set():
                stop_seen = stop_seen or now
                if now - stop_seen >= STOP_GRACE_S:
                    log.info("Chat #%s: model connection did not close in time; stopping anyway", answer.chat_id)
                    break
            if answer.loading and now - last_status >= STATUS_INTERVAL_S:
                last_status = last_event = now
                yield {"type": "status", "text": LOADING_TEXT}
            elif now - last_event >= PING_INTERVAL_S:
                last_event = now
                yield {"type": "ping"}
            continue
        if kind == "error":
            if answer.stop_event.is_set():
                break
            raise value
        if kind == "end":
            break
        for part, text in splitter.feed(value):
            event = answer.take(part, text)
            if event is not None:
                last_event = time.monotonic()
                yield event
        answer.save_progress()
    for part, text in splitter.flush():
        event = answer.take(part, text)
        if event is not None:
            yield event
    if answer.stop_event.is_set():
        return "stopped", None
    if not answer.has_content():
        return "error", "The model returned an empty answer"
    return "complete", None


def _events(answer: _Answer) -> Iterator[dict]:
    try:
        yield answer.start_event()
        try:
            status, error = yield from _pump(answer)
        except llm.LLMError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # reported in-band; the traceback goes to the log
            log.exception("Chat #%s: the answer failed", answer.chat_id)
            status, error = "error", f"Unexpected error while answering: {type(exc).__name__}: {exc}"
        message = answer.finalize(status, error)
        if status == "error":
            yield {"type": "error", "error": error, "message": message}
        else:
            yield {"type": "done", "message": message}
    finally:  # also on GeneratorExit (client gone) and errors in finalize
        answer.stop_worker()
        answer.abandon()


class ChatStream:
    """The event iterator handed to :class:`StreamResponse`.

    ``close()`` – called by the server after streaming, on disconnect, or by
    ``__del__`` when the response is dropped without being iterated – always
    saves the answer as ``stopped`` if it has not finished and releases the
    inference slot.
    """

    def __init__(self, answer: _Answer) -> None:
        self.answer = answer
        self._events = _events(answer)

    def __iter__(self) -> "ChatStream":
        return self

    def __next__(self) -> dict:
        return next(self._events)

    def close(self) -> None:
        try:
            self._events.close()
        except Exception:
            log.exception("Closing the answer stream of chat #%s failed", self.answer.chat_id)
        finally:
            self.answer.abandon()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass


def _open_stream(chat_id: int, percent: object, prepare) -> ChatStream:
    """Shared frame of all answer streams: RAM plan, slot, model, context limits.

    ``prepare(chat, mode, budget, endpoint)`` runs while the slot is held and
    returns the keyword arguments for :class:`_Answer` (it stores the rows).
    """
    plan = memory.plan(default_percent() if percent is None else percent)
    chat = get_conversation(chat_id)
    mode = chat["mode"] if chat["mode"] in MODES else "chat"
    slot = ExitStack()
    slot.enter_context(db.inference_slot(f"chat #{chat_id}"))
    try:
        endpoint = prepare_model()
        budget, max_tokens = context_limits(plan.context_chars, plan.max_tokens, mode, endpoint["ctx_size"])
        fields = prepare(mode, budget, endpoint)
        answer = _Answer(
            max_tokens=max_tokens, temperature=TEMPERATURE[mode], url=endpoint["url"], slot=slot,
            wait=endpoint["wait"], **fields,
        )
        with _ACTIVE_LOCK:
            _ACTIVE[chat_id] = answer
        return ChatStream(answer)
    except BaseException:
        slot.close()
        raise


def start_answer(chat_id: int, content: object, percent: object = None, attachments: object = None) -> ChatStream:
    """Validate, take the inference slot, store the turn and return the event stream.

    Raises ``ValueError`` (400), ``db.NotFound`` (404), ``db.Busy`` or
    ``HTTPError(409)`` (no model) before anything is streamed.
    """
    files = validate_attachments(attachments)
    text = validate_content(content, allow_empty=bool(files))

    def prepare(mode: str, budget: int, endpoint: dict) -> dict:
        history = _recent_messages(chat_id, budget)
        messages = build_messages(mode, history, text, budget, attachments=files, instructions=current_instructions())
        user, assistant, chat = _insert_turn(chat_id, text, files, endpoint["model"])
        return {"chat": chat, "user_message": user, "assistant_message": assistant, "messages": messages}

    return _open_stream(chat_id, percent, prepare)


def regenerate_answer(chat_id: int, percent: object = None) -> ChatStream:
    """Replace the last assistant answer with a new one for the same user message."""

    def prepare(mode: str, budget: int, endpoint: dict) -> dict:
        with _connect() as conn:
            user, old = _last_turn(conn, chat_id)
            if user is None or old is None:
                raise HTTPError(HTTPStatus.CONFLICT, "There is no answer to regenerate in this chat")
            history = _recent_messages(chat_id, budget, before_id=user["id"])
            files = _recent_messages(chat_id, budget, before_id=old["id"], limit=1)[0]["attachments"]
            messages = build_messages(
                mode, history, user["content"], budget, attachments=files, instructions=current_instructions()
            )
            now = db.utcnow()
            with db.transaction(conn):
                cur = conn.execute(
                    "DELETE FROM messages WHERE id = ? AND id = (SELECT MAX(id) FROM messages WHERE conversation_id = ?)",
                    (old["id"], chat_id),
                )
                if cur.rowcount != 1:
                    raise HTTPError(HTTPStatus.CONFLICT, "The chat changed meanwhile; reload it and try again")
                new_id = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, status, model, created_at) "
                    "VALUES (?, 'assistant', '', 'streaming', ?, ?)",
                    (chat_id, endpoint["model"], now),
                ).lastrowid
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, chat_id))
            return {
                "chat": _get_conversation(conn, chat_id), "user_message": user,
                "assistant_message": _get_message(conn, new_id), "messages": messages,
                "action": "regenerate", "extra": {"replaced_message_id": old["id"]},
            }

    return _open_stream(chat_id, percent, prepare)


def continue_answer(chat_id: int, percent: object = None) -> ChatStream:
    """Append to the last assistant answer when it was cut off at the length limit."""

    def prepare(mode: str, budget: int, endpoint: dict) -> dict:
        with _connect() as conn:
            user, old = _last_turn(conn, chat_id)
            if user is None or old is None or not old["truncated"] or old["status"] != "complete":
                raise HTTPError(HTTPStatus.CONFLICT, "Only an answer that was cut off at the length limit can be continued")
            history = _recent_messages(chat_id, budget, before_id=user["id"])
            files = _recent_messages(chat_id, budget, before_id=old["id"], limit=1)[0]["attachments"]
            messages = build_messages(
                mode, history, user["content"], budget, attachments=files,
                instructions=current_instructions(), partial=old["content"],
            )
            with db.transaction(conn):
                cur = conn.execute(
                    "UPDATE messages SET status = 'streaming', finish_reason = NULL, error = NULL "
                    "WHERE id = ? AND status = 'complete' AND finish_reason = 'length'",
                    (old["id"],),
                )
                if cur.rowcount != 1:
                    raise HTTPError(HTTPStatus.CONFLICT, "The chat changed meanwhile; reload it and try again")
            restore = {key: old[key] for key in ("content", "status", "duration_s", "tokens", "tokens_per_s", "finish_reason")}
            return {
                "chat": _get_conversation(conn, chat_id), "user_message": user,
                "assistant_message": {**old, "status": "streaming", "finish_reason": None, "truncated": False},
                "messages": messages, "action": "continue", "restore": restore,
            }

    return _open_stream(chat_id, percent, prepare)


# --------------------------------------------------------------------------- routes


@route("GET", r"/api/chats")
def get_chats(req: Request) -> Response:
    return Response({"chats": list_conversations(req.arg("q"))})


@route("POST", r"/api/chats")
def post_chat(req: Request) -> Response:
    data = req.json()
    return Response({"chat": create_conversation(data.get("title"), data.get("mode"))}, HTTPStatus.CREATED)


@route("POST", r"/api/chats/instructions")
def post_instructions(req: Request) -> Response:
    data = req.json()
    text = clean_instructions(data.get("instructions"))
    settings.update("chat", {"instructions": text})
    return Response({"instructions": text})


@route("GET", r"/api/chats/(\d+)")
def get_chat(req: Request, chat_id: int) -> Response:
    messages = list_messages(chat_id)
    return Response({"chat": get_conversation(chat_id), "messages": messages})


@route("GET", r"/api/chats/(\d+)/export")
def get_export(req: Request, chat_id: int) -> FileResponse:
    text, download_name = export_markdown(chat_id)
    folder = config.home_dir() / "exports"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = artifacts.write_text_atomic(folder / f"chat_{chat_id}.md", text)
    return FileResponse(path, "text/markdown; charset=utf-8", download_name)


@route("POST", r"/api/chats/(\d+)/rename")
def post_rename(req: Request, chat_id: int) -> Response:
    data = req.json()
    return Response({"chat": rename_conversation(chat_id, data.get("title"))})


@route("POST", r"/api/chats/(\d+)/delete")
def post_delete(req: Request, chat_id: int) -> Response:
    req.json()
    delete_conversation(chat_id)
    return Response({"deleted": True})


@route("POST", r"/api/chats/(\d+)/messages")
def post_message(req: Request, chat_id: int) -> StreamResponse:
    data = req.json(max_bytes=MAX_MESSAGE_BODY_BYTES)
    return StreamResponse(start_answer(chat_id, data.get("content"), data.get("percent"), data.get("attachments")))


@route("POST", r"/api/chats/(\d+)/continue")
def post_continue(req: Request, chat_id: int) -> StreamResponse:
    data = req.json()
    return StreamResponse(continue_answer(chat_id, data.get("percent")))


@route("POST", r"/api/chats/(\d+)/regenerate")
def post_regenerate(req: Request, chat_id: int) -> StreamResponse:
    data = req.json()
    return StreamResponse(regenerate_answer(chat_id, data.get("percent")))


@route("POST", r"/api/chats/(\d+)/stop")
def post_stop(req: Request, chat_id: int) -> Response:
    req.json()
    get_conversation(chat_id)
    return Response({"stopped": request_stop(chat_id)})
