"""Speech to text on this computer (whisper.cpp): dictate in the chat, transcribe recordings.

* **Program** – ``whisper-cli`` from whisper.cpp. The app ships it (the release workflow builds it
  from a fixed whisper.cpp commit) and copies it to ``<home>/bin/whisper`` before the first use, so
  a quarantine flag on the downloaded app can never block it. Running from source, ``whisper-cli``
  from the PATH or from Homebrew (``brew install whisper-cpp``) is used; ``LOWRAM_WHISPER_CLI``
  overrides both.
* **Models** – Whisper models in ggml format from huggingface.co/ggerganov/whisper.cpp, downloaded
  only when the user asks, checked against the SHA-256 Hugging Face lists, kept in
  ``<models folder>/whisper``.
* **Audio** – recordings from the page arrive as 16-kHz mono WAV; WAV, MP3, FLAC and Ogg files are
  read by whisper.cpp itself, M4A/AAC/AIFF/CAF (e.g. from the Voice Memos app) are converted with
  macOS' own ``afconvert`` first. The upload is streamed into a private temporary folder and
  **deleted right after the transcription** (and at the next start after a crash): recordings are
  never kept, and the text only lives in memory until the page picked it up (at most an hour).
* **Care for the computer** – one transcription at a time, with lower priority and a thread cap,
  and only with enough free RAM. A recording longer than :data:`LONG_AUDIO_S` waits for the global
  inference slot, battery and heat like other long jobs and keeps the Mac awake. The percentage
  comes from whisper.cpp itself, the time left from it and from earlier runs on this computer.
"""

from __future__ import annotations

import array
import collections
import hashlib
import logging
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import psutil

from . import config, db, memory, power, progress, settings
from .web import HTTPError, Request, Response, on_shutdown, on_startup, route

log = logging.getLogger("lowram.speech")

