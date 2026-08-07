#!/usr/bin/env python3
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_suite_runner.py"
MODULE_SPEC = importlib.util.spec_from_file_location("benchmark_suite_runner", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


FAKE_RECEIPT_RUNNER = textwrap.dedent("""
    import argparse
    import hashlib
    import json
    import os
    import sqlite3
    import sys
    import time
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result-output", type=Path, required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--timeout", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--direct-task", action="store_true")
    mode.add_argument("--bootstrap-task", action="store_true")
    parser.add_argument("--benchmark-run-id")
    parser.add_argument("--benchmark-prompt-path")
    args = parser.parse_args()
    prompt_text = sys.stdin.read()

    def auto_execution_prompt(raw_workload):
        workload_sha256 = hashlib.sha256(raw_workload.encode("utf-8")).hexdigest()
        return "\\n".join([
            "AUTO_BENCHMARK_ENTRY",
            "You are the fixed gpt-5.6-luna|max controller. The workload body is deliberately absent.",
            "Launch exactly one bridge process: invoke the executable in CODEX_AUTO_BENCHMARK_PYTHON directly, with no interpreter discovery or substitution, on `skills/task-analyze-skill/scripts/benchmark_auto_entry_bridge.py` resolved under CODEX_HOME; pass `--prompt-file` from CODEX_AUTO_BENCHMARK_PROMPT_PATH and `--workdir` as the current working directory. Use the longest supported initial wait. If the tool yields a running session, poll that same session until exit; polling is not a retry and must never launch a second process.",
            "On exit 0, return the bridge stdout JSON byte-for-byte and stop. Do not read the prompt/source, solve, explain, verify, retry, launch another task, or run Ending/Fix.",
            f"BOUND_WORKLOAD_SHA256: {workload_sha256}",
            "",
        ])
    time.sleep(0.08 if args.direct_task else 0.005)
    result_message = prompt_text.strip()
    pair = f"{args.model}|{args.effort}"
    thread_id = f"fake-{args.workload_id}"
    role = "result-producer"
    node_type = "direct-task" if args.direct_task else "bootstrap-task"
    authorization_source = "benchmark-direct" if args.direct_task else "benchmark-global-inline"
    total_tokens = 1000 if args.direct_task else 50
    sessions_root = Path(os.environ["CODEX_HOME"]) / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    rollout_path = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout_events = [{"type": "session_meta", "payload": {"id": thread_id, "source": "exec"}}, {"type": "turn_context", "payload": {"model": args.model, "effort": args.effort}}, {"type": "event_msg", "payload": {"type": "task_started"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total_tokens}}}}, {"type": "event_msg", "payload": {"type": "task_complete"}}]
    rollout_path.write_text("\\n".join(json.dumps(event) for event in rollout_events) + "\\n", encoding="utf-8")
    connection = sqlite3.connect(args.state_db)
    connection.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
    connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", (thread_id, str(rollout_path), "exec", args.model, args.effort, total_tokens))
    connection.commit()
    connection.close()
    if args.bootstrap_task:
        adaptive_thread_id = f"{thread_id}-adaptive"
        adaptive_tokens = 400
        adaptive_rollout_path = sessions_root / f"rollout-{adaptive_thread_id}.jsonl"
        adaptive_rollout_events = [{"type": "session_meta", "payload": {"id": adaptive_thread_id, "source": "subagent"}}, {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "max"}}, {"type": "event_msg", "payload": {"type": "task_started"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": adaptive_tokens}}}}, {"type": "event_msg", "payload": {"type": "task_complete"}}]
        adaptive_rollout_path.write_text("\\n".join(json.dumps(event) for event in adaptive_rollout_events) + "\\n", encoding="utf-8")
        adaptive_source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": thread_id}}})
        connection = sqlite3.connect(args.state_db)
        connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", (adaptive_thread_id, str(adaptive_rollout_path), adaptive_source, "gpt-5.6-luna", "max", adaptive_tokens))
        connection.commit()
        connection.close()
    temporary_result = args.result_output.with_suffix(".tmp")
    temporary_result.write_text(result_message + "\\n", encoding="utf-8")
    temporary_result.replace(args.result_output)
    result_ready_monotonic_ns = time.monotonic_ns()
    result_ready_event = {"schema_version": 2, "stage": "result-ready", "workload_id": args.workload_id, "benchmark_run_id": args.benchmark_run_id, "result_path": str(args.result_output), "child_result_ready_monotonic_ns": result_ready_monotonic_ns, "main_thread_id": thread_id}
    result_ready_event_mode = os.environ.get("FAKE_RESULT_READY_EVENT_MODE")
    if result_ready_event_mode == "wrong-path":
        result_ready_event["result_path"] += ".wrong"
    if result_ready_event_mode == "missing-main-thread":
        result_ready_event.pop("main_thread_id")
    if result_ready_event_mode != "missing":
        print(json.dumps(result_ready_event, separators=(",", ":")), flush=True)
    if result_ready_event_mode == "duplicate":
        print(json.dumps(result_ready_event, separators=(",", ":")), flush=True)
    if os.environ.get("FAKE_POST_RESULT_ENDING") == "1":
        time.sleep(0.03)
        ending_thread_id = f"{thread_id}-ending"
        ending_tokens = 250
        ending_rollout_path = sessions_root / f"rollout-{ending_thread_id}.jsonl"
        ending_rollout_events = [{"type": "session_meta", "payload": {"id": ending_thread_id, "source": "subagent"}}, {"type": "turn_context", "payload": {"model": args.model, "effort": args.effort}}, {"type": "event_msg", "payload": {"type": "task_started"}}, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": ending_tokens}}}}, {"type": "event_msg", "payload": {"type": "task_complete"}}]
        ending_rollout_path.write_text("\\n".join(json.dumps(event) for event in ending_rollout_events) + "\\n", encoding="utf-8")
        ending_source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": thread_id}}})
        connection = sqlite3.connect(args.state_db)
        connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", (ending_thread_id, str(ending_rollout_path), ending_source, args.model, args.effort, ending_tokens))
        connection.commit()
        connection.close()
    if args.workload_id == "simple-r01-direct":
        time.sleep(0.15)
    workload_prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    execution_prompt = prompt_text if args.direct_task else auto_execution_prompt(prompt_text)
    execution_prompt_sha256 = hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()
    receipt = {"schema_version": 1, "status": "pass", "failure_class": None, "turn_completed": True, "exit_code": 0, "metrics_complete": True, "tokens_lower_bound": False, "model_match": True, "effort_match": True, "pair_match": True, "authorization_status": "authorized", "authorization_source": authorization_source, "entry_context_active": False, "benchmark_run_id": args.benchmark_run_id, "benchmark_result_source": "controller_final", "workload_id": args.workload_id, "node_type": node_type, "thread_id": thread_id, "requested_pair": pair, "effective_pair": pair, "requested_model": args.model, "requested_effort": args.effort, "resolved_model": args.model, "resolved_effort": args.effort, "effective_model": args.model, "node_role": role, "route_attempts": [{"status": "pass", "executed_pair": pair}], "reroutes": [], "tokens": {"total_tokens": total_tokens}, "output_sha256": hashlib.sha256(result_message.encode("utf-8")).hexdigest(), "result_published": True, "result_ready_monotonic_ns": result_ready_monotonic_ns, "duplicate_result_detected": False, "workload_prompt_sha256": workload_prompt_sha256, "prompt_sha256": execution_prompt_sha256}
    if args.bootstrap_task:
        receipt["benchmark_prompt_file_verified"] = True
        receipt["benchmark_auto_launch_verified"] = True
        receipt["benchmark_auto_workspace_count"] = 1
        receipt["benchmark_auto_bridge_result_verified"] = True
        tier = args.workload_id.split("-r", 1)[0]
        selected_pairs = {"simple": "gpt-5.3-codex-spark|low", "medium": "gpt-5.6-terra|medium", "complex": "gpt-5.6-luna|max"}
        selected_pair = selected_pairs[tier]
        route_signature = {"selected_pair": selected_pair, "effective_pair": selected_pair, "scheduled_graph": False, "assigned_pairs": [selected_pair], "trial": False, "recommendation_state": "frozen", "selection_provenance": "dual_model_history", "capability_assignment": [{"node_id": tier, "effective_pair": selected_pair}]}
        receipt["benchmark_selected_execution"] = {"schema_version": 2, "receipt_sha256": hashlib.sha256(adaptive_thread_id.encode("utf-8")).hexdigest(), "selected_pair": selected_pair, "effective_pair": selected_pair, "steady_state_logical_tokens": adaptive_tokens, "steady_state_execution_elapsed_ms": 1, "calibration_attempt_count": 0, "calibration_failure_elapsed_ms": 0, "calibration_failure_logical_tokens": 0, "route_signature": route_signature}
    args.output.write_text(json.dumps(receipt) + "\\n", encoding="utf-8")
    suite_root = args.output.parents[2]
    call_record = {"run_id": args.workload_id, "direct": args.direct_task, "entry": False, "bootstrap": args.bootstrap_task, "entry_env_present": "CODEX_TASK_ANALYZE_ENTRY_CONTEXT" in os.environ, "benchmark_run_id": args.benchmark_run_id, "model": args.model, "effort": args.effort, "workdir": args.workdir, "state_db": args.state_db, "codex_home": os.environ.get("CODEX_HOME"), "sandbox": args.sandbox, "prompt_sha256": workload_prompt_sha256, "plan_exists": (suite_root / "suite-plan.json").is_file()}
    with (suite_root / "call-order.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(call_record) + "\\n")
""").strip() + "\n"


BUSY_OR_LOCKED_ERROR_NAMES = {"SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_BUSY_OR_LOCKED"}


class BenchmarkSuiteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.quota_reader_patcher = mock.patch.object(module, "read_quota_status", return_value=self.quota_status(used=0))
        self.quota_reader = self.quota_reader_patcher.start()
        self.addCleanup(self.quota_reader_patcher.stop)

    @staticmethod
    def quota_status(used, reached_type=None, primary_reset=2000000000, secondary_used=10, secondary_reset=2100000000):
        return {
            "limit_id": "codex",
            "rate_limit_reached_type": reached_type,
            "primary": {"used_percent": float(used), "window_minutes": 300, "resets_at": primary_reset},
            "secondary": {"used_percent": float(secondary_used), "window_minutes": 10080, "resets_at": secondary_reset},
        }

    def write_suite_inputs(self, root):
        prompts_root = root / "prompts"
        expected_root = root / "expected"
        snapshot_root = root / "snapshot"
        prompts_root.mkdir()
        expected_root.mkdir()
        snapshot_root.mkdir()
        ai_source = snapshot_root / "core" / "script" / "ai" / "ai_model_catalog.py"
        module_source = snapshot_root / "core" / "script" / "module" / "universal_POM_helper.py"
        ai_source.parent.mkdir(parents=True)
        module_source.parent.mkdir(parents=True)
        ai_source.write_text('OPENAI_TESTING_DEFAULT_MODEL = "gpt-5.6-terra"\nPOM_BOM_TEXT_AGENT_MODEL = OPENAI_TESTING_DEFAULT_MODEL\n', encoding="utf-8")
        module_source.write_text("class UniversalPOMHelper:\n    pass\n", encoding="utf-8")
        simple_expected = {"symbol": "POM_BOM_TEXT_AGENT_MODEL", "resolved_value": "gpt-5.6-terra", "definition_chain": ["OPENAI_TESTING_DEFAULT_MODEL = \"gpt-5.6-terra\"", "POM_BOM_TEXT_AGENT_MODEL = OPENAI_TESTING_DEFAULT_MODEL"], "source": "core/script/ai/ai_model_catalog.py"}
        medium_expected = {"class": "UniversalPOMHelper", "method": "build_pom", "prompt_keys_read": ["user_text"], "mutates_prompt_json": ["size_structure"], "always_return_keys": ["Measurement", "sample_size"], "optional_return_keys": ["universal_debug"], "calls_user_pom_helper": False, "source": "core/script/module/universal_POM_helper.py"}
        complex_expected = {"entry": "UniversalPOMHelper.build_pom", "early_exit_conditions": ["not measurement_names"], "stages": [], "final_merge_fields": ["id"], "always_return_keys": ["Measurement", "sample_size"], "optional_return_keys": ["universal_debug"], "source_files": ["core/script/module/universal_POM_helper.py"]}
        expected_documents = {"simple": simple_expected, "medium": medium_expected, "complex": complex_expected}
        for tier in module.TIERS:
            expected_document = expected_documents[tier]
            payload = json.dumps(expected_document, separators=(",", ":")) + "\n"
            (prompts_root / f"{tier}.txt").write_text(payload, encoding="utf-8")
            (expected_root / f"{tier}.json").write_text(payload, encoding="utf-8")

    def write_codex_home(self, path, label):
        path.mkdir()
        (path / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
        (path / "AGENTS.md").write_text(f"# {label}\n", encoding="utf-8")
        (path / "models_cache.json").write_text('{"models":[]}\n', encoding="utf-8")
        memories_root = path / "memories"
        memories_root.mkdir()
        (memories_root / "memory_summary.md").write_text("# Frozen memory\n", encoding="utf-8")
        (memories_root / module.FROZEN_ROUTING_MEMORY_NAME).write_text("", encoding="utf-8")
        obsidian_vault = memories_root / module.FROZEN_OBSIDIAN_VAULT_NAME
        obsidian_vault.mkdir()
        (obsidian_vault / "AGENTS.md").write_text("# Frozen Obsidian context\n", encoding="utf-8")
        skill_root = path / "skills" / "example-skill"
        plugin_root = path / "plugins" / "cache" / "example" / "1.0.0"
        (skill_root / "agents").mkdir(parents=True)
        (plugin_root / ".codex-plugin").mkdir(parents=True)
        (plugin_root / "skills" / "example-plugin-skill" / "agents").mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("# Example Skill\n", encoding="utf-8")
        (skill_root / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
        (plugin_root / ".codex-plugin" / "plugin.json").write_text('{"name":"example"}\n', encoding="utf-8")
        (plugin_root / "skills" / "example-plugin-skill" / "SKILL.md").write_text("# Example Plugin Skill\n", encoding="utf-8")
        (plugin_root / "skills" / "example-plugin-skill" / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")

    def arguments(self, root, repeat_count=2, tier_repeats=None):
        receipt_runner = root / "fake_receipt_runner.py"
        receipt_runner.write_text(FAKE_RECEIPT_RUNNER, encoding="utf-8")
        direct_home = root / "direct-home"
        global_home = root / "global-home"
        quota_home = root / "quota-home"
        quota_home.mkdir()
        fake_codex = root / "fake-codex"
        fake_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_codex.chmod(0o755)
        self.write_codex_home(direct_home, "direct")
        self.write_codex_home(global_home, "global")
        repeat_arguments = ["--tier-repeats", tier_repeats] if tier_repeats is not None else ["--repeat-count", str(repeat_count)]
        return module.parse_args(["--suite-root", str(root), *repeat_arguments, "--direct-codex-home", str(direct_home), "--global-codex-home", str(global_home), "--receipt-runner", str(receipt_runner), "--codex-bin", str(fake_codex), "--sandbox", "read-only", "--timeout", "2", "--outer-timeout-grace", "1", "--poll-interval-ms", "2", "--quota-codex-home", str(quota_home)])

    def test_default_repeat_count_is_six(self):
        args = module.parse_args(["--suite-root", "/suite", "--direct-codex-home", "/direct", "--global-codex-home", "/global"])
        self.assertEqual(args.repeat_count, 6)
        self.assertEqual(f"{args.direct_model}|{args.direct_effort}", "gpt-5.6-sol|ultra")
        self.assertEqual(f"{args.auto_entry_model}|{args.auto_entry_effort}", "gpt-5.6-luna|max")

    def test_first_result_timestamp_excludes_delayed_post_result_receipt_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            _suite_root, tier_inputs, plan = module.build_frozen_plan(args)
            run_plan = plan["runs"][0]
            module.execute_run(args, run_plan, tier_inputs[run_plan["tier"]]["prompt_text"])
            evidence = json.loads(Path(run_plan["evidence_path"]).read_text(encoding="utf-8"))
            receipt = json.loads(Path(run_plan["receipts"][0]["path"]).read_text(encoding="utf-8"))
            call = json.loads((root / "call-order.jsonl").read_text(encoding="utf-8").strip())
        first_result_elapsed_ms = (evidence["first_result_monotonic_ns"] - evidence["started_monotonic_ns"]) // 1_000_000
        total_wall_elapsed_ms = (evidence["producer_finished_monotonic_ns"] - evidence["started_monotonic_ns"]) // 1_000_000
        self.assertEqual(evidence["first_result_monotonic_ns"], receipt["result_ready_monotonic_ns"])
        self.assertLessEqual(evidence["started_monotonic_ns"], receipt["result_ready_monotonic_ns"])
        self.assertLessEqual(receipt["result_ready_monotonic_ns"], evidence["producer_finished_monotonic_ns"])
        self.assertEqual(receipt["result_ready_clock"], "benchmark-runner-monotonic")
        self.assertEqual(receipt["result_ready_event_sequence"], 1)
        self.assertIsInstance(receipt["child_result_ready_monotonic_ns"], int)
        self.assertEqual(evidence["foreground_main_thread_id"], receipt["thread_id"])
        self.assertEqual([session["thread_id"] for session in evidence["foreground_sessions"]], [receipt["thread_id"]])
        self.assertTrue(evidence["foreground_state_snapshot"]["before_complete"])
        self.assertTrue(evidence["foreground_state_snapshot"]["after_complete"])
        self.assertGreaterEqual(total_wall_elapsed_ms - first_result_elapsed_ms, 100)

    def test_runner_rejects_receipt_ready_event_that_does_not_match_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            _suite_root, tier_inputs, plan = module.build_frozen_plan(args)
            run_plan = plan["runs"][0]
            original_loader = module.load_optional_receipt
            with mock.patch.object(module, "load_optional_receipt", side_effect=lambda path: original_loader(path) | {"result_ready_monotonic_ns": 0}), self.assertRaisesRegex(module.BenchmarkRunnerError, "receipt_result_ready_event_invalid"):
                module.execute_run(args, run_plan, tier_inputs[run_plan["tier"]]["prompt_text"])

    def test_runner_rejects_missing_duplicate_and_wrong_path_result_ready_events(self):
        for event_mode in ["missing", "duplicate", "wrong-path", "missing-main-thread"]:
            with self.subTest(event_mode=event_mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_suite_inputs(root)
                args = self.arguments(root)
                _suite_root, tier_inputs, plan = module.build_frozen_plan(args)
                run_plan = plan["runs"][0]
                with mock.patch.dict(module.os.environ, {"FAKE_RESULT_READY_EVENT_MODE": event_mode}), self.assertRaisesRegex(module.BenchmarkRunnerError, "receipt_result_ready_event_invalid"):
                    module.execute_run(args, run_plan, tier_inputs[run_plan["tier"]]["prompt_text"])

    def test_post_run_census_waits_through_exclusive_lock_longer_than_old_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            locking_connection = sqlite3.connect(state_db_path, check_same_thread=False)
            locking_connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            locking_connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("root-thread", "/tmp/rollout.jsonl", "exec", "gpt-5.6-sol", "ultra", 71100))
            locking_connection.commit()
            locking_connection.execute("BEGIN EXCLUSIVE")

            def release_lock():
                # Leave a margin beyond the census connection's 2-second busy
                # timeout so this test deterministically exercises its retry
                # diagnostics rather than succeeding as the lock is released.
                time.sleep(3.0)
                locking_connection.rollback()
                locking_connection.close()

            release_thread = threading.Thread(target=release_lock)
            release_thread.start()
            started = time.monotonic()
            diagnostics = {}
            try:
                snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="root-thread", timeout_seconds=5, diagnostics=diagnostics)
            finally:
                release_thread.join()
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2.8)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["threads"]["root-thread"]["tokens_used"], 71100)
        self.assertGreaterEqual(diagnostics["sqlite_error_count"], 1)
        # sqlite3.Error.sqlite_errorname (3.11+) reports the precise code, e.g. SQLITE_BUSY;
        # the generic label is only the fallback when the attribute is unavailable.
        self.assertIn(diagnostics["last_sqlite_error_name"], BUSY_OR_LOCKED_ERROR_NAMES)

    def test_post_run_census_fails_closed_when_required_thread_never_appears(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            connection = sqlite3.connect(state_db_path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            connection.commit()
            connection.close()
            snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="missing-thread", timeout_seconds=0.02)
        self.assertEqual(snapshot, {"complete": False, "threads": {}})

    def test_post_run_census_waits_for_late_child_row_and_then_quiesces(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            connection = sqlite3.connect(state_db_path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("root-thread", "/tmp/root.jsonl", "exec", "gpt-5.6-sol", "ultra", 71100))
            connection.commit()
            connection.close()

            def insert_child():
                time.sleep(0.1)
                child_connection = sqlite3.connect(state_db_path)
                child_source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}}})
                child_connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("child-thread", "/tmp/child.jsonl", child_source, "gpt-5.6-luna", "low", 500))
                child_connection.commit()
                child_connection.close()

            child_thread = threading.Thread(target=insert_child)
            child_thread.start()
            try:
                snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="root-thread", timeout_seconds=3)
            finally:
                child_thread.join()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(set(snapshot["threads"]), {"root-thread", "child-thread"})

    def test_post_run_census_preserves_stable_success_across_alternating_lock_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            connection = sqlite3.connect(state_db_path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("root-thread", "/tmp/root.jsonl", "exec", "gpt-5.6-sol", "ultra", 72075))
            connection.commit()
            connection.close()
            real_connect = sqlite3.connect
            connect_count = 0

            def alternating_connect(*args, **kwargs):
                nonlocal connect_count
                connect_count += 1
                if connect_count % 2 == 0:
                    raise sqlite3.OperationalError("database is locked")
                return real_connect(*args, **kwargs)

            diagnostics = {}
            with mock.patch.object(module.sqlite3, "connect", side_effect=alternating_connect):
                snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="root-thread", timeout_seconds=1, diagnostics=diagnostics)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(connect_count, 3)
        self.assertEqual(diagnostics["successful_read_count"], 2)
        self.assertEqual(diagnostics["sqlite_error_count"], 1)
        # sqlite3.Error.sqlite_errorname (3.11+) reports the precise code, e.g. SQLITE_BUSY;
        # the generic label is only the fallback when the attribute is unavailable.
        self.assertIn(diagnostics["last_sqlite_error_name"], BUSY_OR_LOCKED_ERROR_NAMES)
        self.assertEqual(diagnostics["status"], "complete")

    def test_post_run_census_uses_immutable_main_db_when_wal_is_empty_and_normal_reads_all_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            connection = sqlite3.connect(state_db_path)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("root-thread", "/tmp/root.jsonl", "exec", "gpt-5.6-sol", "ultra", 71112))
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            self.assertTrue(module.sqlite_main_database_is_wal(state_db_path))
            # Precondition, not the behavior under test: the WAL carries no unread frames.
            # A TRUNCATE checkpoint leaves a 0-byte sidecar on some SQLite builds and
            # removes it entirely on others (3.53 here); both mean "nothing left in WAL".
            wal_path = Path(f"{state_db_path}-wal")
            self.assertEqual(wal_path.stat().st_size if wal_path.exists() else 0, 0)
            real_connect = sqlite3.connect
            normal_connect_count = 0
            immutable_connect_count = 0

            def immutable_only_connect(database, *args, **kwargs):
                nonlocal normal_connect_count, immutable_connect_count
                if "immutable=1" in str(database):
                    immutable_connect_count += 1
                    return real_connect(database, *args, **kwargs)
                normal_connect_count += 1
                raise sqlite3.OperationalError("disk I/O error")

            diagnostics = {}
            with mock.patch.object(module.sqlite3, "connect", side_effect=immutable_only_connect):
                snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="root-thread", timeout_seconds=1, diagnostics=diagnostics)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["threads"]["root-thread"]["tokens_used"], 71112)
        self.assertEqual(normal_connect_count, 2)
        self.assertEqual(immutable_connect_count, 2)
        self.assertEqual(diagnostics["immutable_attempt_count"], 2)
        self.assertEqual(diagnostics["immutable_success_count"], 2)
        self.assertEqual(diagnostics["last_sqlite_error_category"], "disk_io")

    def test_post_run_census_never_uses_immutable_when_wal_has_uncheckpointed_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_db_path = Path(temporary) / "state_5.sqlite"
            writer_connection = sqlite3.connect(state_db_path)
            self.assertEqual(writer_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer_connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT, tokens_used INTEGER NOT NULL)")
            writer_connection.execute("INSERT INTO threads (id, rollout_path, source, model, reasoning_effort, tokens_used) VALUES (?, ?, ?, ?, ?, ?)", ("root-thread", "/tmp/root.jsonl", "exec", "gpt-5.6-sol", "ultra", 100))
            writer_connection.commit()
            self.assertGreater(Path(f"{state_db_path}-wal").stat().st_size, 0)
            diagnostics = {}
            with mock.patch.object(module.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk I/O error")):
                snapshot = module.read_runtime_thread_snapshot(state_db_path, required_thread_id="root-thread", timeout_seconds=0.02, diagnostics=diagnostics)
            writer_connection.close()
        self.assertEqual(snapshot, {"complete": False, "threads": {}})
        self.assertGreater(diagnostics["normal_sqlite_error_count"], 0)
        self.assertEqual(diagnostics["immutable_attempt_count"], 0)

    def test_fake_receipt_suite_freezes_plan_alternates_and_passes_strict_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            (root / "pilot-receipt.json").write_text('{"thread_id":"unreceipted-pilot"}\n', encoding="utf-8")
            result = module.run_suite(args)
            plan = json.loads((root / "suite-plan.json").read_text(encoding="utf-8"))
            summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
            direct_manifest = json.loads((root / "manifests" / "simple-r01-direct.json").read_text(encoding="utf-8"))
            census_diagnostics = json.loads((root / "raw" / "simple-r01-direct" / "census-diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(result["run_count"], 12)
        self.assertEqual(result["overall_status"], "pass")
        self.assertEqual(summary["overall_status"], "pass")
        self.assertTrue(all(summary["tiers"][tier]["paired_wins"] == {"steady_state_logical_tokens": 2, "steady_state_execution_elapsed_ms": 2, "first_result_elapsed_ms": 2, "total_wall_elapsed_ms": 2} for tier in module.TIERS))
        performance_calls = [call for call in calls if not call["run_id"].endswith("-sol-entry-probe")]
        probe_calls = [call for call in calls if call["run_id"].endswith("-sol-entry-probe")]
        self.assertEqual([(call["run_id"], call["direct"]) for call in performance_calls[:6]], [("simple-r01-direct", True), ("simple-r01-global", False), ("medium-r01-direct", True), ("medium-r01-global", False), ("complex-r01-direct", True), ("complex-r01-global", False)])
        self.assertEqual([(call["run_id"], call["direct"]) for call in performance_calls[6:]], [("simple-r02-global", False), ("simple-r02-direct", True), ("medium-r02-global", False), ("medium-r02-direct", True), ("complex-r02-global", False), ("complex-r02-direct", True)])
        self.assertEqual([call["run_id"] for call in probe_calls], ["simple-r01-global-sol-entry-probe", "medium-r01-global-sol-entry-probe", "complex-r01-global-sol-entry-probe"])
        self.assertTrue(all(call["plan_exists"] for call in calls))
        self.assertTrue(all((call["model"], call["effort"]) == (("gpt-5.6-sol", "ultra") if call["direct"] else ("gpt-5.6-luna", "max")) and call["sandbox"] == "read-only" and call["workdir"] == str((root / "snapshot").resolve()) for call in performance_calls))
        self.assertTrue(all((call["model"], call["effort"]) == ("gpt-5.6-sol", "ultra") and call["sandbox"] == "read-only" and call["workdir"] == str((root / "snapshot").resolve()) for call in probe_calls))
        self.assertTrue(all(Path(call["state_db"]).is_absolute() and Path(call["state_db"]).parent == Path(call["codex_home"]) for call in calls))
        self.assertTrue(all(call["benchmark_run_id"] == f"benchmark-{call['run_id']}" for call in calls))
        self.assertTrue(all(call["direct"] != call["bootstrap"] and call["entry"] is False and call["entry_env_present"] is False for call in calls))
        for tier in module.TIERS:
            tier_calls = [call for call in calls if call["run_id"].startswith(tier)]
            self.assertEqual(len({call["prompt_sha256"] for call in tier_calls}), 1)
        self.assertEqual(len(plan["runs"]), 12)
        self.assertNotIn("unreceipted-pilot", json.dumps(plan))
        self.assertNotIn("unreceipted-pilot", json.dumps(summary))
        self.assertEqual(plan["runs"][0]["environment"]["config_path"], str((root / "direct-home" / "config.toml").resolve()))
        self.assertEqual(plan["runs"][1]["environment"]["config_path"], str((root / "global-home" / "config.toml").resolve()))
        self.assertEqual(plan["runs"][0]["receipts"][0]["role"], "result-producer")
        self.assertEqual(plan["runs"][1]["receipts"][0]["role"], "result-producer")
        self.assertEqual(plan["runs"][0]["selected_entry_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(plan["runs"][1]["selected_entry_pair"], "gpt-5.6-luna|max")
        self.assertEqual(len([run for run in plan["runs"] if "dual_entry_probe" in run]), 3)
        self.assertTrue(all(run_summary.get("sol_entry_probe_status") == "pass" for run_summary in result["runs"] if run_summary["run_id"].endswith("r01-global")))
        self.assertEqual(plan["runs"][0]["environment"]["visible_catalog_sha256"], plan["runs"][1]["environment"]["visible_catalog_sha256"])
        self.assertEqual(plan["runs"][0]["environment"]["skills_catalog_file_count"], 2)
        self.assertEqual(plan["runs"][0]["environment"]["plugins_catalog_file_count"], 3)
        self.assertEqual(direct_manifest["acceptance_status"], "pass")
        self.assertIsNotNone(direct_manifest["environment_sha256"])
        self.assertEqual(direct_manifest["runtime_session_count"], 1)
        self.assertEqual(direct_manifest["runtime_root_session_count"], 1)
        self.assertEqual(direct_manifest["runtime_descendant_session_count"], 0)
        self.assertEqual(direct_manifest["logical_total_tokens"], 1000)
        self.assertEqual(set(census_diagnostics), {"schema_version", "before", "foreground", "after"})
        self.assertEqual(census_diagnostics["schema_version"], 2)
        self.assertEqual(census_diagnostics["foreground"]["status"], "complete")
        self.assertEqual(census_diagnostics["after"]["status"], "complete")
        self.assertEqual(census_diagnostics["after"]["successful_read_count"], 2)
        self.assertNotIn("thread_id", json.dumps(census_diagnostics))

    def test_post_result_ending_session_is_censused_but_excluded_from_logical_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            with mock.patch.dict(module.os.environ, {"FAKE_POST_RESULT_ENDING": "1"}):
                module.run_suite(args)
            manifest = json.loads((root / "manifests" / "simple-r01-direct.json").read_text(encoding="utf-8"))
            evidence = json.loads((root / "raw" / "simple-r01-direct" / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "pass")
        self.assertEqual(manifest["runtime_session_count"], 2)
        self.assertEqual(manifest["runtime_descendant_session_count"], 1)
        self.assertEqual(manifest["logical_total_tokens"], 1000)
        self.assertEqual(len(evidence["foreground_sessions"]), 1)
        self.assertEqual(len(evidence["runtime_sessions"]), 2)

    def test_odd_repeat_count_fails_before_plan_or_receipt_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root, repeat_count=3)
            with self.assertRaisesRegex(module.BenchmarkRunnerError, "repeat_count_must_be_even"):
                module.run_suite(args)
            self.assertFalse((root / "suite-plan.json").exists())
            self.assertFalse((root / "call-order.jsonl").exists())

    def test_external_plugin_symlink_is_rejected_before_plan_or_launch(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external_temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            direct_plugins = root / "direct-home" / "plugins"
            shutil.rmtree(direct_plugins)
            external_plugins = Path(external_temporary) / "plugins"
            external_plugins.mkdir()
            direct_plugins.symlink_to(external_plugins, target_is_directory=True)
            with self.assertRaisesRegex(module.BenchmarkRunnerError, "plugins_catalog_symlink_forbidden"):
                module.run_suite(args)
            self.assertFalse((root / "suite-plan.json").exists())
            self.assertFalse((root / "call-order.jsonl").exists())

    def test_external_marketplace_catalog_is_rejected_before_plan_or_launch(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external_temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            marketplace_root = Path(external_temporary) / "marketplace"
            marketplace_root.mkdir()
            (marketplace_root / "marketplace.json").write_text('{"name":"external"}\n', encoding="utf-8")
            marketplace_config = f'\n[marketplaces.external]\nsource_type = "local"\nsource = "{marketplace_root}"\n'
            for home_name in ("direct-home", "global-home"):
                with (root / home_name / "config.toml").open("a", encoding="utf-8") as config_handle:
                    config_handle.write(marketplace_config)
            with self.assertRaisesRegex(module.BenchmarkRunnerError, "catalog_root_outside_suite"):
                module.run_suite(args)
            self.assertFalse((root / "suite-plan.json").exists())
            self.assertFalse((root / "call-order.jsonl").exists())

    def test_environment_drift_is_rejected_before_spending_the_next_arm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            environment_hash = "a" * 64
            drift_error = module.benchmark_suite_gate.BenchmarkGateError("environment_catalog_drift")
            with mock.patch.object(module.benchmark_suite_gate, "validate_environment_snapshot", side_effect=[environment_hash, environment_hash, drift_error]):
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_gate_environment_catalog_drift"):
                    module.run_suite(args)
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call["run_id"] for call in calls], ["simple-r01-direct"])

    def test_per_tier_repeat_cli_freezes_counts_and_alternates_each_tier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root, tier_repeats="simple=4,medium=2,complex=2")
            result = module.run_suite(args)
            plan = json.loads((root / "suite-plan.json").read_text(encoding="utf-8"))
            summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["run_count"], 16)
        self.assertNotIn("repeat_count", plan)
        self.assertEqual(plan["tier_repeat_counts"], {"simple": 4, "medium": 2, "complex": 2})
        self.assertEqual(summary["tier_repeat_counts"], plan["tier_repeat_counts"])
        performance_calls = [call for call in calls if not call["run_id"].endswith("-sol-entry-probe")]
        simple_directions = [call["direct"] for call in performance_calls if call["run_id"].startswith("simple")]
        self.assertEqual(simple_directions, [True, False, False, True, True, False, False, True])
        self.assertEqual(len([call for call in performance_calls if call["run_id"].startswith("medium")]), 4)
        self.assertEqual(len([call for call in performance_calls if call["run_id"].startswith("complex")]), 4)
        self.assertEqual(len(calls) - len(performance_calls), 3)
        self.assertEqual(summary["overall_status"], "pass")

    def test_invalid_per_tier_repeat_cli_fails_before_plan_or_launch(self):
        cases = ["simple=3,medium=2,complex=2", "simple=4,medium=2", "simple=4,medium=2,unknown=2"]
        for tier_repeats in cases:
            with self.subTest(tier_repeats=tier_repeats), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_suite_inputs(root)
                args = self.arguments(root, tier_repeats=tier_repeats)
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "tier_repeats_invalid"):
                    module.run_suite(args)
                self.assertFalse((root / "suite-plan.json").exists())
                self.assertFalse((root / "call-order.jsonl").exists())

    def test_existing_formal_outputs_block_rerun_and_preserve_first_suite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            module.run_suite(args)
            first_plan = (root / "suite-plan.json").read_bytes()
            with self.assertRaisesRegex(module.BenchmarkRunnerError, "suite_outputs_already_exist"):
                module.run_suite(args)
            self.assertEqual((root / "suite-plan.json").read_bytes(), first_plan)
            self.assertEqual(len((root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()), 15)

    def test_quota_response_parser_keeps_only_bounded_scheduling_fields(self):
        response = {
            "id": module.QUOTA_RESPONSE_ID,
            "result": {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 2000000000},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 2100000000},
                    "rateLimitReachedType": None,
                    "credits": {"balance": "private"},
                },
                "rateLimitResetCredits": {"credits": [{"id": "private-credit-id"}]},
            },
        }
        status = module.parse_quota_response(response)
        self.assertEqual(status["limit_id"], "codex")
        self.assertEqual(status["primary"]["used_percent"], 25.0)
        self.assertEqual(status["secondary"]["used_percent"], 40.0)
        self.assertNotIn("credits", status)
        self.assertNotIn("private-credit-id", json.dumps(status))

    def test_quota_response_accepts_explicit_null_secondary_without_fabricating_usage(self):
        response = {"id": module.QUOTA_RESPONSE_ID, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 23, "windowDurationMins": 10080, "resetsAt": 2100000000}, "secondary": None, "rateLimitReachedType": None}}}
        status = module.parse_quota_response(response)
        self.assertEqual(status, {"limit_id": "codex", "rate_limit_reached_type": None, "primary": {"used_percent": 23.0, "window_minutes": 10080, "resets_at": 2100000000}})
        self.assertIsNone(module.quota_pause(status, 80, "simple-r01"))

    def test_quota_response_still_rejects_missing_or_malformed_windows(self):
        base_rate_limits = {"limitId": "codex", "primary": {"usedPercent": 23, "windowDurationMins": 10080, "resetsAt": 2100000000}, "secondary": None, "rateLimitReachedType": None}
        for label, mutate, expected_error in [("missing-primary", lambda value: value.pop("primary"), "quota_primary_missing"), ("missing-secondary", lambda value: value.pop("secondary"), "quota_secondary_missing"), ("malformed-secondary", lambda value: value.update(secondary=[]), "quota_secondary_missing")]:
            with self.subTest(label=label):
                rate_limits = dict(base_rate_limits)
                mutate(rate_limits)
                with self.assertRaisesRegex(module.QuotaStatusError, expected_error):
                    module.parse_quota_response({"id": module.QUOTA_RESPONSE_ID, "result": {"rateLimits": rate_limits}})

    def test_blocked_or_unknown_preflight_creates_zero_suite_outputs(self):
        cases = [
            (self.quota_status(used=100, reached_type="rate_limit_reached"), None, "quota_reached"),
            (None, module.QuotaStatusError("unavailable"), "quota_status_unknown"),
        ]
        for quota_status, side_effect, expected_reason in cases:
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_suite_inputs(root)
                args = self.arguments(root)
                self.quota_reader.reset_mock(side_effect=True)
                self.quota_reader.return_value = quota_status
                self.quota_reader.side_effect = side_effect
                with self.assertRaises(module.BenchmarkPaused) as raised:
                    module.run_suite(args)
                self.assertEqual(raised.exception.reason, expected_reason)
                self.assertEqual(self.quota_reader.call_args.args[1], (root / "quota-home").resolve())
                for output_name in ["suite-plan.json", module.RUNNER_CONFIG_NAME, "summary.json", "raw", "manifests", "call-order.jsonl"]:
                    self.assertFalse((root / output_name).exists())
                self.assertFalse((root / "direct-home" / "state_5.sqlite").exists())
                self.assertFalse((root / "global-home" / "state_5.sqlite").exists())

    def test_pair_boundary_pause_resumes_without_duplicate_arms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            available = self.quota_status(used=0)
            blocked = self.quota_status(used=80)
            self.quota_reader.side_effect = [available, blocked]
            with self.assertRaises(module.BenchmarkPaused) as raised:
                module.run_suite(args)
            self.assertEqual(raised.exception.reason, "quota_headroom_low")
            first_calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call["run_id"] for call in first_calls], ["simple-r01-direct", "simple-r01-global", "simple-r01-global-sol-entry-probe"])
            self.assertTrue((root / "suite-plan.json").is_file())
            self.assertTrue((root / module.RUNNER_CONFIG_NAME).is_file())
            self.assertFalse((root / "summary.json").exists())

            args.resume = True
            self.quota_reader.side_effect = None
            self.quota_reader.return_value = available
            result = module.run_suite(args)
            all_calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(all_calls), 15)
            self.assertEqual(len({call["run_id"] for call in all_calls}), 15)
            self.assertEqual(result["run_count"], 12)
            self.assertEqual(result["overall_status"], "pass")
            self.assertTrue(all(summary.get("resumed_existing") is True for summary in result["runs"][:2]))

    def test_resume_rejects_prompt_drift_and_partial_next_pair(self):
        for contamination in ["prompt-drift", "partial-pair"]:
            with self.subTest(contamination=contamination), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_suite_inputs(root)
                args = self.arguments(root)
                self.quota_reader.side_effect = [self.quota_status(used=0), self.quota_status(used=80)]
                with self.assertRaises(module.BenchmarkPaused):
                    module.run_suite(args)
                if contamination == "prompt-drift":
                    (root / "prompts" / "medium.txt").write_text("changed frozen prompt\n", encoding="utf-8")
                    expected_failure = "resume_plan_drift"
                else:
                    (root / "raw" / "medium-r01-direct").mkdir()
                    expected_failure = "cohort_contaminated_partial_pair"
                args.resume = True
                self.quota_reader.side_effect = None
                self.quota_reader.return_value = self.quota_status(used=0)
                with self.assertRaisesRegex(module.BenchmarkRunnerError, expected_failure):
                    module.run_suite(args)

    def test_launched_availability_failure_contaminates_and_cannot_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            original_execute_run = module.execute_run

            def fail_after_launch(run_args, run_plan, prompt_text):
                run_summary = original_execute_run(run_args, run_plan, prompt_text)
                receipt_path = Path(run_plan["receipts"][0]["path"])
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt.update({"status": "fail", "failure_class": "availability", "turn_completed": False, "metrics_complete": False})
                receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                return run_summary

            with mock.patch.object(module, "execute_run", side_effect=fail_after_launch):
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_availability"):
                    module.run_suite(args)
            self.assertEqual(len((root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            args.resume = True
            with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_partial_pair"):
                module.run_suite(args)

    def test_full_gate_rejects_first_bad_result_before_next_arm_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            original_execute_run = module.execute_run

            def corrupt_result_after_launch(run_args, run_plan, prompt_text):
                run_summary = original_execute_run(run_args, run_plan, prompt_text)
                Path(run_plan["result_path"]).write_text('{"wrong":true}\n', encoding="utf-8")
                return run_summary

            with mock.patch.object(module, "execute_run", side_effect=corrupt_result_after_launch):
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_gate_result_not_exact"):
                    module.run_suite(args)
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([call["run_id"] for call in calls], ["simple-r01-direct"])

    def test_full_gate_rejects_incomplete_census_before_next_arm_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            original_snapshot = module.read_runtime_thread_snapshot

            def incomplete_post_run_snapshot(state_db_path, required_thread_id=None, timeout_seconds=module.RUNTIME_CENSUS_TIMEOUT_SECONDS, diagnostics=None, quiescence_seconds=module.RUNTIME_CENSUS_QUIESCENCE_SECONDS):
                if required_thread_id is not None:
                    return {"complete": False, "threads": {}}
                return original_snapshot(state_db_path, required_thread_id=required_thread_id, timeout_seconds=timeout_seconds, diagnostics=diagnostics, quiescence_seconds=quiescence_seconds)

            with mock.patch.object(module, "read_runtime_thread_snapshot", side_effect=incomplete_post_run_snapshot):
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_gate_evidence_foreground_state_snapshot_incomplete"):
                    module.run_suite(args)
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([call["run_id"] for call in calls], ["simple-r01-direct"])

    def test_rollout_file_census_exposes_hidden_session_before_next_arm_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_suite_inputs(root)
            args = self.arguments(root)
            before_rollouts = {"available": False, "complete": True, "thread_ids": set()}
            after_rollouts = {"available": True, "complete": True, "thread_ids": {"fake-simple-r01-direct", "hidden-child"}}
            with mock.patch.object(module, "read_runtime_rollout_snapshot", side_effect=[before_rollouts, after_rollouts]):
                with self.assertRaisesRegex(module.BenchmarkRunnerError, "cohort_contaminated_gate_evidence_foreground_unknown_session"):
                    module.run_suite(args)
            calls = [json.loads(line) for line in (root / "call-order.jsonl").read_text(encoding="utf-8").splitlines()]
            diagnostics = json.loads((root / "raw" / "simple-r01-direct" / "census-diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual([call["run_id"] for call in calls], ["simple-r01-direct"])
        self.assertEqual(diagnostics["after"]["rollout_new_session_count"], 2)
        self.assertEqual(diagnostics["after"]["rollout_db_cross_check"], "fail")


if __name__ == "__main__":
    unittest.main()
