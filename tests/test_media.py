import os
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from backend import db, media, settings
from tests.helpers import wait_until
from tests.media_helpers import FAKE_FFMPEG, MediaAppTestCase, MediaTestCase, write_script

POSIX = os.name == "posix"
NASTY = 'a "cat"; rm -rf ~ && echo $(whoami) `id` | tee x\n-v --steps 999 \\ \'quoted\''


def image_job(**kw):
    return {"kind": "image", "prompt": "a lovely cat", **kw}


class CatalogTests(unittest.TestCase):
    def test_catalog_entries_are_well_formed(self):
        kinds = set()
        for model in media.CATALOG:
            with self.subTest(model=model["id"]):
                self.assertRegex(model["id"], media.CATALOG_ID_RE)
                kinds.add(model["kind"])
                roles = {f["role"] for f in model["files"]}
                self.assertTrue(roles <= (media.IMAGE_ROLES if model["kind"] == "image" else media.VIDEO_ROLES))
                names = [Path(f["filename"]).name for f in model["files"]]
                self.assertEqual(len(names), len(set(names)))
                for item in model["files"]:
                    self.assertRegex(item["repo"], media.HF_REPO_RE)
                    self.assertRegex(item["filename"], media.HF_PATH_RE)
                self.assertIn(model["defaults"]["sampler"], media.SAMPLERS)
                for key in ("size_bytes", "min_ram_gb", "license", "description", "name"):
                    self.assertTrue(model[key])
                if model.get("weight_type"):
                    self.assertIn(model["weight_type"], media.WEIGHT_TYPES)
        self.assertEqual(kinds, {"image", "video"})
        wan = media.CATALOG_BY_ID["wan2.1-t2v-1.3b"]
        self.assertEqual({f["role"] for f in wan["files"]}, {"diffusion_model", "vae", "text_encoder"})
        self.assertIn("16 GB", wan["warning"])
        self.assertIn("very slow", wan["description"].lower())


class ArgumentTests(MediaTestCase):
    def job(self, kind="image", prompt=NASTY, **params):
        base = {"width": 512, "height": 512, "steps": 4, "seed": 7, "cfg_scale": 1.0, "sampler": "euler_a"}
        if kind == "video":
            base.update(width=832, height=480, frames=33, fps=16, flow_shift=3.0, cfg_scale=6.0, sampler="euler")
        base.update(params)
        return {"kind": kind, "prompt": prompt, "negative_prompt": "ugly; $(x)", "params": base}

    def test_image_args_keep_prompt_one_element(self):
        model = media.CATALOG_BY_ID["sd-turbo"]
        files = media.model_file_paths(model)
        args = media.build_image_args("/bin/sd-cli", model, files, self.job(), Path("/tmp/out.png"))
        self.assertTrue(all(isinstance(a, str) for a in args))
        self.assertEqual(args.count(NASTY), 1)
        self.assertEqual(args[args.index("-p") + 1], NASTY)
        self.assertEqual(args[args.index("-n") + 1], "ugly; $(x)")
        self.assertEqual(args[args.index("-m") + 1], str(files["model"]))
        self.assertEqual(args[args.index("--type") + 1], "f16")
        for flag, value in (("-W", "512"), ("-H", "512"), ("--steps", "4"), ("--cfg-scale", "1"),
                            ("--sampling-method", "euler_a"), ("-s", "7"), ("-o", "/tmp/out.png")):
            self.assertEqual(args[args.index(flag) + 1], value, flag)
        self.assertNotIn("-M", args)
        self.assertNotIn("--vae-tiling", args)
        big = media.build_image_args("sd", model, files, self.job(width=1024, height=1024), Path("o.png"))
        self.assertIn("--vae-tiling", big)

    def test_image_args_with_separate_vae(self):
        model = media.CATALOG_BY_ID["sdxl-turbo"]
        files = media.model_file_paths(model)
        args = media.build_image_args("sd", model, files, self.job(), Path("o.png"))
        self.assertEqual(args[args.index("--vae") + 1], str(files["vae"]))
        self.assertEqual(args[args.index("--type") + 1], "q8_0")

    def test_video_args(self):
        model = media.CATALOG_BY_ID["wan2.1-t2v-1.3b"]
        files = media.model_file_paths(model)
        args = media.build_video_args("sd", model, files, self.job("video"), Path("/t/output.webm"))
        self.assertEqual(args[1:3], ["-M", "vid_gen"])
        for flag, role in (("--diffusion-model", "diffusion_model"), ("--vae", "vae"), ("--t5xxl", "text_encoder")):
            self.assertEqual(args[args.index(flag) + 1], str(files[role]))
        for flag, value in (("--video-frames", "33"), ("--fps", "16"), ("--flow-shift", "3"), ("-W", "832"),
                            ("-H", "480"), ("--sampling-method", "euler"), ("--cfg-scale", "6"),
                            ("-o", "/t/output.webm")):
            self.assertEqual(args[args.index(flag) + 1], value, flag)
        self.assertIn("--vae-tiling", args)
        self.assertEqual(args.count(NASTY), 1)

    def test_transcode_args(self):
        args = media.build_transcode_args("ffmpeg", Path("in.webm"), Path("out.mp4"))
        self.assertIn("libx264", args)
        self.assertEqual(args[args.index("-movflags") + 1], "+faststart")
        self.assertEqual(args[-1], "out.mp4")


