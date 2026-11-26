"""speech.py: dictation and transcription with whisper.cpp – here with a stand-in whisper-cli."""

import json
import math
import os
import struct
import sys
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from backend import config, db, documents, power, progress, speech
from tests.helpers import wait_until
from tests.test_app import AppTestCase

FAKE_WHISPER = r'''
import os, sys, time
args = sys.argv[1:]
log = os.environ.get("FAKE_WHISPER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\n")
mode = os.environ.get("FAKE_WHISPER_MODE", "ok")
if mode == "fail":
    sys.stderr.write("error: failed to read audio data\n")
    sys.exit(3)
steps = range(0, 100, 10) if mode == "slow" else (0, 50)
for percent in steps:
    sys.stderr.write("whisper_print_progress_callback: progress = %3d%%\n" % percent)
    sys.stderr.flush()
    if mode == "slow":
        time.sleep(0.4)
text = os.environ.get("FAKE_WHISPER_TEXT", " Hallo Welt.\n [Musik]\n Das ist ein Test.\n")
with open(args[args.index("-of") + 1] + ".txt", "w", encoding="utf-8") as handle:
    handle.write(text)
'''


def tone(path: Path, seconds: float = 1.5, amplitude: float = 0.3, rate: int = 16000, channels: int = 1) -> Path:
    """A WAV file with a 440-Hz tone (loud enough to count as "something was said")."""
    frames = int(rate * seconds)
    samples = [int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames)]
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"".join(struct.pack("<h", s) * channels for s in samples))
    return path


class AudioTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_formats_are_recognised_by_their_bytes(self):
        cases = {b"RIFF\x24\x00\x00\x00WAVEfmt ": "wav", b"ID3\x04\x00": "mp3", b"\xff\xfb\x90\x00": "mp3",
                 b"fLaC\x00": "flac", b"OggS\x00": "ogg", b"\x00\x00\x00\x20ftypM4A ": "m4a",
                 b"FORM\x00\x00\x00\x00AIFF": "aiff", b"caff\x00": "caf", b"\xff\xf1\x50\x80": "aac",
                 b"\x1aE\xdf\xa3webm": None, b"%PDF-1.7": None, b"": None}
        for head, kind in cases.items():
            self.assertEqual(speech.sniff(head), kind, head)

    def test_wav_length_and_loudness(self):
        loud = tone(self.tmp / "ton.wav", seconds=2)
        seconds, rate, channels, width = speech.wav_info(loud)
        self.assertAlmostEqual(seconds, 2.0, places=2)
        self.assertEqual((rate, channels, width), (16000, 1, 2))
        self.assertGreater(speech.loudest(loud), 0.1)
        quiet = tone(self.tmp / "leise.wav", amplitude=0.0005)
        self.assertLess(speech.loudest(quiet), speech.SILENCE_RMS)
        with self.assertRaisesRegex(speech.SpeechError, "nichts zu hören"):
            speech.check_heard(quiet)
        speech.check_heard(loud)  # no error

    def test_a_header_cannot_claim_more_audio_than_the_file_has(self):
        path = tone(self.tmp / "kurz.wav", seconds=1)
        data = bytearray(path.read_bytes())
        data[40:44] = struct.pack("<I", 0x7FFFFFF0)  # data chunk "size": hours of audio
        path.write_bytes(bytes(data))
        info = speech.wav_info(path)
        self.assertIsNotNone(info)
        self.assertLess(info[0], 1.1)

    def test_length_limits(self):
        with self.assertRaisesRegex(speech.SpeechError, "zu kurz"):
            speech.check_length(0.2)
        with self.assertRaisesRegex(speech.SpeechError, "Stunden"):
            speech.check_length(speech.MAX_AUDIO_S + 1)
        speech.check_length(10)

    def test_tidy_removes_noise_and_subtitle_credits(self):
        raw = (" [Musik]\n Hallo zusammen.\n(Applaus)\n Untertitel der Amara.org-Community\n *Lachen* Weiter geht's.\n"
               " [BLANK_AUDIO]\n Untertitel im Auftrag des ZDF für funk, 2017\n")
        self.assertEqual(speech.tidy(raw), "Hallo zusammen. Weiter geht's.")
        self.assertEqual(speech.tidy(" [Musik]\n\n"), "")

    def test_long_text_gets_paragraphs(self):
        sentence = "Das ist ein ganz normaler Satz aus einer langen Besprechung mit vielen Teilnehmern."
        text = speech.tidy("\n".join([sentence] * 30))
        paragraphs = text.split("\n\n")
        self.assertGreater(len(paragraphs), 2)
        self.assertTrue(all(p.endswith(".") for p in paragraphs))
        self.assertEqual(" ".join(paragraphs), " ".join([sentence] * 30))


