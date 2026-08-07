#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "model_execution_receipt.py"
MODULE_SPEC = importlib.util.spec_from_file_location("model_execution_receipt", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class ModelExecutionReceiptTests(unittest.TestCase):
    def test_immediate_operational_fallback_requires_zero_token_unpublished_failure(self):
        eligible = module.annotate_operational_fallback({"status": "fail", "failure_class": "availability", "turn_completed": False, "tokens": {"total_tokens": 0}, "result_published": False, "route_attempts": [{}]})
        published = dict(eligible, result_published=True)
        consumed = dict(eligible, tokens={"total_tokens": 1})
        completed = dict(eligible, turn_completed=True)
        unauthorized = dict(eligible, failure_class="authorization")
        self.assertTrue(module.immediate_operational_fallback(eligible))
        self.assertEqual(eligible["failure_stage"], "pre_execution")
        self.assertFalse(module.immediate_operational_fallback(published))
        self.assertFalse(module.immediate_operational_fallback(consumed))
        self.assertFalse(module.immediate_operational_fallback(completed))
        self.assertFalse(module.immediate_operational_fallback(unauthorized))

    def test_parse_stdout_uses_only_safe_summary_fields(self):
        stdout_text = "\n".join([json.dumps({"type": "thread.started", "thread_id": "thread-1"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "secret response text"}}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 2}})])
        summary = module.parse_stdout_events(stdout_text)
        self.assertEqual(summary["thread_id"], "thread-1")
        self.assertTrue(summary["turn_completed"])
        self.assertEqual(summary["usage"]["input_tokens"], 100)
        self.assertEqual(summary["output_hash"], module.sha256_text("secret response text"))
        self.assertFalse(summary["availability_failure"])
        self.assertNotIn("secret response text", json.dumps(summary))

    def test_parse_stdout_classifies_usage_limit_without_storing_raw_message(self):
        stdout_text = json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Purchase more credits or try again later."}})
        summary = module.parse_stdout_events(stdout_text)
        self.assertTrue(summary["turn_failed"])
        self.assertTrue(summary["availability_failure"])
        self.assertNotIn("purchase more credits", json.dumps(summary).lower())

    def test_parse_stdout_uses_latest_terminal_turn_event(self):
        recovered = "\n".join([
            json.dumps({"type": "error", "message": "transient tool stream error"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}),
        ])
        failed = "\n".join([
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}),
            json.dumps({"type": "turn.failed", "error": {"message": "terminal failure"}}),
        ])
        self.assertTrue(module.parse_stdout_events(recovered)["turn_completed"])
        self.assertFalse(module.parse_stdout_events(recovered)["turn_failed"])
        self.assertFalse(module.parse_stdout_events(failed)["turn_completed"])
        self.assertTrue(module.parse_stdout_events(failed)["turn_failed"])

    def test_raw_result_extraction_is_separate_from_sanitized_summary(self):
        stdout_text = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "result kept only when requested"}})
        self.assertEqual(module.extract_last_agent_message(stdout_text), "result kept only when requested")
        self.assertNotIn("result kept only when requested", json.dumps(module.parse_stdout_events(stdout_text)))

    def test_parse_rollout_allowlist_reads_resolved_model_reroute_tokens_and_timing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            rollout_path = Path(temp_dir) / "rollout.jsonl"
            events = [{"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-terra", "effort": "high", "base_instructions": "do not copy"}}, {"type": "event_msg", "payload": {"type": "model_reroute", "from_model": "gpt-5.6-terra", "to_model": "gpt-5.6-luna", "reason": "allowed fallback"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 10, "total_tokens": 150}}, "rate_limits": {"limit_id": "premium", "credits": {"has_credits": True, "unlimited": False}, "rate_limit_reached_type": None}}}, {"type": "event_msg", "payload": {"type": "task_complete", "duration_ms": 420, "time_to_first_token_ms": 40}}]
            rollout_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            observed = module.parse_rollout_allowlist(rollout_path)
        self.assertEqual(observed["turn_context"], {"turn_id": "turn-1", "model": "gpt-5.6-terra", "effort": "high"})
        self.assertEqual(observed["reroutes"][-1]["to_model"], "gpt-5.6-luna")
        self.assertEqual(observed["usage"]["total_tokens"], 150)
        self.assertEqual(observed["task_complete"]["time_to_first_token_ms"], 40)
        self.assertEqual(observed["availability"]["limit_id"], "premium")
        self.assertTrue(observed["availability"]["has_credits"])
        self.assertNotIn("base_instructions", json.dumps(observed))

    def test_read_thread_state_retries_transient_operational_error_and_closes_connections(self):
        class FakeConnection:
            def __init__(self, row=None, error=None):
                self.row = row
                self.error = error
                self.closed = False

            def execute(self, _query, _parameters):
                if self.error is not None:
                    raise self.error
                return SimpleNamespace(fetchone=lambda: self.row)

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp_dir:
            state_db = Path(temp_dir) / "state.sqlite"
            state_db.touch()
            Path(f"{state_db}-wal").touch()
            failed_connection = FakeConnection(error=module.sqlite3.OperationalError("temporarily unavailable"))
            row = (str(Path(temp_dir) / "rollout.jsonl"), "gpt-5.6-sol", "ultra", 42, "test", "openai", "exec")
            successful_connection = FakeConnection(row=row)
            with patch.object(module.sqlite3, "connect", side_effect=[failed_connection, successful_connection]) as connect, patch.object(module.time, "sleep") as sleep:
                observed = module.read_thread_state(state_db, "thread-1")
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(0.1)
        self.assertTrue(failed_connection.closed)
        self.assertTrue(successful_connection.closed)
        self.assertEqual(observed["model"], "gpt-5.6-sol")
        self.assertEqual(observed["effort"], "ultra")
        self.assertEqual(observed["tokens_used"], 42)

    def test_read_thread_state_uses_immutable_read_only_fallback_without_wal_sidecars(self):
        class FakeConnection:
            def __init__(self, row=None, error=None):
                self.row = row
                self.error = error
                self.closed = False

            def execute(self, _query, _parameters):
                if self.error is not None:
                    raise self.error
                return SimpleNamespace(fetchone=lambda: self.row)

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp_dir:
            state_db = Path(temp_dir) / "state.sqlite"
            state_db.touch()
            primary_connection = FakeConnection(error=module.sqlite3.OperationalError("readonly shm unavailable"))
            row = (str(Path(temp_dir) / "rollout.jsonl"), "gpt-5.6-sol", "ultra", 77, "test", "openai", "exec")
            immutable_connection = FakeConnection(row=row)
            with patch.object(module.sqlite3, "connect", side_effect=[primary_connection, immutable_connection]) as connect, patch.object(module.time, "sleep") as sleep:
                observed = module.read_thread_state(state_db, "thread-immutable")
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args_list[0].args[0], f"file:{state_db}?mode=ro")
        self.assertEqual(connect.call_args_list[1].args[0], f"file:{state_db}?mode=ro&immutable=1")
        self.assertTrue(connect.call_args_list[0].kwargs["uri"])
        self.assertTrue(connect.call_args_list[1].kwargs["uri"])
        sleep.assert_not_called()
        self.assertTrue(primary_connection.closed)
        self.assertTrue(immutable_connection.closed)
        self.assertEqual(observed["model"], "gpt-5.6-sol")
        self.assertEqual(observed["tokens_used"], 77)

    def test_read_thread_state_raises_persistent_operational_error_after_bound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_db = Path(temp_dir) / "state.sqlite"
            state_db.touch()
            Path(f"{state_db}-wal").touch()
            failures = [module.sqlite3.OperationalError("still unavailable") for _ in range(20)]
            with patch.object(module.sqlite3, "connect", side_effect=failures) as connect, patch.object(module.time, "sleep") as sleep:
                with self.assertRaisesRegex(module.sqlite3.OperationalError, "still unavailable"):
                    module.read_thread_state(state_db, "thread-1")
        self.assertEqual(connect.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    def test_run_receipt_requests_exact_model_and_effort_over_stdin(self):
        stdout_text = "\n".join([json.dumps({"type": "thread.started", "thread_id": "thread-1"}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 2}})])
        process = SimpleNamespace(stdout=stdout_text, stderr="one warning\n", returncode=0)
        thread_state = {"rollout_path": Path("/tmp/rollout"), "model": "gpt-5.3-codex-spark", "effort": "high", "tokens_used": 110, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {"turn_id": "turn-1", "model": "gpt-5.3-codex-spark", "effort": "high"}, "reroutes": [], "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10, "reasoning_output_tokens": 2, "total_tokens": 110}, "task_complete": {"duration_ms": 300, "time_to_first_token_ms": 100}}
        args = argparse.Namespace(model="gpt-5.3-codex-spark", effort="high", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="same-work", allow_fallback=[])
        with patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            receipt = module.run_receipt(args, "same prompt")
        command = run_mock.call_args.args[0]
        self.assertIn("gpt-5.3-codex-spark", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("features.multi_agent=false", command)
        self.assertEqual(command[-1], "-")
        self.assertTrue(run_mock.call_args.kwargs["input"].startswith("LOCKED_ROUTE_NODE"))
        self.assertIn("This is the result node only", run_mock.call_args.kwargs["input"])
        self.assertIn("Do not create, launch, wait for, or summarize End/Fix tasks", run_mock.call_args.kwargs["input"])
        self.assertIn("entry parent owns post-result lifecycle after this node's passing receipt", run_mock.call_args.kwargs["input"])
        self.assertIn(f"canonical working directory `{Path('/tmp').resolve()}`", run_mock.call_args.kwargs["input"])
        self.assertTrue(run_mock.call_args.kwargs["input"].endswith("same prompt"))
        self.assertTrue(run_mock.call_args.kwargs["shell"] is False)
        self.assertEqual(receipt["status"], "pass")
        self.assertTrue(receipt["model_match"])
        self.assertTrue(receipt["effort_match"])

    def test_route_markers_define_non_recursive_lifecycle_ownership(self):
        result_boundary = module.route_node_lifecycle_boundary("LOCKED_ROUTE_NODE")
        ending_boundary = module.route_node_lifecycle_boundary("ENDING_TASK_WORKER")
        self.assertIn("entry parent owns post-result lifecycle", result_boundary)
        self.assertIn("never create a nested End/Fix task", ending_boundary)
        with self.assertRaisesRegex(ValueError, "unsupported route marker"):
            module.route_node_lifecycle_boundary("UNKNOWN")

    def test_run_receipt_includes_sanitized_route_attempt_metadata(self):
        stdout_text = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "secret response text"}},
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 101, "cached_input_tokens": 1, "output_tokens": 9, "reasoning_output_tokens": 0}}),
        ])
        process = SimpleNamespace(stdout=stdout_text, stderr="", returncode=0)
        thread_state = {"rollout_path": Path("/tmp/rollout"), "model": "gpt-5.6-luna", "effort": "low", "tokens_used": 110, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {"turn_id": "turn-2", "model": "gpt-5.6-luna", "effort": "low"}, "reroutes": [], "usage": {"input_tokens": 101, "cached_input_tokens": 1, "output_tokens": 9, "reasoning_output_tokens": 0, "total_tokens": 110}, "task_complete": {"duration_ms": 200, "time_to_first_token_ms": 20}}
        args = argparse.Namespace(model="gpt-5.6-luna", effort="low", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="route-attempt", allow_fallback=[])
        with patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            receipt = module.run_receipt(args, "same prompt")
        self.assertEqual(run_mock.call_args.args[0], ["codex", "exec", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=\"low\"", "-c", "features.multi_agent=false", "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--ignore-user-config", "-"])
        attempt = receipt["route_attempts"][0]
        self.assertEqual(attempt["requested_pair"], "gpt-5.6-luna|low")
        self.assertEqual(attempt["resolved_pair"], "gpt-5.6-luna|low")
        self.assertEqual(attempt["effective_pair"], "gpt-5.6-luna|low")
        self.assertEqual(attempt["executed_pair"], "gpt-5.6-luna|low")
        self.assertEqual(attempt["status"], "pass")
        self.assertIsNone(attempt["failure_class"])
        self.assertTrue(attempt["model_match"])
        self.assertTrue(attempt["effort_match"])
        self.assertEqual(attempt["pair_match"], True)
        self.assertEqual(attempt["tokens"]["total_tokens"], 110)
        self.assertEqual(attempt["thread_id"], "thread-1")
        self.assertNotIn("secret response text", json.dumps(attempt))

    def test_run_receipt_marks_execution_failure_class_when_runtime_fails_before_resolution(self):
        stdout_text = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
        process = SimpleNamespace(stdout=stdout_text, stderr="boom", returncode=1)
        thread_state = {"rollout_path": Path("/tmp/rollout"), "model": "gpt-5.3-codex-spark", "effort": "low", "tokens_used": 110, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {}, "reroutes": [], "usage": {}, "task_complete": {}}
        args = argparse.Namespace(model="gpt-5.3-codex-spark", effort="low", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="runtime-fail", allow_fallback=[])
        with patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            receipt = module.run_receipt(args, "same prompt")
        self.assertEqual(run_mock.call_args.args[0][0], "codex")
        attempt = receipt["route_attempts"][0]
        self.assertEqual(attempt["status"], "fail")
        self.assertEqual(attempt["failure_class"], "execution")
        self.assertEqual(attempt["requested_pair"], "gpt-5.3-codex-spark|low")
        self.assertIsNone(attempt["resolved_pair"])
        self.assertIsNone(attempt["effective_pair"])
        self.assertIsNone(receipt["resolved_model"])
        self.assertIsNone(receipt["effective_model"])
        self.assertFalse(attempt["model_match"])
        self.assertFalse(attempt["effort_match"])
        self.assertFalse(attempt["pair_match"])
        self.assertNotIn("boom", json.dumps(attempt))

    def test_run_receipt_ignores_stale_thread_state_when_rollout_missing_turn_context(self):
        stdout_text = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
        process = SimpleNamespace(stdout=stdout_text, stderr="boom", returncode=1)
        thread_state = {"rollout_path": Path("/tmp/rollout"), "model": "gpt-5.3-codex-spark", "effort": "low", "tokens_used": 110, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {}, "reroutes": [], "usage": {}, "task_complete": {}}
        args = argparse.Namespace(model="gpt-5.6-terra", effort="low", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="runtime-fail-stale", allow_fallback=[])
        with patch.object(module.subprocess, "run", return_value=process), patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            receipt = module.run_receipt(args, "same prompt")
        attempt = receipt["route_attempts"][0]
        self.assertIsNone(attempt["resolved_pair"])
        self.assertIsNone(attempt["effective_pair"])
        self.assertIsNone(receipt["resolved_model"])
        self.assertIsNone(receipt["effective_model"])
        self.assertEqual(attempt["status"], "fail")
        self.assertFalse(receipt["turn_completed"])
        self.assertEqual(attempt["executed_pair"], "gpt-5.6-terra|low")

    def test_failed_run_receipt_is_sanitized_and_does_not_claim_execution(self):
        args = argparse.Namespace(
            model="gpt-5.6-luna",
            effort="high",
            workload_id="timeout-proof",
            entry_task=False,
            allow_fallback=["gpt-5.6-terra|medium"],
        )
        receipt = module.failed_run_receipt(args, "timeout")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "timeout")
        self.assertIsNone(receipt["effective_model"])
        self.assertFalse(receipt["turn_completed"])
        self.assertFalse(receipt["metrics_complete"])
        self.assertFalse(receipt["tokens_lower_bound"])
        self.assertEqual(receipt["route_attempts"][0]["executed_pair"], "gpt-5.6-luna|high")
        self.assertNotIn("error", json.dumps(receipt).lower())

    def test_run_receipt_preserves_sanitized_timeout_telemetry_from_partial_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_output = Path(temp_dir) / "partial-result.md"
            partial_stdout = "\n".join([json.dumps({"type": "thread.started", "thread_id": "thread-timeout"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "secret partial response"}}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 70, "output_tokens": 30, "total_tokens": 100}})])
            timeout_error = module.subprocess.TimeoutExpired(["codex", "exec"], 30, output=partial_stdout.encode("utf-8"), stderr=b"private stderr")
            thread_state = {"rollout_path": Path(temp_dir) / "rollout.jsonl", "model": "gpt-5.6-luna", "effort": "low", "tokens_used": 125, "cli_version": "test", "model_provider": "openai", "source": "exec"}
            rollout = {"turn_context": {"turn_id": "turn-timeout", "model": "gpt-5.6-luna", "effort": "low"}, "reroutes": [{"from_model": "gpt-5.6-luna", "to_model": "gpt-5.6-terra", "reason": "capacity"}], "usage": {"input_tokens": 80, "cached_input_tokens": 5, "output_tokens": 45, "reasoning_output_tokens": 12, "total_tokens": 125}, "task_complete": {"duration_ms": 900, "time_to_first_token_ms": 30}}
            args = argparse.Namespace(model="gpt-5.6-luna", effort="low", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, result_output=result_output, timeout=30, workdir=Path(temp_dir), state_db=Path(temp_dir) / "state.sqlite", workload_id="timeout-work", allow_fallback=["gpt-5.6-terra|low"])
            with patch.object(module.subprocess, "run", side_effect=timeout_error), patch.object(module, "read_thread_state", return_value=thread_state) as read_state, patch.object(module, "parse_rollout_allowlist", return_value=rollout):
                receipt = module.run_receipt(args, "confidential prompt")
        self.assertEqual(read_state.call_args.args[1], "thread-timeout")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "timeout")
        self.assertFalse(receipt["turn_completed"])
        self.assertFalse(receipt["metrics_complete"])
        self.assertTrue(receipt["tokens_lower_bound"])
        self.assertEqual(receipt["tokens"]["total_tokens"], 125)
        self.assertEqual(receipt["resolved_model"], "gpt-5.6-luna")
        self.assertEqual(receipt["effective_model"], "gpt-5.6-terra")
        self.assertEqual(receipt["route_attempts"][0]["failure_class"], "timeout")
        self.assertEqual(receipt["workload_prompt_sha256"], module.sha256_text("confidential prompt"))
        self.assertGreaterEqual(receipt["process_elapsed_ms"], 0)
        self.assertFalse(result_output.exists())
        self.assertNotIn("secret partial response", json.dumps(receipt))
        self.assertNotIn("private stderr", json.dumps(receipt))
        self.assertNotIn("confidential prompt", json.dumps(receipt))

    def test_compare_receipts_reports_positive_savings_for_routed_run(self):
        routed = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "prompt_sha256": "wrapper-a", "output_sha256": "same-output", "effective_model": "gpt-5.3-codex-spark", "resolved_effort": "high", "process_elapsed_ms": 800, "tokens": {"total_tokens": 120, "uncached_input_tokens": 80}}
        baseline = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "prompt_sha256": "wrapper-b", "output_sha256": "same-output", "effective_model": "gpt-5.6-sol", "resolved_effort": "ultra", "process_elapsed_ms": 1400, "tokens": {"total_tokens": 200, "uncached_input_tokens": 130}}
        comparison = module.compare_receipts(routed, baseline)
        self.assertTrue(comparison["valid_like_for_like_smoke"])
        self.assertTrue(comparison["performance_eligible"])
        self.assertEqual(comparison["measured_savings"]["total_tokens"], 80)
        self.assertEqual(comparison["measured_savings"]["process_elapsed_ms"], 600)

    def test_strategy_bundle_counts_unique_entry_and_descendant_receipts(self):
        entry = {"receipt_id": "entry", "tokens": {"total_tokens": 30, "uncached_input_tokens": 20}, "process_elapsed_ms": 30}
        child = {"receipt_id": "child", "tokens": {"total_tokens": 40, "uncached_input_tokens": 25}, "process_elapsed_ms": 40}
        routed = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "output_sha256": "same-output", "receipts": [entry, child, dict(child)]}
        baseline = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "output_sha256": "same-output", "tokens": {"total_tokens": 100, "uncached_input_tokens": 70}, "process_elapsed_ms": 80}
        comparison = module.compare_receipts(routed, baseline)
        self.assertEqual(comparison["routed"]["receipt_count"], 2)
        self.assertEqual(comparison["routed"]["total_tokens"], 70)
        self.assertEqual(comparison["routed"]["process_elapsed_ms"], 70)
        self.assertTrue(comparison["performance_eligible"])

    def test_token_cheaper_but_slower_strategy_is_not_performance_eligible(self):
        entry = {"receipt_id": "entry", "tokens": {"total_tokens": 30}, "process_elapsed_ms": 50}
        child = {"receipt_id": "child", "tokens": {"total_tokens": 40}, "process_elapsed_ms": 40}
        routed = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "output_sha256": "same-output", "receipts": [entry, child]}
        baseline = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-workload", "output_sha256": "same-output", "tokens": {"total_tokens": 100}, "process_elapsed_ms": 80}
        comparison = module.compare_receipts(routed, baseline)
        self.assertTrue(comparison["valid_like_for_like_smoke"])
        self.assertEqual(comparison["measured_savings"]["total_tokens"], 30)
        self.assertEqual(comparison["measured_savings"]["process_elapsed_ms"], -10)
        self.assertFalse(comparison["performance_eligible"])
        self.assertIn("strategy must have lower complete critical-path time", comparison["performance_failures"])

    def test_compare_receipts_rejects_different_workload_prompt_hashes(self):
        routed = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "a", "output_sha256": "same-output", "tokens": {}}
        baseline = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "b", "output_sha256": "same-output", "tokens": {}}
        comparison = module.compare_receipts(routed, baseline)
        self.assertFalse(comparison["valid_like_for_like_smoke"])
        self.assertIn("workload prompt hash mismatch", comparison["failures"])

    def test_compare_receipts_accepts_external_semantic_verification(self):
        routed = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-hash", "output_sha256": "output-a", "process_elapsed_ms": 80, "tokens": {"total_tokens": 12, "uncached_input_tokens": 8}}
        baseline = {"status": "pass", "workload_id": "same-work", "workload_prompt_sha256": "same-hash", "output_sha256": "output-b", "process_elapsed_ms": 100, "tokens": {"total_tokens": 20, "uncached_input_tokens": 10}}
        evidence = {"status": "pass", "workload_id": "same-work", "same_acceptance_criteria": True}
        comparison = module.compare_receipts(routed, baseline, evidence)
        self.assertTrue(comparison["valid_like_for_like_smoke"])
        self.assertEqual(comparison["acceptance"]["evidence_type"], "external-semantic-verification")

    def test_run_command_summary_emits_result_only_when_explicit_and_passed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.md"
            result_path.write_text("bounded result\n", encoding="utf-8")
            args = argparse.Namespace(output=Path(temp_dir) / "receipt.json", result_output=result_path, emit_result=True)
            summary = module.run_command_summary(args, {"status": "pass"})
            self.assertEqual(summary["result"], "bounded result")
            args.emit_result = False
            self.assertNotIn("result", module.run_command_summary(args, {"status": "pass"}))
            args.emit_result = True
            self.assertNotIn("result", module.run_command_summary(args, {"status": "fail"}))

    def test_entry_launch_installs_inherited_context_marker(self):
        stdout_text = "\n".join([json.dumps({"type": "thread.started", "thread_id": "entry-thread"}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0}})])
        process = SimpleNamespace(stdout=stdout_text, stderr="", returncode=0)
        thread_state = {"rollout_path": Path("/tmp/entry-rollout"), "model": "gpt-5.6-sol", "effort": "ultra", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {"turn_id": "entry-turn", "model": "gpt-5.6-sol", "effort": "ultra"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 5, "time_to_first_token_ms": 1}}
        args = argparse.Namespace(model="gpt-5.6-sol", effort="ultra", codex_bin="codex", sandbox="read-only", ignore_user_config=False, entry_task=True, result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="entry-marker", allow_fallback=[])
        with patch.dict(os.environ, {}, clear=False), patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            receipt = module.run_receipt(args, "entry task")
        self.assertEqual(run_mock.call_args.kwargs["env"][module.ENTRY_CONTEXT_ENV], "1")
        self.assertEqual(receipt["node_role"], "entry")
        self.assertTrue(receipt["entry_context_active"])
        self.assertEqual(receipt["authorization_source"], "entry-launch")

    def test_direct_task_benchmark_runs_exact_raw_prompt_with_explicit_metadata(self):
        raw_prompt = "exact raw benchmark prompt\nwithout a locked marker"
        stdout_text = "\n".join([json.dumps({"type": "thread.started", "thread_id": "direct-thread"}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0}})])
        process = SimpleNamespace(stdout=stdout_text, stderr="", returncode=0)
        thread_state = {"rollout_path": Path("/tmp/direct-rollout"), "model": "gpt-5.6-sol", "effort": "ultra", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {"turn_id": "direct-turn", "model": "gpt-5.6-sol", "effort": "ultra"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 5, "time_to_first_token_ms": 1}}
        args = argparse.Namespace(model="gpt-5.6-sol", effort="ultra", codex_bin="codex", sandbox="read-only", ignore_user_config=False, entry_task=False, direct_task=True, benchmark_run_id="benchmark-direct-benchmark", result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="direct-benchmark", allow_fallback=[])
        with patch.dict(os.environ, {}, clear=False), patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            receipt = module.run_receipt(args, raw_prompt)
        self.assertEqual(run_mock.call_args.kwargs["input"], raw_prompt)
        self.assertNotIn(module.ENTRY_CONTEXT_ENV, run_mock.call_args.kwargs["env"])
        self.assertEqual(receipt["node_type"], "direct-task")
        self.assertEqual(receipt["node_role"], "result-producer")
        self.assertFalse(receipt["entry_context_active"])
        self.assertEqual(receipt["authorization_source"], "benchmark-direct")
        self.assertEqual(receipt["benchmark_run_id"], "benchmark-direct-benchmark")
        self.assertEqual(receipt["prompt_sha256"], receipt["workload_prompt_sha256"])

    def test_benchmark_stream_freezes_first_strict_json_before_receipt_telemetry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "result.json"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json
                import sys
                import time

                sys.stdin.read()
                def emit(value):
                    print(json.dumps(value), flush=True)

                emit({"type": "thread.started", "thread_id": "benchmark-stream-thread"})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "commentary before result"}})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "{\\"answer\\":"}})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "{\\"answer\\":1,\\"answer\\":1}"}})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "{\\"answer\\":NaN}"}})
                sys.stderr.write("x" * 200000 + "\\n")
                sys.stderr.flush()
                time.sleep(0.1)
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "{ \\"answer\\": 0 }"}})
                time.sleep(0.2)
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "{\\n  \\"answer\\": 1\\n}"}})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "commentary after result"}})
                emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}})
            """), encoding="utf-8")
            fake_codex.chmod(0o755)
            thread_state = {"rollout_path": root / "rollout.jsonl", "model": "gpt-5.6-sol", "effort": "ultra", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
            rollout = {"turn_context": {"turn_id": "benchmark-turn", "model": "gpt-5.6-sol", "effort": "ultra"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 250, "time_to_first_token_ms": 1}}
            args = argparse.Namespace(model="gpt-5.6-sol", effort="ultra", codex_bin=str(fake_codex), sandbox="read-only", ignore_user_config=False, entry_task=False, direct_task=True, bootstrap_task=False, benchmark_run_id="benchmark-stream-result", result_output=result_path, timeout=2, workdir=root, state_db=root / "state.sqlite", workload_id="stream-result", allow_fallback=[])
            with patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout), patch("builtins.print") as print_mock, ThreadPoolExecutor(max_workers=1) as executor:
                started = time.monotonic()
                future = executor.submit(module.run_receipt, args, "exact raw prompt")
                time.sleep(0.05)
                self.assertFalse(result_path.exists())
                deadline = time.monotonic() + 1
                while not result_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.005)
                first_result_elapsed = time.monotonic() - started
                first_published_result = result_path.read_text(encoding="utf-8")
                self.assertFalse(future.done())
                receipt = future.result(timeout=2)
                result_ready_event = json.loads(print_mock.call_args.args[0])
            final_result = result_path.read_text(encoding="utf-8")
        self.assertEqual(first_published_result, '{ "answer": 0 }\n')
        self.assertGreaterEqual(first_result_elapsed, 0.08)
        self.assertEqual(final_result, '{ "answer": 0 }\n')
        self.assertEqual(receipt["output_sha256"], module.sha256_text('{ "answer": 0 }'))
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "protocol")
        self.assertTrue(receipt["duplicate_result_detected"])
        self.assertEqual(result_ready_event, {"schema_version": 2, "stage": "result-ready", "workload_id": "stream-result", "benchmark_run_id": "benchmark-stream-result", "result_path": str(result_path), "child_result_ready_monotonic_ns": receipt["result_ready_monotonic_ns"], "main_thread_id": "benchmark-stream-thread"})
        self.assertEqual(receipt["stderr_line_count"], 1)
        self.assertNotIn("commentary before result", json.dumps(receipt))
        self.assertNotIn("commentary after result", json.dumps(receipt))

    def test_production_stream_ignores_partial_or_commentary_and_freezes_first_ready_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "result.md"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json
                import sys
                import time

                sys.stdin.read()
                def emit(value):
                    print(json.dumps(value), flush=True)

                emit({"type": "thread.started", "thread_id": "production-stream-thread"})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "plain commentary"}})
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "RESULT_READY_BEGIN\\npartial only"}})
                time.sleep(0.1)
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "RESULT_READY_BEGIN\\nFIRST RESULT\\nRESULT_READY_END"}})
                time.sleep(0.2)
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": "RESULT_READY_BEGIN\\nLATER RESULT\\nRESULT_READY_END"}})
                emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}})
            """), encoding="utf-8")
            fake_codex.chmod(0o755)
            thread_state = {"rollout_path": root / "rollout.jsonl", "model": "gpt-5.6-luna", "effort": "low", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
            rollout = {"turn_context": {"turn_id": "production-turn", "model": "gpt-5.6-luna", "effort": "low"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 250, "time_to_first_token_ms": 1}}
            args = argparse.Namespace(model="gpt-5.6-luna", effort="low", codex_bin=str(fake_codex), sandbox="read-only", ignore_user_config=False, entry_task=False, direct_task=False, bootstrap_task=False, benchmark_run_id=None, node_role="result-producer", route_marker="LOCKED_ROUTE_NODE", stream_result_ready=True, result_output=result_path, timeout=2, workdir=root, state_db=root / "state.sqlite", workload_id="production-stream", allow_fallback=[])
            with patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(module.run_receipt, args, "bounded production task")
                time.sleep(0.05)
                self.assertFalse(result_path.exists())
                deadline = time.monotonic() + 1
                while not result_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.005)
                first_published_result = result_path.read_text(encoding="utf-8")
                self.assertFalse(future.done())
                receipt = future.result(timeout=2)
            final_result = result_path.read_text(encoding="utf-8")
        self.assertEqual(first_published_result, "FIRST RESULT\n")
        self.assertEqual(final_result, "FIRST RESULT\n")
        self.assertEqual(receipt["output_sha256"], module.sha256_text("FIRST RESULT"))
        self.assertTrue(receipt["result_published"])
        self.assertIsInstance(receipt["result_ready_monotonic_ns"], int)
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "protocol")
        self.assertTrue(receipt["duplicate_result_detected"])

    def test_bootstrap_task_runs_frozen_auto_wrapper_without_entry_context(self):
        raw_prompt = "exact Global inline-bootstrap benchmark prompt"
        stdout_text = "\n".join([json.dumps({"type": "thread.started", "thread_id": "bootstrap-thread"}), json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0}})])
        process = SimpleNamespace(stdout=stdout_text, stderr="", returncode=0)
        thread_state = {"rollout_path": Path("/tmp/bootstrap-rollout"), "model": "gpt-5.6-sol", "effort": "ultra", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
        rollout = {"turn_context": {"turn_id": "bootstrap-turn", "model": "gpt-5.6-sol", "effort": "ultra"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 5, "time_to_first_token_ms": 1}}
        with tempfile.TemporaryDirectory() as temporary:
            prompt_path = Path(temporary) / "prompt.txt"
            prompt_path.write_text(raw_prompt, encoding="utf-8")
            args = argparse.Namespace(model="gpt-5.6-sol", effort="ultra", codex_bin="codex", sandbox="read-only", ignore_user_config=False, entry_task=False, direct_task=False, bootstrap_task=True, benchmark_run_id="benchmark-bootstrap-benchmark", benchmark_prompt_path=prompt_path, result_output=None, timeout=30, workdir=Path(temporary), state_db=Path(temporary) / "state.sqlite", workload_id="bootstrap-benchmark", allow_fallback=[])
            with patch.dict(os.environ, {}, clear=False), patch.object(module.subprocess, "run", return_value=process) as run_mock, patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout):
                os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
                receipt = module.run_receipt(args, raw_prompt)
        execution_prompt = module.auto_benchmark_execution_prompt(raw_prompt, "gpt-5.6-sol|ultra")
        self.assertEqual(run_mock.call_args.kwargs["input"], execution_prompt)
        self.assertIn("AUTO_BENCHMARK_ENTRY", execution_prompt)
        self.assertIn("benchmark_auto_entry_bridge.py", execution_prompt)
        self.assertIn("Launch exactly one bridge process", execution_prompt)
        self.assertIn("poll that same session until exit", execution_prompt)
        self.assertIn("must never launch a second process", execution_prompt)
        self.assertEqual(execution_prompt.count(raw_prompt), 0)
        self.assertIn(module.sha256_text(raw_prompt), execution_prompt)
        self.assertNotIn(module.ENTRY_CONTEXT_ENV, run_mock.call_args.kwargs["env"])
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_PROMPT_PATH"], str(prompt_path.resolve()))
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_WORKLOAD_SHA256"], module.sha256_text(raw_prompt))
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_PYTHON"], str(Path(module.sys.executable).resolve()))
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_ENTRY_MODEL"], "gpt-5.6-sol")
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_ENTRY_EFFORT"], "ultra")
        self.assertEqual(run_mock.call_args.kwargs["env"]["CODEX_AUTO_BENCHMARK_CACHE_ROOT"], str(args.workdir / "Cache" / "task-analyze" / args.workload_id))
        self.assertEqual(receipt["node_type"], "bootstrap-task")
        self.assertEqual(receipt["node_role"], "result-producer")
        self.assertFalse(receipt["entry_context_active"])
        self.assertEqual(receipt["authorization_source"], "benchmark-global-inline")
        self.assertEqual(receipt["benchmark_run_id"], "benchmark-bootstrap-benchmark")
        self.assertTrue(receipt["benchmark_prompt_file_verified"])
        self.assertEqual(receipt["workload_prompt_sha256"], module.sha256_text(raw_prompt))
        self.assertEqual(receipt["prompt_sha256"], module.sha256_text(execution_prompt))
        self.assertNotEqual(receipt["prompt_sha256"], receipt["workload_prompt_sha256"])

    def test_bootstrap_uses_verified_bridge_result_only_when_controller_final_is_empty(self):
        raw_prompt = "exact adaptive bridge handoff prompt"
        bridge_result = '{"answer":1}'
        bridge_receipt_result = "Complexity: 18/100 (small) · Model: gpt-5.3-codex-spark|low · Route: downgrade\nEvidence: runtime receipt\n\n" + bridge_result
        for terminal_message, handoff_expected in (("", True), ("wrong non-json final", False)):
            with self.subTest(terminal_message=terminal_message), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prompt_path = root / "prompt.txt"
                result_path = root / "result.json"
                prompt_path.write_text(raw_prompt, encoding="utf-8")
                fake_codex = root / "fake-codex"
                fake_codex.write_text(textwrap.dedent("""\
                    #!/usr/bin/env python3
                    import json
                    import sys

                    terminal_message = __TERMINAL_MESSAGE__
                    sys.stdin.read()
                    def emit(value):
                        print(json.dumps(value), flush=True)

                    emit({"type": "thread.started", "thread_id": "bootstrap-handoff-thread"})
                    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "bridge launched"}})
                    emit({"type": "item.completed", "item": {"type": "agent_message", "text": terminal_message}})
                    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}})
                """).replace("__TERMINAL_MESSAGE__", repr(terminal_message)), encoding="utf-8")
                fake_codex.chmod(0o755)
                cache_root = result_path.parent / "auto-route-cache"
                workspace = cache_root / "source-copy-one"
                output_root = workspace / "Cache" / "task-analyze" / "bridge-output"
                output_root.mkdir(parents=True)
                (cache_root / "adaptive-entry-launch.json").write_text(json.dumps({"schema_version": 3, "workload_sha256": module.sha256_text(raw_prompt), "entry_pair": "gpt-5.6-luna|max"}) + "\n", encoding="utf-8")
                (output_root / "result.json").write_text(bridge_receipt_result + "\n", encoding="utf-8")
                bridge_receipt = {
                    "status": "pass",
                    "metrics_complete": True,
                    "result_published": True,
                    "duplicate_result_detected": False,
                    "output_sha256": module.sha256_text(bridge_result),
                    "selected_pair": "gpt-5.6-luna|max",
                    "effective_pair": "gpt-5.6-luna|max",
                    "tokens": {"total_tokens": 12},
                    "process_elapsed_ms": 5,
                    "route_attempts": [{"status": "pass", "process_elapsed_ms": 5, "tokens": {"total_tokens": 12}}],
                    "reroutes": [],
                    "trial": False,
                    "recommendation_state": "frozen",
                    "selection_provenance": "local_history",
                    "capability_assignment": [{"node_id": "result", "effective_pair": "gpt-5.6-luna|max"}],
                    "node_role": "result-producer",
                }
                (output_root / "receipt.json").write_text(json.dumps(bridge_receipt) + "\n", encoding="utf-8")
                thread_state = {"rollout_path": root / "rollout.jsonl", "model": "gpt-5.6-luna", "effort": "max", "tokens_used": 12, "cli_version": "test", "model_provider": "openai", "source": "exec"}
                rollout = {"turn_context": {"turn_id": "bootstrap-handoff-turn", "model": "gpt-5.6-luna", "effort": "max"}, "reroutes": [], "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 12}, "task_complete": {"duration_ms": 5, "time_to_first_token_ms": 1}}
                args = argparse.Namespace(model="gpt-5.6-luna", effort="max", codex_bin=str(fake_codex), sandbox="read-only", ignore_user_config=False, entry_task=False, direct_task=False, bootstrap_task=True, benchmark_run_id="benchmark-bootstrap-handoff", benchmark_prompt_path=prompt_path, result_output=result_path, timeout=30, workdir=root, state_db=root / "state.sqlite", workload_id="bootstrap-handoff", allow_fallback=[])
                with patch.dict(os.environ, {}, clear=False), patch.object(module, "read_thread_state", return_value=thread_state), patch.object(module, "parse_rollout_allowlist", return_value=rollout), patch("builtins.print") as print_mock:
                    os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
                    receipt = module.run_receipt(args, raw_prompt)
                self.assertTrue(receipt["benchmark_auto_launch_verified"])
                self.assertTrue(receipt["benchmark_auto_bridge_result_verified"])
                if handoff_expected:
                    self.assertEqual(result_path.read_text(encoding="utf-8"), bridge_result + "\n")
                    self.assertEqual(receipt["benchmark_result_source"], "adaptive_bridge_handoff")
                    self.assertEqual(receipt["output_sha256"], module.sha256_text(bridge_result))
                    self.assertTrue(receipt["result_published"])
                    self.assertEqual(receipt["status"], "pass")
                    self.assertEqual(json.loads(print_mock.call_args.args[0])["main_thread_id"], "bootstrap-handoff-thread")
                else:
                    self.assertFalse(result_path.exists())
                    self.assertIsNone(receipt["benchmark_result_source"])
                    self.assertFalse(receipt["result_published"])
                    self.assertEqual(receipt["status"], "fail")
                    print_mock.assert_not_called()

    def test_direct_task_requires_benchmark_id_and_is_forbidden_in_entry_context(self):
        missing_id = argparse.Namespace(entry_task=False, direct_task=True, benchmark_run_id=None, workload_id="direct-missing", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "direct_task_benchmark_run_id_required"):
                module.authorize_receipt_run(missing_id)
        direct = argparse.Namespace(entry_task=False, direct_task=True, benchmark_run_id="benchmark-direct-001", workload_id="direct-001", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {module.ENTRY_CONTEXT_ENV: "1"}, clear=False):
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "direct_task_entry_context_forbidden"):
                module.authorize_receipt_run(direct)
        bootstrap_missing_id = argparse.Namespace(entry_task=False, direct_task=False, bootstrap_task=True, benchmark_run_id=None, workload_id="global-missing", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "bootstrap_task_benchmark_run_id_required"):
                module.authorize_receipt_run(bootstrap_missing_id)
        bootstrap = argparse.Namespace(entry_task=False, direct_task=False, bootstrap_task=True, benchmark_run_id="benchmark-global-001", workload_id="global-001", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {module.ENTRY_CONTEXT_ENV: "1"}, clear=False):
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "bootstrap_task_entry_context_forbidden"):
                module.authorize_receipt_run(bootstrap)
        wrong_id = argparse.Namespace(entry_task=False, direct_task=False, bootstrap_task=True, benchmark_run_id="benchmark-other", workload_id="global-001", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "benchmark_run_id_workload_mismatch"):
                module.authorize_receipt_run(wrong_id)

    def test_direct_task_and_entry_task_are_cli_mutually_exclusive(self):
        argv = ["run", "--model", "gpt-5.6-sol", "--effort", "ultra", "--workload-id", "conflict", "--output", "/tmp/conflict.json", "--entry-task", "--direct-task", "--benchmark-run-id", "benchmark-conflict"]
        with self.assertRaises(SystemExit):
            module.parse_args(argv)

    def test_benchmark_run_id_cannot_change_an_ordinary_downstream_node(self):
        args = argparse.Namespace(entry_task=False, direct_task=False, benchmark_run_id="benchmark-forged", route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "benchmark_run_id_requires_benchmark_task"):
                module.authorize_receipt_run(args)
            stream_without_output = argparse.Namespace(entry_task=False, direct_task=False, bootstrap_task=False, benchmark_run_id=None, stream_result_ready=True, node_role="result-producer", result_output=None, route_marker="LOCKED_ROUTE_NODE")
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "stream_result_ready_requires_result_output"):
                module.authorize_receipt_run(stream_without_output)
            stream_verifier = argparse.Namespace(entry_task=False, direct_task=False, bootstrap_task=False, benchmark_run_id=None, stream_result_ready=True, node_role="verification", result_output=Path("/tmp/result.md"), route_marker="LOCKED_ROUTE_NODE")
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "stream_result_ready_requires_result_producer"):
                module.authorize_receipt_run(stream_verifier)

    def test_direct_result_producer_is_rejected_inside_entry_context(self):
        args = argparse.Namespace(model="gpt-5.6-terra", effort="high", codex_bin="codex", sandbox="read-only", ignore_user_config=True, entry_task=False, route_marker="LOCKED_ROUTE_NODE", result_output=None, timeout=30, workdir=Path("/tmp"), state_db=Path("/tmp/state.sqlite"), workload_id="blocked-fixed-result", allow_fallback=[])
        with patch.dict(os.environ, {module.ENTRY_CONTEXT_ENV: "1"}, clear=False), patch.object(module.subprocess, "run") as run_mock:
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "entry_context_adaptive_runner_required") as raised:
                module.run_receipt(args, "private bounded prompt")
            rejected = module.rejected_run_receipt(args, raised.exception)
        run_mock.assert_not_called()
        self.assertEqual(rejected["status"], "fail")
        self.assertEqual(rejected["failure_class"], "authorization")
        self.assertEqual(rejected["authorization_status"], "rejected")
        self.assertEqual(rejected["authorization_reason"], "entry_context_adaptive_runner_required")
        self.assertNotIn("private bounded prompt", json.dumps(rejected))
        self.assertNotIn(module.ENTRY_CONTEXT_ENV, json.dumps(rejected))

    def test_recursive_entry_flag_cannot_bypass_entry_context_guard(self):
        args = argparse.Namespace(entry_task=True, route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {module.ENTRY_CONTEXT_ENV: "1"}, clear=False):
            with self.assertRaisesRegex(module.ReceiptAuthorizationError, "recursive_entry_task_forbidden"):
                module.authorize_receipt_run(args)

    def test_fixed_result_baseline_remains_authorized_outside_entry_context(self):
        args = argparse.Namespace(entry_task=False, route_marker="LOCKED_ROUTE_NODE")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(module.ENTRY_CONTEXT_ENV, None)
            authorization = module.authorize_receipt_run(args)
        self.assertEqual(authorization["node_role"], "result-producer")
        self.assertEqual(authorization["authorization_source"], "outside-entry-context")
        self.assertFalse(authorization["entry_context_active"])

    def test_dispatcher_fixed_roles_require_matching_in_process_authorization(self):
        with patch.dict(os.environ, {module.ENTRY_CONTEXT_ENV: "1"}, clear=False):
            for node_role in sorted(module.DISPATCHER_FIXED_ROLES):
                args = argparse.Namespace(entry_task=False, node_role=node_role, route_marker="LOCKED_ROUTE_NODE")
                with self.assertRaises(module.ReceiptAuthorizationError):
                    module.authorize_receipt_run(args)
                with module.dispatcher_node_authorization(node_role):
                    authorization = module.authorize_receipt_run(args)
                self.assertEqual(authorization["authorization_source"], "dispatcher")
                self.assertEqual(authorization["node_role"], node_role)


if __name__ == "__main__":
    unittest.main()