REPO = "ggerganov/whisper.cpp"
MODELS: tuple[dict, ...] = (
    {"id": "base", "name": "Whisper Base", "filename": "ggml-base.bin", "size_bytes": 147_951_465,
     "quality": "Schnell – gut für kurze Diktate", "min_ram_gb": 4},
    {"id": "small", "name": "Whisper Small", "filename": "ggml-small.bin", "size_bytes": 487_601_967,
     "quality": "Gute Erkennung", "min_ram_gb": 8},
    {"id": "turbo", "name": "Whisper Large v3 Turbo", "filename": "ggml-large-v3-turbo-q5_0.bin",
     "size_bytes": 574_041_195, "quality": "Sehr gute Erkennung, auch für lange Aufnahmen", "min_ram_gb": 12},
)
LANGUAGES = {
    "de": "Deutsch", "en": "Englisch", "auto": "Automatisch erkennen", "fr": "Französisch", "es": "Spanisch",
    "it": "Italienisch", "nl": "Niederländisch", "pl": "Polnisch", "pt": "Portugiesisch", "tr": "Türkisch",
    "uk": "Ukrainisch", "ru": "Russisch",
}
EXE = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
HOMEBREW_DIRS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
AFCONVERT = Path("/usr/bin/afconvert")
NATIVE_FORMATS = ("wav", "mp3", "flac", "ogg")  # whisper.cpp reads these itself
CONVERTED_FORMATS = ("m4a", "aac", "aiff", "caf")  # macOS converts these first (afconvert)
FORMAT_LABELS = {"wav": "WAV", "mp3": "MP3", "flac": "FLAC", "ogg": "Ogg", "m4a": "M4A", "aac": "AAC",
                 "aiff": "AIFF", "caf": "CAF"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
DISK_RESERVE_BYTES = 500 * 1024 * 1024
MIN_AUDIO_S = 0.5
MAX_AUDIO_S = 4 * 3600
MAX_RECORD_S = 30 * 60  # the page stops a recording after this (it is held in the page's memory)
LONG_AUDIO_S = 120.0  # longer: waits for the inference slot, battery and heat like a long job
SILENCE_RMS = 0.002  # loudest tenth of a second below this (about -54 dBFS): nothing was recorded
RAM_MARGIN_BYTES = 500 * 1024 * 1024
RAM_WAIT_SHORT_S = 60.0
RAM_WAIT_LONG_S = 600.0
WAIT_STEP_S = 5.0
CONVERT_TIMEOUT_S = 600.0
KEEP_JOBS = 20
MAX_QUEUE = 3  # recordings waiting at once (each is a file on disk until it is done)
RESULT_TTL_S = 3600.0
PARAGRAPH_CHARS = 700
MISSING_CLI = ("Die Spracherkennung (whisper.cpp) fehlt. In der fertigen App ist sie enthalten – beim Start aus "
               "dem Quellcode installiere sie mit Homebrew: brew install whisper-cpp")
_PROGRESS = re.compile(r"progress\s*=\s*(\d{1,3})\s*%")
_SECRET_ENV = re.compile(r"(?i)(key|token|secret|passw)")
# whisper writes these for music, noise and silence, and sometimes TV subtitle credits it saw in training
_NOISE = re.compile(
    r"[\[(*]\s*(?:BLANK_AUDIO|Musik|Music|Applaus|Applause|Lachen|Laughter|Gelächter|Stille|Silence|Geräusche?|"
    r"Noise|Husten|Räuspern)\s*[\])*]"
    r"|Untertitel(?:ung)?\s+(?:der|des|von|im Auftrag des)\s[^.!?\n]{0,60}?(?:Amara\.org|ZDF|funk|WDR|SWR|NDR)"
    r"[^.!?\n]{0,40}[.!?]?",
    re.IGNORECASE,
)


class SpeechError(RuntimeError):
    """The recording cannot be transcribed; the message is for the user."""


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- models and settings

def _models_folder() -> Path:
    return config.models_dir() / "whisper"


def model_path(model: dict) -> Path:
    return _models_folder() / model["filename"]


def get_model(model_id: object) -> dict:
    for model in MODELS:
        if model["id"] == model_id:
            return model
    raise db.NotFound("Dieses Sprachmodell gibt es nicht.")


def installed(model: dict) -> bool:
    try:
        path = model_path(model)
        return path.is_file() and path.stat().st_size > 1_000_000
    except OSError:
        return False


def recommended_model(total_bytes: int | None = None) -> str:
    total_gb = (total_bytes if total_bytes is not None else psutil.virtual_memory().total) / 1024**3
    return "turbo" if total_gb >= 12 else "small" if total_gb >= 8 else "base"


def _setting(key: str) -> Any:
    try:
        return settings.get("speech", key)
    except (KeyError, TypeError, ValueError):
        return None


def selected_model() -> dict | None:
    """The chosen model if it is installed, else the best installed one, else None."""
    present = [model for model in MODELS if installed(model)]
    for model in present:
        if model["id"] == _setting("model"):
            return model
    return present[-1] if present else None


def language() -> str:
    value = _setting("language")
    return value if value in LANGUAGES else "de"


def ram_needed(model: dict) -> int:
    """Free RAM whisper.cpp needs for ``model``: the weights plus working memory."""
    try:
        size = model_path(model).stat().st_size
    except OSError:
        size = model["size_bytes"]
    return int(size * 1.3) + RAM_MARGIN_BYTES


# --------------------------------------------------------------------------- the program

_cli_lock = threading.Lock()
_cli_copied: tuple | None = None  # (size, mtime) of the app's program that was copied last


def _bundled_cli() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    path = Path(base) / "whisper" / EXE
    return path if path.is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _install_bundled(source: Path) -> Path:
    """Copy the app's whisper-cli next to LowRAM AI's data (a plain copy carries no quarantine flag)."""
    global _cli_copied
    target = config.home_dir() / "bin" / "whisper" / EXE
    with _cli_lock:
        stat = source.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        if _cli_copied == stamp and target.is_file():
            return target
        if not (target.is_file() and _sha256(target) == _sha256(source)):
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            partial.chmod(0o755)
            os.replace(partial, target)
            log.info("whisper-cli installed from the app into %s", target)
        _cli_copied = stamp
    return target


def find_cli() -> dict:
    """``{"path", "source"}``: the override, the app's own copy, PATH/Homebrew – or ``None`` for both."""
    override = os.environ.get("LOWRAM_WHISPER_CLI", "").strip()
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return {"path": override, "source": "env"}
    bundled = _bundled_cli()
    if bundled is not None:
        try:
            return {"path": str(_install_bundled(bundled)), "source": "app"}
        except OSError as exc:
            log.warning("Could not install the bundled whisper-cli: %s", exc)
    found = shutil.which(EXE)
    if not found and sys.platform == "darwin":  # apps started from the Finder have a short PATH
        found = next((str(folder / EXE) for folder in HOMEBREW_DIRS if (folder / EXE).is_file()), None)
    return {"path": found, "source": "path" if found else None}


def _cli_command() -> list[str] | None:
    path = find_cli()["path"]
    return [path] if path else None


def can_convert() -> bool:
    return sys.platform == "darwin" and AFCONVERT.is_file()


def _child_env() -> dict[str, str]:
    """No secrets for the child; frozen Linux builds must not hand PyInstaller's library path on."""
    env = {key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)}
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _threads() -> int:
    try:
        from .engine import inference_threads

        return inference_threads()
    except Exception:  # noqa: BLE001 - the engine module is optional for this
        return max(2, (psutil.cpu_count(logical=False) or 4) - 2)