class FindCliTests(AppTestCase):
    def test_override_bundled_copy_and_path(self):
        with mock.patch.dict(os.environ, {"LOWRAM_WHISPER_CLI": ""}):
            with mock.patch.object(speech.shutil, "which", return_value=None), \
                    mock.patch.object(speech, "HOMEBREW_DIRS", ()):
                self.assertEqual(speech.find_cli(), {"path": None, "source": None})
            with mock.patch.object(speech.shutil, "which", return_value="/usr/local/bin/whisper-cli"):
                self.assertEqual(speech.find_cli()["source"], "path")

            bundle = self.tmp / "bundle"
            (bundle / "whisper").mkdir(parents=True)
            source = bundle / "whisper" / speech.EXE
            source.write_bytes(b"#!/bin/sh\necho v1\n")
            with mock.patch.object(speech.sys, "_MEIPASS", str(bundle), create=True):
                found = speech.find_cli()
                copy = Path(found["path"])
                self.assertEqual(found["source"], "app")
                self.assertEqual(copy, config.home_dir() / "bin" / "whisper" / speech.EXE)
                self.assertEqual(copy.read_bytes(), source.read_bytes())
                if os.name != "nt":
                    self.assertTrue(os.access(copy, os.X_OK))
                stamp = copy.stat().st_mtime_ns
                speech.find_cli()
                self.assertEqual(copy.stat().st_mtime_ns, stamp)  # unchanged: not copied again
                source.write_bytes(b"#!/bin/sh\necho v2\n")  # an app update brings a new program
                speech.find_cli()
                self.assertEqual(copy.read_bytes(), source.read_bytes())

        program = self.tmp / "own-whisper"
        program.write_bytes(b"#!/bin/sh\n")
        program.chmod(0o755)
        with mock.patch.dict(os.environ, {"LOWRAM_WHISPER_CLI": str(program)}):
            self.assertEqual(speech.find_cli(), {"path": str(program), "source": "env"})


class SpeechApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        speech._cli_copied = None
        self.worker = speech.SpeechWorker()
        patcher = mock.patch.object(speech, "WORKER", self.worker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.worker.stop)
        fake = self.tmp / "fake_whisper.py"
        fake.write_text(FAKE_WHISPER, encoding="utf-8")
        self.calls = self.tmp / "calls.log"
        command = mock.patch.object(speech, "_cli_command", return_value=[sys.executable, str(fake)])
        command.start()
        self.addCleanup(command.stop)
        env = mock.patch.dict(os.environ, {"FAKE_WHISPER_LOG": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("WAIT_STEP_S", 0.05), ("RAM_WAIT_SHORT_S", 0.3)):
            step = mock.patch.object(speech, name, value)
            step.start()
            self.addCleanup(step.stop)

    def install_model(self, model_id="base"):
        path = speech.model_path(speech.get_model(model_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 1_100_000)
        return path

    def send(self, data, name="Diktat.wav", query=""):
        status, _, payload = self.request("POST", "/api/speech/transcribe" + query, raw=data,
                                          content_type="application/octet-stream",
                                          headers={"X-Filename": quote(name)})
        return status, json.loads(payload)

    def job(self, job_id):
        status, data = self.api("GET", f"/api/speech/jobs/{job_id}")
        self.assertEqual(status, 200, data)  # on a failure: what the server answered
        return data["job"]

    def wait_for(self, job_id, *statuses, timeout=15):
        self.assertTrue(wait_until(lambda: self.job(job_id)["status"] in statuses, timeout), self.job(job_id))
        return self.job(job_id)

    def leftovers(self):
        folder = config.home_dir() / "speech" / "tmp"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def test_status_before_setup(self):
        with mock.patch.object(speech, "find_cli", return_value={"path": None, "source": None}):
            status, data = self.api("GET", "/api/speech")
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertIsNone(data["model"])
        self.assertEqual(data["language"], "de")
        by_id = {m["id"]: m for m in data["models"]}
        self.assertEqual(set(by_id), {"base", "small", "turbo"})
        self.assertTrue(by_id["turbo"]["recommended"])  # 16 GB in the tests
        self.assertFalse(any(m["installed"] for m in data["models"]))
        self.assertIn("wav", data["formats"])
        status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
        self.assertEqual(status, 409)
        self.assertIn("Sprachmodell", data["error"])

    def test_dictation_becomes_text_and_the_recording_is_deleted(self):
        model = self.install_model()
        status, data = self.send(tone(self.tmp / "a.wav", seconds=2).read_bytes(), name="Mein Diktat.wav")
        self.assertEqual(status, 202, data)
        self.assertEqual(data["job"]["audio_s"], 2.0)
        job = self.wait_for(data["job"]["id"], "done", "failed")
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["text"], "Hallo Welt. Das ist ein Test.")
        self.assertEqual((job["percent"], job["name"], job["model"], job["language"]), (100, "Mein Diktat.wav", "base", "de"))
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())
        args = self.calls.read_text(encoding="utf-8").split()
        self.assertEqual(args[args.index("-m") + 1], str(model))
        self.assertEqual(args[args.index("-l") + 1], "de")
        for flag in ("-otxt", "-pp", "-np", "-t", "-f", "-of"):
            self.assertIn(flag, args)
        self.assertIsNotNone(progress.typical("speech_min", "base"))  # the next estimate learns from this one

        status, data = self.api("POST", f"/api/speech/jobs/{job['id']}/document", {})
        self.assertEqual(status, 201, data)
        upload = data["upload"]
        self.assertEqual((upload["kind"], upload["kind_label"]), ("audio", "Verschriftlichung (Audio)"))
        self.assertEqual(upload["name"], "Mein Diktat (Verschriftlichung)")
        self.assertEqual(documents.load_upload(upload["id"])[1], "Hallo Welt. Das ist ein Test.")

    def test_language_and_model_settings(self):
        self.install_model("base")
        self.install_model("small")
        status, data = self.api("POST", "/api/speech/settings", {"model": "base", "language": "en"})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["model"], data["language"], data["available"]), ("base", "en", True))
        status, data = self.send(tone(self.tmp / "a.wav").read_bytes(), query="?language=auto")
        job = self.wait_for(data["job"]["id"], "done", "failed")
        self.assertEqual((job["model"], job["language"]), ("base", "auto"))
        self.assertEqual(self.api("POST", "/api/speech/settings", {"language": "xx"})[0], 400)
        self.assertEqual(self.api("POST", "/api/speech/settings", {"model": "huge"})[0], 404)
        self.assertEqual(self.send(tone(self.tmp / "b.wav").read_bytes(), query="?language=xx")[0], 400)
        speech.model_path(speech.get_model("base")).unlink()
        self.assertEqual(self.api("GET", "/api/speech")[1]["model"], "small")  # the chosen one is gone

    def test_silence_short_and_foreign_files_are_refused_at_once(self):
        self.install_model()
        cases = (
            (tone(self.tmp / "still.wav", amplitude=0.0).read_bytes(), "nichts zu hören"),
            (tone(self.tmp / "kurz.wav", seconds=0.2).read_bytes(), "zu kurz"),
            (b"%PDF-1.7 kein Audio" * 10, "keine Audiodatei"),
            (b"\x1aE\xdf\xa3" + b"\0" * 400, "WebM"),
        )
        for data, message in cases:
            status, payload = self.send(data)
            self.assertEqual(status, 422, payload)
            self.assertIn(message, payload["error"])
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.calls.exists())  # whisper-cli never ran

    def test_upload_needs_the_page_token(self):
        self.install_model()
        status, _, _ = self.request("POST", "/api/speech/transcribe", raw=tone(self.tmp / "a.wav").read_bytes(),
                                    content_type="application/octet-stream", token=None)
        self.assertEqual(status, 403)
        self.assertEqual(self.leftovers(), [])

    def test_a_failing_program_is_reported(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "fail"}):
            status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
            job = self.wait_for(data["job"]["id"], "failed", "done")
        self.assertEqual(job["status"], "failed")
        self.assertIn("failed to read audio data", job["error"])
        self.assertIsNone(job["text"])
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_nothing_recognised_is_an_error_not_an_empty_text(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_TEXT": " [Musik]\n Untertitel der Amara.org-Community\n"}):
            status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
            job = self.wait_for(data["job"]["id"], "failed", "done")
        self.assertEqual(job["status"], "failed")
        self.assertIn("keine Sprache erkannt", job["error"])

    def test_cancel_stops_the_program_and_deletes_the_recording(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "slow"}):
            status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
            job_id = data["job"]["id"]
            self.assertTrue(wait_until(lambda: self.job(job_id)["percent"] >= 10, 10), self.job(job_id))
            running = self.job(job_id)
            self.assertEqual(running["status"], "running")
            self.assertIsNotNone(running["eta_s"])
            self.assertEqual(self.api("POST", f"/api/speech/jobs/{job_id}/cancel", {})[0], 200)
            job = self.wait_for(job_id, "cancelled", "done", "failed")
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_a_second_recording_waits_for_the_first(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "slow"}):
            first = self.send(tone(self.tmp / "a.wav").read_bytes())[1]["job"]
            second = self.send(tone(self.tmp / "b.wav").read_bytes())[1]["job"]
            self.assertEqual(second["status"], "queued")
            self.assertIn("vorherige Aufnahme", second["message"])
            self.assertEqual(self.api("POST", f"/api/speech/jobs/{second['id']}/cancel", {})[1]["job"]["status"],
                             "cancelled")
            self.assertEqual(self.wait_for(first["id"], "done", "failed")["status"], "done")
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_only_a_few_recordings_may_wait(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "slow"}), mock.patch.object(speech, "MAX_QUEUE", 1):
            first = self.send(tone(self.tmp / "a.wav").read_bytes())[1]["job"]
            self.assertTrue(wait_until(lambda: self.job(first["id"])["status"] == "running", 10))
            second = self.send(tone(self.tmp / "b.wav").read_bytes())[1]["job"]
            status, data = self.send(tone(self.tmp / "c.wav").read_bytes())
            self.assertEqual(status, 429, data)
            self.assertIn("warten schon", data["error"])
            self.assertEqual(len(self.leftovers()), 2)  # the refused recording was not kept
            for job in (first, second):
                self.api("POST", f"/api/speech/jobs/{job['id']}/cancel", {})
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_a_transcription_that_takes_too_long_is_stopped_with_a_clear_message(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "slow"}), \
                mock.patch.object(speech, "_timeout", return_value=0.5):
            status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
            job = self.wait_for(data["job"]["id"], "failed", "done")
        self.assertEqual(job["status"], "failed")
        self.assertIn("zu lange gedauert", job["error"])
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_an_unreadable_ogg_voice_message_gets_a_clear_hint(self):
        self.install_model()
        with mock.patch.dict(os.environ, {"FAKE_WHISPER_MODE": "fail"}):
            status, data = self.send(b"OggS\x00\x02" + b"\0" * 4000, name="Sprachnachricht.ogg")
            self.assertEqual(status, 202, data)
            job = self.wait_for(data["job"]["id"], "failed", "done")
        self.assertIn("Opus", job["error"])

    def test_long_recordings_wait_for_other_work(self):
        self.install_model()
        release, held = threading.Event(), threading.Event()

        def hold_slot():
            with db.inference_slot("ein Aufgaben-Durchlauf"):
                held.set()
                release.wait(10)

        threading.Thread(target=hold_slot, daemon=True).start()
        self.assertTrue(held.wait(5))
        with mock.patch.object(speech, "LONG_AUDIO_S", 1.0), \
                mock.patch.object(power, "keep_awake", wraps=power.keep_awake) as awake:
            status, data = self.send(tone(self.tmp / "a.wav", seconds=2).read_bytes())
            job_id = data["job"]["id"]
            self.assertTrue(wait_until(lambda: "andere Arbeit" in (self.job(job_id)["message"] or ""), 10),
                            self.job(job_id))
            self.assertEqual(self.job(job_id)["status"], "waiting")
            release.set()
            job = self.wait_for(job_id, "done", "failed")
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(awake.called)

    def test_not_enough_free_memory(self):
        self.install_model()
        self.set_memory(16, available_mib=200)
        status, data = self.send(tone(self.tmp / "a.wav").read_bytes())
        job = self.wait_for(data["job"]["id"], "failed", "done")
        self.assertIn("Arbeitsspeicher", job["error"])
        self.assertFalse(self.calls.exists())

    def test_voice_memos_are_converted_on_the_mac_only(self):
        self.install_model()
        m4a = b"\x00\x00\x00\x20ftypM4A " + b"\0" * 2000
        with mock.patch.object(speech, "can_convert", return_value=False):
            status, data = self.send(m4a, name="Sprachmemo.m4a")
        self.assertEqual(status, 422)
        self.assertIn("nur auf dem Mac", data["error"])

        def convert(source, target):
            tone(target, seconds=3)

        with mock.patch.object(speech, "can_convert", return_value=True), \
                mock.patch.object(speech, "_convert", side_effect=convert) as converted:
            status, data = self.send(m4a, name="Sprachmemo.m4a")
            self.assertEqual(status, 202, data)
            self.assertIsNone(data["job"]["audio_s"])
            job = self.wait_for(data["job"]["id"], "done", "failed")
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["audio_s"], 3.0)
        self.assertTrue(converted.called)
        args = self.calls.read_text(encoding="utf-8").split()
        self.assertTrue(args[args.index("-f") + 1].endswith("-16k.wav"))
        self.assertTrue(wait_until(lambda: self.leftovers() == []), self.leftovers())

    def test_old_results_are_forgotten(self):
        self.install_model()
        job = self.wait_for(self.send(tone(self.tmp / "a.wav").read_bytes())[1]["job"]["id"], "done")
        with mock.patch.object(speech.time, "time", return_value=time.time() + speech.RESULT_TTL_S + 5):
            self.assertEqual(self.api("GET", f"/api/speech/jobs/{job['id']}")[0], 404)
        self.assertEqual(self.api("GET", "/api/speech/jobs/zz")[0], 404)

    def test_the_speech_recognition_is_prepared_once(self):
        model = speech.get_model("base")
        self.install_model()
        with mock.patch.object(speech, "WARM_UP_PLATFORMS", (sys.platform,)):
            with mock.patch.object(power, "battery_block_reason", return_value="Akku fast leer"):
                self.assertFalse(speech.warm_up(model))  # never on a low battery
            self.assertTrue(speech.warm_up(model))
            self.assertFalse(speech.warm_up(model))  # once per program and model
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("warm-", calls[0])
        self.assertIsNone(progress.typical("speech_min", "base"))  # preparing is no measurement for estimates
        self.assertEqual(self.leftovers(), [])
        with mock.patch.object(speech, "WARM_UP_PLATFORMS", ()):
            (config.home_dir() / "speech" / "warmed.json").unlink()
            self.assertFalse(speech.warm_up(model))  # only where the GPU code needs it (the Mac)

    def test_model_install_and_delete(self):
        meta = {"url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
                "sha256": "a" * 64, "size": 147_951_465}
        with mock.patch("backend.media.hf_file_metadata", return_value=meta) as lookup, \
                mock.patch("backend.downloads.start", return_value={"id": "speech-base", "state": "queued"}) as start:
            status, data = self.api("POST", "/api/speech/models/base/install", {})
        self.assertEqual(status, 202, data)
        lookup.assert_called_once_with("ggerganov/whisper.cpp", "ggml-base.bin")
        args, kwargs = start.call_args
        self.assertEqual(args[0], "speech-base")
        self.assertEqual((kwargs["sha256"], kwargs["size"], kwargs["kind"]), ("a" * 64, 147_951_465, "speech-model"))
        self.assertTrue(callable(kwargs["on_complete"]))  # prepares the speech recognition after the download
        self.assertEqual(Path(args[2]), speech.model_path(speech.get_model("base")))

        path = self.install_model("base")
        status, data = self.api("POST", "/api/speech/models/base/delete", {})
        self.assertEqual(status, 200, data)
        self.assertFalse(path.exists())
        self.assertEqual(self.api("POST", "/api/speech/models/huge/install", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
