#!/usr/bin/env python3
"""Checks speech recognition in the built app (``--headless``) on the CI runner.

    python desktop/speech_smoke.py http://127.0.0.1:8797

What only the real app can show: the bundled whisper-cli starts (copied out of the app; Metal on
the Mac), the Whisper model downloads with its SHA-256 check, a spoken sentence comes back as text –
from WAV and, on the Mac, from AIFF converted with afconvert –, silence is refused, and no recording
is left behind. The sentence is spoken by the system (``say`` on macOS, SAPI on Windows).
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

SENTENCES = {
    "de": "Hallo, das ist ein Test der Spracherkennung. Heute ist ein schöner Tag.",
    "en": "Hello, this is a test of the speech recognition. Today is a beautiful day.",
}
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the app on 127.0.0.1, never via a proxy


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    log(f"FAILED: {message}")
    sys.exit(1)


class App:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        page = LOCAL.open(self.base + "/", timeout=30).read().decode("utf-8")
        match = re.search(r'name="lowram-token" content="([^"]+)"', page)
        if not match:
            fail("the served page has no CSRF token")
        self.token = match.group(1)

    def call(self, method: str, path: str, body: dict | None = None, *, raw: bytes | None = None,
             name: str | None = None, timeout: float = 120) -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body or {}).encode() if method == "POST" else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if method == "POST":
            request.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
            request.add_header("X-LowRAM-Token", self.token)
        if name:
            request.add_header("X-Filename", quote(name))
        try:
            with LOCAL.open(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


def speak(folder: Path) -> tuple[str, list[Path]]:
    """``(language, audio files)`` of one spoken test sentence; no files if the system cannot speak."""
    if sys.platform == "darwin":
        for lang, voice in (("de", "Anna"), ("en", None)):
            aiff = folder / f"satz-{lang}.aiff"
            command = ["say", "-o", str(aiff), *(["-v", voice] if voice else []), SENTENCES[lang]]
            if subprocess.run(command, capture_output=True, check=False).returncode == 0 and aiff.is_file() \
                    and aiff.stat().st_size > 1000:
                wav = folder / f"satz-{lang}.wav"
                subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
                               check=True)
                return lang, [wav, aiff]  # WAV as the page sends it, AIFF converted inside the app
            log(f"   note: no {voice or 'default'} voice")
        fail("macOS could not speak the test sentence")
    if os.name == "nt":
        wav = folder / "satz-en.wav"
        script = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{SENTENCES['en']}'); $s.Dispose()")
        done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, check=False)
        if done.returncode == 0 and wav.is_file() and wav.stat().st_size > 1000:
            return "en", [wav]
        log(f"   note: Windows could not speak ({done.stderr.decode(errors='replace').strip()[:200]})")
    return "en", []


def silence(path: Path, seconds: float = 2.0) -> Path:
    frames = int(16000 * seconds)
    header = (b"RIFF" + struct.pack("<I", 36 + frames * 2) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000,
              32000, 2, 16) + b"data" + struct.pack("<I", frames * 2))
    path.write_bytes(header + b"\x00\x00" * frames)
    return path


def wait_for_model(app: App) -> None:
    deadline = time.monotonic() + 900
    shown = None
    while True:
        _, status = app.call("GET", "/api/speech")
        base = next(model for model in status["models"] if model["id"] == "base")
        download = base.get("download") or {}
        if base["installed"] and download.get("state") in (None, "done"):
            return
        if download.get("state") in ("failed", "cancelled"):
            fail(f"model download: {download}")
        if time.monotonic() > deadline:
            fail(f"the model download did not finish: {download}")
        now = (download.get("state"), (download.get("bytes_done") or 0) // (20 * 1024 * 1024))
        if now != shown:
            log(f"   {download.get('state')}: {download.get('bytes_done') or 0} of {download.get('bytes_total')} bytes")
            shown = now
        time.sleep(3)


def transcribe(app: App, audio: Path, lang: str) -> dict:
    started = time.monotonic()
    status, data = app.call("POST", f"/api/speech/transcribe?language={lang}", raw=audio.read_bytes(), name=audio.name)
    if status != 202:
        fail(f"{audio.name}: {status} {data}")
    job = data["job"]
    while job["status"] not in ("done", "failed", "cancelled"):
        if time.monotonic() - started > 600:
            fail(f"{audio.name}: no result after 10 minutes: {job}")
        time.sleep(0.5)
        _, data = app.call("GET", f"/api/speech/jobs/{job['id']}")
        job = data["job"]
    if job["status"] != "done":
        fail(f"{audio.name}: {job}")
    job["seconds"] = time.monotonic() - started
    return job


def main() -> None:
    app = App(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8797")
    folder = Path(tempfile.mkdtemp(prefix="lowram-speech-"))

    log("1. The app's own whisper-cli")
    _, status = app.call("GET", "/api/speech")
    if status.get("cli") != "app":
        fail(f"the bundled whisper-cli was not found: {status}")
    log(f"   ok, formats: {', '.join(status['formats'])}")

    log("2. Model download (Whisper Base, SHA-256 checked)")
    code, data = app.call("POST", "/api/speech/models/base/install", {})
    if code != 202:
        fail(f"install: {code} {data}")
    wait_for_model(app)
    lang, files = speak(folder)
    code, data = app.call("POST", "/api/speech/settings", {"model": "base", "language": lang})
    if code != 200 or not data.get("available"):
        fail(f"settings: {code} {data}")
    log("   ok, speech recognition is ready")

    if sys.platform == "darwin":  # the app prepares the speech recognition after the download (Metal, once)
        warmed = Path(os.environ["LOWRAM_HOME"]) / "speech" / "warmed.json"
        started = time.monotonic()
        while not warmed.exists() and time.monotonic() - started < 120:
            time.sleep(1)
        log(f"   prepared in the background: {'yes' if warmed.exists() else 'NO'} "
            f"(waited {time.monotonic() - started:.0f} s)")

    log("3. Silence is refused")
    code, data = app.call("POST", "/api/speech/transcribe", raw=silence(folder / "stille.wav").read_bytes(),
                          name="stille.wav")
    if code != 422 or "nichts zu hören" not in data.get("error", ""):
        fail(f"silence: {code} {data}")
    log("   ok")

    log("4. A spoken sentence becomes text")
    expected = set(re.findall(r"\w+", SENTENCES[lang].lower()))
    for audio in files:
        job = transcribe(app, audio, lang)
        words = set(re.findall(r"\w+", job["text"].lower()))
        share = len(words & expected) / len(expected)
        log(f"   {audio.suffix[1:].upper()}: {job['seconds']:.1f} s for {job['audio_s']} s of audio: {job['text']!r} "
            f"({share:.0%} of the words)" + (" – the first dictation" if audio == files[0] else ""))
        if share < 0.5:
            fail("the text does not match what was said")
    if not files:
        log("   skipped: this system has no voice to speak the test sentence")

    log("5. No recording is kept")
    leftovers = list((Path(os.environ["LOWRAM_HOME"]) / "speech" / "tmp").glob("*"))
    if leftovers:
        fail(f"recordings were left behind: {[p.name for p in leftovers]}")
    log("   ok")
    log("OK – speech recognition works in the built app")


if __name__ == "__main__":
    main()