class ValidationTests(MediaTestCase):
    def test_defaults_and_random_seed(self):
        spec = media.validate_job_request(image_job())
        self.assertEqual(spec["model_id"], "sd-turbo")
        self.assertEqual(spec["params"]["width"], 512)
        self.assertGreaterEqual(spec["params"]["seed"], 0)
        spec = media.validate_job_request(image_job(seed=42, steps=10, width=768, height=1536))
        self.assertEqual((spec["params"]["seed"], spec["params"]["steps"], spec["params"]["height"]), (42, 10, 1536))
        video = media.validate_job_request({"kind": "video", "prompt": "waves"})
        self.assertEqual((video["params"]["frames"], video["params"]["height"]), (33, 480))
        self.assertTrue(video["negative_prompt"])  # Wan default negative prompt

    def test_rejects_bad_input(self):
        bad = [
            {}, {"kind": "audio", "prompt": "x"}, image_job(prompt=""), image_job(prompt="   "),
            image_job(prompt="x" * 2001), image_job(prompt=5), image_job(prompt="a\x00b"),
            image_job(negative_prompt="n" * 2001), image_job(width=500), image_job(width=128),
            image_job(width=1600), image_job(height=True), image_job(steps=0), image_job(steps=151),
            image_job(steps=2.5), image_job(steps="10"), image_job(seed=-2), image_job(seed="1"),
            image_job(seed=2**40), image_job(cfg_scale=float("nan")), image_job(frames=9),
            image_job(model_id="sd15"),  # not installed
            image_job(model_id="wan2.1-t2v-1.3b"), image_job(model_id="../etc"), image_job(model_id="nope"),
            image_job(extra=1), {"kind": "video", "prompt": "x", "width": 896},
            {"kind": "video", "prompt": "x", "frames": 0}, {"kind": "video", "prompt": "x", "frames": 162},
            {"kind": "video", "prompt": "x", "frames": 34}, {"kind": "video", "prompt": "x", "width": 840},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:80]):
                with self.assertRaises(ValueError):
                    media.validate_job_request(body)

    def test_no_model_installed(self):
        for path in media.model_file_paths(media.CATALOG_BY_ID["sd-turbo"]).values():
            path.unlink()
        with self.assertRaisesRegex(ValueError, "No image model"):
            media.validate_job_request(image_job())

    def test_settings_choose_default_model(self):
        self.install_model_files("sd15")
        self.assertEqual(media.validate_job_request(image_job())["model_id"], "sd-turbo")
        settings.update("media", {"image_model_id": "sd15"})
        self.assertEqual(media.validate_job_request(image_job())["model_id"], "sd15")


