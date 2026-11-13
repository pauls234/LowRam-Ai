"""Chat with the local model: conversations, context building and streamed answers.

Design notes
------------
* Conversations and messages live in SQLite (``conversations``/``messages``,
  deleted together via ``ON DELETE CASCADE``).
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
* With the managed engine the prompt is sized to the real llama-server
  context (``engine.ctx_size()``), not only to the RAM budget.
* An assistant message is never left ``streaming``: it ends ``complete``,
  ``stopped`` (stop button / disconnect; partial text kept) or ``error`` (with
  the reason). Leftovers from a crash are marked ``stopped`` on startup.
* Model output is untrusted text. It is stored and returned verbatim as JSON
  strings; the UI renders it as text only.
"""

from __future__ import annotations

import importlib
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack
from http import HTTPStatus

from . import config, db, llm, memory, settings, web
from .web import HTTPError, Request, Response, StreamResponse, route

log = logging.getLogger("lowram.chat")

MODES = ("chat", "code")
MAX_CONTENT_CHARS = 20_000
MAX_TITLE_CHARS = 120
AUTO_TITLE_CHARS = 60
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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""
db.register_schema(SCHEMA)

_CONVERSATION_SQL = (
    "SELECT c.id, c.title, c.mode, c.created_at, c.updated_at, "
    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
    "FROM conversations c"
)
_MESSAGE_COLUMNS = "id, conversation_id, role, content, status, error, model, duration_s, created_at"


# --------------------------------------------------------------------------- validation


def _strip_controls(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


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


def auto_title(content: str) -> str:
    """Title from the first user message: whitespace collapsed, at most 60 characters."""
    title = " ".join(_strip_controls(" ".join(content.split())).split())
    if len(title) > AUTO_TITLE_CHARS:
        title = title[: AUTO_TITLE_CHARS - 1].rstrip() + "…"
    return title or "New chat"


def validate_mode(value: object) -> str:
    if value is None:
        return "chat"
    if value not in MODES:
        raise ValueError("mode must be 'chat' or 'code'")
    return str(value)


def validate_content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Write a message first (content must not be empty)")
    if len(value) > MAX_CONTENT_CHARS:
        raise ValueError(f"The message is too long: at most {MAX_CONTENT_CHARS} characters, got {len(value)}")
    if "\x00" in value:
        raise ValueError("The message contains a NUL character")
    return value


def default_percent() -> object:
    try:
        return settings.get("budget_percent")
    except Exception:  # a broken settings file must not block chatting
        log.exception("Could not read budget_percent from the settings; using the default")
        return memory.DEFAULT_PERCENT


# --------------------------------------------------------------------------- storage


def _conversation_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _message_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _get_conversation(conn: sqlite3.Connection, chat_id: int) -> dict:
    row = conn.execute(f"{_CONVERSATION_SQL} WHERE c.id = ?", (chat_id,)).fetchone()
    if row is None:
        raise db.NotFound(f"Chat #{chat_id} not found")
    return _conversation_dict(row)


def get_conversation(chat_id: int) -> dict:
    with db.connect() as conn:
        return _get_conversation(conn, chat_id)


def list_conversations() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(f"{_CONVERSATION_SQL} ORDER BY c.updated_at DESC, c.id DESC").fetchall()
        return [_conversation_dict(row) for row in rows]


def create_conversation(title: object = None, mode: object = None) -> dict:
    clean = clean_title(title)
    clean_mode = validate_mode(mode)
    now = db.utcnow()
    with db.connect() as conn:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO conversations (title, mode, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (clean, clean_mode, now, now),
            )
        return _get_conversation(conn, int(cur.lastrowid))


def rename_conversation(chat_id: int, title: object) -> dict:
    clean = clean_title(title, required=True)
    with db.connect() as conn:
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (clean, db.utcnow(), chat_id)
            )
            if cur.rowcount != 1:
                raise db.NotFound(f"Chat #{chat_id} not found")
        return _get_conversation(conn, chat_id)


