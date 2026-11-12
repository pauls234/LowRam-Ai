import os
import unittest
from unittest import mock

from backend import config, memory
from tests.helpers import GIB, MIB, LowRAMTestCase


class ValidatePercentTests(unittest.TestCase):
    def test_accepts_bounds_and_coercible_values(self):
        self.assertEqual(memory.validate_percent(20), 20)
        self.assertEqual(memory.validate_percent(80), 80)
        self.assertEqual(memory.validate_percent("40"), 40)
        self.assertEqual(memory.validate_percent(" 40 "), 40)
        self.assertEqual(memory.validate_percent(40.0), 40)

    def test_rejects_invalid_values(self):
        for bad in (19, 81, 0, -40, True, False, 40.5, "abc", "", "40.5", None, [40], {}):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    memory.validate_percent(bad)


class PlanTests(LowRAMTestCase):
    def test_default_plan_values(self):
        self.set_memory(total_gib=8, available_mib=4096)
        plan = memory.plan()
        self.assertEqual(plan.percent, 40)
        self.assertEqual(plan.total_bytes, 8 * GIB)
        self.assertEqual(plan.available_bytes, 4096 * MIB)
        self.assertAlmostEqual(plan.budget_bytes / GIB, 3.2, places=3)
        self.assertAlmostEqual(plan.context_chars, 6400, delta=1)
        self.assertAlmostEqual(plan.max_tokens, 256 + 3.2 * 96, delta=1)
        self.assertTrue(plan.can_run)
        self.assertEqual(plan.to_dict()["percent"], 40)

    def test_lower_clamps(self):
        self.set_memory(total_gib=1, available_mib=512)
        plan = memory.plan(20)
        self.assertEqual(plan.context_chars, memory.MIN_CONTEXT_CHARS)
        self.assertGreaterEqual(plan.max_tokens, memory.MIN_MAX_TOKENS)
        self.assertLess(plan.max_tokens, 300)

    def test_upper_clamps(self):
        self.set_memory(total_gib=256, available_mib=100000)
        plan = memory.plan(80)
        self.assertEqual(plan.context_chars, memory.MAX_CONTEXT_CHARS)
        self.assertEqual(plan.max_tokens, memory.MAX_MAX_TOKENS)

    def test_budget_grows_with_percent(self):
        low, high = memory.plan(20), memory.plan(80)
        self.assertLess(low.budget_bytes, high.budget_bytes)
        self.assertLessEqual(low.context_chars, high.context_chars)
        self.assertLessEqual(low.max_tokens, high.max_tokens)

    def test_invalid_percent_raises(self):
        with self.assertRaises(ValueError):
            memory.plan(19)

    def test_can_run_threshold_is_inclusive(self):
        min_free = 256 * MIB
        with mock.patch("backend.memory.psutil.virtual_memory") as vm:
            vm.return_value = mock.Mock(total=8 * GIB, available=min_free, percent=90.0)
            self.assertTrue(memory.plan().can_run)
            vm.return_value = mock.Mock(total=8 * GIB, available=min_free - 1, percent=90.0)
            plan = memory.plan()
            self.assertFalse(plan.can_run)
            self.assertIn("Paused", plan.reason)
            self.assertIn("LOWRAM_MIN_FREE_MB", plan.reason)

    def test_min_free_env_changes_decision(self):
        self.set_memory(total_gib=8, available_mib=2000)
        with mock.patch.dict(os.environ, {"LOWRAM_MIN_FREE_MB": "4096"}):
            self.assertFalse(memory.plan().can_run)
        with mock.patch.dict(os.environ, {"LOWRAM_MIN_FREE_MB": "0"}):
            self.assertTrue(memory.plan().can_run)

    def test_bad_env_raises_config_error(self):
        for bad in ("abc", "-1", "99999999999"):
            with self.subTest(value=bad), mock.patch.dict(os.environ, {"LOWRAM_MIN_FREE_MB": bad}):
                with self.assertRaises(config.ConfigError):
                    memory.plan()

    def test_blank_env_uses_default(self):
        with mock.patch.dict(os.environ, {"LOWRAM_MIN_FREE_MB": "  "}):
            self.assertEqual(config.min_free_bytes(), config.DEFAULT_MIN_FREE_MB * MIB)

    def test_bad_timeout_and_port_raise(self):
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_TIMEOUT": "0"}):
            with self.assertRaises(config.ConfigError):
                config.llm_timeout()
        with mock.patch.dict(os.environ, {"LOWRAM_PORT": "80"}):
            with self.assertRaises(config.ConfigError):
                config.port()

    def test_home_is_isolated(self):
        self.assertEqual(config.home_dir(), self.home)
        self.assertTrue(self.home.is_dir())


if __name__ == "__main__":
    unittest.main()
