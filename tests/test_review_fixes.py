#!/usr/bin/env python3
"""Regression tests for four defects found reviewing the first implementation.

Each of these passed the original test suite because those tests checked what the code
did, not what it should do. They are written here as the behavior, not the mechanism, so
a future refactor cannot quietly reintroduce the defect.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ENGINE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ENGINE_ROOT / "scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fast_path = load_module("fast_path")
result_cache = load_module("result_cache")
local_producer = load_module("local_producer")
task_signals = load_module("task_signals")

LOCAL_ENV = {
    local_producer.BASE_URL_ENV: "http://127.0.0.1:9/v1",
    local_producer.MODEL_ENV: "local-small",
}


class GateIndependenceTests(unittest.TestCase):
    """Defect 1: the gate crashed when an unrelated file was corrupt.

    The gate is the first thing every task runs. It must answer from the prompt alone.
    """

    def test_task_signals_imports_nothing_but_stdlib(self):
        source = (SCRIPTS / "task_signals.py").read_text(encoding="utf-8")
        for forbidden in ("import model_registry", "routing_policy", "obsidian_model_memory", "model_execution_receipt"):
            self.assertNotIn(forbidden, source, "the signal module must stay dependency-free")

    def test_gate_answers_when_the_model_ladder_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = Path(temporary) / "engine"
            shutil.copytree(ENGINE_ROOT, engine, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            (engine / "assets" / "model-capability-ladder.json").write_text("{broken", encoding="utf-8")

            process = subprocess.run(
                [sys.executable, str(engine / "scripts" / "fast_path.py"), "evaluate", "fix a typo in a.py"],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(process.returncode, 0, process.stderr)
        decision = json.loads(process.stdout)
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["complexity_score"], 10)

    def test_gate_loads_far_fewer_modules_than_the_routing_stack(self):
        before = set(sys.modules)
        load_module("fast_path")
        loaded = set(sys.modules) - before
        heavy = {name for name in loaded if "dispatcher" in name or "receipt" in name or "routing_policy" in name}
        self.assertEqual(heavy, set(), f"the gate pulled in routing internals: {sorted(heavy)}")

    def test_suppression_still_round_trips(self):
        # The laziness that fixed the import weight must not break the feature it guards.
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "events.jsonl"
            prompt = "fix the typo in a.py"
            self.assertTrue(fast_path.evaluate(prompt, ledger=ledger)["eligible"])

            event = fast_path.record(prompt, outcome="corrected", ledger=ledger)
            self.assertIsNotNone(event["capability_fingerprint"], "a record without a fingerprint is inert")

            after = fast_path.evaluate(prompt, ledger=ledger)
            self.assertFalse(after["eligible"])
            self.assertIn("fingerprint_suppressed_by_prior_correction", after["reasons"])

    def test_signal_definitions_are_shared_not_copied(self):
        # Identity comparison would not work here (each loader creates its own module
        # object), so assert the property that actually matters: the runner no longer
        # carries its own copy, and its re-exports agree with the canonical ones.
        runner_source = (SCRIPTS / "obsidian_adaptive_model_runner.py").read_text(encoding="utf-8")
        for name in ("infer_complexity_score", "infer_task_type", "infer_operation"):
            with self.subTest(function=name):
                self.assertNotIn(f"def {name}(", runner_source, "the runner reintroduced a local copy")

        runner = load_module("obsidian_adaptive_model_runner")
        for prompt in ("fix the typo in a.py", "what does f return?", "migrate the schema across services"):
            with self.subTest(prompt=prompt):
                self.assertEqual(runner.infer_complexity_score(prompt), task_signals.infer_complexity_score(prompt))
                self.assertEqual(runner.infer_task_type(prompt), task_signals.infer_task_type(prompt))
                self.assertEqual(runner.infer_operation(prompt), task_signals.infer_operation(prompt))


class CacheHitRateTests(unittest.TestCase):
    """Defect 2: hit_rate used `entries + hits` as the denominator, so misses that never
    led to a store were invisible. The keep/drop decision is made on this number."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.cache = Path(self._temp.name) / "cache.sqlite"
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_hits_reports_one_hundred_percent(self):
        result_cache.store("q", "a", [], "m|low", 100, cache_path=self.cache)
        result_cache.lookup("q", [], "m|low", cache_path=self.cache)
        result_cache.lookup("q", [], "m|low", cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual(report["hit_rate"], 1.0)
        self.assertEqual((report["hits"], report["misses"]), (2, 0))

    def test_all_misses_report_zero_percent(self):
        for index in range(5):
            result_cache.lookup(f"never-{index}", [], "m|low", cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual(report["hit_rate"], 0.0)
        self.assertEqual((report["hits"], report["misses"]), (0, 5))

    def test_mixed_traffic_reports_the_real_ratio(self):
        result_cache.store("q", "a", [], "m|low", 100, cache_path=self.cache)
        result_cache.lookup("q", [], "m|low", cache_path=self.cache)
        result_cache.lookup("x", [], "m|low", cache_path=self.cache)
        result_cache.lookup("y", [], "m|low", cache_path=self.cache)
        self.assertEqual(result_cache.stats(cache_path=self.cache)["hit_rate"], 0.3333)

    def test_storing_entries_does_not_inflate_the_denominator(self):
        for index in range(20):
            result_cache.store(f"q{index}", "a", [], "m|low", 10, cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual(report["lookups"], 0)
        self.assertEqual(report["hit_rate"], 0.0)

    def test_expired_entry_counts_as_a_miss(self):
        result_cache.store("q", "a", [], "m|low", 10, cache_path=self.cache)
        result_cache.lookup("q", [], "m|low", cache_path=self.cache, max_age_days=0)
        self.assertEqual(result_cache.stats(cache_path=self.cache)["misses"], 1)

    def test_unreadable_input_is_not_counted_either_way(self):
        result_cache.lookup("q", [Path(self._temp.name) / "absent.py"], "m|low", cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual(report["lookups"], 0)

    def test_purge_clears_the_lookup_ledger(self):
        result_cache.store("q", "a", [], "m|low", 10, cache_path=self.cache)
        result_cache.lookup("q", [], "m|low", cache_path=self.cache)
        result_cache.purge(cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual((report["hits"], report["misses"]), (0, 0))


class LocalExecutorFidelityTests(unittest.TestCase):
    """Defect 3: the local executor silently rewrote the requested pair to its own and
    dropped allow_fallback, producing a receipt for a pair that never ran."""

    def args(self, **overrides):
        defaults = {
            "model": "local-small",
            "effort": "low",
            "codex_bin": "codex",
            "sandbox": "read-only",
            "ignore_user_config": False,
            "workload_id": "w",
            "timeout": 5,
            "workdir": "/tmp",
            "allow_fallback": [],
            "entry_task": False,
            "state_db": Path("/tmp/state.sqlite"),
            "result_output": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def run_receipt(self, **overrides):
        receipt_module = load_module("model_execution_receipt")
        with patch.dict(os.environ, LOCAL_ENV, clear=False):
            return receipt_module.run_receipt(self.args(**overrides), "hello")

    def test_unsupported_effort_is_refused_not_substituted(self):
        receipt = self.run_receipt(effort="high")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "execution")
        # The receipt names what was asked for, never a quietly rewritten pair.
        self.assertEqual(receipt["requested_pair"], "local-small|high")
        self.assertIsNone(receipt["effective_pair"])
        self.assertIn("Refusing", receipt["error_message"])

    def test_fallback_list_is_refused_rather_than_dropped(self):
        receipt = self.run_receipt(allow_fallback=["gpt-5.6-luna|low"])
        self.assertEqual(receipt["status"], "fail")
        self.assertIn("fallback", receipt["error_message"].lower())

    def test_supported_effort_still_executes(self):
        receipt = self.run_receipt()
        self.assertEqual(receipt["requested_pair"], "local-small|low")
        # The endpoint is unreachable in tests, so this is an operational failure -- the
        # point is that it reached execution rather than being refused up front.
        self.assertIn("unreachable", receipt["error_message"])

    def test_refusal_is_operational_so_the_caller_can_fall_through(self):
        # A quality verdict would poison the ladder for a model that never produced text.
        for receipt in (self.run_receipt(effort="high"), self.run_receipt(allow_fallback=["gpt-5.6-luna|low"])):
            with self.subTest(message=receipt["error_message"][:40]):
                self.assertEqual(receipt["failure_class"], "execution")
                self.assertTrue(receipt["outside_quality_ladder"])


class DocumentedScopeTests(unittest.TestCase):
    """Defect 4: docs overstated both suppression granularity and backend independence."""

    def test_fingerprint_is_documented_as_a_bucket(self):
        source = (SCRIPTS / "fast_path.py").read_text(encoding="utf-8")
        self.assertIn("bucket", source.lower())

    def test_distinct_tasks_of_one_shape_share_a_fingerprint(self):
        # Documenting it is only meaningful if it is actually true.
        first = fast_path.capability_fingerprint("fix the typo in a.py")
        second = fast_path.capability_fingerprint("fix the label in d.py")
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_no_backend_registry_implies_pluggability_that_does_not_exist(self):
        # The half-finished registry was removed rather than left advertising a seam that
        # only worked for the in-process local endpoint. If it comes back, the receipt
        # reconstruction below the command must come with it.
        self.assertFalse((SCRIPTS / "executors.py").exists())

    def test_codex_command_and_its_receipt_reconstruction_stay_together(self):
        source = (SCRIPTS / "model_execution_receipt.py").read_text(encoding="utf-8")
        for marker in ('"exec"', "parse_stdout_events", "read_thread_state", "parse_rollout_allowlist"):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_local_dispatch_is_by_configured_identity_not_name(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(local_producer.handles_model("local-small"))
        with patch.dict(os.environ, LOCAL_ENV, clear=True):
            self.assertTrue(local_producer.handles_model("local-small"))
            self.assertFalse(local_producer.handles_model("gpt-5.6-sol"))
            self.assertFalse(local_producer.handles_model(None))


if __name__ == "__main__":
    unittest.main()