class ProgressTests(unittest.TestCase):
    def test_parses_bars_steps_eta_and_phases(self):
        p = media.ProgressParser("image", 20)
        self.assertTrue(p.feed("\x1b[K  |############                        | 3/10 - 120.00MB/s"))
        self.assertIn("Loading", p.message)
        self.assertAlmostEqual(p.progress, 0.044, places=3)
        p.feed("  |=======>                 | 12/20 - 3.52s/it - ETA 00:28\x1b[K")
        self.assertAlmostEqual(p.progress, 0.58, places=2)
        self.assertEqual(p.message, "Generating image: step 12/20 (3.5 s/step, about 00:28 left)")
        p.feed("  |==================================================| 20/20 - 1.20it/s")
        self.assertAlmostEqual(p.progress, 0.9, places=2)
        p.feed("  |=====>       | 2/8 - 0.50s/it")  # VAE tiles after sampling
        self.assertIn("Decoding", p.message)
        self.assertGreater(p.progress, 0.9)
        p.feed("[INFO ] main.cpp - save result image 0 to 'x.png' (success)")
        self.assertEqual(p.message, "Saving…")
        self.assertLess(p.progress, 1.0)
        self.assertFalse(p.feed(""))

    def test_generic_step_lines_and_monotonic(self):
        p = media.ProgressParser("video", None)
        p.feed("step 5/10")
        self.assertAlmostEqual(p.progress, 0.5)
        self.assertIn("Generating video", p.message)
        p.feed("step 2/10")
        self.assertAlmostEqual(p.progress, 0.5)  # never goes backwards
        p.feed("[ERROR] something exploded")
        self.assertIn("exploded", p.failure_summary())


