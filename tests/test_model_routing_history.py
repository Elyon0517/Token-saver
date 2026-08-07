#!/usr/bin/env python3
import importlib.util
import json
import io
import multiprocessing
from copy import deepcopy
import os
import sys
import stat
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "model_routing_history.py"
MODULE_SPEC = importlib.util.spec_from_file_location("model_routing_history", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


CONDITION = {"task_family": "code", "artifact": "script", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "owning_skill": "code-skill", "project_family": "global", "verification_shape": "real", "execution_domain": "python"}
SUMMARY = "Implement a compact verified routing-history behavior test."
LADDER = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-terra|low"]
FULL_SOL_LADDER = [
    "gpt-5.3-codex-spark|low",
    "gpt-5.3-codex-spark|medium",
    "gpt-5.3-codex-spark|high",
    "gpt-5.3-codex-spark|xhigh",
    "gpt-5.6-luna|low",
    "gpt-5.6-luna|medium",
    "gpt-5.6-luna|high",
    "gpt-5.6-luna|xhigh",
    "gpt-5.6-luna|max",
    "gpt-5.6-terra|low",
    "gpt-5.6-terra|medium",
    "gpt-5.6-terra|high",
    "gpt-5.6-terra|xhigh",
    "gpt-5.6-terra|max",
    "gpt-5.6-terra|ultra",
    "gpt-5.6-sol|low",
    "gpt-5.6-sol|medium",
    "gpt-5.6-sol|high",
    "gpt-5.6-sol|xhigh",
    "gpt-5.6-sol|max",
    "gpt-5.6-sol|ultra",
]


class FileLockPlatformTests(unittest.TestCase):
    def test_windows_file_lock_uses_msvcrt_and_releases_same_byte(self):
        lock_handle = Mock()
        lock_handle.tell.return_value = 0
        windows_lock = Mock()
        windows_lock.LK_LOCK = 1
        windows_lock.LK_UNLCK = 2
        with patch.object(module.os, "name", "nt"), patch.object(module, "msvcrt", windows_lock, create=True):
            module._acquire_file_lock(lock_handle)
            module._release_file_lock(lock_handle)
        lock_handle.write.assert_called_once_with("\0")
        self.assertEqual(windows_lock.locking.call_args_list, [call(lock_handle.fileno(), windows_lock.LK_LOCK, 1), call(lock_handle.fileno(), windows_lock.LK_UNLCK, 1)])


def arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-one"):
    return SimpleNamespace(
        **CONDITION,
        task_summary=SUMMARY,
        candidate_ladder=LADDER,
        static_suggestion="gpt-5.6-luna|low",
        hard_floor="gpt-5.3-codex-spark|low",
        history=history,
        receipt=receipt,
        verify_level=verify_level,
        verify_status=verify_status,
        failure_class=failure_class,
        run_id=run_id,
        trial=False,
    )


def write_receipt(path, model="gpt-5.6-luna", effort="low", status="pass", turn_completed=None, total_tokens=12, process_elapsed_ms=5, route_attempts=None, workload_prompt_sha256="a" * 64):
    if turn_completed is None:
        turn_completed = status == "pass"
    payload = {"requested_model": model, "requested_effort": effort, "resolved_model": model, "resolved_effort": effort, "effective_model": model, "status": status, "turn_completed": turn_completed, "model_match": True, "effort_match": True, "tokens": {"total_tokens": total_tokens}, "process_elapsed_ms": process_elapsed_ms, "workload_prompt_sha256": workload_prompt_sha256}
    if route_attempts is not None:
        payload["route_attempts"] = route_attempts
    path.write_text(json.dumps(payload), encoding="utf-8")


def route_attempt_fail(model="gpt-5.3-codex-spark", effort="low", failure_class="execution"):
    return [{"status": "fail", "failure_class": failure_class, "requested_model": model, "requested_effort": effort, "resolved_model": model, "resolved_effort": effort, "effective_model": model, "effective_effort": effort, "executed_model": model, "executed_effort": effort}]


def legacy_task(run_id, pair, *, mini_status="unknown", real_status="unknown", failure_class="none", receipt_status="pass", turn_completed=True, model_match=True, effort_match=True):
    return {"run_id": run_id, "summary": "Legacy task content must remain untouched.", "requested_pair": pair, "resolved_pair": pair, "effective_pair": pair, "executed_pair": pair, "operational_failure_pairs": [], "receipt_status": receipt_status, "mini_status": mini_status, "real_status": real_status, "effective_verdict": "legacy-raw-value", "allowlisted_failure_class": failure_class, "turn_completed": turn_completed, "model_match": model_match, "effort_match": effort_match, "trial": True, "workload_prompt_sha256": "b" * 64, "token_totals": {"input": 101, "cached_input": 7, "output": 13, "reasoning_output": 3, "total": 114}, "process_ms": 9876, "recorded_at": "2026-07-10T00:00:00+00:00"}


def legacy_record(condition, tasks, *, success_model=None, failed_model=None, calibration_state="frozen", best_pair="gpt-5.6-luna|low"):
    return {"condition": condition, "summary": "Legacy profile must remain read-only.", "candidate_ladder": list(LADDER), "static_suggestion": "gpt-5.6-luna|low", "hard_floor": "gpt-5.3-codex-spark|low", "success_model": success_model, "failed_model": failed_model, "active_ladder_fingerprint": "legacy-ladder", "profile_fingerprint": "legacy-profile", "calibration_state": calibration_state, "best_pair": best_pair, "selection_basis": "receipt_cost", "cost_evidence": {"status": "like_for_like", "compared_pairs": list(LADDER), "shared_cohort_count": 9, "shared_cohort_digest": "c" * 64, "scores": {"gpt-5.6-luna|low": {"median_total_tokens": 114, "median_process_ms": 9876}}}, "tasks": tasks}


def concurrent_record(history, receipt, number):
    module.record_event(arguments(Path(history), Path(receipt), run_id=f"run-{number}"))


def parse_profile_args(argv):
    original_argv = sys.argv[:]
    try:
        sys.argv = ["task-analyze-skill"] + argv
        return module.parse_args()
    finally:
        sys.argv = original_argv


class ModelRoutingHistoryTests(unittest.TestCase):
    def test_default_local_model_experience_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            history.write_text('{"sentinel":true}', encoding="utf-8")
            receipt = root / "receipt.json"
            write_receipt(receipt)
            original_default = module.DEFAULT_HISTORY_PATH
            module.DEFAULT_HISTORY_PATH = history
            try:
                before = history.read_bytes()
                with self.assertRaisesRegex(ValueError, "read-only"):
                    module.recommend_route(arguments(history, receipt))
                with self.assertRaisesRegex(ValueError, "read-only"):
                    module.record_event(arguments(history, receipt))
                module.load_history(history)
                module.status(history)
                self.assertEqual(history.read_bytes(), before)
                history.unlink()
                self.assertEqual(module.status(history), {"schema_version": 3, "conditions": 0, "tasks": 0})
                self.assertFalse(history.exists())
            finally:
                module.DEFAULT_HISTORY_PATH = original_default

    @contextmanager
    def _with_rust_domain(self, owner="code-skill", spark_first=True, language_alias="rust"):
        original_domains = deepcopy(module.EXECUTION_DOMAINS)
        rust_reference_path = "code-skill/references/rust-small-code.md"
        original_control = module.CONTROL_ENUMS["execution_domain"]
        try:
            module.EXECUTION_DOMAINS["rust"] = {
                "display_name": "Rust",
                "kind": "code",
                "language_aliases": [language_alias],
                "owner_skill": owner,
                "owner_enforced": True,
                "spark_first": spark_first,
                "reference_path": rust_reference_path,
                "active": True,
                "history_only": False,
            }
            if "execution_domain" in module.CONTROL_ENUMS:
                module.CONTROL_ENUMS["execution_domain"] = set(module.EXECUTION_DOMAINS.keys())
            yield
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original_domains)
            module.CONTROL_ENUMS["execution_domain"] = original_control

    @contextmanager
    def _with_inactive_domain(self, domain_name="legacy_inactive"):
        original_domains = deepcopy(module.EXECUTION_DOMAINS)
        original_control = module.CONTROL_ENUMS["execution_domain"]
        try:
            module.EXECUTION_DOMAINS[domain_name] = {
                "display_name": "Legacy Inactive",
                "kind": "code",
                "language_aliases": [domain_name],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": False,
                "reference_path": "code-skill/references/legacy-inactive.md",
                "active": False,
                "history_only": False,
            }
            if "execution_domain" in module.CONTROL_ENUMS:
                module.CONTROL_ENUMS["execution_domain"] = set(module.EXECUTION_DOMAINS.keys())
            yield
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original_domains)
            module.CONTROL_ENUMS["execution_domain"] = original_control

    def test_validate_condition_normalizes_inactive_domain_with_trimming(self):
        with self._with_inactive_domain():
            condition = module.validate_condition(dict(CONDITION, execution_domain="  code_unspecified  "), allow_history_only=True)
            self.assertEqual(condition["execution_domain"], "code_unspecified")

    def test_validate_condition_accepts_canonical_plugin_owning_skill(self):
        condition = module.validate_condition(dict(CONDITION, owning_skill="build-web-apps:frontend-app-builder", execution_domain="general"))
        self.assertEqual(condition["owning_skill"], "build-web-apps:frontend-app-builder")

    def test_validate_condition_rejects_unknown_domain_without_crashing(self):
        with self.assertRaises(ValueError) as error:
            module.validate_condition(dict(CONDITION, execution_domain="definitely_unknown_domain"))
        self.assertIn("execution_domain is unknown", str(error.exception))

    def test_validate_condition_accepts_synthetic_rust_domain(self):
        with self._with_rust_domain():
            self.assertEqual(module.validate_condition(dict(CONDITION, execution_domain="rust"))["execution_domain"], "rust")

    def test_bootstrap_preserves_legacy_and_private_summary_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "events.jsonl"
            source = json.dumps({"requested_model": "gpt-5.6-luna", "requested_effort": "low", "receipt_status": "pass", "verify_level": "mini", "verify_status": "pass"}) + "\n"
            legacy.write_text(source, encoding="utf-8")
            history = root / "model_experience.json"
            loaded = module.load_history(history)
            self.assertEqual(legacy.read_text(encoding="utf-8"), source)
            self.assertEqual(loaded["schema_version"], 3)
            self.assertEqual(stat.S_IMODE(history.stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                module.validate_summary("Read /private/token.txt and api_key=secret now.")

    def test_status_creates_private_ledger_when_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            self.assertFalse(history.exists())
            status = module.status(history)
            self.assertTrue(history.exists())
            self.assertEqual(status["schema_version"], module.SCHEMA_VERSION)
            self.assertEqual(status["conditions"], 0)
            self.assertEqual(status["tasks"], 0)
            loaded = module.load_history(history)
            self.assertEqual(loaded["schema_version"], module.SCHEMA_VERSION)
            self.assertEqual(stat.S_IMODE(history.stat().st_mode), 0o600)

    def test_validate_condition_rejects_inactive_domain_in_new_profiles(self):
        with self._with_inactive_domain():
            with self.assertRaises(ValueError):
                module.validate_condition(dict(CONDITION, execution_domain="legacy_inactive"))

    def test_validate_condition_allows_inactive_domain_for_history_records(self):
        with self._with_inactive_domain():
            condition = module.validate_condition(
                dict(CONDITION, execution_domain="legacy_inactive"),
                allow_history_only=True,
            )
        self.assertEqual(condition["execution_domain"], "legacy_inactive")

    def test_condition_identity_ignores_summary_and_effort_precedes_model(self):
        self.assertEqual([module.pair_text(*pair) for pair in module.canonical_pairs(["gpt-5.6-terra|xhigh", "gpt-5.3-codex-spark|medium", "gpt-5.6-luna|low", "gpt-5.6-luna|max", "gpt-5.6-sol|xhigh", "gpt-5.6-terra|medium", "gpt-5.6-sol|max"])], ["gpt-5.3-codex-spark|medium", "gpt-5.6-luna|low", "gpt-5.6-luna|max", "gpt-5.6-terra|medium", "gpt-5.6-terra|xhigh", "gpt-5.6-sol|xhigh", "gpt-5.6-sol|max"])
        self.assertEqual(module.condition_key(CONDITION), module.condition_key(dict(CONDITION)))

    def test_cross_model_failure_promotes_within_model_before_moving_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|max", "gpt-5.6-terra|xhigh", "gpt-5.6-terra|max", "gpt-5.6-sol|xhigh", "gpt-5.6-sol|max"]
            args = arguments(root / "history.json", receipt, run_id="run-pass")
            args.candidate_ladder = candidate_ladder
            args.static_suggestion = "gpt-5.6-luna|low"
            write_receipt(receipt, "gpt-5.6-sol", "max", "pass")
            module.record_event(args)
            write_receipt(receipt, "gpt-5.6-sol", "xhigh", "pass")
            failure = arguments(root / "history.json", receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-fail")
            failure.candidate_ladder = candidate_ladder
            failure.static_suggestion = "gpt-5.6-luna|low"
            module.record_event(failure)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-sol|max")

    def test_static_tiny_success_failure_and_runtime_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            write_receipt(receipt, "gpt-5.6-luna")
            args = arguments(root / "history.json", receipt)
            self.assertEqual(module.recommend_route(args)["selected_pair"], "gpt-5.6-luna|low")
            tiny_condition = dict(CONDITION, task_family="tiny_code")
            tiny = SimpleNamespace(**tiny_condition, task_summary=SUMMARY, candidate_ladder=LADDER, static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.3-codex-spark|low", history=root / "tiny.json")
            self.assertEqual(module.recommend_route(tiny)["selected_pair"], "gpt-5.3-codex-spark|low")
            module.record_event(arguments(root / "history.json", receipt, "real", "fail", "quality", "run-fail"))
            self.assertEqual(module.recommend_route(args)["selected_pair"], "gpt-5.6-terra|low")
            module.record_event(arguments(root / "history.json", receipt, "real", "pass", "none", "run-success"))
            self.assertEqual(module.recommend_route(args)["selected_pair"], "gpt-5.6-terra|low")
            module.record_event(arguments(root / "history.json", receipt, "real", "fail", "quality", "run-success"))
            self.assertEqual(module.recommend_route(args)["failed_model"], "gpt-5.6-luna|low")
            module.record_event(arguments(root / "history.json", receipt, "real", "pass", "none", "run-boundary"))
            module.record_event(arguments(root / "history.json", receipt, "real", "fail", "quality", "run-boundary"))
            self.assertEqual(module.recommend_route(args)["failed_model"], "gpt-5.6-luna|low")

    def test_quality_failure_is_sticky_within_one_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            write_receipt(receipt, "gpt-5.6-luna")

            module.record_event(arguments(history, receipt, "real", "fail", "quality", "run-first"))
            module.record_event(arguments(history, receipt, "real", "pass", "none", "run-first"))
            first_record = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][0]
            self.assertEqual(module.task_verdict(first_record), "fail")

            module.record_event(arguments(history, receipt, "real", "pass", "none", "run-real"))
            module.record_event(arguments(history, receipt, "real", "fail", "quality", "run-real"))
            module.record_event(arguments(history, receipt, "real", "pass", "none", "run-real"))
            real_record = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][1]
            self.assertEqual(real_record["real_status"], "fail")
            self.assertEqual(module.task_verdict(real_record), "fail")

    def test_merged_and_narrowed_same_condition_ladders_preserve_historical_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            initial_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.3-codex-spark|medium", "gpt-5.6-luna|low", "gpt-5.6-luna|medium"]
            initial = SimpleNamespace(**CONDITION, task_summary=SUMMARY, candidate_ladder=initial_ladder, static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.3-codex-spark|low", history=history, receipt=receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-fail", trial=False)
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", total_tokens=88, process_elapsed_ms=11)
            module.record_event(initial)
            recovery = SimpleNamespace(**CONDITION, task_summary=SUMMARY, candidate_ladder=initial_ladder, static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.3-codex-spark|low", history=history, receipt=receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-pass", trial=False)
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=64, process_elapsed_ms=9)
            module.record_event(recovery)
            merged_key = module.condition_key(CONDITION)
            merged = module.load_history(history)["conditions"][merged_key]
            self.assertEqual(merged["failed_model"], "gpt-5.3-codex-spark|low")
            self.assertEqual(merged["success_model"], "gpt-5.6-luna|low")
            narrowed = SimpleNamespace(**CONDITION, task_summary=SUMMARY, candidate_ladder=["gpt-5.6-luna|low", "gpt-5.6-terra|low"], static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.6-luna|low", history=history, receipt=receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-narrow", trial=False)
            write_receipt(receipt, "gpt-5.6-terra", "low", total_tokens=72, process_elapsed_ms=13)
            module.record_event(narrowed)
            narrowed_record = module.load_history(history)["conditions"][merged_key]
            self.assertEqual(narrowed_record["failed_model"], "gpt-5.3-codex-spark|low")
            self.assertEqual(narrowed_record["success_model"], "gpt-5.6-luna|low")
            self.assertIn("gpt-5.3-codex-spark|low", narrowed_record["candidate_ladder"])

    def test_top_quality_failure_exhausts_with_no_selected_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            args = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-top")
            args.candidate_ladder = ["gpt-5.6-sol|ultra"]
            args.static_suggestion = "gpt-5.6-sol|ultra"
            args.hard_floor = "gpt-5.6-sol|ultra"
            write_receipt(receipt, "gpt-5.6-sol", "ultra", total_tokens=40, process_elapsed_ms=20)
            module.record_event(args)
            recommendation = module.recommend_route(args)
            self.assertIsNone(recommendation["selected_pair"])
            self.assertEqual(recommendation["reason"], "quality_failure_boundary_exhausted")
            self.assertEqual(recommendation["failed_model"], "gpt-5.6-sol|ultra")

    def test_route_attempts_runtime_history_keeps_tiny_spark_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            tiny_condition = dict(CONDITION, task_family="tiny_code")
            args = SimpleNamespace(**tiny_condition, task_summary=SUMMARY, candidate_ladder=LADDER, static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.3-codex-spark|low", history=history, receipt=receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-tiny-route", trial=False)
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", total_tokens=16, process_elapsed_ms=7, route_attempts=route_attempt_fail())
            module.record_event(args)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertIsNone(recommendation["failed_model"])
            self.assertFalse(recommendation["trial"])

    def test_cold_start_never_selects_below_hard_floor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"

            normal = arguments(root / "normal.json", receipt)
            normal.candidate_ladder = ["gpt-5.6-luna|low", "gpt-5.6-terra|low", "gpt-5.6-terra|medium"]
            normal.static_suggestion = "gpt-5.6-luna|low"
            normal.hard_floor = "gpt-5.6-terra|medium"
            self.assertEqual(module.recommend_route(normal)["selected_pair"], "gpt-5.6-terra|medium")

            tiny = arguments(root / "tiny.json", receipt)
            tiny.task_family = "tiny_code"
            tiny.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-terra|low"]
            tiny.static_suggestion = "gpt-5.6-luna|low"
            tiny.hard_floor = "gpt-5.6-luna|low"
            recommendation = module.recommend_route(tiny)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertNotEqual(recommendation["reason"], "tiny_spark_auto")

    def test_performance_evidence_cannot_bypass_weakest_verified_quality_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            weak_low_a = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-low-a")
            weak_low_a.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=100, process_elapsed_ms=200)
            module.record_event(weak_low_a)
            weak_low_b = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-low-b")
            weak_low_b.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=200, process_elapsed_ms=400)
            module.record_event(weak_low_b)
            strong = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-high")
            strong.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=150, process_elapsed_ms=50)
            module.record_event(strong)
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", total_tokens=50, process_elapsed_ms=30)
            module.record_event(failure)
            recommendation = module.recommend_route(weak_low_a)
            record = next(iter(module.load_history(history)["conditions"].values()))
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["reason"], "verified_quality_boundary")
            self.assertEqual(recommendation["selection_basis"], "quality_boundary")
            self.assertEqual(record["cost_evidence"]["status"], "like_for_like")

    def test_record_retains_valid_workload_prompt_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            expected_hash = "b" * 64
            args = arguments(history, receipt, verify_level="real", verify_status="pass")
            write_receipt(receipt, workload_prompt_sha256=expected_hash)
            result = module.record_event(args)
            record = next(iter(module.load_history(history)["conditions"].values()))
            self.assertEqual(record["tasks"][0]["workload_prompt_sha256"], expected_hash)
            self.assertIn("best_pair", result)
            self.assertIn("cost_evidence", result)

    def test_different_workload_hashes_fall_back_to_quality_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]

            high = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-high")
            high.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=10, process_elapsed_ms=10, workload_prompt_sha256="a" * 64)
            module.record_event(high)
            low = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-low")
            low.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=100, process_elapsed_ms=100, workload_prompt_sha256="b" * 64)
            module.record_event(low)
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", workload_prompt_sha256="c" * 64)
            module.record_event(failure)

            recommendation = module.recommend_route(low)
            record = next(iter(module.load_history(history)["conditions"].values()))
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["selection_basis"], "quality_boundary")
            self.assertEqual(record["cost_evidence"]["status"], "no_common_workload")

    def test_incomplete_like_for_like_metrics_fall_back_to_quality_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]
            shared_hash = "d" * 64

            high = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-high")
            high.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=10, process_elapsed_ms=10, workload_prompt_sha256=shared_hash)
            module.record_event(high)
            low = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-low")
            low.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=None, process_elapsed_ms=100, workload_prompt_sha256=shared_hash)
            module.record_event(low)
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", workload_prompt_sha256=shared_hash)
            module.record_event(failure)

            recommendation = module.recommend_route(low)
            record = next(iter(module.load_history(history)["conditions"].values()))
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["selection_basis"], "quality_boundary")
            self.assertEqual(record["cost_evidence"]["status"], "incomplete_metrics")

    def test_repeated_recommendations_and_passes_keep_calibrated_adjacent_best(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", status="pass")
            module.record_event(failure)
            success = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-success-a")
            write_receipt(receipt, "gpt-5.6-luna", "low", status="pass")
            module.record_event(success)
            for run_id in ("run-success-a", "run-success-b"):
                success.run_id = run_id
                module.record_event(success)
                recommendation = module.recommend_route(success)
                self.assertFalse(recommendation["trial"])

    def test_later_cost_samples_do_not_move_frozen_adjacent_best(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]

            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", total_tokens=20, process_elapsed_ms=10)
            module.record_event(failure)

            high = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-high-fast")
            high.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=50, process_elapsed_ms=50)
            module.record_event(high)

            low = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-low")
            low.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=100, process_elapsed_ms=100)
            module.record_event(low)
            frozen = module.recommend_route(low)
            self.assertEqual(frozen["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(frozen["selection_basis"], "quality_boundary")
            self.assertFalse(frozen["trial"])

            later = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-high-expensive")
            later.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=1000, process_elapsed_ms=1000)
            module.record_event(later)
            still_frozen = module.recommend_route(later)
            self.assertEqual(still_frozen["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(still_frozen["selection_basis"], "quality_boundary")
            self.assertFalse(still_frozen["trial"])

    def test_gap_selects_intermediate_then_freezes_after_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(failure)
            stronger = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-stronger")
            stronger.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high")
            module.record_event(stronger)
            recommendation = module.recommend_route(stronger)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["reason"], "quality_boundary_gap_trial")
            self.assertTrue(recommendation["trial"])
            trial = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-trial")
            trial.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "low")
            module.record_event(trial)
            frozen = module.recommend_route(trial)
            self.assertEqual(frozen["selected_pair"], "gpt-5.6-luna|low")
            self.assertFalse(frozen["trial"])

    def test_recommendation_stays_on_gap_rung_until_tested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high"]
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(failure)
            stronger = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-stronger")
            stronger.candidate_ladder = ladder
            write_receipt(receipt, "gpt-5.6-luna", "high")
            module.record_event(stronger)
            first = module.recommend_route(stronger)
            self.assertEqual(first["selected_pair"], "gpt-5.6-luna|low")
            self.assertTrue(first["trial"])
            second = module.recommend_route(stronger)
            self.assertEqual(second["selected_pair"], "gpt-5.6-luna|low")
            self.assertTrue(second["trial"])

    def test_best_quality_failure_reopens_immediate_stronger_and_freezes_after_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            baseline = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-baseline")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(baseline)
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-best-failure")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(failure)
            reopened = module.recommend_route(failure)
            self.assertEqual(reopened["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(reopened["reason"], "failure_and_success_boundary")
            self.assertTrue(reopened["trial"])
            stronger = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-stronger")
            write_receipt(receipt, "gpt-5.6-luna", "low")
            module.record_event(stronger)
            frozen = module.recommend_route(stronger)
            self.assertEqual(frozen["selected_pair"], "gpt-5.6-luna|low")
            self.assertFalse(frozen["trial"])

    def test_hard_floor_best_ignores_operational_and_receipt_invalid_quality_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            baseline = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-floor")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(baseline)
            operational = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="execution", run_id="run-operational")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", status="fail", turn_completed=False, route_attempts=route_attempt_fail())
            module.record_event(operational)
            invalid_quality = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-invalid-quality")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", status="fail", turn_completed=False)
            module.record_event(invalid_quality)
            recommendation = module.recommend_route(baseline)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.3-codex-spark|low")
            self.assertFalse(recommendation["trial"])

    def test_same_profile_summary_shares_best_while_complexity_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            baseline = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-shared")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(baseline)
            changed_summary = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-summary")
            changed_summary.task_summary = "A second summary with the same routing profile remains shared."
            shared = module.recommend_route(changed_summary)
            self.assertEqual(shared["selected_pair"], "gpt-5.3-codex-spark|low")
            split = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-complex")
            split.complexity = "complex"
            self.assertEqual(module.recommend_route(split)["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(len(module.load_history(history)["conditions"]), 2)

    def test_inserting_intermediate_rung_reopens_searching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            old_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-terra|low"]
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = old_ladder
            failure.static_suggestion = old_ladder[1]
            failure.hard_floor = old_ladder[0]
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(failure)
            success = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-success")
            success.candidate_ladder = old_ladder
            success.static_suggestion = old_ladder[1]
            success.hard_floor = old_ladder[0]
            write_receipt(receipt, "gpt-5.6-terra", "low")
            module.record_event(success)
            self.assertEqual(module.recommend_route(success)["selected_pair"], "gpt-5.6-terra|low")
            new_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-terra|low"]
            changed = arguments(history, receipt, run_id="run-new-rung")
            changed.candidate_ladder = new_ladder
            changed.static_suggestion = new_ladder[2]
            changed.hard_floor = new_ladder[0]
            recommendation = module.recommend_route(changed)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["reason"], "quality_boundary_gap_trial")
            self.assertTrue(recommendation["trial"])

    def test_current_ladder_narrowing_cannot_select_removed_historical_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            full = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(full)
            full.run_id = "run-success"
            full.verify_status = "pass"
            full.failure_class = "none"
            write_receipt(receipt, "gpt-5.6-luna", "low")
            module.record_event(full)
            narrowed = arguments(history, receipt, run_id="run-narrowed")
            narrowed.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-terra|low"]
            narrowed.static_suggestion = "gpt-5.6-terra|low"
            recommendation = module.recommend_route(narrowed)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|low")
            self.assertEqual(recommendation["reason"], "failure_and_success_boundary")
            self.assertEqual(recommendation["failed_model"], "gpt-5.3-codex-spark|low")
            self.assertEqual(recommendation["success_model"], "gpt-5.6-luna|low")
            write_receipt(receipt, "gpt-5.6-terra", "low")
            module.record_event(narrowed)
            frozen = module.recommend_route(narrowed)
            self.assertEqual(frozen["selected_pair"], "gpt-5.6-terra|low")
            self.assertFalse(frozen["trial"])
            self.assertEqual(frozen["success_model"], "gpt-5.6-luna|low")

    def test_removed_historical_success_recalibrates_current_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            success = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-success")
            write_receipt(receipt, "gpt-5.6-luna", "low")
            module.record_event(success)
            changed = arguments(history, receipt, run_id="run-policy-change")
            changed.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-terra|low"]
            changed.static_suggestion = "gpt-5.6-terra|low"
            recommendation = module.recommend_route(changed)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.3-codex-spark|low")
            self.assertEqual(recommendation["reason"], "success_boundary_trial")
            self.assertTrue(recommendation["trial"])

    def test_historical_success_below_raised_floor_recalibrates_current_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            old_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low"]
            success = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="run-old-success")
            success.candidate_ladder = old_ladder
            success.static_suggestion = old_ladder[1]
            write_receipt(receipt, "gpt-5.6-luna", "low")
            module.record_event(success)

            current_ladder = old_ladder + ["gpt-5.6-terra|low", "gpt-5.6-sol|low"]
            changed = arguments(history, receipt, run_id="run-raised-floor")
            changed.candidate_ladder = current_ladder
            changed.static_suggestion = "gpt-5.6-terra|low"
            changed.hard_floor = "gpt-5.6-terra|low"
            recommendation = module.recommend_route(changed)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|low")
            self.assertEqual(recommendation["reason"], "success_boundary_exhausted")
            self.assertFalse(recommendation["trial"])
            write_receipt(receipt, "gpt-5.6-terra", "low")
            module.record_event(changed)
            for _ in range(2):
                frozen = module.recommend_route(changed)
                self.assertEqual(frozen["selected_pair"], "gpt-5.6-terra|low")
                self.assertFalse(frozen["trial"])
            stored = module.load_history(history)["conditions"][module.condition_key(CONDITION)]
            self.assertEqual(stored["success_model"], "gpt-5.6-luna|low")
            self.assertEqual(stored["hard_floor"], "gpt-5.6-terra|low")

    def test_high_risk_pass_does_not_auto_downgrade_and_reports_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            high = SimpleNamespace(**dict(CONDITION, risk="high"), task_summary=SUMMARY, candidate_ladder=LADDER, static_suggestion="gpt-5.6-terra|low", hard_floor="gpt-5.3-codex-spark|low", history=root / "high.json", receipt=receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-high", trial=False)
            write_receipt(receipt, "gpt-5.6-terra", "low")
            module.record_event(high)
            recommendation = module.recommend_route(high)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|low")
            self.assertEqual(recommendation["reason"], "high_risk_no_autodowngrade")
            self.assertFalse(recommendation["trial"])

    def test_high_risk_does_not_downgrade_and_concurrent_records_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            write_receipt(receipt)
            high = SimpleNamespace(**dict(CONDITION, risk="high"), task_summary=SUMMARY, candidate_ladder=LADDER, static_suggestion="gpt-5.6-luna|low", hard_floor="gpt-5.3-codex-spark|low", history=root / "high.json")
            self.assertEqual(module.recommend_route(high)["selected_pair"], "gpt-5.6-luna|low")
            processes = [multiprocessing.Process(target=concurrent_record, args=(str(root / "shared.json"), str(receipt), number)) for number in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join()
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(module.status(root / "shared.json")["tasks"], 4)

    def test_sol_ultra_pass_recommends_sol_max_on_full_sol_ladder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, run_id="run-sol-ultra")
            args.candidate_ladder = FULL_SOL_LADDER
            args.static_suggestion = "gpt-5.6-luna|low"
            args.hard_floor = "gpt-5.3-codex-spark|low"
            write_receipt(receipt, "gpt-5.6-sol", "ultra", "pass")
            module.record_event(args)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-sol|max")

    def test_quality_failures_raise_failed_model_rung_by_rung(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            ladder = [
                "gpt-5.6-luna|low",
                "gpt-5.6-luna|medium",
                "gpt-5.6-luna|high",
                "gpt-5.6-terra|low",
            ]
            args = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="sol-failure")
            args.candidate_ladder = ladder
            args.static_suggestion = "gpt-5.6-luna|low"
            args.hard_floor = "gpt-5.6-luna|low"
            for index, pair in enumerate(ladder[:3], start=1):
                model, effort = pair.split("|", 1)
                write_receipt(receipt, model, effort, "pass")
                args.run_id = f"run-fail-{index}"
                args.verify_level = "real"
                args.verify_status = "fail"
                args.failure_class = "quality"
                module.record_event(args)
                self.assertEqual(module.recommend_route(args)["failed_model"], pair)

    def test_condition_fields_share_experience_without_summary_changes_and_split_on_field_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            write_receipt(receipt, "gpt-5.6-luna", "low", "pass")
            args = arguments(root / "history.json", receipt, run_id="run-shared")
            args.task_summary = "A detailed but safe routing summary used for testing."
            module.record_event(args)
            args.run_id = "run-shared-2"
            args.task_summary = "A different detailed but safe summary should not split the key."
            module.record_event(args)
            shared_history = module.load_history(root / "history.json")
            shared_key = module.condition_key(CONDITION)
            self.assertIn(shared_key, shared_history["conditions"])
            self.assertEqual(len(shared_history["conditions"][shared_key]["tasks"]), 2)

            args.risk = "high"
            args.run_id = "run-split"
            args.task_summary = "A high-risk but still safe summary."
            module.record_event(args)
            split_history = module.load_history(root / "history.json")
            self.assertEqual(len(split_history["conditions"]), 2)

    def test_unknown_real_evidence_does_not_create_success_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            args = arguments(history, receipt, verify_level="real", verify_status="unknown", failure_class="none", run_id="run-unknown")
            args.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|high", "gpt-5.6-terra|low"]
            args.static_suggestion = "gpt-5.6-terra|low"
            args.hard_floor = "gpt-5.3-codex-spark|low"
            write_receipt(receipt, "gpt-5.6-terra", "low", total_tokens=120, process_elapsed_ms=60)
            module.record_event(args)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|low")

    def test_current_ladder_limits_selection_to_current_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            args = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-baseline")
            args.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high", "gpt-5.6-terra|low"]
            args.static_suggestion = "gpt-5.6-luna|low"
            args.hard_floor = "gpt-5.3-codex-spark|low"
            write_receipt(receipt, "gpt-5.6-luna", "low", total_tokens=900, process_elapsed_ms=900)
            module.record_event(args)
            args.run_id = "run-high"
            write_receipt(receipt, "gpt-5.6-luna", "high", total_tokens=100, process_elapsed_ms=100)
            module.record_event(args)
            failure = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-failure")
            failure.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-luna|high", "gpt-5.6-terra|low"]
            failure.static_suggestion = "gpt-5.6-luna|low"
            failure.hard_floor = "gpt-5.3-codex-spark|low"
            write_receipt(receipt, "gpt-5.6-terra", "low", status="fail", total_tokens=10, process_elapsed_ms=10)
            module.record_event(failure)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")

    def test_schema2_records_preserve_tasks_and_boundaries_with_inferred_execution_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            payload = {
                "schema_version": 2,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "conditions": {
                    "legacy": {
                        "condition": {
                            "task_family": "code",
                            "artifact": "script",
                            "scope": "single",
                            "ambiguity": "low",
                            "modality": "text",
                            "risk": "low",
                            "complexity": "easy",
                            "owning_skill": "code-skill",
                            "project_family": "global",
                            "verification_shape": "mini_real",
                            "execution_domain": "code_unspecified",
                        },
                        "execution_domain": "python",
                        "summary": "Schema2 route history migration test summary.",
                        "candidate_ladder": [
                            "gpt-5.3-codex-spark|low",
                            "gpt-5.6-luna|low",
                            "gpt-5.6-luna|medium",
                            "gpt-5.6-terra|low",
                        ],
                        "static_suggestion": "gpt-5.6-luna|low",
                        "hard_floor": "gpt-5.3-codex-spark|low",
                        "success_model": "gpt-5.6-luna|medium",
                        "failed_model": "gpt-5.6-luna|low",
                        "tasks": [
                            {
                                "run_id": "run-a",
                                "requested_pair": "gpt-5.3-codex-spark|low",
                                "resolved_pair": "gpt-5.3-codex-spark|low",
                                "effective_pair": "gpt-5.3-codex-spark|low",
                                "executed_pair": "gpt-5.3-codex-spark|low",
                                "receipt_status": "pass",
                                "mini_status": "fail",
                                "real_status": "unknown",
                                "allowlisted_failure_class": "quality",
                                "turn_completed": True,
                                "model_match": True,
                                "effort_match": True,
                                "trial": False,
                                "recorded_at": datetime.now(timezone.utc).isoformat(),
                            },
                            {
                                "run_id": "run-b",
                                "requested_pair": "gpt-5.6-luna|medium",
                                "resolved_pair": "gpt-5.6-luna|medium",
                                "effective_pair": "gpt-5.6-luna|medium",
                                "executed_pair": "gpt-5.6-luna|medium",
                                "receipt_status": "pass",
                                "mini_status": "pass",
                                "real_status": "pass",
                                "allowlisted_failure_class": "none",
                                "turn_completed": True,
                                "model_match": True,
                                "effort_match": True,
                                "trial": False,
                                "recorded_at": datetime.now(timezone.utc).isoformat(),
                            },
                        ],
                    }
                },
            }
            history.write_text(json.dumps(payload), encoding="utf-8")
            loaded = module.load_history(history)
            self.assertEqual(loaded["schema_version"], 3)
            expected_key = module.condition_key(
                dict(payload["conditions"]["legacy"]["condition"], execution_domain="code_unspecified"),
                allow_history_only=True,
            )
            self.assertIn(expected_key, loaded["conditions"])
            loaded_record = loaded["conditions"][expected_key]
            self.assertEqual(len(loaded_record["tasks"]), 2)
            self.assertEqual(loaded_record["failed_model"], "gpt-5.6-luna|low")
            self.assertEqual(loaded_record["success_model"], "gpt-5.6-luna|medium")

    def test_schema2_records_accept_legacy_code_unspecified_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            payload = {
                "schema_version": 2,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "conditions": {
                    "legacy": {
                        "condition": {
                            "task_family": "code",
                            "artifact": "script",
                            "scope": "single",
                            "ambiguity": "low",
                            "modality": "text",
                            "risk": "low",
                            "complexity": "easy",
                            "owning_skill": "code-skill",
                            "project_family": "global",
                            "verification_shape": "mini_real",
                        },
                        "execution_domain": "code_unspecified",
                        "summary": "Schema2 legacy history retains code-unspecified.",
                        "candidate_ladder": ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low"],
                        "static_suggestion": "gpt-5.6-luna|low",
                        "hard_floor": "gpt-5.3-codex-spark|low",
                        "success_model": "gpt-5.6-luna|low",
                        "failed_model": None,
                        "tasks": [],
                    }
                },
            }
            history.write_text(json.dumps(payload), encoding="utf-8")
            loaded = module.load_history(history)
            expected_key = module.condition_key(
                dict(payload["conditions"]["legacy"]["condition"], execution_domain="code_unspecified"),
                allow_history_only=True,
            )
            self.assertIn(expected_key, loaded["conditions"])
            self.assertEqual(loaded["conditions"][expected_key]["failed_model"], None)

    def test_python_and_unity_distinct_execution_domains_change_condition_keys(self):
        python_condition = dict(CONDITION, execution_domain="python")
        unity_condition = dict(CONDITION, execution_domain="unity_csharp")
        self.assertNotEqual(module.condition_key(module.validate_condition(python_condition)), module.condition_key(module.validate_condition(unity_condition)))

    def test_cli_profile_domain_distinguishes_python_and_unity_keys(self):
        args_py = parse_profile_args(["recommend", "--profile-preset", "code-easy", "--project-family", "global", "--task-summary", SUMMARY, "--execution-domain", "python"])
        args_unity = parse_profile_args(["recommend", "--profile-preset", "code-easy", "--project-family", "global", "--task-summary", SUMMARY, "--execution-domain", "unity_csharp"])
        self.assertNotEqual(module.condition_key(module.validate_condition(vars(args_py))), module.condition_key(module.validate_condition(vars(args_unity))))

    def test_concise_grounded_complex_preset_uses_real_identity_and_keeps_legacy_key_readable(self):
        args = parse_profile_args(["recommend", "--profile-preset", "grounded-repository-answer-complex", "--project-family", "museai", "--owning-skill", "muse-ai-plugin:muse-ai-dev-skill", "--task-summary", SUMMARY])
        condition, _, pairs, static_pair, hard_pair = module._profile(args)
        self.assertEqual(condition["verification_shape"], "real")
        self.assertEqual(module.condition_key(condition), "dc7d1ed94cf7a1c5aec354248f843f25ea98187903cd9ce60bd44957d5fa06ff")
        fingerprint = module.profile_fingerprint(condition, pairs, static_pair, hard_pair)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotEqual(fingerprint, module.profile_fingerprint(condition, pairs[:-1], static_pair, hard_pair))
        legacy_condition = dict(condition, verification_shape="mini_real")
        self.assertEqual(module.validate_condition(legacy_condition, allow_history_only=True)["verification_shape"], "mini_real")
        self.assertEqual(module.condition_key(legacy_condition, allow_history_only=True), "fc20f19053552e814bcbd1bc7027d06dad1e0a8f16bb1887441c7d080eb498e0")
        self.assertEqual(args.candidate_ladder, module.normal_adaptive_pair_texts())

    def test_active_profiles_reject_all_history_only_verification_shapes(self):
        for shape in ("mini", "mini_real", "result"):
            with self.subTest(shape=shape):
                legacy_condition = dict(CONDITION, verification_shape=shape)
                self.assertEqual(
                    module.validate_condition(legacy_condition, allow_history_only=True)["verification_shape"],
                    shape,
                )
                with self.assertRaisesRegex(ValueError, "active profiles require verification_shape=real"):
                    module.validate_condition(legacy_condition)

    def test_active_record_rejects_history_only_shapes_before_receipt_or_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for shape in ("mini", "mini_real", "result"):
                with self.subTest(shape=shape):
                    history = root / f"{shape}.json"
                    receipt = root / f"{shape}-missing-receipt.json"
                    args = arguments(history, receipt, verify_level="real", verify_status="pass")
                    args.verification_shape = shape
                    with self.assertRaisesRegex(ValueError, "active profiles require verification_shape=real"):
                        module.record_event(args)
                    self.assertFalse(receipt.exists())
                    self.assertFalse(history.exists())
                    self.assertFalse(history.with_suffix(history.suffix + ".lock").exists())

    def test_real_only_profile_rejects_mini_pass_without_creating_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, verify_level="mini", verify_status="pass")
            args.verification_shape = "real"
            self.assertFalse(receipt.exists())
            with self.assertRaisesRegex(ValueError, "active writes require verify_level=real"):
                module.record_event(args)
            self.assertFalse(receipt.exists())
            self.assertFalse(history.exists())
            self.assertFalse(history.with_suffix(history.suffix + ".lock").exists())

    def test_real_only_profile_rejects_mini_quality_failure_without_mutating_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            baseline = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="real-pass")
            baseline.verification_shape = "real"
            write_receipt(receipt)
            module.record_event(baseline)
            before = history.read_bytes()
            before_mtime_ns = history.stat().st_mtime_ns
            lock_path = history.with_suffix(history.suffix + ".lock")
            before_lock = lock_path.read_bytes()
            before_lock_mtime_ns = lock_path.stat().st_mtime_ns

            rejected = arguments(history, receipt, verify_level="mini", verify_status="fail", failure_class="quality", run_id="mini-fail")
            rejected.verification_shape = "real"
            rejected.receipt = root / "missing-receipt.json"
            with self.assertRaisesRegex(ValueError, "active writes require verify_level=real"):
                module.record_event(rejected)
            self.assertEqual(history.read_bytes(), before)
            self.assertEqual(history.stat().st_mtime_ns, before_mtime_ns)
            self.assertEqual(lock_path.read_bytes(), before_lock)
            self.assertEqual(lock_path.stat().st_mtime_ns, before_lock_mtime_ns)

    def test_real_only_profile_real_pass_and_failure_still_move_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            passed = arguments(history, receipt, verify_level="real", verify_status="pass", run_id="real-pass")
            passed.verification_shape = "real"
            write_receipt(receipt)
            pass_result = module.record_event(passed)
            self.assertEqual(pass_result["success_model"], "gpt-5.6-luna|low")

            failed = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="real-fail")
            failed.verification_shape = "real"
            write_receipt(receipt)
            fail_result = module.record_event(failed)
            self.assertEqual(fail_result["failed_model"], "gpt-5.6-luna|low")

    def test_active_real_recommendation_derives_and_freezes_legacy_real_boundary_without_mutating_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            legacy_condition = dict(CONDITION, verification_shape="mini_real")
            legacy_key = module.condition_key(legacy_condition, allow_history_only=True)
            legacy = legacy_record(legacy_condition, [legacy_task("legacy-real-fail", "gpt-5.3-codex-spark|low", mini_status="pass", real_status="fail", failure_class="quality"), legacy_task("legacy-real-pass", "gpt-5.6-luna|low", mini_status="fail", real_status="pass", failure_class="quality")], success_model="gpt-5.6-luna|low", failed_model="gpt-5.3-codex-spark|low")
            payload = module.empty_history()
            payload["conditions"][legacy_key] = legacy
            history.write_text(json.dumps(payload), encoding="utf-8")
            legacy_before = deepcopy(legacy)

            args = arguments(history, root / "unused-receipt.json")
            first = module.recommend_route(args)
            second = module.recommend_route(args)
            self.assertEqual(first["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(first["failed_model"], "gpt-5.3-codex-spark|low")
            self.assertEqual(first["success_model"], "gpt-5.6-luna|low")
            self.assertEqual(first["calibration_state"], "frozen")
            self.assertFalse(first["trial"])
            self.assertEqual(second["selected_pair"], first["selected_pair"])

            loaded = module.load_history(history)
            active = loaded["conditions"][module.condition_key(CONDITION)]
            self.assertEqual(loaded["conditions"][legacy_key], legacy_before)
            self.assertEqual(len(active["tasks"]), 2)
            self.assertTrue(all(task["evidence_origin"] == "legacy_real_boundary" for task in active["tasks"]))
            self.assertTrue(all("mini_status" not in task for task in active["tasks"]))
            self.assertTrue(all(task["workload_prompt_sha256"] is None and task["process_ms"] is None for task in active["tasks"]))
            self.assertTrue(all(all(value is None for value in task["token_totals"].values()) for task in active["tasks"]))
            self.assertEqual(active["cost_evidence"]["status"], "insufficient_pairs")

    def test_active_real_recommendation_excludes_mini_only_invalid_receipt_and_unclassified_real_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            legacy_condition = dict(CONDITION, verification_shape="mini")
            legacy_key = module.condition_key(legacy_condition, allow_history_only=True)
            tasks = [legacy_task("mini-only", "gpt-5.3-codex-spark|low", mini_status="pass"), legacy_task("invalid-real-pass", "gpt-5.6-luna|low", real_status="pass", receipt_status="fail"), legacy_task("mismatched-real-pass", "gpt-5.6-luna|low", real_status="pass", model_match=False), legacy_task("unclassified-real-fail", "gpt-5.6-luna|low", real_status="fail", failure_class="none")]
            legacy = legacy_record(legacy_condition, tasks, success_model="gpt-5.3-codex-spark|low", failed_model="gpt-5.6-luna|low")
            payload = module.empty_history()
            payload["conditions"][legacy_key] = legacy
            history.write_text(json.dumps(payload), encoding="utf-8")
            legacy_before = deepcopy(legacy)

            recommendation = module.recommend_route(arguments(history, root / "unused-receipt.json"))
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["calibration_state"], "cold_start")
            self.assertIsNone(recommendation["success_model"])
            self.assertIsNone(recommendation["failed_model"])
            self.assertEqual(recommendation["samples"], 0)
            loaded = module.load_history(history)
            self.assertEqual(loaded["conditions"][legacy_key], legacy_before)
            self.assertEqual(loaded["conditions"][module.condition_key(CONDITION)]["tasks"], [])

    def test_active_real_recommendation_combines_result_and_mini_real_durable_real_evidence_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            result_condition = dict(CONDITION, verification_shape="result")
            mini_real_condition = dict(CONDITION, verification_shape="mini_real")
            payload = module.empty_history()
            payload["conditions"][module.condition_key(result_condition, allow_history_only=True)] = legacy_record(result_condition, [legacy_task("result-real-fail", "gpt-5.6-luna|low", real_status="fail", failure_class="correctness")], failed_model="gpt-5.6-luna|low", best_pair=None)
            payload["conditions"][module.condition_key(mini_real_condition, allow_history_only=True)] = legacy_record(mini_real_condition, [legacy_task("mini-real-pass", "gpt-5.6-terra|low", mini_status="pass", real_status="pass")], success_model="gpt-5.6-terra|low", best_pair="gpt-5.6-terra|low")
            history.write_text(json.dumps(payload), encoding="utf-8")

            recommendation = module.recommend_route(arguments(history, root / "unused-receipt.json"))
            self.assertEqual(recommendation["failed_model"], "gpt-5.6-luna|low")
            self.assertEqual(recommendation["success_model"], "gpt-5.6-terra|low")
            self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|low")
            self.assertEqual(recommendation["calibration_state"], "frozen")
            self.assertFalse(recommendation["trial"])

    def test_cli_profile_rejects_manual_candidate_ladder(self):
        with self.assertRaises(SystemExit):
            parse_profile_args(["recommend", "--profile-preset", "grounded-repository-answer-easy", "--project-family", "global", "--owning-skill", "workflow-skill", "--task-summary", SUMMARY, "--candidate-ladder", "gpt-5.6-luna|low"])

    def test_cli_profile_without_execution_domain_rejects_omitted_code_domain(self):
        with self.assertRaises(ValueError):
            parse_profile_args(["recommend", "--profile-preset", "code-easy", "--project-family", "global", "--task-summary", SUMMARY])
        args_general = parse_profile_args(["recommend", "--profile-preset", "grounded-repository-answer-easy", "--project-family", "global", "--owning-skill", "workflow-skill", "--task-summary", SUMMARY])
        self.assertEqual(module.validate_condition(vars(args_general))["execution_domain"], "general")

    def test_cli_profile_rejects_explicit_code_unspecified(self):
        with self.assertRaises(SystemExit):
            parse_profile_args(["recommend", "--profile-preset", "code-easy", "--project-family", "global", "--task-summary", SUMMARY, "--execution-domain", "code_unspecified"])

    def test_cli_profile_rejects_explicit_inactive_non_history_domain(self):
        with self._with_inactive_domain():
            with self.assertRaises(SystemExit):
                parse_profile_args(["recommend", "--profile-preset", "code-easy", "--project-family", "global", "--task-summary", SUMMARY, "--execution-domain", "legacy_inactive"])

    def test_cli_profile_domains_command_filters_history_only_domains(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            history.write_text(json.dumps(module.empty_history()), encoding="utf-8")
            payload = io.StringIO()
            with redirect_stdout(payload), patch.object(sys, "argv", ["model_routing_history.py", "--history", str(history), "domains"]):
                module.main()
            rows = json.loads(payload.getvalue()).get("rows", [])
        self.assertNotIn("code_unspecified", rows)

    def test_cli_domains_command_is_read_only_and_reports_registry_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            history.write_text(json.dumps(module.empty_history()), encoding="utf-8")
            before = history.read_text(encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch.object(sys, "argv", ["model_routing_history.py", "--history", str(history), "domains"]):
                module.main()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload.get("schema_version"), 1)
            self.assertEqual(payload.get("registry_version"), module.EXECUTION_DOMAIN_REGISTRY_VERSION)
            self.assertEqual(payload.get("rows"), module.public_execution_domain_rows())
            self.assertEqual(history.read_text(encoding="utf-8"), before)

    def test_cli_profiles_command_is_read_only_and_reports_stable_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch.object(sys, "argv", ["model_routing_history.py", "--history", str(history), "profiles"]):
                module.main()
            payload = json.loads(stdout.getvalue())
        self.assertFalse(history.exists())
        self.assertEqual(payload["registry_version"], module.PROFILE_PRESET_VERSION)
        self.assertEqual(payload["rows"], module.public_profile_preset_rows())

    def test_cli_domains_command_does_not_create_or_mutate_history_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "model_experience.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch.object(sys, "argv", ["model_routing_history.py", "--history", str(history), "domains"]):
                module.main()
            self.assertFalse(history.exists())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload.get("schema_version"), 1)
            self.assertEqual(payload.get("rows"), module.public_execution_domain_rows())

    def test_record_event_same_run_id_unions_operational_failure_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, run_id="run-union")
            args.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-terra|low"]
            write_receipt(
                receipt,
                "gpt-5.6-terra",
                "low",
                status="fail",
                route_attempts=route_attempt_fail("gpt-5.6-terra", "low"),
            )
            module.record_event(args)
            args.verify_level = "real"
            args.verify_status = "fail"
            args.failure_class = "execution"
            write_receipt(
                receipt,
                "gpt-5.6-luna",
                "low",
                status="fail",
                route_attempts=route_attempt_fail("gpt-5.6-luna", "low"),
            )
            module.record_event(args)
            task = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][0]
            self.assertEqual(task["operational_failure_pairs"], ["gpt-5.6-luna|low", "gpt-5.6-terra|low"])

    def test_hard_floor_verified_success_is_retained_when_floor_strength_is_max(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            history = root / "history.json"
            args = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-floor")
            args.candidate_ladder = ["gpt-5.3-codex-spark|low", "gpt-5.6-luna|low", "gpt-5.6-terra|low"]
            args.static_suggestion = "gpt-5.6-luna|low"
            args.hard_floor = "gpt-5.3-codex-spark|low"
            write_receipt(receipt, "gpt-5.3-codex-spark", "low", total_tokens=77, process_elapsed_ms=77)
            module.record_event(args)
            recommendation = module.recommend_route(args)
            self.assertEqual(recommendation["selected_pair"], "gpt-5.3-codex-spark|low")
            self.assertEqual(recommendation["trial"], False)
            self.assertEqual(recommendation["reason"], "verified_quality_boundary")

    def test_same_run_valid_real_pass_survives_operational_and_invalid_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-sticky-pass")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(args)

            args.verify_level = "real"
            args.verify_status = "fail"
            args.failure_class = "execution"
            write_receipt(receipt, "gpt-5.6-luna", "low", status="pass", total_tokens=99, process_elapsed_ms=19)
            module.record_event(args)

            args.failure_class = "quality"
            write_receipt(receipt, "gpt-5.6-terra", "low", status="fail", turn_completed=False, total_tokens=101, process_elapsed_ms=21)
            module.record_event(args)
            task = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][0]
            self.assertIsNone(module.task_verdict(task))
            self.assertEqual(task["receipt_status"], "fail")
            self.assertEqual(task["executed_pair"], "gpt-5.3-codex-spark|low")
            self.assertEqual(task["token_totals"]["total"], 12)
            self.assertEqual(task["process_ms"], 5)
            recommendation = module.recommend_route(args)
            self.assertFalse(recommendation["trial"])

    def test_same_run_valid_quality_failure_survives_operational_and_invalid_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, verify_level="real", verify_status="fail", failure_class="quality", run_id="run-sticky-fail")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(args)

            args.verify_level = "real"
            args.verify_status = "fail"
            args.failure_class = "execution"
            write_receipt(receipt, "gpt-5.6-luna", "low", status="pass", total_tokens=99, process_elapsed_ms=19)
            module.record_event(args)

            args.failure_class = "quality"
            write_receipt(receipt, "gpt-5.6-terra", "low", status="fail", turn_completed=False, total_tokens=101, process_elapsed_ms=21)
            module.record_event(args)
            task = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][0]
            self.assertIsNone(module.task_verdict(task))
            self.assertEqual(task["real_status"], "fail")
            self.assertNotIn("mini_status", task)
            self.assertEqual(task["allowlisted_failure_class"], "quality")
            self.assertEqual(task["executed_pair"], "gpt-5.6-terra|low")
            self.assertEqual(task["token_totals"]["total"], 12)
            self.assertEqual(task["process_ms"], 5)

    def test_same_run_receipt_valid_quality_failure_reopens_preserved_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "history.json"
            receipt = root / "receipt.json"
            args = arguments(history, receipt, verify_level="real", verify_status="pass", failure_class="none", run_id="run-reopen")
            write_receipt(receipt, "gpt-5.3-codex-spark", "low")
            module.record_event(args)
            args.verify_level = "real"
            args.verify_status = "fail"
            args.failure_class = "quality"
            write_receipt(receipt, "gpt-5.6-luna", "low", status="pass")
            module.record_event(args)
            task = module.load_history(history)["conditions"][module.condition_key(CONDITION)]["tasks"][0]
            self.assertEqual(module.task_verdict(task), "fail")
            self.assertEqual(task["executed_pair"], "gpt-5.6-luna|low")
            self.assertEqual(task["real_status"], "fail")


if __name__ == "__main__":
    unittest.main()