def _lower_priority(pid: int) -> None:
    try:
        from .engine import lower_priority

        lower_priority(pid, 10)  # never raises
    except ImportError:
        pass


def _popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session: the group id is the pid
    except (ProcessLookupError, PermissionError, OSError):
        pass


# --------------------------------------------------------------------------- audio

def sniff(head: bytes) -> str | None:
    """The audio format from the first bytes of a file (never from its name)."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[:4] == b"caff":
        return "caf"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:  # ADTS: sync word, layer 00
        return "aac"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:  # MPEG audio frame sync
        return "mp3"
    return None


def wav_info(path: Path) -> tuple[float, int, int, int] | None:
    """``(seconds, rate, channels, sample width)`` of a PCM WAV file; None if Python cannot read it."""
    try:
        size = path.stat().st_size
        with wave.open(str(path), "rb") as audio:
            rate, channels, width, frames = (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(),
                                             audio.getnframes())
    except (wave.Error, EOFError, OSError, ValueError):
        return None
    if rate <= 0 or channels <= 0 or width <= 0:
        return None
    frames = min(frames, max(0, size - 44) // (channels * width))  # a header may claim more than the file has
    return frames / rate, rate, channels, width


def loudest(path: Path, windows: int = 300) -> float | None:
    """RMS (0…1) of the loudest tenth of a second, from up to ``windows`` spread over a 16-bit WAV file."""
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getsampwidth() != 2:
                return None
            size = max(1, audio.getframerate() // 10)
            count = max(1, audio.getnframes() // size)
            best = 0.0
            for index in range(0, count, max(1, count // windows)):
                audio.setpos(index * size)
                raw = audio.readframes(size)
                samples = array.array("h")
                samples.frombytes(raw[: len(raw) - len(raw) % 2])
                if sys.byteorder == "big":
                    samples.byteswap()
                if samples:
                    best = max(best, math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768)
            return best
    except (wave.Error, EOFError, OSError, ValueError):
        return None


def check_length(seconds: float) -> None:
    if seconds < MIN_AUDIO_S:
        raise SpeechError("Die Aufnahme ist zu kurz – sprich mindestens einen kurzen Satz.")
    if seconds > MAX_AUDIO_S:
        raise SpeechError(f"Die Aufnahme ist länger als {MAX_AUDIO_S // 3600} Stunden – teile sie bitte auf.")


def check_heard(path: Path) -> None:
    level = loudest(path)
    if level is not None and level < SILENCE_RMS:
        raise SpeechError("Auf der Aufnahme ist nichts zu hören. Ist das richtige Mikrofon gewählt und nicht "
                          "stummgeschaltet?")


def _convert(source: Path, target: Path) -> None:
    """M4A/AAC/AIFF/CAF (or an unusual WAV) → 16-kHz mono WAV with macOS' own converter."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed program, argument list
            [str(AFCONVERT), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(source), str(target)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=CONVERT_TIMEOUT_S, check=False, env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SpeechError("Die Aufnahme ließ sich nicht rechtzeitig umwandeln.") from exc
    except OSError as exc:
        raise SpeechError(f"Die Aufnahme ließ sich nicht umwandeln ({exc}).") from exc
    if proc.returncode != 0 or not target.is_file():
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-200:]
        raise SpeechError("Diese Audiodatei ließ sich nicht lesen" + (f" ({detail})." if detail else "."))


