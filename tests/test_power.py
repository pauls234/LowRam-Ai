"""power.py: keep-awake reference counting, battery pause, notifications, /api/power and the hooks."""

import contextlib
import os
import sys
import threading
import types
import unittest
from collections import namedtuple
from unittest import mock

from backend import app, db, media, power, runner, settings, worker
from tests.cluster_helpers import ClusterTestCase
from tests.helpers import LowRAMTestCase, wait_until
from tests.media_helpers import MediaTestCase
from tests.test_app import AppTestCase

Battery = namedtuple("Battery", "percent secsleft power_plugged")
LOW = "On battery at 15 % – long jobs pause below 20 %. Plug in to continue."
REAL_THERMAL_VALUE = power._thermal_value  # the tests' base class swaps it for "no sensor"


@contextlib.contextmanager
def only_module(name, module):
    """Put one entry into sys.modules and restore just that one (patch.dict would drop every import made meanwhile)."""
    missing = object()
    old = sys.modules.get(name, missing)
    sys.modules[name] = module
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


class FakeInhibitor:
    started = 0
    stopped = 0

    def __init__(self):
        FakeInhibitor.started += 1

    def stop(self):
        FakeInhibitor.stopped += 1


class PowerTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        power._MANAGER.release_all()
        power._MANAGER._linux_noted = False
        power._last_notified.clear()
        FakeInhibitor.started = FakeInhibitor.stopped = 0
        self.addCleanup(power._MANAGER.release_all)

    def platform(self, name):
        patcher = mock.patch("backend.power._platform", return_value=name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def battery(self, percent, plugged):
        value = None if percent is None else Battery(percent, 3600, plugged)
        patcher = mock.patch("backend.power.psutil.sensors_battery", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)


class KeepAwakeTests(PowerTestCase):
    def test_reference_counted_across_threads_on_macos(self):
        self.platform("darwin")
        entered, release = threading.Event(), threading.Event()

        def job():
            with power.keep_awake("media job"):
                entered.set()
                release.wait(5)

        with mock.patch("backend.power._Caffeinate", FakeInhibitor):
            with power.keep_awake("task loop"):
                thread = threading.Thread(target=job)
                thread.start()
                self.assertTrue(entered.wait(5))
                self.assertEqual(sorted(power.active_reasons()), ["media job", "task loop"])
                self.assertEqual(FakeInhibitor.started, 1, "one caffeinate for all holders")
                release.set()
                thread.join(5)
                self.assertEqual(power.active_reasons(), ["task loop"])
                self.assertEqual(FakeInhibitor.stopped, 0)
            self.assertEqual(power.active_reasons(), [])
            self.assertEqual(FakeInhibitor.stopped, 1)
            with power.keep_awake("again"):
                self.assertEqual(FakeInhibitor.started, 2)

    def test_caffeinate_command_is_tied_to_our_pid(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch("backend.power.subprocess.Popen", return_value=proc) as popen:
            inhibitor = power._Caffeinate()
            inhibitor.stop()
        argv = popen.call_args[0][0]
        self.assertTrue(argv[0].endswith("caffeinate"))
        self.assertEqual(argv[1:], ["-i", "-w", str(os.getpid())])
        self.assertNotIn("shell", popen.call_args[1])
        proc.terminate.assert_called_once()

    def test_windows_state_is_held_by_one_keeper_thread(self):
        self.platform("win32")
        calls = []
        done = threading.Event()

        def record(flags):
            calls.append((flags, threading.get_ident()))
            if flags == power.ES_CONTINUOUS:
                done.set()

        with mock.patch("backend.power._win_set_state", side_effect=record):
            with power.keep_awake("loop"), power.keep_awake("media"):
                self.assertTrue(wait_until(lambda: calls, 5))
            self.assertTrue(done.wait(5))
        self.assertEqual([flags for flags, _ in calls],
                         [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, power.ES_CONTINUOUS])
        self.assertEqual(calls[0][1], calls[1][1], "set and cleared on the same thread")
        self.assertNotEqual(calls[0][1], threading.get_ident())

    def test_linux_is_a_logged_noop(self):
        self.platform("linux")
        with self.assertLogs("lowram.power", "INFO") as logs:
            with power.keep_awake("a"):
                self.assertEqual(power.active_reasons(), ["a"])
            with power.keep_awake("b"):
                pass
        self.assertEqual(sum("not supported" in line for line in logs.output), 1)

    def test_disabled_setting_and_failures_never_break_the_job(self):
        self.platform("darwin")
        settings.update("power", {"keep_awake": False})
        with mock.patch("backend.power._Caffeinate", FakeInhibitor), power.keep_awake("x"):
            self.assertEqual(power.active_reasons(), [])
        self.assertEqual(FakeInhibitor.started, 0)
        settings.update("power", {"keep_awake": True})
        ran = []
        with mock.patch("backend.power._Caffeinate", side_effect=OSError("no caffeinate")), \
                self.assertLogs("lowram.power", "WARNING"):
            with power.keep_awake("x"):
                ran.append(True)
        self.assertEqual(ran, [True])

    def test_shutdown_hook_releases_everything(self):
        self.platform("darwin")
        with mock.patch("backend.power._Caffeinate", FakeInhibitor):
            with power.keep_awake("forgotten"):
                power._release_on_shutdown(None)
                self.assertEqual((power.active_reasons(), FakeInhibitor.stopped), ([], 1))


class HeatTests(PowerTestCase):
    """macOS' own heat judgement (NSProcessInfo.thermalState) pauses long jobs – the MacBook must not suffer."""

    def setUp(self):
        super().setUp()  # LowRAMTestCase: no real heat sensor, fresh heat cache
        self.platform("darwin")
        self.level = 0  # 0 nominal, 1 fair, 2 serious, 3 critical
        sensor = mock.patch("backend.power._thermal_value", side_effect=lambda: self.level)
        sensor.start()
        self.addCleanup(sensor.stop)
        for name, value in (("THERMAL_CACHE_S", 0.0), ("COOL_CHECK_S", 0.02)):
            patcher = mock.patch.object(power, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reads_macos_own_judgement(self):
        info = types.SimpleNamespace(thermalState=lambda: 2)
        fake = types.ModuleType("Foundation")
        fake.NSProcessInfo = types.SimpleNamespace(processInfo=lambda: info)
        with only_module("Foundation", fake):
            self.assertEqual(REAL_THERMAL_VALUE(), 2)
        with only_module("Foundation", None), self.assertRaises(ImportError):  # plain Python: no pyobjc
            REAL_THERMAL_VALUE()

    def test_states_and_reasons(self):
        for level, state in enumerate(power.THERMAL_STATES):
            self.level = level
            self.assertEqual(power.thermal_state(), state)
        self.level = 1
        self.assertIsNone(power.heat_block_reason())  # "fair" is normal under load
        self.level = 2
        self.assertIn("sehr warm", power.heat_block_reason())
        self.level = 3
        self.assertIn("überhitzt", power.heat_block_reason())
        settings.update("power", {"pause_when_hot": False})
        self.assertIsNone(power.heat_block_reason())
        self.assertEqual(power.status()["thermal"], "critical")

    def test_unknown_without_pyobjc_or_on_other_systems(self):
        with mock.patch("backend.power._thermal_value", side_effect=ImportError("No module named 'Foundation'")):
            self.assertIsNone(power.thermal_state())
        self.level = 7  # a value a future macOS might add
        self.assertIsNone(power.thermal_state())
        self.platform("linux")
        self.level = 3
        self.assertIsNone(power.thermal_state())
        self.assertIsNone(power.heat_block_reason())

    def test_wait_until_cool(self):
        self.level = 2
        seen = []

        def cools_down(message):
            seen.append(message)
            self.level = 1

        self.assertTrue(power.wait_until_cool(on_wait=cools_down, max_wait_s=5))
        self.assertEqual(len(seen), 1)
        self.level = 3
        self.assertFalse(power.wait_until_cool(max_wait_s=0.1))  # stays hot: give up after the limit
        stop = threading.Event()
        stop.set()
        self.assertFalse(power.wait_until_cool(stop, max_wait_s=5))

    def test_loop_pauses_while_hot_and_continues(self):
        pid = runner.create_project("p", "a\nb")
        seen = []

        def on_step(outcome):
            seen.append((outcome["status"], outcome["reason"]))
            if outcome["status"] == "done" and [status for status, _ in seen].count("done") == 1:
                self.level = 2  # gets very warm after the first step …
                threading.Timer(0.1, lambda: setattr(self, "level", 0)).start()  # … and cools down

        summary = runner.run_loop(pid, on_step=on_step)
        self.assertEqual((summary["stopped_because"], summary["completed"]), ("queue_empty", 2))
        waits = [reason for status, reason in seen if status == "waiting"]
        self.assertTrue(waits and "Abkühlpause" in waits[0], seen)

    def test_loop_stops_when_it_stays_hot(self):
        settings.update("power", {"cool_wait_minutes": 0.002})  # 0.12 s
        pid = runner.create_project("p", "a\nb")

        def on_step(outcome):
            if outcome["status"] == "done":
                self.level = 3

        summary = runner.run_loop(pid, on_step=on_step)
        self.assertEqual((summary["stopped_because"], summary["completed"]), ("too_hot", 1))
        self.assertEqual(summary["message"], "Der Mac blieb sehr warm – der Durchlauf wurde angehalten, damit er "
                                             "abkühlen kann. Du kannst ihn später fortsetzen.")  # no "0 Minuten"
        self.assertEqual([t["status"] for t in db.list_tasks(pid)], ["done", "pending"])  # nothing lost


class BatteryTests(PowerTestCase):
    def test_battery_state(self):
        self.battery(15.4, False)
        self.assertEqual(power.battery(), {"available": True, "percent": 15, "plugged": False})

    def test_no_battery_or_sensor_error(self):
        self.battery(None, None)
        self.assertEqual(power.battery(), {"available": False, "percent": None, "plugged": None})
        with mock.patch("backend.power.psutil.sensors_battery", side_effect=RuntimeError("sensor")):
            self.assertFalse(power.battery()["available"])
            self.assertIsNone(power.battery_block_reason())

    def test_block_reason(self):
        self.battery(15, False)
        self.assertEqual(power.battery_block_reason(), LOW)
        settings.update("power", {"battery_min_percent": 10})
        self.assertIsNone(power.battery_block_reason())
        settings.update("power", {"battery_min_percent": 0})  # never pause
        self.assertIsNone(power.battery_block_reason())

    def test_plugged_in_or_unknown_never_blocks(self):
        for plugged in (True, None):
            with mock.patch("backend.power.psutil.sensors_battery", return_value=Battery(5, 60, plugged)):
                self.assertIsNone(power.battery_block_reason())


class NotifyTests(PowerTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.delivered = threading.Event()

        def deliver(title, body):
            self.sent.append((title, body))
            self.delivered.set()

        patcher = mock.patch("backend.power._deliver", side_effect=deliver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_notify_cleans_caps_and_rate_limits_per_title(self):
        self.assertTrue(power.notify("Image\x1b[31m ready‮", "line1\nline2 " + "x" * 500))
        self.assertTrue(self.delivered.wait(5))
        title, body = self.sent[0]
        self.assertEqual(title, "Image [31m ready")
        self.assertTrue(body.startswith("line1 line2 x"))
        self.assertLessEqual(len(body), power.MAX_BODY_CHARS)
        self.assertFalse(power.notify("Image [31m ready", "again"), "same title within 5 s")
        self.assertTrue(power.notify("Other", "ok"))
        with mock.patch("backend.power.time.monotonic", return_value=10**9):
            self.assertTrue(power.notify("Other", "later"))

    def test_disabled_and_failures(self):
        settings.update("power", {"notifications": False})
        self.assertFalse(power.notify("T", "B"))
        settings.update("power", {"notifications": True})
        with mock.patch("backend.power.threading.Thread", side_effect=RuntimeError("no threads")), \
                self.assertLogs("lowram.power", "WARNING"):
            self.assertFalse(power.notify("T", "B"))


class NotificationCommandTests(unittest.TestCase):
    def test_macos_passes_texts_as_arguments(self):
        argv = power.notification_command('Done "x" & do shell script "rm"', "-e evil", "darwin")
        self.assertTrue(argv[0].endswith("osascript"))
        self.assertEqual(argv[1:7], ["-e", "on run argv", "-e",
                                     "display notification (item 2 of argv) with title (item 1 of argv)",
                                     "-e", "end run"])
        self.assertEqual(argv[7:], ['Done "x" & do shell script "rm"', " -e evil"])

    def test_other_platforms(self):
        with mock.patch("backend.power.shutil.which", return_value="/usr/bin/notify-send"):
            self.assertEqual(power.notification_command("T", "-b", "linux"),
                             ["/usr/bin/notify-send", "--app-name=LowRAM AI", "--", "T", "-b"])
        with mock.patch("backend.power.shutil.which", return_value=None):
            self.assertIsNone(power.notification_command("T", "B", "linux"))
        self.assertIsNone(power.notification_command("T", "B", "win32"))

    def test_deliver_never_raises(self):
        with mock.patch("backend.power.notification_command", return_value=["/nonexistent/osascript"]), \
                self.assertLogs("lowram.power", "WARNING"):
            power._deliver("T", "B")


class ApiTests(AppTestCase):
    def test_get_power(self):
        self.assertIn("power", app.LOADED_FEATURES)
        with mock.patch("backend.power.psutil.sensors_battery", return_value=Battery(80, 1000, True)):
            status, data = self.api("GET", "/api/power")
        self.assertEqual(status, 200)
        self.assertEqual(data["battery"], {"available": True, "percent": 80, "plugged": True})
        self.assertEqual(data["keep_awake"], {"enabled": True, "active": []})
        self.assertEqual(data["settings"], settings.DEFAULTS["power"])


class LoopIntegrationTests(PowerTestCase):
    def test_loop_stops_on_battery_between_tasks_and_keeps_awake(self):
        pid = runner.create_project("p", "one\ntwo\nthree")
        seen = []

        def check():
            seen.append(power.active_reasons())
            return LOW if len(seen) > 1 else None

        with mock.patch("backend.power.battery_block_reason", side_effect=check):
            summary = runner.run_loop(pid, 40, 10)
        self.assertEqual((summary["stopped_because"], summary["message"], summary["completed"]), ("on_battery", LOW, 1))
        self.assertIn("on_battery", runner.STOP_REASONS)
        self.assertEqual(seen[0], ["task loop"])
        self.assertEqual(power.active_reasons(), [])
        self.assertEqual(db.list_tasks(pid)[1]["status"], "pending", "the next task is left for later")

    def test_dashboard_loop_notifies_when_finished(self):
        pid = runner.create_project("p", "one\ntwo")
        controller = app.LoopController()
        with mock.patch("backend.power.notify") as notify:
            controller.start(pid, 40, 5)
            self.assertTrue(wait_until(lambda: not controller.is_active() and notify.called, 15))
        notify.assert_called_once_with("Task queue finished", "2 done, 0 failed.")

    def test_dashboard_loop_without_tasks_does_not_notify(self):
        pid = runner.create_project("p", "one")
        runner.run_loop(pid, 40, 5)  # nothing is pending afterwards
        controller = app.LoopController()
        with mock.patch("backend.power.notify") as notify:
            controller.start(pid, 40, 5)
            self.assertTrue(wait_until(lambda: not controller.is_active(), 15))
        notify.assert_not_called()


class MediaIntegrationTests(MediaTestCase):
    def test_job_waits_on_battery_then_runs_awake_and_notifies(self):
        state = {"low": True}
        held = []

        @contextlib.contextmanager
        def recording_keep_awake(reason):
            held.append(reason)
            yield

        with mock.patch("backend.power.battery_block_reason", side_effect=lambda: LOW if state["low"] else None), \
                mock.patch("backend.power.keep_awake", side_effect=recording_keep_awake), \
                mock.patch("backend.power.notify") as notify, \
                mock.patch("backend.media.BATTERY_RETRY_S", 0.05):
            job = media.submit_job({"kind": "image", "prompt": "a lovely cat"})
            self.assertTrue(wait_until(lambda: media.get_job(job["id"])["message"] == LOW, 10))
            self.assertEqual(media.get_job(job["id"])["status"], "queued")
            state["low"] = False
            done = self.wait_status(job["id"])
            self.assertEqual(done["status"], "done", done)
            self.assertTrue(wait_until(lambda: notify.called, 5))
        self.assertEqual(held, [f"image generation #{job['id']}"])
        title, body = notify.call_args[0]
        self.assertEqual(title, "Image ready")
        self.assertIn("a lovely cat", body)

    def test_failed_job_notifies(self):
        with mock.patch("backend.power.notify") as notify:
            job = self.wait_status(media.submit_job({"kind": "image", "prompt": "FAIL please"})["id"])
            self.assertEqual(job["status"], "failed")
            self.assertTrue(wait_until(lambda: notify.called, 5))
        self.assertEqual(notify.call_args[0][0], "Image failed")


class WorkerBatteryTests(ClusterTestCase):
    def test_no_claims_while_on_low_battery(self):
        self.enable()
        state = {"low": True}
        with mock.patch("backend.power.battery_block_reason", side_effect=lambda: LOW if state["low"] else None), \
                mock.patch.object(worker.Worker, "_claim", return_value=None) as claim:
            w = self.make_worker(self.new_code(), "laptop")
            self.start_worker(w)
            self.wait(lambda: w.status().get("message") == LOW, message=str(w.status()))
            self.assertEqual(claim.call_count, 0)
            state["low"] = False
            self.wait(lambda: claim.call_count > 0, message="claims resume when plugged in")


if __name__ == "__main__":
    unittest.main()
