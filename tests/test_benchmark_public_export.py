#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_public_export.py"
MODULE_SPEC = importlib.util.spec_from_file_location("benchmark_public_export", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)

RENDERER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_benchmark_svg.py"
RENDERER_SPEC = importlib.util.spec_from_file_location("render_benchmark_svg_for_export_test", RENDERER_PATH)
renderer = importlib.util.module_from_spec(RENDERER_SPEC)
RENDERER_SPEC.loader.exec_module(renderer)


class BenchmarkPublicExportTests(unittest.TestCase):
    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    def relative(self, root, path):
        return path.relative_to(root).as_posix()

    def make_environment(self, root, arm, source_root):
        codex_home = root / f"{arm}-codex-home"
        config_path = codex_home / "config.toml"
        agents_path = codex_home / "AGENTS.md"
        models_cache_path = codex_home / "models_cache.json"
        memories_root = codex_home / "memories"
        receipt_runner_path = root / "receipt-runner.py"
        skill_path = codex_home / "skills" / "test-skill" / "SKILL.md"
        plugin_manifest_path = codex_home / "plugins" / "cache" / "test-plugin" / "1.0.0" / ".codex-plugin" / "plugin.json"
        skill_path.parent.mkdir(parents=True)
        plugin_manifest_path.parent.mkdir(parents=True)
        config_path.write_text("model = 'test'\n", encoding="utf-8")
        agents_path.write_text(f"# {arm}\n", encoding="utf-8")
        models_cache_path.write_text('{"models":[]}\n', encoding="utf-8")
        memories_root.mkdir()
        (memories_root / "memory_summary.md").write_text("# Frozen memory\n", encoding="utf-8")
        if not receipt_runner_path.exists():
            receipt_runner_path.write_text("# runner\n", encoding="utf-8")
        skill_path.write_text("# Test skill\n", encoding="utf-8")
        plugin_manifest_path.write_text('{"name":"test-plugin"}\n', encoding="utf-8")
        catalog = module.benchmark_suite_gate.catalog_snapshot(codex_home, config_path)
        return {"codex_home": str(codex_home), "config_path": str(config_path), "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(), "agents_path": str(agents_path), "agents_sha256": hashlib.sha256(agents_path.read_bytes()).hexdigest(), "models_cache_path": str(models_cache_path), "models_cache_sha256": module.benchmark_suite_gate.models_cache_sha256(models_cache_path), "memories_root": str(memories_root), "memories_sha256": module.benchmark_suite_gate.sha256_source_tree(memories_root), "workdir": str(source_root), "sandbox": "read-only", "receipt_runner_path": str(receipt_runner_path), "receipt_runner_sha256": hashlib.sha256(receipt_runner_path.read_bytes()).hexdigest(), **catalog}

    def make_receipt(self, path, thread_id, pair, result_message, total_tokens, workload_prompt_sha256, workload_id, arm, result_ready_monotonic_ns, raw_prompt_text):
        model, effort = pair.split("|", 1)
        node_type = "direct-task" if arm == "direct" else "bootstrap-task"
        authorization_source = "benchmark-direct" if arm == "direct" else "benchmark-global-inline"
        execution_prompt = raw_prompt_text if arm == "direct" else module.benchmark_suite_gate.auto_benchmark_execution_prompt(raw_prompt_text)
        receipt = {"schema_version": 1, "status": "pass", "failure_class": None, "turn_completed": True, "exit_code": 0, "metrics_complete": True, "tokens_lower_bound": False, "model_match": True, "effort_match": True, "pair_match": True, "authorization_status": "authorized", "authorization_source": authorization_source, "entry_context_active": False, "benchmark_run_id": f"benchmark-{workload_id}", "benchmark_result_source": "controller_final", "workload_id": workload_id, "node_type": node_type, "thread_id": thread_id, "requested_pair": pair, "effective_pair": pair, "requested_model": model, "requested_effort": effort, "resolved_model": model, "resolved_effort": effort, "effective_model": model, "node_role": "result-producer", "route_attempts": [{"status": "pass", "executed_pair": pair}], "reroutes": [], "tokens": {"total_tokens": total_tokens}, "process_elapsed_ms": 60 if arm == "global" else 100, "output_sha256": hashlib.sha256(result_message.encode("utf-8")).hexdigest(), "result_published": True, "result_ready_monotonic_ns": result_ready_monotonic_ns, "child_result_ready_monotonic_ns": result_ready_monotonic_ns, "result_ready_clock": "benchmark-runner-monotonic", "result_ready_event_sequence": 1, "duplicate_result_detected": False, "workload_prompt_sha256": workload_prompt_sha256, "prompt_sha256": hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()}
        if arm == "global":
            receipt["benchmark_prompt_file_verified"] = True
            receipt["benchmark_auto_launch_verified"] = True
            receipt["benchmark_auto_workspace_count"] = 1
            receipt["benchmark_auto_bridge_result_verified"] = True
        self.write_json(path, receipt)

    def make_evidence(self, path, run_id, thread_id, pair, total_tokens, first_result_ms, producer_elapsed_ms, arm, task_tokens=None):
        model, effort = pair.split("|", 1)
        runtime_session = {"thread_id": thread_id, "parent_thread_id": None, "source_kind": "root", "model": model, "effort": effort, "tokens_used": total_tokens, "rollout_sha256": "a" * 64, "rollout_model": model, "rollout_effort": effort, "rollout_total_tokens": total_tokens, "turn_completed": True}
        runtime_sessions = [runtime_session]
        if arm == "global":
            adaptive_pair = "gpt-5.6-terra|high"
            adaptive_model, adaptive_effort = adaptive_pair.split("|", 1)
            runtime_sessions.append({"thread_id": f"{thread_id}-adaptive", "parent_thread_id": thread_id, "source_kind": "subagent", "model": adaptive_model, "effort": adaptive_effort, "tokens_used": task_tokens, "rollout_sha256": "b" * 64, "rollout_model": adaptive_model, "rollout_effort": adaptive_effort, "rollout_total_tokens": task_tokens, "turn_completed": True})
        before_thread_ids = []
        after_thread_ids = [runtime["thread_id"] for runtime in runtime_sessions]
        state_snapshot = {"before_complete": True, "after_complete": True, "before_thread_count": 0, "after_thread_count": len(after_thread_ids), "before_thread_ids_sha256": module.benchmark_suite_gate.sha256_text(module.benchmark_suite_gate.canonical_json(before_thread_ids)), "after_thread_ids_sha256": module.benchmark_suite_gate.sha256_text(module.benchmark_suite_gate.canonical_json(after_thread_ids))}
        foreground_sessions = [{key: runtime[key] for key in module.benchmark_suite_gate.FOREGROUND_SESSION_KEYS} for runtime in runtime_sessions]
        evidence = {"schema_version": module.benchmark_suite_gate.SCHEMA_VERSION, "run_id": run_id, "started_monotonic_ns": 1_000_000_000, "first_result_monotonic_ns": 1_000_000_000 + first_result_ms * 1_000_000, "producer_finished_monotonic_ns": 1_000_000_000 + producer_elapsed_ms * 1_000_000, "producer_process_exit_code": 0, "producer_timed_out": False, "producer_complete": True, "foreground_main_thread_id": thread_id, "foreground_state_snapshot": state_snapshot, "foreground_sessions": foreground_sessions, "launched_session_ids": after_thread_ids, "retry_session_ids": [], "fallback_session_ids": [], "repair_session_ids": [], "state_snapshot": state_snapshot, "runtime_sessions": runtime_sessions}
        self.write_json(path, evidence)

    def build_fixture(self, root, tier_repeat_counts=None):
        tier_repeat_counts = tier_repeat_counts or {tier: module.MINIMUM_PUBLIC_PAIR_COUNT for tier in module.benchmark_suite_gate.TIERS}
        source_root = root / "snapshot"
        source_root.mkdir()
        (source_root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        source_snapshot_sha256 = module.benchmark_suite_gate.sha256_source_tree(source_root)
        environments = {arm: self.make_environment(root, arm, source_root) for arm in module.benchmark_suite_gate.ARMS}
        runs = []
        order_index = 1
        for repeat_index in range(1, max(tier_repeat_counts.values()) + 1):
            arm_order = ("direct", "global") if repeat_index % 2 == 1 else ("global", "direct")
            for tier in module.benchmark_suite_gate.TIERS:
                if repeat_index > tier_repeat_counts[tier]:
                    continue
                prompt_path = root / "prompts" / f"{tier}.txt"
                expected_path = root / "expected" / f"{tier}.json"
                expected_document = {"tier": tier, "answer": "ok", "source_files": ["source.py"]}
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(f"{tier} prompt\n", encoding="utf-8")
                self.write_json(expected_path, expected_document)
                pair_id = f"{tier}-r{repeat_index:02d}"
                prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
                expected_sha256 = hashlib.sha256(expected_path.read_bytes()).hexdigest()
                for arm in arm_order:
                    selected_pair = module.benchmark_suite_gate.ARM_ENTRY_PAIRS[arm]
                    run_id = f"{pair_id}-{arm}"
                    raw_root = root / "raw" / run_id
                    result_path = raw_root / "result.json"
                    evidence_path = raw_root / "evidence.json"
                    receipt_path = raw_root / "receipt.json"
                    result_message = json.dumps(expected_document, sort_keys=True, separators=(",", ":"))
                    self.write_json(result_path, expected_document)
                    total_tokens = 1000 + repeat_index if arm == "direct" else 50 + repeat_index
                    task_tokens = total_tokens if arm == "direct" else 500 + repeat_index
                    first_result_ms = 200 + repeat_index if arm == "direct" else 100 + repeat_index
                    producer_elapsed_ms = 240 + repeat_index if arm == "direct" else 120 + repeat_index
                    thread_id = f"private-session-{run_id}"
                    result_ready_monotonic_ns = 1_000_000_000 + first_result_ms * 1_000_000
                    self.make_receipt(receipt_path, thread_id, selected_pair, result_message, total_tokens, prompt_sha256, run_id, arm, result_ready_monotonic_ns, prompt_path.read_text(encoding="utf-8"))
                    if arm == "global":
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                        capability_assignment = [{"node_id": "result", "step_kind": "implementation", "capability_tags": ["code-authoring"], "effective_pair": "gpt-5.6-terra|high"}]
                        signature = {"selected_pair": "gpt-5.6-terra|high", "effective_pair": "gpt-5.6-terra|high", "scheduled_graph": False, "assigned_pairs": ["gpt-5.6-terra|high"], "trial": False, "recommendation_state": "frozen", "selection_provenance": "local_history", "capability_assignment": capability_assignment}
                        selected_execution = {"schema_version": 2, "receipt_sha256": "b" * 64, "selected_pair": "gpt-5.6-terra|high", "effective_pair": "gpt-5.6-terra|high", "steady_state_logical_tokens": task_tokens, "steady_state_execution_elapsed_ms": 60, "calibration_attempt_count": 0, "calibration_failure_elapsed_ms": 0, "calibration_failure_logical_tokens": 0, "route_signature": signature}
                        receipt["benchmark_selected_execution"] = selected_execution
                        self.write_json(receipt_path, receipt)
                    self.make_evidence(evidence_path, run_id, thread_id, selected_pair, total_tokens, first_result_ms, producer_elapsed_ms, arm, task_tokens)
                    receipt_spec = {"path": self.relative(root, receipt_path), "pair": selected_pair, "role": "result-producer", "bind_result": True, "workload_prompt_sha256": prompt_sha256}
                    run_plan = {"run_id": run_id, "pair_id": pair_id, "tier": tier, "repeat_index": repeat_index, "arm": arm, "order_index": order_index, "prompt_path": self.relative(root, prompt_path), "prompt_sha256": prompt_sha256, "expected_result_path": self.relative(root, expected_path), "expected_sha256": expected_sha256, "result_path": self.relative(root, result_path), "evidence_path": self.relative(root, evidence_path), "receipts": [receipt_spec], "selected_entry_pair": selected_pair, "entry_execution_mode": "executed", "source_root": self.relative(root, source_root), "source_files_pointer": "/source_files", "source_snapshot_sha256": source_snapshot_sha256, "environment": environments[arm]}
                    if arm == "global" and repeat_index == 1:
                        probe_root = root / "entry-invariance-probes" / tier
                        probe_receipt_path = probe_root / "receipt.json"
                        probe_result_path = probe_root / "result.json"
                        probe_summary_path = probe_root / "summary.json"
                        self.write_json(probe_result_path, expected_document)
                        probe_execution = {**selected_execution, "receipt_sha256": "d" * 64}
                        probe_receipt = {"schema_version": 1, "status": "pass", "failure_class": None, "node_type": "bootstrap-task", "requested_pair": module.benchmark_suite_gate.SOL_ENTRY_PROBE_PAIR, "effective_pair": module.benchmark_suite_gate.SOL_ENTRY_PROBE_PAIR, "benchmark_prompt_file_verified": True, "benchmark_auto_launch_verified": True, "benchmark_auto_workspace_count": 1, "benchmark_auto_bridge_result_verified": True, "result_published": True, "duplicate_result_detected": False, "output_sha256": hashlib.sha256(result_message.encode("utf-8")).hexdigest(), "benchmark_selected_execution": probe_execution}
                        self.write_json(probe_receipt_path, probe_receipt)
                        probe_summary = {"schema_version": 1, "status": "pass", "reason": "exact_output_and_route_signature_match", "tier": tier, "primary_run_id": run_id, "primary_entry_pair": module.benchmark_suite_gate.AUTO_ENTRY_PAIR, "probe_entry_pair": module.benchmark_suite_gate.SOL_ENTRY_PROBE_PAIR, "expected_result_sha256": expected_sha256, "primary_result_sha256": expected_sha256, "probe_result_sha256": expected_sha256, "primary_route_signature": signature, "probe_route_signature": signature, "route_signature_match": True, "capability_assignment_match": True}
                        self.write_json(probe_summary_path, probe_summary)
                        run_plan["dual_entry_probe"] = {"schema_version": 1, "entry_pair": module.benchmark_suite_gate.SOL_ENTRY_PROBE_PAIR, "receipt_path": self.relative(root, probe_receipt_path), "result_path": self.relative(root, probe_result_path), "summary_path": self.relative(root, probe_summary_path)}
                    runs.append(run_plan)
                    order_index += 1
        plan = {"schema_version": module.benchmark_suite_gate.SCHEMA_VERSION, "suite_id": "benchmark-suite-public-test", "tier_repeat_counts": tier_repeat_counts, "runs": runs}
        module.benchmark_suite_gate.validate_plan(plan)
        plan_path = root / "suite-plan.json"
        self.write_json(plan_path, plan)
        manifest_dir = root / "manifests"
        summary_path = root / "summary.json"
        summary = module.benchmark_suite_gate.evaluate_suite(plan_path, manifest_dir, summary_path)
        return {"plan": plan_path, "summary": summary_path, "manifests": manifest_dir, "output": root / "public.json", "plan_document": plan, "summary_document": summary, "raw": root / "raw"}

    def make_medium_majority_failure(self, paths):
        run_plan = next(run for run in paths["plan_document"]["runs"] if run["run_id"] == "medium-r02-global")
        plan_root = paths["plan"].parent
        evidence_path = plan_root / run_plan["evidence_path"]
        receipt_path = plan_root / run_plan["receipts"][0]["path"]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        result_ready_monotonic_ns = evidence["started_monotonic_ns"] + 3_003_000_000
        evidence["first_result_monotonic_ns"] = result_ready_monotonic_ns
        evidence["producer_finished_monotonic_ns"] = evidence["started_monotonic_ns"] + 3_023_000_000
        receipt["result_ready_monotonic_ns"] = result_ready_monotonic_ns
        receipt["child_result_ready_monotonic_ns"] = result_ready_monotonic_ns
        receipt["benchmark_selected_execution"]["steady_state_execution_elapsed_ms"] = 3_000
        self.write_json(evidence_path, evidence)
        self.write_json(receipt_path, receipt)
        return module.benchmark_suite_gate.evaluate_suite(paths["plan"], paths["manifests"], paths["summary"])

    def test_happy_path_exports_only_sanitized_passing_metrics_and_public_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.build_fixture(Path(temporary))
            public_document = module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            public_text = paths["output"].read_text(encoding="utf-8")
            output_mode = stat.S_IMODE(paths["output"].stat().st_mode)
        self.assertEqual(public_document["overall_status"], "pass")
        self.assertTrue(public_document["all_correct"])
        self.assertEqual(public_document["expected_run_count"], 12)
        self.assertEqual(public_document["tier_repeat_counts"], {"simple": 2, "medium": 2, "complex": 2})
        self.assertEqual(public_document["rules"], {"tokens": module.TOKEN_RULE, "time": module.TIME_RULE, "overall": module.OVERALL_RULE, "minimum_pairs_per_tier": 2})
        self.assertEqual(public_document["entry_pairs"], module.benchmark_suite_gate.ARM_ENTRY_PAIRS)
        self.assertTrue(public_document["configuration"]["config_hash_equal"])
        self.assertTrue(public_document["configuration"]["runtime_context_hash_equal"])
        expected_environment = paths["plan_document"]["runs"][0]["environment"]
        self.assertEqual(public_document["configuration"]["models_cache_sha256"], expected_environment["models_cache_sha256"])
        self.assertEqual(public_document["configuration"]["memories_sha256"], expected_environment["memories_sha256"])
        self.assertTrue(public_document["configuration"]["catalog_hash_equal"])
        self.assertEqual(public_document["configuration"]["catalog_schema_version"], 1)
        self.assertEqual(public_document["configuration"]["catalog_file_counts"], {"skills": expected_environment["skills_catalog_file_count"], "plugins": expected_environment["plugins_catalog_file_count"], "marketplaces": expected_environment["marketplace_catalog_file_count"], "marketplace_sources": len(expected_environment["marketplace_catalog_sources"])})
        self.assertEqual(set(public_document["configuration"]["catalog_sha256"]), {"skills", "plugins", "marketplaces", "visible"})
        self.assertNotEqual(public_document["configuration"]["agents_sha256"]["direct"], public_document["configuration"]["agents_sha256"]["global"])
        self.assertEqual(public_document["execution_integrity"], {"complete_runs": 12, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "runtime_session_count": 18, "runtime_root_count": 12, "runtime_descendant_count": 6, "task_session_count": 12, "controller_tokens_excluded": 309, "multi_session_run_count": 6, "sol_entry_probe_count": 3, "sol_entry_probe_pass_count": 3})
        self.assertEqual([task["label"] for task in public_document["tasks"]], [module.TASK_LABELS[tier] for tier in module.benchmark_suite_gate.TIERS])
        self.assertTrue(all(task["status"] == "pass" and task["failures"] == [] for task in public_document["tasks"]))
        self.assertTrue(all(task["paired_savings_percent_medians"]["steady_state_logical_tokens"] > 0 for task in public_document["tasks"]))
        self.assertIn("not a billing-token", public_document["caveats"]["tokens"])
        self.assertIn("not a universal guarantee", public_document["caveats"]["generalization"])
        self.assertNotIn("/private/", public_text)
        self.assertNotIn("private-session", public_text)
        self.assertNotIn("receipt_session_ids", public_text)
        self.assertNotIn("skills_catalog_root", public_text)
        self.assertNotIn("plugins_catalog_root", public_text)
        self.assertEqual(output_mode, 0o644)

    def test_failed_strategy_gate_with_all_runs_correct_is_exported_and_rendered_as_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.build_fixture(root)
            summary = self.make_medium_majority_failure(paths)
            public_document = module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            desktop_path = root / "desktop.svg"
            mobile_path = root / "mobile.svg"
            renderer.render_svgs(paths["output"], desktop_path, mobile_path)
            desktop_text = desktop_path.read_text(encoding="utf-8")
            mobile_text = mobile_path.read_text(encoding="utf-8")
        medium = next(task for task in public_document["tasks"] if task["tier"] == "medium")
        self.assertEqual(summary["overall_status"], "fail")
        self.assertEqual(public_document["overall_status"], "fail")
        self.assertTrue(public_document["all_correct"])
        self.assertEqual(medium["status"], "pass")
        self.assertEqual(medium["optimization_status"], "fail")
        self.assertEqual(medium["failures"], [])
        self.assertIn("steady_state_aggregate_loss", medium["performance_diagnostics"])
        self.assertIn("Real A/B benchmark · FAIL", desktop_text)
        self.assertIn("Strategy gate FAIL", mobile_text)

    def test_two_pair_confirmation_suite_is_publicly_exportable(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.build_fixture(Path(temporary), {tier: 2 for tier in module.benchmark_suite_gate.TIERS})
            public_document = module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            self.assertEqual(public_document["expected_run_count"], 12)
            self.assertTrue(paths["output"].is_file())

    def test_more_than_minimum_pairs_round_trip_from_export_to_both_renderers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.build_fixture(root, {tier: 8 for tier in module.benchmark_suite_gate.TIERS})
            public_document = module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            desktop_path = root / "desktop.svg"
            mobile_path = root / "mobile.svg"
            renderer.render_svgs(paths["output"], desktop_path, mobile_path)
            desktop_text = desktop_path.read_text(encoding="utf-8")
            mobile_text = mobile_path.read_text(encoding="utf-8")
        self.assertEqual(public_document["expected_run_count"], 48)
        self.assertEqual(public_document["tier_repeat_counts"], {"simple": 8, "medium": 8, "complex": 8})
        self.assertTrue(all(task["pair_count"] == 8 and task["run_count"] == 16 for task in public_document["tasks"]))
        self.assertEqual(desktop_text.count("CORRECT · 8 pairs · 16 runs"), 3)
        self.assertEqual(mobile_text.count("CORRECT · 8 pairs · 16 runs"), 3)

    def test_public_export_requires_exactly_one_direct_runtime_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.build_fixture(Path(temporary))
            manifest_path = sorted(paths["manifests"].glob("*.json"))[0]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime_session_count"] = 2
            manifest["runtime_root_session_count"] = 2
            manifest["runtime_descendant_session_count"] = 0
            manifest["executed_pairs"] = [manifest["selected_entry_pair"], manifest["selected_entry_pair"]]
            self.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(module.PublicExportError, "manifest_runtime_session_count_invalid"):
                module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            self.assertFalse(paths["output"].exists())

    def test_public_export_accepts_runtime_proven_global_adaptive_process_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.build_fixture(root)
            run_plan = next(run for run in paths["plan_document"]["runs"] if run["run_id"] == "simple-r01-global")
            evidence_path = root / run_plan["evidence_path"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            main_thread_id = evidence["foreground_main_thread_id"]
            for session_group in (evidence["foreground_sessions"], evidence["runtime_sessions"]):
                for session in session_group:
                    if session["thread_id"] != main_thread_id:
                        session["source_kind"] = "root"
                        session["parent_thread_id"] = None
            self.write_json(evidence_path, evidence)
            module.benchmark_suite_gate.evaluate_suite(paths["plan"], paths["manifests"], paths["summary"])
            public_document = module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
        self.assertEqual(public_document["overall_status"], "pass")

    def test_plan_summary_and_manifest_tampering_fail_closed_without_overwriting_output(self):
        cases = ["plan_hash", "summary_rule", "summary_extra_key", "manifest_acceptance", "manifest_metric", "manifest_runtime_pair", "manifest_extra_key", "extra_manifest"]
        for tamper_case in cases:
            with self.subTest(tamper_case=tamper_case), tempfile.TemporaryDirectory() as temporary:
                paths = self.build_fixture(Path(temporary))
                paths["output"].write_text("preserve-me\n", encoding="utf-8")
                if tamper_case == "plan_hash":
                    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
                    summary["plan_sha256"] = "0" * 64
                    self.write_json(paths["summary"], summary)
                elif tamper_case in {"summary_rule", "summary_extra_key"}:
                    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
                    if tamper_case == "summary_rule":
                        summary["time_rule"] = "weaker rule"
                    else:
                        summary["caller_claimed_pass"] = True
                    self.write_json(paths["summary"], summary)
                elif tamper_case in {"manifest_acceptance", "manifest_metric", "manifest_runtime_pair", "manifest_extra_key"}:
                    manifest_path = sorted(paths["manifests"].glob("*.json"))[0]
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if tamper_case == "manifest_acceptance":
                        manifest["acceptance_status"] = "fail"
                    elif tamper_case == "manifest_metric":
                        manifest["logical_total_tokens"] = 1
                    elif tamper_case == "manifest_runtime_pair":
                        manifest["executed_pairs"] = ["gpt-5.6-luna|low"]
                    else:
                        manifest["caller_claimed_pass"] = True
                    self.write_json(manifest_path, manifest)
                else:
                    self.write_json(paths["manifests"] / "extra.json", {"run_id": "extra"})
                with self.assertRaises(module.PublicExportError):
                    module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
                preserved_output = paths["output"].read_text(encoding="utf-8")
            self.assertEqual(preserved_output, "preserve-me\n")

    def test_synthetic_passing_manifests_without_raw_evidence_cannot_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.build_fixture(Path(temporary))
            shutil.rmtree(paths["raw"])
            with self.assertRaisesRegex(module.PublicExportError, "manifest_raw_recompute_mismatch"):
                module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            self.assertFalse(paths["output"].exists())

    def test_stale_or_tampered_raw_evidence_cannot_export(self):
        for tamper_case in ["prompt", "expected", "result", "source", "receipt", "evidence", "config", "agents", "models_cache", "memories", "receipt_runner", "skills_catalog", "plugins_catalog"]:
            with self.subTest(tamper_case=tamper_case), tempfile.TemporaryDirectory() as temporary:
                paths = self.build_fixture(Path(temporary))
                plan_root = paths["plan"].parent
                first_run = paths["plan_document"]["runs"][0]
                environment = first_run["environment"]
                if tamper_case == "prompt":
                    prompt_path = plan_root / first_run["prompt_path"]
                    prompt_path.write_text("stale prompt\n", encoding="utf-8")
                elif tamper_case == "expected":
                    expected_path = plan_root / first_run["expected_result_path"]
                    self.write_json(expected_path, {"answer": "stale", "source_files": ["source.py"], "tier": first_run["tier"]})
                elif tamper_case == "result":
                    result_path = plan_root / first_run["result_path"]
                    self.write_json(result_path, {"answer": "stale", "source_files": ["source.py"], "tier": first_run["tier"]})
                elif tamper_case == "source":
                    (plan_root / first_run["source_root"] / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
                elif tamper_case == "receipt":
                    receipt_path = plan_root / first_run["receipts"][0]["path"]
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    receipt["tokens"]["total_tokens"] += 1
                    self.write_json(receipt_path, receipt)
                elif tamper_case == "evidence":
                    evidence_path = plan_root / first_run["evidence_path"]
                    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                    evidence["first_result_monotonic_ns"] += 1_000_000
                    self.write_json(evidence_path, evidence)
                elif tamper_case in {"config", "agents", "models_cache", "receipt_runner"}:
                    Path(environment[f"{tamper_case}_path"]).write_text("tampered\n", encoding="utf-8")
                elif tamper_case == "memories":
                    (Path(environment["memories_root"]) / "tampered.md").write_text("tampered\n", encoding="utf-8")
                elif tamper_case == "skills_catalog":
                    (Path(environment["skills_catalog_root"]) / "test-skill" / "SKILL.md").write_text("tampered\n", encoding="utf-8")
                else:
                    (Path(environment["plugins_catalog_root"]) / "cache" / "test-plugin" / "1.0.0" / ".codex-plugin" / "plugin.json").write_text('{"name":"tampered"}\n', encoding="utf-8")
                with self.assertRaisesRegex(module.PublicExportError, "manifest_raw_recompute_mismatch"):
                    module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
                self.assertFalse(paths["output"].exists())

    def test_invalid_stored_ending_diagnostic_arithmetic_cannot_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.build_fixture(Path(temporary))
            manifest_path = sorted(paths["manifests"].glob("*.json"))[0]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["producer_elapsed_ms"] += 1
            manifest["total_wall_elapsed_ms"] += 1
            self.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(module.PublicExportError, "manifest_diagnostic_arithmetic_mismatch"):
                module.export_public_json(paths["plan"], paths["summary"], paths["manifests"], paths["output"])
            self.assertFalse(paths["output"].exists())

    def test_privacy_guard_rejects_paths_private_identifiers_and_raw_fields(self):
        private_strings = {"private-session-id", "/private/source/result.json"}
        cases = [{"debug": "/private/source/result.json"}, {"debug": "private-session-id"}, {"raw_prompt": "secret"}, {"receipt_session_ids": []}]
        for public_document in cases:
            with self.subTest(public_document=public_document):
                with self.assertRaisesRegex(module.PublicExportError, "public_privacy_violation"):
                    module.validate_public_privacy(public_document, private_strings)


if __name__ == "__main__":
    unittest.main()