def tidy(raw: str) -> str:
    """whisper's lines → readable text: noise markers and subtitle credits removed, paragraphs."""
    paragraphs: list[str] = []
    current = ""
    for line in raw.splitlines():
        line = " ".join(_NOISE.sub(" ", line).split())
        if not line:
            continue
        current = f"{current} {line}".strip()
        if len(current) >= PARAGRAPH_CHARS and current[-1] in ".!?…":
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def _temp_dir() -> Path:
    folder = config.home_dir() / "speech" / "tmp"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    return folder


def clean_temp() -> None:
    """Recordings never outlive a crash: whatever an earlier run left behind goes."""
    try:
        leftovers = list(_temp_dir().iterdir())
    except OSError:
        return
    for path in leftovers:
        try:
            path.unlink()
        except OSError:
            pass


def _remove(*paths: Path | None) -> None:
    for path in paths:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete the recording %s", path)


# --------------------------------------------------------------------------- jobs

class _Job:
    def __init__(self, job_id: str, audio: Path, kind: str, audio_s: float | None, model: dict, lang: str,
                 name: str) -> None:
        self.id = job_id
        self.audio = audio
        self.kind = kind
        self.converted: Path | None = None
        self.audio_s = audio_s
        self.model = model
        self.language = lang
        self.name = name
        self.status = "queued"
        self.percent = 0
        self.message: str | None = None
        self.text: str | None = None
        self.error: str | None = None
        self.started: float | None = None
        self.finished: float | None = None
        self.expected_s: float | None = None
        self.first_run = False  # nothing measured on this computer yet (Metal prepares itself once)
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self.status in ("queued", "preparing", "waiting", "running")

    def view(self) -> dict:
        eta = None
        if self.status == "running" and self.started is not None:
            elapsed = time.monotonic() - self.started
            if self.percent >= 5:
                eta = max(1.0, elapsed * (100 - self.percent) / self.percent)
            elif self.expected_s:
                eta = progress.estimate(elapsed, self.expected_s)["eta_s"]
        done = self.status == "done"
        return {"id": self.id, "name": self.name, "status": self.status, "percent": 100 if done else self.percent,
                "eta_s": None if eta is None else round(eta, 1), "message": self.message,
                "text": self.text if done else None, "error": self.error,
                "audio_s": None if self.audio_s is None else round(self.audio_s, 1),
                "model": self.model["id"], "language": self.language, "first_run": self.first_run}