def delete_conversation(chat_id: int) -> None:
    request_stop(chat_id)  # a running answer ends as "stopped"; its rows go away with the chat
    with db.connect() as conn, db.transaction(conn):
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (chat_id,))
        if cur.rowcount != 1:
            raise db.NotFound(f"Chat #{chat_id} not found")


def list_messages(chat_id: int) -> list[dict]:
    with db.connect() as conn:
        _get_conversation(conn, chat_id)
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        return [_message_dict(row) for row in rows]


def _recent_messages(chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    """The newest ``limit`` messages of a chat in chronological order."""
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [_message_dict(row) for row in reversed(rows)]


def _insert_turn(chat_id: int, content: str, model: str | None) -> tuple[dict, dict, dict]:
    """Store the user message and an empty ``streaming`` assistant message atomically."""
    now = db.utcnow()
    with db.connect() as conn:
        with db.transaction(conn):
            _get_conversation(conn, chat_id)
            user_id = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, status, created_at) "
                "VALUES (?, 'user', ?, 'complete', ?)",
                (chat_id, content, now),
            ).lastrowid
            assistant_id = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, status, model, created_at) "
                "VALUES (?, 'assistant', '', 'streaming', ?, ?)",
                (chat_id, model, now),
            ).lastrowid
            conn.execute(
                "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                "WHERE id = ?",
                (now, auto_title(content), chat_id),
            )
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id IN (?, ?) ORDER BY id", (user_id, assistant_id)
        ).fetchall()
        return _message_dict(rows[0]), _message_dict(rows[1]), _get_conversation(conn, chat_id)


@web.on_startup
def recover_interrupted(server: object = None) -> int:
    """Mark answers that were still ``streaming`` when LowRAM AI stopped as ``stopped``."""
    with _ACTIVE_LOCK:
        active = [answer.assistant_id for answer in _ACTIVE.values()]
    marks = ",".join("?" for _ in active)
    extra = f" AND id NOT IN ({marks})" if active else ""
    with db.connect() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE messages SET status = 'stopped', error = COALESCE(error, ?) WHERE status = 'streaming'" + extra,
            (INTERRUPTED_ERROR, *active),
        )
    if cur.rowcount:
        log.warning("Marked %s interrupted chat answer(s) as stopped", cur.rowcount)
    return cur.rowcount


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
            turns.append({"role": "user", "content": message["content"]})
            turns.append({"role": "assistant", "content": answer["content"]})
    return turns