@unittest.skipUnless(POSIX, "fake executables are POSIX scripts")
class RuntimeDiscoveryTests(MediaTestCase):
    def test_managed_runtime_found_with_version(self):
        info = media.find_runtime()
        self.assertTrue(info["installed"])
        self.assertEqual(info["path"], str(self.sd))
        self.assertEqual(info["source"], "managed")
        self.assertEqual(info["version"], "master-999-fake (abc1234def)")

    def test_unrelated_sd_tool_is_ignored(self):
        self.sd.unlink()
        impostor = write_script(self.tmp / "pathbin" / "sd", "#!/usr/bin/env python3\nprint('sd 1.0.0')\n")
        with mock.patch.dict(os.environ, {"PATH": f"{impostor.parent}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(media, "HOMEBREW_DIRS", ()):
            info = media.find_runtime()
        self.assertFalse(info["installed"])

    def test_restores_executable_bit(self):
        self.sd.chmod(0o644)
        media._probe_cache.clear()
        self.assertTrue(media.find_runtime()["installed"])
        self.assertTrue(os.access(self.sd, os.X_OK))


@unittest.skipUnless(POSIX, "fake executables are POSIX scripts")
class WorkerTests(MediaTestCase):
    def test_image_job_end_to_end(self):
        job = media.submit_job(image_job(prompt=NASTY, seed=11))
        self.assertEqual(job["status"], "queued")
        job = self.wait_status(job["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["progress"], 1.0)
        self.assertEqual(job["output_type"], "image/png")
        self.assertEqual(job["output_url"], f"/api/media/jobs/{job['id']}/file")
        self.assertIsNotNone(job["duration_s"])
        path = self.home / "media" / f"{job['id']}.png"
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        call = self.argv_calls()[-1]
        self.assertEqual(call["argv"].count(NASTY), 1)
        self.assertEqual(call["argv"][call["argv"].index("-s") + 1], "11")
        self.assertTrue(Path(call["cwd"]).resolve().is_relative_to((self.home / "media" / "tmp").resolve()))
        self.assertFalse((self.home / "media" / "tmp" / f"job_{job['id']}").exists())  # temp dir removed
        self.assertEqual(self.engine.events, ["pause", "resume"])  # unload_llm defaults to on
        self.assertIsNone(db.busy_reason())  # slot released

    def test_no_pause_when_disabled_or_engine_missing(self):
        settings.update("media", {"unload_llm": False})
        self.assertEqual(self.wait_status(media.submit_job(image_job())["id"])["status"], "done")
        self.assertEqual(self.engine.events, [])
        settings.update("media", {"unload_llm": True})
        self.inject_module("backend.engine", None)  # import fails like a missing module
        self.assertEqual(self.wait_status(media.submit_job(image_job())["id"])["status"], "done")

    def test_failure_is_reported(self):
        job = self.wait_status(media.submit_job(image_job(prompt="FAIL please"))["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("exit code 3", job["error"])
        self.assertIn("bad magic", job["error"])
        self.assertIsNone(job["output_url"])
        self.assertEqual(list((self.home / "media").glob(f"{job['id']}.*")), [])

    def test_success_without_file_is_a_failure(self):
        job = self.wait_status(media.submit_job(image_job(prompt="NOFILE"))["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("no output file", job["error"])

    def test_timeout(self):
        with mock.patch.object(media, "IMAGE_TIMEOUT_S", 1.0):
            job = self.wait_status(media.submit_job(image_job(prompt="HANG"))["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("did not finish", job["error"])

    def test_queue_runs_in_order_one_at_a_time(self):
        ids = [media.submit_job(image_job(prompt=f"SLOW {i}"))["id"] for i in range(3)]
        max_running = 0

        def check():
            nonlocal max_running
            jobs = [media.get_job(i) for i in ids]
            max_running = max(max_running, sum(j["status"] == "running" for j in jobs))
            return all(j["status"] == "done" for j in jobs)

        self.assertTrue(wait_until(check, 30, 0.01))
        self.assertEqual(max_running, 1)
        jobs = [media.get_job(i) for i in ids]
        self.assertEqual([j["started_at"] for j in jobs], sorted(j["started_at"] for j in jobs))
        prompts = [c["argv"][c["argv"].index("-p") + 1] for c in self.argv_calls()]
        self.assertEqual(prompts, ["SLOW 0", "SLOW 1", "SLOW 2"])

    def test_waits_for_inference_slot(self):
        with db.inference_slot("chat reply"):
            job = media.submit_job(image_job())
            self.assertTrue(wait_until(lambda: media.get_job(job["id"])["message"] == media.WAITING_MESSAGE, 10))
            time.sleep(0.3)
            self.assertEqual(media.get_job(job["id"])["status"], "queued")
            self.assertEqual(self.argv_calls(), [])
        self.assertEqual(self.wait_status(job["id"])["status"], "done")

    def test_cancel_running_kills_process_group(self):
        pid_file = self.tmp / "sd.pid"
        os.environ["FAKE_SD_PID_FILE"] = str(pid_file)
        job = media.submit_job(image_job(prompt="HANG"))
        self.assertTrue(wait_until(pid_file.exists, 10))
        self.assertTrue(wait_until(lambda: media.get_job(job["id"])["progress"] > 0.1, 10))
        pid = int(pid_file.read_text())
        started = time.monotonic()
        cancelled = media.cancel_job(job["id"])
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(wait_until(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == "zombie", 3))
        self.assertIsNone(db.busy_reason())
        self.assertEqual(self.engine.events, ["pause", "resume"])
        with self.assertRaises(db.InvalidTransition):
            media.cancel_job(job["id"])

    def test_cancel_queued_job_never_runs(self):
        first = media.submit_job(image_job(prompt="HANG"))
        second = media.submit_job(image_job(prompt="second"))
        self.assertTrue(wait_until(lambda: media.get_job(first["id"])["status"] == "running", 10))
        self.assertEqual(media.cancel_job(second["id"])["status"], "cancelled")
        media.cancel_job(first["id"])
        time.sleep(0.5)
        prompts = [c["argv"][c["argv"].index("-p") + 1] for c in self.argv_calls()]
        self.assertNotIn("second", prompts)

    def test_interrupted_jobs_recovered_and_queue_resumes(self):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO media_jobs (kind, prompt, model_id, params, status, owner, created_at, started_at) "
                "VALUES ('image', 'old', 'sd-turbo', '{}', 'running', 'host:999999999:1.00', ?, ?)",
                (db.utcnow(), db.utcnow()),
            )
            stale = conn.execute("SELECT MAX(id) FROM media_jobs").fetchone()[0]
            spec = media.validate_job_request(image_job(prompt="queued before restart"))
            conn.execute(
                "INSERT INTO media_jobs (kind, prompt, model_id, params, status, created_at) "
                "VALUES ('image', ?, 'sd-turbo', ?, 'queued', ?)",
                (spec["prompt"], media.json.dumps(spec["params"]), db.utcnow()),
            )
            queued = conn.execute("SELECT MAX(id) FROM media_jobs").fetchone()[0]
        (self.home / "media" / "tmp" / "job_123").mkdir(parents=True)
        media._start_worker(None)  # the startup hook
        job = media.get_job(stale)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Interrupted", job["error"])
        self.assertFalse((self.home / "media" / "tmp" / "job_123").exists())
        self.assertEqual(self.wait_status(queued)["status"], "done")

    def test_shutdown_marks_running_job_interrupted(self):
        job = media.submit_job(image_job(prompt="HANG"))
        self.assertTrue(wait_until(lambda: media.get_job(job["id"])["status"] == "running", 10))
        self.assertTrue(wait_until(lambda: self.argv_calls() != [], 10))
        media._stop_worker(None)
        job = media.get_job(job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("closed", job["error"])

    def test_video_native_webm_without_ffmpeg(self):
        job = self.wait_status(media.submit_job({"kind": "video", "prompt": "waves", "frames": 17})["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["output_type"], "video/webm")
        self.assertTrue((self.home / "media" / f"{job['id']}.webm").exists())
        argv = self.argv_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--video-frames") + 1], "17")

    def test_old_build_avi_is_sniffed(self):
        job = self.wait_status(media.submit_job({"kind": "video", "prompt": "AVI waves"})["id"])
        self.assertEqual(job["output_type"], "video/x-msvideo")
        self.assertTrue((self.home / "media" / f"{job['id']}.avi").exists())

    def test_single_frame_video_is_png(self):
        job = self.wait_status(media.submit_job({"kind": "video", "prompt": "still", "frames": 1})["id"])
        self.assertEqual(job["output_type"], "image/png")

    def test_video_converted_to_mp4_with_ffmpeg(self):
        ffmpeg = write_script(self.tmp / "ff" / "ffmpeg", FAKE_FFMPEG)
        log = self.tmp / "ffmpeg.jsonl"
        os.environ["FAKE_FFMPEG_LOG"] = str(log)
        with mock.patch.object(media, "find_ffmpeg", return_value=ffmpeg):
            job = self.wait_status(media.submit_job({"kind": "video", "prompt": "waves"})["id"])
            self.assertEqual(job["output_type"], "video/mp4")
            self.assertTrue((self.home / "media" / f"{job['id']}.mp4").exists())
            self.assertIn("+faststart", log.read_text())
            os.environ["FAKE_FFMPEG_FAIL"] = "1"
            job = self.wait_status(media.submit_job({"kind": "video", "prompt": "waves"})["id"])
            self.assertEqual((job["status"], job["output_type"]), ("done", "video/webm"))  # kept native


@unittest.skipUnless(POSIX, "fake executables are POSIX scripts")
class ApiTests(MediaAppTestCase):
    def test_status(self):
        status, data = self.api("GET", "/api/media/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["runtime"]["installed"])
        self.assertEqual(data["runtime"]["version"], "master-999-fake (abc1234def)")
        self.assertEqual({m["id"] for m in data["image_models"]}, {"sd-turbo", "sd15", "sdxl-turbo"})
        self.assertEqual([m["id"] for m in data["video_models"]], ["wan2.1-t2v-1.3b"])
        turbo = next(m for m in data["image_models"] if m["id"] == "sd-turbo")
        self.assertTrue(turbo["installed"])
        self.assertFalse(next(m for m in data["image_models"] if m["id"] == "sd15")["installed"])
        self.assertIsNone(data["active_job_id"])
        self.assertIn("16 GB", data["video_models"][0]["warning"])

    def test_job_lifecycle_and_file_route(self):
        status, data = self.api("POST", "/api/media/jobs", image_job(prompt="HANG"))
        self.assertEqual(status, 202, data)
        job_id = data["job"]["id"]
        self.assertTrue(wait_until(lambda: media.get_job(job_id)["status"] == "running", 10))
        self.assertEqual(self.api("GET", "/api/media/status")[1]["active_job_id"], job_id)
        self.assertEqual(self.request("GET", f"/api/media/jobs/{job_id}/file")[0], 404)
        status, data = self.api("POST", f"/api/media/jobs/{job_id}/cancel")
        self.assertEqual((status, data["job"]["status"]), (200, "cancelled"))
        self.assertEqual(self.api("POST", f"/api/media/jobs/{job_id}/cancel")[0], 409)

        status, data = self.api("POST", "/api/media/jobs", image_job())
        job_id = data["job"]["id"]
        self.wait_status(job_id)
        status, headers, body = self.request("GET", f"/api/media/jobs/{job_id}/file")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertNotIn("Content-Disposition", headers)
        _, headers, _ = self.request("GET", f"/api/media/jobs/{job_id}/file?download=1")
        self.assertIn(f"lowram-image-{job_id}.png", headers["Content-Disposition"])
        status, data = self.api("GET", f"/api/media/jobs/{job_id}")
        self.assertEqual(data["job"]["status"], "done")
        status, data = self.api("GET", "/api/media/jobs")
        self.assertEqual([j["id"] for j in data["jobs"]][0], job_id)

        path = self.home / "media" / f"{job_id}.png"
        self.assertTrue(path.exists())
        self.assertEqual(self.api("POST", f"/api/media/jobs/{job_id}/delete"), (200, {"deleted": True}))
        self.assertFalse(path.exists())
        self.assertEqual(self.api("GET", f"/api/media/jobs/{job_id}")[0], 404)
        self.assertEqual(self.request("GET", f"/api/media/jobs/{job_id}/file")[0], 404)

    def test_delete_running_job_cancels_it(self):
        _, data = self.api("POST", "/api/media/jobs", image_job(prompt="HANG"))
        job_id = data["job"]["id"]
        self.assertTrue(wait_until(lambda: media.get_job(job_id)["status"] == "running", 10))
        self.assertEqual(self.api("POST", f"/api/media/jobs/{job_id}/delete")[0], 200)
        self.assertEqual(self.api("GET", f"/api/media/jobs/{job_id}")[0], 404)

    def test_validation_csrf_and_404(self):
        self.assertEqual(self.api("POST", "/api/media/jobs", image_job(), token=None)[0], 403)
        self.assertEqual(self.api("POST", "/api/media/jobs", image_job(), token="wrong")[0], 403)
        status, data = self.api("POST", "/api/media/jobs", image_job(width=500))
        self.assertEqual(status, 400)
        self.assertIn("multiple of 64", data["error"])
        self.assertEqual(self.api("POST", "/api/media/jobs", {"kind": "image"})[0], 400)
        self.assertEqual(media.list_jobs(), [])
        self.assertEqual(self.api("GET", "/api/media/jobs/999")[0], 404)
        self.assertEqual(self.api("POST", "/api/media/jobs/999/cancel")[0], 404)
        self.assertEqual(self.api("GET", "/api/media/jobs/99999999999999999999999")[0], 404)
        self.assertEqual(self.api("POST", "/api/media/models/nope/install")[0], 404)

    def test_job_refused_without_runtime(self):
        self.sd.unlink()
        media._probe_cache.clear()
        with mock.patch.object(media, "HOMEBREW_DIRS", ()), mock.patch.object(media.shutil, "which", return_value=None):
            status, data = self.api("POST", "/api/media/jobs", image_job())
        self.assertEqual(status, 409)
        self.assertIn("not installed", data["error"])

    def test_runtime_install(self):
        release = {
            "tag_name": "master-500-abcdef0",
            "assets": [
                {"name": "sd-master-abcdef0-bin-win-cuda12-x64.zip", "size": 9,
                 "browser_download_url": media.RELEASE_DOWNLOAD_PREFIX + "x/a.zip"},
                {"name": "sd-master-abcdef0-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip", "size": 9,
                 "browser_download_url": media.RELEASE_DOWNLOAD_PREFIX + "x/b.zip"},
                {"name": "sd-master-abcdef0-bin-Linux-Ubuntu-24.04-x86_64.zip", "size": 1234,
                 "digest": "sha256:" + "ab" * 32,
                 "browser_download_url": media.RELEASE_DOWNLOAD_PREFIX + "master-500-abcdef0/linux.zip"},
            ],
        }
        with mock.patch.object(media, "fetch_json", return_value=release) as fetch, \
                mock.patch.object(media, "_platform_key", return_value=("linux", "x86_64")):
            status, data = self.api("POST", "/api/media/runtime/install")
        self.assertEqual(status, 202, data)
        fetch.assert_called_once_with(media.RELEASES_URL)
        call = self.downloads.started[-1]
        self.assertEqual(call["url"], media.RELEASE_DOWNLOAD_PREFIX + "master-500-abcdef0/linux.zip")
        self.assertEqual((call["sha256"], call["size"], call["kind"]), ("ab" * 32, 1234, "media-runtime"))
        root = self.home / "bin" / "stable-diffusion.cpp"
        self.assertEqual(Path(call["extract"]), root / "master-500-abcdef0")
        self.assertEqual(call["dest"].parent, root)
        self.assertEqual(data["download"]["id"], "media-runtime")
        # an active download is returned instead of starting a second one
        with mock.patch.object(media, "fetch_json") as fetch:
            self.assertEqual(self.api("POST", "/api/media/runtime/install")[0], 202)
            fetch.assert_not_called()

    def test_runtime_install_errors(self):
        with mock.patch.object(media, "fetch_json", side_effect=media.MediaSourceError("Could not reach api.github.com")):
            status, data = self.api("POST", "/api/media/runtime/install")
        self.assertEqual(status, 502)
        self.assertIn("api.github.com", data["error"])
        self.inject_module("backend.downloads", None)
        status, data = self.api("POST", "/api/media/runtime/install")
        self.assertEqual(status, 503)
        self.assertIn("downloads", data["error"].lower())

    def test_model_install_verified(self):
        sha = "cd" * 32

        def tree(url):
            self.assertTrue(url.startswith(media.HF_API_MODELS))
            if "city96" in url:
                return [{"type": "file", "path": "umt5-xxl-encoder-Q8_0.gguf", "size": 99,
                         "lfs": {"oid": sha, "size": 6_000_000_000}}]
            folder = url.rsplit("/tree/main/", 1)[1]
            name = {"split_files/diffusion_models": "wan2.1_t2v_1.3B_fp16.safetensors",
                    "split_files/vae": "wan_2.1_vae.safetensors"}[folder]
            return [{"type": "directory", "path": folder},
                    {"type": "file", "path": f"{folder}/{name}", "lfs": {"oid": sha, "size": 2000}}]

        for path in media.model_file_paths(media.CATALOG_BY_ID["wan2.1-t2v-1.3b"]).values():
            path.unlink()
        with mock.patch.object(media, "fetch_json", side_effect=tree):
            status, data = self.api("POST", "/api/media/models/wan2.1-t2v-1.3b/install")
        self.assertEqual(status, 202, data)
        self.assertEqual(len(data["downloads"]), 3)
        self.assertFalse(data["installed"])
        by_id = {c["id"]: c for c in self.downloads.started}
        te = by_id["media-model-wan2.1-t2v-1.3b-text-encoder"]
        self.assertEqual(te["url"], "https://huggingface.co/city96/umt5-xxl-encoder-gguf/resolve/main/umt5-xxl-encoder-Q8_0.gguf")
        self.assertEqual((te["sha256"], te["size"], te["kind"]), (sha, 6_000_000_000, "media-model"))
        self.assertEqual(te["dest"], self.home / "models" / "media" / "wan2.1-t2v-1.3b" / "umt5-xxl-encoder-Q8_0.gguf")
        dm = by_id["media-model-wan2.1-t2v-1.3b-diffusion-model"]
        self.assertTrue(dm["url"].endswith("/resolve/main/split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors"))
        _, status_data = self.api("GET", "/api/media/status")
        self.assertTrue(status_data["video_models"][0]["downloading"])

    def test_model_install_refuses_unverified(self):
        self.downloads.started.clear()
        unverified = [{"type": "file", "path": "v1-5-pruned-emaonly-fp16.safetensors", "size": 5,
                       "oid": "0123456789abcdef0123456789abcdef01234567"}]
        with mock.patch.object(media, "fetch_json", return_value=unverified):
            status, data = self.api("POST", "/api/media/models/sd15/install")
        self.assertEqual(status, 502)
        self.assertIn("unverified", data["error"])
        self.assertEqual(self.downloads.started, [])
        with mock.patch.object(media, "fetch_json", return_value=[]):
            status, data = self.api("POST", "/api/media/models/sd15/install")
        self.assertEqual(status, 502)
        self.assertIn("not found", data["error"])

    def test_installed_model_needs_no_download(self):
        status, data = self.api("POST", "/api/media/models/sd-turbo/install")
        self.assertEqual((status, data), (202, {"downloads": [], "installed": True}))


class SourceSafetyTests(unittest.TestCase):
    def test_metadata_hosts_allow_listed(self):
        for url in ("http://huggingface.co/api/models/a/b", "https://evil.com/x", "https://huggingface.co:8443/x",
                    "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(media.MediaSourceError):
                media.fetch_json(url)

    def test_hf_reference_validation(self):
        for repo, name in (("a/b", "../x"), ("../b", "x"), ("a", "x"), ("a/b", "/abs")):
            with self.subTest(repo=repo, name=name), self.assertRaises(media.MediaSourceError):
                media.hf_file_metadata(repo, name)

    def test_pick_runtime_asset(self):
        names = [
            "sd-master-1-bin-Darwin-macOS-15.7.1-arm64.zip", "sd-master-1-bin-Linux-Ubuntu-24.04-x86_64.zip",
            "sd-master-1-bin-Linux-Ubuntu-24.04-x86_64-rocm-6.4.zip", "sd-master-1-bin-win-cuda12-x64.zip",
            "sd-master-1-bin-win-cpu-x64.zip", "sd-master-1-bin-win-avx2-x64.zip",
        ]
        assets = [{"name": n} for n in names]
        self.assertEqual(media.pick_runtime_asset(assets, "darwin", "arm64")["name"], names[0])
        self.assertEqual(media.pick_runtime_asset(assets, "linux", "x86_64")["name"], names[1])
        self.assertEqual(media.pick_runtime_asset(assets, "windows", "x86_64")["name"], names[5])
        self.assertEqual(media.pick_runtime_asset(assets[:5], "windows", "x86_64")["name"], names[4])
        with self.assertRaises(media.MediaSourceError):
            media.pick_runtime_asset(assets, "linux", "arm64")
        with self.assertRaises(media.MediaSourceError):
            media.pick_runtime_asset([], "darwin", "arm64")

    def test_child_env_drops_secrets(self):
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "s3cret", "HF_TOKEN": "t", "KEEP_ME": "1"}):
            env = media._child_env()
        self.assertNotIn("LOWRAM_LLM_API_KEY", env)
        self.assertNotIn("HF_TOKEN", env)
        self.assertEqual(env["KEEP_ME"], "1")

    def test_sniff(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for data, ext in ((b"\x89PNG\r\n\x1a\nxxxx", "png"), (b"RIFF\0\0\0\0AVI LIST", "avi"),
                              (b"RIFF\0\0\0\0WEBPVP8 ", "webp"), (b"\x1a\x45\xdf\xa3\0\0", "webm"),
                              (b"\0\0\0\x18ftypisom", "mp4")):
                path = Path(tmp) / "f"
                path.write_bytes(data)
                self.assertEqual(media.sniff_media(path)[0], ext)
            path.write_bytes(b"<html>")
            self.assertIsNone(media.sniff_media(path))


if __name__ == "__main__":
    unittest.main()