class SpeechWorker:
    """One transcription at a time; queue and results live in memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: collections.OrderedDict[str, _Job] = collections.OrderedDict()
        self._queue: collections.deque[_Job] = collections.deque()
        self._running = False
        self._stopped = False

    def submit(self, job: _Job) -> dict:
        with self._lock:
            if self._stopped:
                _remove(job.audio)
                raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "LowRAM AI wird gerade beendet.")
            self._prune_locked()
            if len(self._queue) >= MAX_QUEUE:
                _remove(job.audio)
                raise HTTPError(HTTPStatus.TOO_MANY_REQUESTS, "Es warten schon mehrere Aufnahmen – bitte warte, bis "
                                                              "sie verschriftlicht sind.")
            if self._running or self._queue:
                job.message = "Wartet, bis die vorherige Aufnahme verschriftlicht ist."
            self._jobs[job.id] = job
            self._queue.append(job)
            if not self._running:
                self._running = True
                threading.Thread(target=self._loop, name="lowram-speech", daemon=True).start()
            return job.view()

    def _prune_locked(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if job.finished and now - job.finished > RESULT_TTL_S:
                del self._jobs[job_id]
        finished = [job_id for job_id, job in self._jobs.items() if not job.active]
        for job_id in finished[: max(0, len(self._jobs) - KEEP_JOBS + 1)]:
            del self._jobs[job_id]

    def _find(self, job_id: str) -> _Job:
        with self._lock:
            self._prune_locked()  # texts of old recordings leave the memory after an hour
            job = self._jobs.get(job_id)
        if job is None or (job.finished and time.time() - job.finished > RESULT_TTL_S):
            raise db.NotFound("Diese Verschriftlichung gibt es nicht (mehr).")
        return job

    def get(self, job_id: str) -> dict:
        return self._find(job_id).view()

    def cancel(self, job_id: str) -> dict:
        job = self._find(job_id)
        job.cancel.set()
        with self._lock:
            if job in self._queue:
                self._queue.remove(job)
                job.status, job.message, job.finished = "cancelled", None, time.time()
                _remove(job.audio)
        proc = job.proc
        if proc is not None:
            _kill(proc)
        return job.view()

    def busy_with(self, model_id: str) -> bool:
        with self._lock:
            return any(job.active and job.model["id"] == model_id for job in self._jobs.values())

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            jobs = list(self._jobs.values())
            queued = list(self._queue)
            self._queue.clear()
        for job in jobs:
            job.cancel.set()
            if job.proc is not None:
                _kill(job.proc)
        for job in queued:
            _remove(job.audio)

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self._stopped:
                    self._running = False
                    return
                job = self._queue.popleft()
            try:
                self._run(job)
            finally:
                _remove(job.audio, job.converted)
                job.proc = None
                job.finished = time.time()

    def _wait(self, job: _Job, message: str, seconds: float = WAIT_STEP_S) -> None:
        job.status, job.message = "waiting", message
        if job.cancel.wait(seconds):
            raise _Cancelled

    def _run(self, job: _Job) -> None:
        try:
            if job.cancel.is_set():
                raise _Cancelled
            self._prepare(job)
            long_audio = job.audio_s is None or job.audio_s > LONG_AUDIO_S
            self._wait_for_ram(job, RAM_WAIT_LONG_S if long_audio else RAM_WAIT_SHORT_S)
            if not long_audio:  # a short dictation runs at once: light, and the user is waiting for it
                job.text = transcribe(job)
            else:
                while (reason := power.battery_block_reason() or power.heat_block_reason()):
                    self._wait(job, reason, 20.0)
                while True:
                    try:
                        with db.inference_slot(f"Verschriftlichung „{job.name}“"), \
                                power.keep_awake("Verschriftlichung einer Aufnahme"):
                            job.text = transcribe(job)
                        break
                    except db.Busy as exc:
                        self._wait(job, f"Wartet, bis die andere Arbeit fertig ist ({exc}).")
            job.status, job.message, job.percent = "done", None, 100
        except _Cancelled:
            job.status, job.message = "cancelled", None
        except SpeechError as exc:
            job.status, job.error, job.message = "failed", str(exc), None
        except Exception as exc:  # reported to the user, never swallowed
            log.exception("Transcription %s failed", job.id)
            job.status, job.error, job.message = "failed", f"Die Verschriftlichung ist fehlgeschlagen: {exc}", None

    def _prepare(self, job: _Job) -> None:
        """Formats whisper.cpp cannot read become 16-kHz WAV first; length and silence are checked."""
        unusual_wav = job.kind == "wav" and job.audio_s is None
        if job.kind not in CONVERTED_FORMATS and not (unusual_wav and can_convert()):
            return
        if not can_convert():
            raise SpeechError(f"{FORMAT_LABELS.get(job.kind, job.kind)}-Dateien kann LowRAM AI nur auf dem Mac "
                              "umwandeln – speichere die Aufnahme bitte als MP3 oder WAV.")
        job.status, job.message = "preparing", "Bereitet die Aufnahme vor …"
        job.converted = job.audio.with_name(f"{job.id}-16k.wav")
        _convert(job.audio, job.converted)
        if job.cancel.is_set():
            raise _Cancelled
        info = wav_info(job.converted)
        if info is None:
            raise SpeechError("Diese Audiodatei ließ sich nicht lesen.")
        check_length(info[0])
        check_heard(job.converted)
        job.audio_s = info[0]

    def _wait_for_ram(self, job: _Job, patience_s: float) -> None:
        need = ram_needed(job.model)
        deadline = time.monotonic() + patience_s
        while True:
            free = int(psutil.virtual_memory().available)
            if free >= need and memory.plan().can_run:
                job.message = None
                return
            if time.monotonic() > deadline:
                raise SpeechError("Es ist nicht genug Arbeitsspeicher frei für die Spracherkennung – schließe andere "
                                  "Programme oder wähle ein kleineres Sprachmodell.")
            self._wait(job, f"Wartet auf freien Arbeitsspeicher ({free // 2**20} MB frei, {need // 2**20} MB nötig).")


def _timeout(job: _Job) -> float:
    return 120.0 + 10.0 * job.audio_s if job.audio_s else 6 * 3600.0


def transcribe(job: _Job) -> str:
    """Run whisper-cli for one job: the percentage from its output, the text from the file it writes."""
    command = _cli_command()
    if not command:
        raise SpeechError(MISSING_CLI)
    audio = job.converted or job.audio
    out_base = audio.with_name(f"{job.id}-text")
    text_file = out_base.with_name(out_base.name + ".txt")
    per_minute = progress.typical("speech_min", job.model["id"])  # seconds of work per minute of audio, measured here
    job.expected_s = per_minute * job.audio_s / 60 if per_minute and job.audio_s else None
    job.first_run = per_minute is None
    argv = [*command, "-m", str(model_path(job.model)), "-f", str(audio), "-l", job.language,
            "-t", str(_threads()), "-otxt", "-of", str(out_base), "-pp", "-np"]
    job.status, job.message, job.percent, job.started = "running", None, 0, time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=str(audio.parent), env=_child_env(), **_popen_kwargs(),
        )
    except OSError as exc:
        raise SpeechError(f"Die Spracherkennung ließ sich nicht starten ({exc}).") from exc
    job.proc = proc
    _lower_priority(proc.pid)
    if job.cancel.is_set():
        _kill(proc)
    timer = threading.Timer(_timeout(job), _kill, args=(proc,))
    timer.daemon = True
    timer.start()
    tail: collections.deque[str] = collections.deque(maxlen=8)
    try:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            found = _PROGRESS.search(line)
            if found:
                job.percent = min(99, int(found.group(1)))
            elif line:
                tail.append(line[:200])
        code = proc.wait()
    finally:
        timer.cancel()
        job.proc = None
        if proc.stderr is not None:
            proc.stderr.close()
    try:
        if job.cancel.is_set():
            raise _Cancelled
        if code != 0 or not text_file.is_file():
            if job.kind == "ogg" and job.converted is None:
                raise SpeechError("Diese Ogg-Datei ließ sich nicht lesen – Sprachnachrichten im Opus-Format kann "
                                  "LowRAM AI noch nicht verschriftlichen. Speichere sie bitte als MP3 oder WAV.")
            detail = " · ".join(tail)[-300:] or f"Code {code}"
            raise SpeechError(f"Die Spracherkennung hat nicht geklappt ({detail}).")
        text = tidy(text_file.read_text(encoding="utf-8", errors="replace"))
    finally:
        _remove(text_file)
    if job.audio_s:
        progress.remember("speech_min", job.model["id"], (time.monotonic() - job.started) * 60 / job.audio_s)
    if not text:
        raise SpeechError("Es wurde keine Sprache erkannt. Sprich etwas näher am Mikrofon oder wähle die richtige "
                          "Sprache.")
    return text


WORKER = SpeechWorker()


@on_startup
def _clean_leftovers(server: Any) -> None:
    clean_temp()


@on_shutdown
def _stop_worker(server: Any) -> None:
    WORKER.stop()


# --------------------------------------------------------------------------- status and models

def _download(model: dict) -> dict | None:
    try:
        from . import downloads

        return downloads.get(f"speech-{model['id']}")
    except Exception:  # noqa: BLE001 - status must never fail because of a download
        return None


def status() -> dict:
    try:
        total = int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001
        total = None
    cli = find_cli()
    ready = bool(_cli_command())
    chosen = selected_model()
    models = []
    for model in MODELS:
        models.append({key: model[key] for key in ("id", "name", "size_bytes", "quality", "min_ram_gb")} | {
            "installed": installed(model), "download": _download(model),
            "fits": total is None or total >= model["min_ram_gb"] * 1024**3,
            "recommended": model["id"] == recommended_model(total)})
    formats = [*NATIVE_FORMATS, *(CONVERTED_FORMATS if can_convert() else ())]
    return {"available": ready and chosen is not None, "cli": cli["source"] if ready else None,
            "model": chosen["id"] if chosen else None, "language": language(),
            "languages": [{"id": key, "label": label} for key, label in LANGUAGES.items()],
            "models": models, "formats": formats, "max_record_s": MAX_RECORD_S, "max_upload_bytes": MAX_UPLOAD_BYTES}


def install_model(model_id: str) -> dict:
    from . import downloads, media

    model = get_model(model_id)
    folder = config.models_dir()
    if not folder.is_dir() and folder != config.default_models_dir():  # never fill the boot disk instead
        raise HTTPError(HTTPStatus.CONFLICT, "Der Modell-Ordner ist nicht erreichbar – ist das externe Laufwerk "
                                             "angeschlossen?")
    current = _download(model)
    if current and current.get("state") in downloads.ACTIVE_STATES:
        return current
    try:
        meta = media.hf_file_metadata(REPO, model["filename"])
    except Exception as exc:  # noqa: BLE001 - network problems are the user's information
        raise HTTPError(HTTPStatus.BAD_GATEWAY, f"Hugging Face ist gerade nicht erreichbar ({exc}).") from exc
    _models_folder().mkdir(parents=True, exist_ok=True)
    return downloads.start(f"speech-{model['id']}", meta["url"], model_path(model), sha256=meta["sha256"],
                           size=meta["size"], label=f"Sprachmodell {model['name']}", kind="speech-model")


def delete_model(model_id: str) -> None:
    from . import downloads

    model = get_model(model_id)
    if WORKER.busy_with(model["id"]):
        raise HTTPError(HTTPStatus.CONFLICT, "Dieses Sprachmodell arbeitet gerade – warte, bis die Verschriftlichung "
                                             "fertig ist.")
    current = _download(model)
    if current and current.get("state") in downloads.ACTIVE_STATES:
        downloads.cancel(f"speech-{model['id']}")
    try:
        model_path(model).unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPError(HTTPStatus.CONFLICT, f"Das Sprachmodell ließ sich nicht löschen ({exc}).") from exc


# --------------------------------------------------------------------------- routes

_JOB_ID = re.compile(r"[0-9a-f]{12}")


def _job_id(raw: str) -> str:
    if not _JOB_ID.fullmatch(str(raw)):
        raise db.NotFound("Diese Verschriftlichung gibt es nicht.")
    return str(raw)


def _upload_name(req: Request) -> str:
    name = unquote(req.headers.get("X-Filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:120] or "Aufnahme"


def _receive(req: Request, path: Path) -> None:
    """Stream the request body into ``path`` – a long recording is never held in memory."""
    if req.headers.get("Transfer-Encoding"):
        raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "Chunked request bodies are not supported")
    try:
        length = int(req.headers.get("Content-Length") or "0")
    except ValueError as exc:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
    if length <= 0:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "Die Aufnahme ist leer.")
    if length > MAX_UPLOAD_BYTES:
        raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"Die Audiodatei ist größer als {MAX_UPLOAD_BYTES // 2**20} MB – speichere sie als MP3.")
    if shutil.disk_usage(path.parent).free < length + DISK_RESERVE_BYTES:
        raise HTTPError(HTTPStatus.INSUFFICIENT_STORAGE, "Auf dem Laufwerk ist zu wenig Platz frei für die Aufnahme.")
    left = length
    with path.open("wb") as handle:
        while left:
            chunk = req.handler.rfile.read(min(1024 * 1024, left))
            if not chunk:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "Die Übertragung der Aufnahme brach ab.")
            handle.write(chunk)
            left -= len(chunk)


def accept_audio(path: Path) -> tuple[str, float | None]:
    """``(format, seconds or None)`` of an uploaded file; SpeechError if it cannot be transcribed."""
    with path.open("rb") as handle:
        head = handle.read(16)
    kind = sniff(head)
    if kind is None:
        if head[:4] == b"\x1aE\xdf\xa3":
            raise SpeechError("WebM-Aufnahmen kann LowRAM AI nicht lesen – speichere sie bitte als MP3 oder WAV.")
        raise SpeechError("Das ist keine Audiodatei, die LowRAM AI lesen kann (WAV, MP3, FLAC, Ogg"
                          + (", M4A, AAC, AIFF" if can_convert() else "") + ").")
    if kind in CONVERTED_FORMATS and not can_convert():
        raise SpeechError(f"{FORMAT_LABELS[kind]}-Dateien kann LowRAM AI nur auf dem Mac umwandeln – speichere die "
                          "Aufnahme bitte als MP3 oder WAV.")
    seconds = None
    if kind == "wav":
        info = wav_info(path)
        if info is not None:
            seconds = info[0]
            check_length(seconds)
            check_heard(path)
    return kind, seconds


@route("GET", r"/api/speech")
def get_status(req: Request) -> Response:
    return Response(status())


@route("POST", r"/api/speech/settings")
def post_settings(req: Request) -> Response:
    data = req.json()
    values: dict[str, str] = {}
    if "model" in data:
        values["model"] = get_model(data["model"])["id"]
    if "language" in data:
        if data["language"] not in LANGUAGES:
            raise ValueError("Unbekannte Sprache.")
        values["language"] = data["language"]
    if values:
        settings.update("speech", values)
    return Response(status())


@route("POST", r"/api/speech/models/(base|small|turbo)/install")
def post_install(req: Request, model_id: str) -> Response:
    req.json()
    return Response({"download": install_model(model_id)}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/speech/models/(base|small|turbo)/delete")
def post_delete_model(req: Request, model_id: str) -> Response:
    req.json()
    delete_model(model_id)
    return Response(status())


@route("POST", r"/api/speech/transcribe")
def post_transcribe(req: Request) -> Response:
    req.check_csrf()
    if not _cli_command():
        raise HTTPError(HTTPStatus.CONFLICT, MISSING_CLI)
    model = selected_model()
    if model is None:
        raise HTTPError(HTTPStatus.CONFLICT, "Die Spracherkennung ist noch nicht eingerichtet – lade zuerst ein "
                                             "Sprachmodell herunter (Einstellungen → Sprache).")
    lang = req.arg("language") or language()
    if lang not in LANGUAGES:
        raise ValueError("Unbekannte Sprache.")
    job_id = secrets.token_hex(6)
    path = _temp_dir() / f"{job_id}.audio"
    try:
        _receive(req, path)
        kind, seconds = accept_audio(path)
        return Response({"job": WORKER.submit(_Job(job_id, path, kind, seconds, model, lang, _upload_name(req)))},
                        HTTPStatus.ACCEPTED)
    except SpeechError as exc:
        _remove(path)
        raise HTTPError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
    except BaseException:
        _remove(path)
        raise


@route("GET", r"/api/speech/jobs/([0-9a-f]+)")
def get_job(req: Request, job_id: str) -> Response:
    return Response({"job": WORKER.get(_job_id(job_id))})


@route("POST", r"/api/speech/jobs/([0-9a-f]+)/cancel")
def post_cancel(req: Request, job_id: str) -> Response:
    req.json()
    return Response({"job": WORKER.cancel(_job_id(job_id))})


@route("POST", r"/api/speech/jobs/([0-9a-f]+)/document")
def post_document(req: Request, job_id: str) -> Response:
    """The transcript as a document (to summarise, translate or correct it like any document)."""
    req.json()
    job = WORKER.get(_job_id(job_id))
    if job["status"] != "done" or not job["text"]:
        raise HTTPError(HTTPStatus.CONFLICT, "Die Verschriftlichung ist noch nicht fertig.")
    from . import documents

    stem = Path(job["name"]).stem or "Aufnahme"
    return Response({"upload": documents.save_text_upload(f"{stem} (Verschriftlichung)", job["text"], "audio")},
                    HTTPStatus.CREATED)