def build_messages(mode: str, history: list[dict], new_content: str, char_budget: int) -> list[dict]:
    """System prompt + the newest turns that fit ``char_budget`` + the new user message.

    The oldest messages are dropped first and messages are never split, except
    that the newest earlier message and the new message itself are shortened
    in the middle (with a marker) when one alone is larger than the budget.
    """
    system = SYSTEM_PROMPTS[mode]
    remaining = char_budget - len(system) - MESSAGE_OVERHEAD_CHARS
    user_text = truncate_middle(new_content, max(remaining - MESSAGE_OVERHEAD_CHARS, MIN_TRUNCATED_CHARS))
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
        if not picked and room >= MIN_TRUNCATED_CHARS:
            picked.append({**message, "content": truncate_middle(message["content"], room)})
        break
    picked.reverse()
    if picked and picked[0]["role"] == "assistant":
        picked.insert(0, {"role": "user", "content": OMITTED_NOTE})
    return [{"role": "system", "content": system}, *picked, {"role": "user", "content": user_text}]


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
    """

    def __init__(self, *, chat: dict, user_message: dict, assistant_message: dict, messages: list[dict],
                 max_tokens: int, temperature: float, url: str | None, slot: ExitStack,
                 wait=None) -> None:
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
        self.loading = False
        self.stop_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.final: dict | None = None
        self._slot = slot
        self._released = False
        self._lock = threading.RLock()
        self._parts: list[str] = []
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
                url=self.url, stop_event=self.stop_event,
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

    def has_content(self) -> bool:
        return bool("".join(self._parts).strip())

    def save_progress(self) -> None:
        now = time.monotonic()
        if now - self._last_save < SAVE_INTERVAL_S:
            return
        self._last_save = now
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE messages SET content = ? WHERE id = ? AND status = 'streaming'",
                    ("".join(self._parts), self.assistant_id),
                )
        except sqlite3.Error:  # the final save still records everything
            log.warning("Could not save the partial answer of chat #%s", self.chat_id, exc_info=True)

    def finalize(self, status: str, error: str | None) -> dict:
        """Persist the final state once; later calls return the first result."""
        with self._lock:
            if self.final is not None:
                return self.final
            content = "".join(self._parts).rstrip()
            error = error[: db.MAX_ERROR_CHARS] if error else None
            duration = round(time.monotonic() - self._started, 2)
            now = db.utcnow()
            for attempt in range(3):
                try:
                    with db.connect() as conn, db.transaction(conn):
                        conn.execute(
                            "UPDATE messages SET content = ?, status = ?, error = ?, duration_s = ? WHERE id = ?",
                            (content, status, error, duration, self.assistant_id),
                        )
                        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, self.chat_id))
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            self.final = {**self.assistant, "content": content, "status": status, "error": error, "duration_s": duration}
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
        yield {
            "type": "start",
            "chat": answer.chat,
            "user_message": answer.user_message,
            "assistant_message": answer.assistant,
        }
        try:
            status, error = yield from _pump(answer)
        except llm.LLMError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # reported in-band; the traceback goes to the log
            log.exception("Chat #%s: the answer failed", answer.chat_id)
            status, error = "error", f"Unexpected error while answering: {type(exc).__name__}: {exc}"
        message = answer.finalize(status, error)
        if status == "error":
            yield {"type": "error", "error": message["error"], "message": message}
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


def start_answer(chat_id: int, content: object, percent: object = None) -> ChatStream:
    """Validate, take the inference slot, store the turn and return the event stream.

    Raises ``ValueError`` (400), ``db.NotFound`` (404), ``db.Busy`` or
    ``HTTPError(409)`` (no model) before anything is streamed.
    """
    text = validate_content(content)
    plan = memory.plan(default_percent() if percent is None else percent)
    chat = get_conversation(chat_id)
    mode = chat["mode"] if chat["mode"] in MODES else "chat"
    slot = ExitStack()
    slot.enter_context(db.inference_slot(f"chat #{chat_id}"))
    try:
        endpoint = prepare_model()
        budget, max_tokens = context_limits(plan.context_chars, plan.max_tokens, mode, endpoint["ctx_size"])
        messages = build_messages(mode, _recent_messages(chat_id), text, budget)
        user_message, assistant_message, chat = _insert_turn(chat_id, text, endpoint["model"])
        answer = _Answer(
            chat=chat, user_message=user_message, assistant_message=assistant_message, messages=messages,
            max_tokens=max_tokens, temperature=TEMPERATURE[mode],
            url=endpoint["url"], slot=slot, wait=endpoint["wait"],
        )
        with _ACTIVE_LOCK:
            _ACTIVE[chat_id] = answer
        return ChatStream(answer)
    except BaseException:
        slot.close()
        raise


# --------------------------------------------------------------------------- routes


@route("GET", r"/api/chats")
def get_chats(req: Request) -> Response:
    return Response({"chats": list_conversations()})


@route("POST", r"/api/chats")
def post_chat(req: Request) -> Response:
    data = req.json()
    return Response({"chat": create_conversation(data.get("title"), data.get("mode"))}, HTTPStatus.CREATED)


@route("GET", r"/api/chats/(\d+)")
def get_chat(req: Request, chat_id: int) -> Response:
    messages = list_messages(chat_id)
    return Response({"chat": get_conversation(chat_id), "messages": messages})


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
    data = req.json()
    return StreamResponse(start_answer(chat_id, data.get("content"), data.get("percent")))


@route("POST", r"/api/chats/(\d+)/stop")
def post_stop(req: Request, chat_id: int) -> Response:
    req.json()
    get_conversation(chat_id)
    return Response({"stopped": request_stop(chat_id)})
