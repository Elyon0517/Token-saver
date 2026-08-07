#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_suite_gate.py"
MODULE_SPEC = importlib.util.spec_from_file_location("benchmark_suite_gate", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


LEGACY_SIMPLE_EXPECTED = {"symbol": "POM_BOM_TEXT_AGENT_MODEL", "resolved_value": "gpt-5.6-terra", "definition_chain": ["OPENAI_TESTING_DEFAULT_MODEL = \"gpt-5.6-terra\"", "POM_BOM_TEXT_AGENT_MODEL = OPENAI_TESTING_DEFAULT_MODEL"], "source": "core/script/ai/ai_model_catalog.py"}
LEGACY_SIMPLE_ACTUAL = {"symbol": "POM_BOM_TEXT_AGENT_MODEL", "resolved_value": "gpt-5.6-terra", "definition_chain": ["POM_BOM_TEXT_AGENT_MODEL=OPENAI_TESTING_DEFAULT_MODEL", "OPENAI_TESTING_DEFAULT_MODEL=\"gpt-5.6-terra\""], "source": "core/script/ai/ai_model_catalog.py"}


class BenchmarkSuiteGateTests(unittest.TestCase):
    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")

    def relative(self, root, path):
        return path.relative_to(root).as_posix()

    def aggregate_tier(self, tier, repeat_count, manifests):
        primary_manifests = []
        for manifest in manifests:
            primary_manifest = dict(manifest)
            primary_manifest.setdefault("steady_state_logical_tokens", primary_manifest["logical_total_tokens"])
            primary_manifest.setdefault("steady_state_execution_elapsed_ms", primary_manifest["first_result_elapsed_ms"])
            primary_manifests.append(primary_manifest)
        return module.aggregate_tier(tier, repeat_count, primary_manifests)

    def make_receipt(self, path, thread_id, pair, role, result_message, total_tokens, workload_prompt_sha256, workload_id, arm, result_ready_monotonic_ns=1, raw_prompt_text=None):
        model, effort = pair.split("|", 1)
        node_type = "direct-task" if arm == "direct" else "bootstrap-task"
        authorization_source = "benchmark-direct" if arm == "direct" else "benchmark-global-inline"
        execution_prompt = module.auto_benchmark_execution_prompt(raw_prompt_text) if arm == "global" and raw_prompt_text is not None else raw_prompt_text
        execution_prompt_sha256 = hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest() if execution_prompt is not None else workload_prompt_sha256
        receipt = {"schema_version": 1, "status": "pass", "failure_class": None, "turn_completed": True, "exit_code": 0, "metrics_complete": True, "tokens_lower_bound": False, "model_match": True, "effort_match": True, "pair_match": True, "authorization_status": "authorized", "authorization_source": authorization_source, "entry_context_active": False, "benchmark_run_id": f"benchmark-{workload_id}", "benchmark_result_source": "controller_final", "workload_id": workload_id, "node_type": node_type, "thread_id": thread_id, "requested_pair": pair, "effective_pair": pair, "requested_model": model, "requested_effort": effort, "resolved_model": model, "resolved_effort": effort, "effective_model": model, "node_role": role, "route_attempts": [{"status": "pass", "executed_pair": pair}], "reroutes": [], "tokens": {"total_tokens": total_tokens}, "process_elapsed_ms": 60 if arm == "global" else 100, "output_sha256": hashlib.sha256(result_message.encode("utf-8")).hexdigest(), "result_published": True, "result_ready_monotonic_ns": result_ready_monotonic_ns, "child_result_ready_monotonic_ns": result_ready_monotonic_ns, "result_ready_clock": "benchmark-runner-monotonic", "result_ready_event_sequence": 1, "duplicate_result_detected": False, "workload_prompt_sha256": workload_prompt_sha256, "prompt_sha256": execution_prompt_sha256}
        if arm == "global":
            receipt["benchmark_prompt_file_verified"] = True
            receipt["benchmark_auto_launch_verified"] = True
            receipt["benchmark_auto_workspace_count"] = 1
            receipt["benchmark_auto_bridge_result_verified"] = True
        self.write_json(path, receipt)
        return receipt

    def make_runtime_session(self, thread_id, pair, total_tokens, parent_thread_id=None, source_kind="root", turn_completed=True):
        model, effort = pair.split("|", 1)
        return {"thread_id": thread_id, "parent_thread_id": parent_thread_id, "source_kind": source_kind, "model": model, "effort": effort, "tokens_used": total_tokens, "rollout_sha256": "a" * 64, "rollout_model": model, "rollout_effort": effort, "rollout_total_tokens": total_tokens, "turn_completed": turn_completed}

    def make_foreground_session(self, runtime_session):
        return {key: runtime_session[key] for key in module.FOREGROUND_SESSION_KEYS}

    def make_evidence(self, path, run_id, session_ids, first_result_ms, total_wall_ms, pair="gpt-5.6-sol|ultra", total_tokens=100, runtime_sessions=None, foreground_sessions=None, foreground_main_thread_id=None):
        runtime_sessions = runtime_sessions or [self.make_runtime_session(session_id, pair, total_tokens) for session_id in session_ids]
        foreground_sessions = [self.make_foreground_session(runtime_session) for runtime_session in runtime_sessions] if foreground_sessions is None else foreground_sessions
        foreground_main_thread_id = foreground_main_thread_id or foreground_sessions[0]["thread_id"]
        before_thread_ids = []
        after_thread_ids = [runtime_session["thread_id"] for runtime_session in runtime_sessions]
        foreground_thread_ids = [foreground_session["thread_id"] for foreground_session in foreground_sessions]
        state_snapshot = {"before_complete": True, "after_complete": True, "before_thread_count": 0, "after_thread_count": len(after_thread_ids), "before_thread_ids_sha256": module.sha256_text(module.canonical_json(sorted(before_thread_ids))), "after_thread_ids_sha256": module.sha256_text(module.canonical_json(sorted(after_thread_ids)))}
        foreground_state_snapshot = {"before_complete": True, "after_complete": True, "before_thread_count": 0, "after_thread_count": len(foreground_thread_ids), "before_thread_ids_sha256": module.sha256_text(module.canonical_json(sorted(before_thread_ids))), "after_thread_ids_sha256": module.sha256_text(module.canonical_json(sorted(foreground_thread_ids)))}
        evidence = {"schema_version": module.SCHEMA_VERSION, "run_id": run_id, "started_monotonic_ns": 1_000_000_000, "first_result_monotonic_ns": 1_000_000_000 + first_result_ms * 1_000_000, "producer_finished_monotonic_ns": 1_000_000_000 + total_wall_ms * 1_000_000, "producer_process_exit_code": 0, "producer_timed_out": False, "producer_complete": True, "foreground_main_thread_id": foreground_main_thread_id, "foreground_state_snapshot": foreground_state_snapshot, "foreground_sessions": foreground_sessions, "launched_session_ids": session_ids, "retry_session_ids": [], "fallback_session_ids": [], "repair_session_ids": [], "state_snapshot": state_snapshot, "runtime_sessions": runtime_sessions}
        self.write_json(path, evidence)
        return evidence

    def selected_execution(self, task_tokens):
        pair = "gpt-5.6-luna|max"
        signature = {
            "selected_pair": pair,
            "effective_pair": pair,
            "scheduled_graph": False,
            "assigned_pairs": [pair],
            "trial": False,
            "recommendation_state": "frozen",
            "selection_provenance": "local_history",
            "capability_assignment": [{"node_id": "result", "step_kind": "implementation", "capability_tags": ["benchmark"], "effective_pair": pair}],
        }
        return {
            "schema_version": 2,
            "receipt_sha256": "b" * 64,
            "selected_pair": pair,
            "effective_pair": pair,
            "steady_state_logical_tokens": task_tokens,
            "steady_state_execution_elapsed_ms": 60,
            "calibration_attempt_count": 0,
            "calibration_failure_elapsed_ms": 0,
            "calibration_failure_logical_tokens": 0,
            "route_signature": signature,
        }

    def write_passing_probe(self, root, run_plan, receipt, result_message):
        probe_root = root / "entry-probes" / run_plan["run_id"]
        receipt_path = probe_root / "receipt.json"
        result_path = probe_root / "result.json"
        summary_path = probe_root / "summary.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(result_message + "\n", encoding="utf-8")
        selected_execution = receipt["benchmark_selected_execution"]
        probe_receipt = dict(receipt)
        probe_receipt.update({
            "thread_id": f"probe-{run_plan['run_id']}",
            "workload_id": f"{run_plan['run_id']}-sol-entry-probe",
            "benchmark_run_id": f"benchmark-{run_plan['run_id']}-sol-entry-probe",
            "requested_pair": module.SOL_ENTRY_PROBE_PAIR,
            "effective_pair": module.SOL_ENTRY_PROBE_PAIR,
            "requested_model": "gpt-5.6-sol",
            "requested_effort": "ultra",
            "resolved_model": "gpt-5.6-sol",
            "resolved_effort": "ultra",
            "effective_model": "gpt-5.6-sol",
            "output_sha256": hashlib.sha256(result_message.encode("utf-8")).hexdigest(),
            "benchmark_selected_execution": selected_execution,
        })
        self.write_json(receipt_path, probe_receipt)
        result_sha256 = hashlib.sha256(result_path.read_bytes()).hexdigest()
        signature = selected_execution["route_signature"]
        summary = {
            "schema_version": 1,
            "status": "pass",
            "reason": "exact_output_and_route_signature_match",
            "tier": run_plan["tier"],
            "primary_run_id": run_plan["run_id"],
            "primary_entry_pair": module.AUTO_ENTRY_PAIR,
            "probe_entry_pair": module.SOL_ENTRY_PROBE_PAIR,
            "expected_result_sha256": run_plan["expected_sha256"],
            "primary_result_sha256": hashlib.sha256((result_message + "\n").encode("utf-8")).hexdigest(),
            "probe_result_sha256": result_sha256,
            "primary_route_signature": signature,
            "probe_route_signature": signature,
            "route_signature_match": True,
            "capability_assignment_match": True,
        }
        self.write_json(summary_path, summary)
        run_plan["dual_entry_probe"] = {
            "schema_version": 1,
            "entry_pair": module.SOL_ENTRY_PROBE_PAIR,
            "receipt_path": self.relative(root, receipt_path),
            "result_path": self.relative(root, result_path),
            "summary_path": self.relative(root, summary_path),
        }

    def make_environment(self, root, name, skill_text="# Test skill\n", marketplace_root=None):
        codex_home = root / name
        config_path = codex_home / "config.toml"
        agents_path = codex_home / "AGENTS.md"
        models_cache_path = codex_home / "models_cache.json"
        memories_root = codex_home / "memories"
        receipt_runner_path = root / "receipt-runner.py"
        skill_path = codex_home / "skills" / "test-skill" / "SKILL.md"
        skill_agent_path = codex_home / "skills" / "test-skill" / "agents" / "openai.yaml"
        plugin_manifest_path = codex_home / "plugins" / "cache" / "test-plugin" / "1.0.0" / ".codex-plugin" / "plugin.json"
        plugin_skill_path = codex_home / "plugins" / "cache" / "test-plugin" / "1.0.0" / "skills" / "plugin-skill" / "SKILL.md"
        skill_agent_path.parent.mkdir(parents=True)
        plugin_manifest_path.parent.mkdir(parents=True)
        plugin_skill_path.parent.mkdir(parents=True)
        config_text = "model = 'test'\n"
        if marketplace_root is not None:
            config_text += f"\n[marketplaces.test-marketplace]\nsource_type = 'local'\nsource = '{marketplace_root}'\n"
        config_path.write_text(config_text, encoding="utf-8")
        agents_path.write_text("# Test agents\n", encoding="utf-8")
        models_cache_path.write_text('{"models":[]}\n', encoding="utf-8")
        memories_root.mkdir()
        (memories_root / "memory_summary.md").write_text("# Frozen memory\n", encoding="utf-8")
        receipt_runner_path.write_text("# runner\n", encoding="utf-8")
        skill_path.write_text(skill_text, encoding="utf-8")
        skill_agent_path.write_text("interface: {}\n", encoding="utf-8")
        plugin_manifest_path.write_text('{"name":"test-plugin"}\n', encoding="utf-8")
        plugin_skill_path.write_text("# Plugin skill\n", encoding="utf-8")
        catalog = module.catalog_snapshot(codex_home, config_path)
        environment = {"codex_home": str(codex_home), "config_path": str(config_path), "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(), "agents_path": str(agents_path), "agents_sha256": hashlib.sha256(agents_path.read_bytes()).hexdigest(), "models_cache_path": str(models_cache_path), "models_cache_sha256": module.models_cache_sha256(models_cache_path), "memories_root": str(memories_root), "memories_sha256": module.sha256_source_tree(memories_root), "workdir": str(root), "sandbox": "read-only", "receipt_runner_path": str(receipt_runner_path), "receipt_runner_sha256": hashlib.sha256(receipt_runner_path.read_bytes()).hexdigest(), **catalog}
        return environment, {"config": config_path, "models_cache": models_cache_path, "memories": memories_root, "skill": skill_path, "plugin": plugin_manifest_path}

    def build_suite(self, root, repeat_count=2, tier_repeat_counts=None):
        source_root = root / "snapshot"
        source_root.mkdir()
        (source_root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        source_snapshot_sha256 = module.sha256_source_tree(source_root)
        runs = []
        run_paths = {}
        order_index = 1
        resolved_repeat_counts = tier_repeat_counts or {tier: repeat_count for tier in module.TIERS}
        for tier in module.TIERS:
            prompt_text = f"{tier} prompt"
            prompt_path = root / "prompts" / f"{tier}.txt"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt_text, encoding="utf-8")
            expected_path = root / "expected" / f"{tier}.json"
            expected_document = {"tier": tier, "answer": "ok", "source_files": ["source.py"]}
            self.write_json(expected_path, expected_document)
            expected_sha256 = hashlib.sha256(expected_path.read_bytes()).hexdigest()
            prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            for repeat_index in range(1, resolved_repeat_counts[tier] + 1):
                pair_id = f"{tier}-{repeat_index}"
                arm_order = ["direct", "global"] if repeat_index % 2 == 1 else ["global", "direct"]
                for arm in arm_order:
                    run_id = f"{pair_id}-{arm}"
                    result_path = root / "raw" / run_id / "result.json"
                    result_message = json.dumps(expected_document, separators=(",", ":"))
                    result_path.parent.mkdir(parents=True, exist_ok=True)
                    result_path.write_text(result_message + "\n", encoding="utf-8")
                    evidence_path = root / "raw" / run_id / "evidence.json"
                    receipt_path = root / "raw" / run_id / "receipt.json"
                    thread_id = f"session-{run_id}"
                    pair = module.ARM_ENTRY_PAIRS[arm]
                    tokens = 1000 + repeat_index if arm == "direct" else 50 + repeat_index
                    task_tokens = tokens if arm == "direct" else 400 + repeat_index
                    first_result_ms = 200 + repeat_index if arm == "direct" else 80 + repeat_index
                    total_wall_ms = 300 + repeat_index if arm == "direct" else 120 + repeat_index
                    result_ready_monotonic_ns = 1_000_000_000 + first_result_ms * 1_000_000
                    self.make_receipt(receipt_path, thread_id, pair, "result-producer", result_message, tokens, prompt_sha256, run_id, arm, result_ready_monotonic_ns, prompt_text)
                    runtime_sessions = [self.make_runtime_session(thread_id, pair, tokens)]
                    if arm == "global":
                        runtime_sessions.append(self.make_runtime_session(f"{thread_id}-adaptive", "gpt-5.6-luna|max", task_tokens, thread_id, "subagent"))
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                        selected_execution = self.selected_execution(task_tokens)
                        receipt["benchmark_selected_execution"] = selected_execution
                        self.write_json(receipt_path, receipt)
                    foreground_sessions = [self.make_foreground_session(runtime_session) for runtime_session in runtime_sessions]
                    self.make_evidence(evidence_path, run_id, [runtime_session["thread_id"] for runtime_session in runtime_sessions], first_result_ms, total_wall_ms, pair, tokens, runtime_sessions, foreground_sessions, thread_id)
                    receipt_spec = {"path": self.relative(root, receipt_path), "pair": pair, "role": "result-producer", "bind_result": True, "workload_prompt_sha256": prompt_sha256}
                    run_plan = {"run_id": run_id, "pair_id": pair_id, "tier": tier, "repeat_index": repeat_index, "arm": arm, "order_index": order_index, "prompt_path": self.relative(root, prompt_path), "prompt_sha256": prompt_sha256, "expected_result_path": self.relative(root, expected_path), "expected_sha256": expected_sha256, "result_path": self.relative(root, result_path), "evidence_path": self.relative(root, evidence_path), "receipts": [receipt_spec], "selected_entry_pair": pair, "entry_execution_mode": "executed", "source_root": self.relative(root, source_root), "source_files_pointer": "/source_files", "source_snapshot_sha256": source_snapshot_sha256}
                    if arm == "global" and repeat_index == 1:
                        self.write_passing_probe(root, run_plan, receipt, result_message)
                    runs.append(run_plan)
                    run_paths[run_id] = {"result": result_path, "evidence": evidence_path, "receipt": receipt_path, "expected": expected_path}
                    order_index += 1
        plan = {"schema_version": module.SCHEMA_VERSION, "suite_id": "suite-test", "tier_repeat_counts": resolved_repeat_counts, "runs": runs} if tier_repeat_counts is not None else {"schema_version": module.SCHEMA_VERSION, "suite_id": "suite-test", "repeat_count": repeat_count, "runs": runs}
        plan_path = root / "plan.json"
        self.write_json(plan_path, plan)
        return plan_path, plan, run_paths

    def test_passing_suite_derives_atomic_manifests_medians_and_strict_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, _ = self.build_suite(root)
            manifest_dir = root / "manifests"
            summary_path = root / "summary.json"
            summary = module.evaluate_suite(plan_path, manifest_dir, summary_path)
            simple_direct_manifest = json.loads((manifest_dir / "simple-1-direct.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["overall_status"], "pass")
        self.assertEqual(summary["overall_rule"], module.OVERALL_RULE)
        self.assertEqual(summary["token_rule"], module.TOKEN_RULE)
        self.assertEqual(summary["time_rule"], module.TIME_RULE)
        self.assertTrue(all(summary["tiers"][tier]["status"] == "pass" for tier in module.TIERS))
        self.assertEqual(summary["tiers"]["simple"]["paired_wins"]["steady_state_logical_tokens"], 2)
        self.assertEqual(summary["tiers"]["simple"]["paired_wins"]["steady_state_execution_elapsed_ms"], 2)
        self.assertEqual(summary["tiers"]["simple"]["direct_medians"]["steady_state_logical_tokens"], 1001.5)
        self.assertEqual(summary["tiers"]["simple"]["global_medians"]["steady_state_logical_tokens"], 401.5)
        self.assertEqual(simple_direct_manifest["acceptance_status"], "pass")
        self.assertEqual(simple_direct_manifest["steady_state_logical_tokens"], 1001)
        self.assertEqual(simple_direct_manifest["steady_state_execution_elapsed_ms"], 201)
        self.assertEqual(simple_direct_manifest["route_selection_elapsed_ms"], 0)
        self.assertEqual(simple_direct_manifest["first_result_elapsed_ms"], 201)
        self.assertEqual(simple_direct_manifest["producer_elapsed_ms"], 301)
        self.assertEqual(simple_direct_manifest["total_wall_elapsed_ms"], simple_direct_manifest["producer_elapsed_ms"] + simple_direct_manifest["ending_real_elapsed_ms"])
        self.assertEqual(simple_direct_manifest["gate"]["generated_by"], "benchmark_suite_gate")
        self.assertEqual(summary["tiers"]["simple"]["direct_totals"]["steady_state_logical_tokens"], 2003)
        self.assertEqual(summary["tiers"]["simple"]["global_totals"]["steady_state_logical_tokens"], 803)
        self.assertEqual(summary["tiers"]["simple"]["metric_gates"]["steady_state_execution_elapsed_ms"]["status"], "pass")

    def test_global_verified_adaptive_bridge_handoff_is_an_accepted_result_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            target_paths = run_paths["simple-1-global"]
            receipt = json.loads(target_paths["receipt"].read_text(encoding="utf-8"))
            receipt["benchmark_result_source"] = "adaptive_bridge_handoff"
            receipt["benchmark_auto_bridge_result_verified"] = True
            self.write_json(target_paths["receipt"], receipt)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "pass")
        self.assertNotIn("receipt_result_source_invalid", manifest["gate"]["failures"])

    def test_arm_pairs_and_auto_controller_exclusion_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, plan, run_paths = self.build_suite(root)
            summary = module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            direct = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
            global_manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
            forged = dict(next(run for run in plan["runs"] if run["run_id"] == "simple-1-global"), selected_entry_pair="gpt-5.6-sol|ultra")
        self.assertEqual(summary["overall_status"], "pass")
        self.assertEqual(direct["selected_entry_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(direct["controller_tokens_excluded"], 0)
        self.assertEqual(global_manifest["selected_entry_pair"], "gpt-5.6-luna|max")
        self.assertEqual(global_manifest["controller_tokens_excluded"], 51)
        self.assertEqual(global_manifest["task_session_count"], 1)
        self.assertEqual(global_manifest["logical_total_tokens"], 401)
        with self.assertRaisesRegex(module.BenchmarkGateError, "plan_arm_entry_pair"):
            module.validate_run_plan(forged, 2)

    def test_auto_arm_without_adaptive_child_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            evidence_path = run_paths["simple-1-global"]["evidence"]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            main_thread_id = evidence["foreground_main_thread_id"]
            evidence["runtime_sessions"] = [session for session in evidence["runtime_sessions"] if session["thread_id"] == main_thread_id]
            evidence["foreground_sessions"] = [session for session in evidence["foreground_sessions"] if session["thread_id"] == main_thread_id]
            evidence["launched_session_ids"] = [main_thread_id]
            for snapshot_name in ("state_snapshot", "foreground_state_snapshot"):
                evidence[snapshot_name]["after_thread_count"] = 1
                evidence[snapshot_name]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json([main_thread_id]))
            self.write_json(evidence_path, evidence)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "fail")
        self.assertFalse(manifest["metrics_complete"])
        self.assertIn("global_adaptive_child_missing", manifest["gate"]["failures"])

    def test_auto_arm_accepts_runtime_proven_adaptive_exec_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            target_paths = run_paths["simple-1-global"]
            evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
            main_thread_id = evidence["foreground_main_thread_id"]
            for session_group in (evidence["foreground_sessions"], evidence["runtime_sessions"]):
                for session in session_group:
                    if session["thread_id"] != main_thread_id:
                        session["source_kind"] = "root"
                        session["parent_thread_id"] = None
            self.write_json(target_paths["evidence"], evidence)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "pass")
        self.assertEqual(manifest["runtime_root_session_count"], 2)
        self.assertEqual(manifest["task_session_count"], 1)

    def test_bound_receipt_ready_timestamp_must_exactly_match_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            target_paths = run_paths["simple-1-direct"]
            receipt = json.loads(target_paths["receipt"].read_text(encoding="utf-8"))
            receipt["result_ready_monotonic_ns"] += 1
            self.write_json(target_paths["receipt"], receipt)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
        self.assertIn("receipt_result_ready_timing_mismatch", manifest["gate"]["failures"])
        self.assertFalse(manifest["metrics_complete"])
        self.assertEqual(manifest["acceptance_status"], "fail")

    def test_result_ready_timestamp_must_lie_within_runner_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            target_paths = run_paths["simple-1-direct"]
            evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
            receipt = json.loads(target_paths["receipt"].read_text(encoding="utf-8"))
            evidence["first_result_monotonic_ns"] = evidence["started_monotonic_ns"] - 1
            receipt["result_ready_monotonic_ns"] = evidence["first_result_monotonic_ns"]
            self.write_json(target_paths["evidence"], evidence)
            self.write_json(target_paths["receipt"], receipt)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
        self.assertIn("evidence_timing", manifest["gate"]["failures"])
        self.assertEqual(manifest["acceptance_status"], "fail")

    def test_slower_first_result_is_visible_without_changing_steady_state_verdict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            evidence = json.loads(run_paths["simple-2-global"]["evidence"].read_text(encoding="utf-8"))
            evidence["first_result_monotonic_ns"] = evidence["started_monotonic_ns"] + 500 * 1_000_000
            evidence["producer_finished_monotonic_ns"] = evidence["started_monotonic_ns"] + 600 * 1_000_000
            receipt = json.loads(run_paths["simple-2-global"]["receipt"].read_text(encoding="utf-8"))
            receipt["result_ready_monotonic_ns"] = evidence["first_result_monotonic_ns"]
            self.write_json(run_paths["simple-2-global"]["evidence"], evidence)
            self.write_json(run_paths["simple-2-global"]["receipt"], receipt)
            summary = module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
        self.assertEqual(summary["overall_status"], "pass")
        self.assertEqual(summary["tiers"]["simple"]["status"], "pass")
        self.assertEqual(summary["tiers"]["simple"]["optimization_status"], "pass")
        self.assertEqual(summary["tiers"]["simple"]["performance_diagnostics"], [])
        self.assertGreater(summary["tiers"]["simple"]["global_totals"]["first_result_elapsed_ms"], summary["tiers"]["simple"]["direct_totals"]["first_result_elapsed_ms"])
        self.assertEqual(summary["tiers"]["medium"]["status"], "pass")
        self.assertEqual(summary["tiers"]["complex"]["status"], "pass")
        self.assertEqual(summary["tiers"]["simple"]["metric_gates"]["steady_state_execution_elapsed_ms"]["status"], "pass")

    def test_one_slower_first_result_pair_is_a_separate_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root, repeat_count=4)
            evidence = json.loads(run_paths["simple-4-global"]["evidence"].read_text(encoding="utf-8"))
            evidence["first_result_monotonic_ns"] = evidence["started_monotonic_ns"] + 210 * 1_000_000
            evidence["producer_finished_monotonic_ns"] = evidence["started_monotonic_ns"] + 310 * 1_000_000
            receipt = json.loads(run_paths["simple-4-global"]["receipt"].read_text(encoding="utf-8"))
            receipt["result_ready_monotonic_ns"] = evidence["first_result_monotonic_ns"]
            self.write_json(run_paths["simple-4-global"]["evidence"], evidence)
            self.write_json(run_paths["simple-4-global"]["receipt"], receipt)
            summary = module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
        simple_summary = summary["tiers"]["simple"]
        self.assertEqual(summary["overall_status"], "pass")
        self.assertEqual(simple_summary["status"], "pass")
        self.assertEqual(simple_summary["optimization_status"], "pass")
        self.assertEqual(simple_summary["paired_wins"]["first_result_elapsed_ms"], 3)
        self.assertEqual(simple_summary["paired_wins"]["total_wall_elapsed_ms"], 3)
        self.assertNotIn("total_wall_elapsed_ms", simple_summary["metric_gates"])
        self.assertLess(simple_summary["global_medians"]["first_result_elapsed_ms"], simple_summary["direct_medians"]["first_result_elapsed_ms"])
        self.assertGreaterEqual(simple_summary["paired_savings_percent_medians"]["first_result_elapsed_ms"], module.MINIMUM_PAIRED_TIME_SAVINGS_PERCENT)
        self.assertEqual(simple_summary["performance_diagnostics"], [])

    def test_short_task_time_jitter_over_five_percent_but_under_two_seconds_is_not_material(self):
        gate = module.evaluate_paired_metric(
            [15000] * 6,
            [8000, 8000, 8000, 8000, 8000, 16074],
            maximum_absolute_regression=module.MAXIMUM_PAIRED_TIME_REGRESSION_MS,
        )
        self.assertEqual(gate["status"], "pass")
        self.assertLess(gate["worst_pair_savings_percent"], -module.MAXIMUM_PAIRED_REGRESSION_PERCENT)
        self.assertEqual(gate["worst_pair_regression_ms"], 1074)
        self.assertEqual(gate["material_pair_regression_count"], 0)
        self.assertTrue(gate["worst_pair_regression_within_limit"])

    def test_time_regression_over_five_percent_and_two_seconds_is_material(self):
        gate = module.evaluate_paired_metric(
            [15000] * 6,
            [8000, 8000, 8000, 8000, 8000, 18001],
            maximum_absolute_regression=module.MAXIMUM_PAIRED_TIME_REGRESSION_MS,
        )
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["worst_pair_regression_ms"], 3001)
        self.assertEqual(gate["material_pair_regression_count"], 1)
        self.assertFalse(gate["worst_pair_regression_within_limit"])

    def test_tier_gate_reports_material_first_result_regression_as_tail_diagnostic(self):
        manifests = []
        for repeat_index in range(1, 7):
            global_time = 18001 if repeat_index == 6 else 8000
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 15000, "total_wall_elapsed_ms": 15000}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": global_time, "total_wall_elapsed_ms": global_time}
            manifests.extend([direct_manifest, global_manifest])
        tier_summary = self.aggregate_tier("simple", 6, manifests)
        time_gate = tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]
        self.assertEqual(tier_summary["status"], "pass")
        self.assertNotIn("steady_state_regression_bound_loss", tier_summary["performance_diagnostics"])
        self.assertEqual(time_gate["material_pair_regression_count"], 1)
        self.assertFalse(time_gate["worst_pair_regression_within_limit"])
        self.assertFalse(time_gate["regression_bound_required"])
        self.assertEqual(time_gate["status"], "pass")

    def test_token_regression_over_five_percent_remains_material_without_absolute_floor(self):
        gate = module.evaluate_paired_metric(
            [100] * 6,
            [50, 50, 50, 50, 50, 106],
            require_strict_majority=False,
        )
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["worst_pair_savings_percent"], -6.0)
        self.assertFalse(gate["worst_pair_regression_within_limit"])
        self.assertNotIn("maximum_pair_regression_ms", gate)

    def test_simple_time_within_five_percent_passes(self):
        manifests = []
        for repeat_index in range(1, 5):
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": 96, "total_wall_elapsed_ms": 96}
            manifests.extend([direct_manifest, global_manifest])
        tier_summary = self.aggregate_tier("simple", 4, manifests)
        self.assertEqual(tier_summary["status"], "pass")
        self.assertEqual(tier_summary["paired_wins"]["steady_state_execution_elapsed_ms"], 4)
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["raw_global_median_lower"])
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["strict_majority_better"])
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["paired_savings_median_meets_threshold"])
        self.assertNotIn("steady_state_savings_threshold_loss", tier_summary["performance_diagnostics"])
        self.assertNotIn("total_wall_savings_threshold_loss", tier_summary["failures"])

    def test_exact_five_percent_time_savings_passes(self):
        manifests = []
        for repeat_index in range(1, 5):
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": 95, "total_wall_elapsed_ms": 95}
            manifests.extend([direct_manifest, global_manifest])
        tier_summary = self.aggregate_tier("simple", 4, manifests)
        self.assertEqual(tier_summary["status"], "pass")
        self.assertEqual(tier_summary["paired_savings_percent_medians"]["steady_state_execution_elapsed_ms"], 5.0)
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["paired_savings_median_meets_threshold"])

    def test_medium_non_majority_time_wins_fail_even_when_raw_and_savings_medians_pass(self):
        manifests = []
        for repeat_index, global_time in enumerate([80, 80, 101, 101], start=1):
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "medium", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "medium", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": global_time, "total_wall_elapsed_ms": global_time}
            manifests.extend([direct_manifest, global_manifest])
        tier_summary = self.aggregate_tier("medium", 4, manifests)
        self.assertEqual(tier_summary["status"], "pass")
        self.assertEqual(tier_summary["optimization_status"], "fail")
        self.assertEqual(tier_summary["paired_wins"]["steady_state_execution_elapsed_ms"], 2)
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["raw_global_median_lower"])
        self.assertTrue(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["paired_savings_median_meets_threshold"])
        self.assertFalse(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["strict_majority_better"])
        self.assertIn("steady_state_majority_loss", tier_summary["performance_diagnostics"])
        self.assertNotIn("total_wall_majority_loss", tier_summary["failures"])

    def test_zero_or_negative_time_median_fails(self):
        cases = [(120, 0.0), (130, -5.0)]
        for second_global_time, expected_maximum_median in cases:
            with self.subTest(second_global_time=second_global_time):
                manifests = []
                for repeat_index, global_time in [(1, 80), (2, second_global_time)]:
                    direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "medium", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
                    global_manifest = {"run_id": f"global-{repeat_index}", "tier": "medium", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": global_time, "total_wall_elapsed_ms": global_time}
                    manifests.extend([direct_manifest, global_manifest])
                tier_summary = self.aggregate_tier("medium", 2, manifests)
            self.assertEqual(tier_summary["status"], "pass")
            self.assertIn("steady_state_raw_median_loss", tier_summary["performance_diagnostics"])
            self.assertNotIn("total_wall_raw_median_loss", tier_summary["failures"])
            self.assertLessEqual(tier_summary["paired_savings_percent_medians"]["first_result_elapsed_ms"], expected_maximum_median)

    def test_ending_time_is_diagnostic_and_cannot_fail_a_passing_first_result(self):
        manifests = []
        for repeat_index in range(1, 7):
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "simple", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "first_result_elapsed_ms": 50, "total_wall_elapsed_ms": 10000}
            manifests.extend([direct_manifest, global_manifest])
            tier_summary = self.aggregate_tier("simple", 6, manifests)
        self.assertEqual(tier_summary["status"], "pass")
        self.assertNotIn("total_wall_elapsed_ms", tier_summary["metric_gates"])
        self.assertGreater(tier_summary["global_totals"]["total_wall_elapsed_ms"], tier_summary["direct_totals"]["total_wall_elapsed_ms"])

    def test_complex_first_result_time_is_diagnostic(self):
        manifests = []
        for repeat_index in range(1, 5):
            direct_manifest = {"run_id": f"direct-{repeat_index}", "tier": "complex", "repeat_index": repeat_index, "arm": "direct", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 100, "steady_state_logical_tokens": 100, "steady_state_execution_elapsed_ms": 100, "first_result_elapsed_ms": 100, "total_wall_elapsed_ms": 100}
            global_manifest = {"run_id": f"global-{repeat_index}", "tier": "complex", "repeat_index": repeat_index, "arm": "global", "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "logical_total_tokens": 50, "steady_state_logical_tokens": 50, "steady_state_execution_elapsed_ms": 50, "first_result_elapsed_ms": 150, "total_wall_elapsed_ms": 150}
            manifests.extend([direct_manifest, global_manifest])
        tier_summary = self.aggregate_tier("complex", 4, manifests)
        self.assertEqual(tier_summary["status"], "pass")
        self.assertEqual(tier_summary["metric_gates"]["steady_state_execution_elapsed_ms"]["status"], "pass")
        self.assertEqual(tier_summary["performance_diagnostics"], [])
        self.assertGreater(tier_summary["global_totals"]["first_result_elapsed_ms"], tier_summary["direct_totals"]["first_result_elapsed_ms"])

    def test_per_tier_repeat_counts_freeze_run_count_and_alternating_order(self):
        tier_repeat_counts = {"simple": 4, "medium": 2, "complex": 2}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, plan, _ = self.build_suite(root, tier_repeat_counts=tier_repeat_counts)
            module.validate_plan(plan)
            summary = module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
        self.assertEqual(len(plan["runs"]), 16)
        self.assertEqual(summary["repeat_count"], None)
        self.assertEqual(summary["tier_repeat_counts"], tier_repeat_counts)
        self.assertEqual(summary["tiers"]["simple"]["pair_count"], 4)
        self.assertEqual(summary["tiers"]["medium"]["pair_count"], 2)
        self.assertEqual(summary["overall_status"], "pass")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, missing_run_plan, _ = self.build_suite(root, tier_repeat_counts=tier_repeat_counts)
            missing_run_plan["runs"] = [run_plan for run_plan in missing_run_plan["runs"] if run_plan["run_id"] != "simple-4-global"]
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_run_count"):
                module.validate_plan(missing_run_plan)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bad_order_plan, _ = self.build_suite(root, tier_repeat_counts=tier_repeat_counts)
            direct_run = next(run_plan for run_plan in bad_order_plan["runs"] if run_plan["run_id"] == "simple-2-direct")
            global_run = next(run_plan for run_plan in bad_order_plan["runs"] if run_plan["run_id"] == "simple-2-global")
            direct_run["order_index"], global_run["order_index"] = global_run["order_index"], direct_run["order_index"]
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_order_not_alternating"):
                module.validate_plan(bad_order_plan)

    def test_exact_json_mismatch_generates_failed_manifest_without_manual_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            run_paths["medium-1-global"]["result"].write_text('{"tier":"medium","answer":"wrong","source_files":["source.py"]}\n', encoding="utf-8")
            summary = module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "medium-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["tiers"]["medium"]["status"], "fail")
        self.assertEqual(manifest["completion"], "complete")
        self.assertEqual(manifest["acceptance_status"], "fail")
        self.assertEqual(manifest["ending_real"], {"method": module.ENDING_REAL_METHOD, "completed": True, "status": "fail"})
        self.assertIn("result_not_exact", manifest["gate"]["failures"])
        self.assertIn("receipt_result_hash", manifest["gate"]["failures"])

    def test_semantically_equal_reordered_or_pretty_json_fails_exact_output_contract(self):
        result_variants = [
            '{"source_files":["source.py"],"answer":"ok","tier":"simple"}',
            json.dumps({"tier": "simple", "answer": "ok", "source_files": ["source.py"]}, indent=2),
        ]
        for result_message in result_variants:
            with self.subTest(result_message=result_message), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan_path, _, run_paths = self.build_suite(root)
                result_path = run_paths["simple-1-direct"]["result"]
                receipt_path = run_paths["simple-1-direct"]["receipt"]
                result_path.write_text(result_message + "\n", encoding="utf-8")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["output_sha256"] = hashlib.sha256(result_message.encode("utf-8")).hexdigest()
                self.write_json(receipt_path, receipt)
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["acceptance_status"], "fail")
            self.assertIn("result_not_exact", manifest["gate"]["failures"])
            self.assertNotIn("receipt_result_hash", manifest["gate"]["failures"])

    def test_source_pointer_must_remain_inside_configured_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            bad_document = {"tier": "complex", "answer": "ok", "source_files": ["../outside.py"]}
            self.write_json(run_paths["complex-1-direct"]["expected"], bad_document)
            result_message = json.dumps(bad_document, separators=(",", ":"))
            run_paths["complex-1-direct"]["result"].write_text(result_message + "\n", encoding="utf-8")
            receipt = json.loads(run_paths["complex-1-direct"]["receipt"].read_text(encoding="utf-8"))
            receipt["output_sha256"] = hashlib.sha256(result_message.encode("utf-8")).hexdigest()
            self.write_json(run_paths["complex-1-direct"]["receipt"], receipt)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "complex-1-direct.json").read_text(encoding="utf-8"))
        self.assertIn("source_outside_root", manifest["gate"]["failures"])
        self.assertEqual(manifest["acceptance_status"], "fail")

    def test_frozen_expected_and_source_hashes_reject_post_plan_mutation(self):
        cases = ["expected", "source"]
        for changed_input in cases:
            with self.subTest(changed_input=changed_input), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan_path, _, run_paths = self.build_suite(root)
                if changed_input == "expected":
                    expected_document = json.loads(run_paths["simple-1-direct"]["expected"].read_text(encoding="utf-8"))
                    run_paths["simple-1-direct"]["expected"].write_text(json.dumps(expected_document, indent=2) + "\n", encoding="utf-8")
                    expected_failure = "expected_hash_mismatch"
                else:
                    (root / "snapshot" / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
                    expected_failure = "source_snapshot_hash_mismatch"
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
            self.assertIn(expected_failure, manifest["gate"]["failures"])
            self.assertEqual(manifest["acceptance_status"], "fail")

    def test_optional_environment_snapshot_rejects_config_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, plan, _ = self.build_suite(root)
            environment, environment_paths = self.make_environment(root, "codex-home")
            for run_plan in plan["runs"]:
                run_plan["environment"] = environment
            self.write_json(plan_path, plan)
            environment_paths["config"].write_text("model = 'changed'\n", encoding="utf-8")
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
        self.assertIn("environment_hash_mismatch", manifest["gate"]["failures"])
        self.assertEqual(manifest["acceptance_status"], "fail")

    def test_environment_snapshot_rejects_models_cache_and_memory_drift(self):
        for changed_context in ["models_cache", "memories"]:
            with self.subTest(changed_context=changed_context), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan_path, plan, _ = self.build_suite(root)
                environment, environment_paths = self.make_environment(root, "codex-home")
                for run_plan in plan["runs"]:
                    run_plan["environment"] = environment
                self.write_json(plan_path, plan)
                if changed_context == "models_cache":
                    environment_paths["models_cache"].write_text('{"models":["changed"]}\n', encoding="utf-8")
                else:
                    (environment_paths["memories"] / "memory_summary.md").write_text("# Changed memory\n", encoding="utf-8")
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
            self.assertIn("environment_hash_mismatch", manifest["gate"]["failures"])
            self.assertEqual(manifest["acceptance_status"], "fail")

    def test_environment_snapshot_ignores_models_cache_fetch_timestamp_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, environment_paths = self.make_environment(root, "codex-home")
            environment_paths["models_cache"].write_text('{"fetched_at":"before","models":[]}\n', encoding="utf-8")
            environment["models_cache_sha256"] = module.models_cache_sha256(environment_paths["models_cache"])
            environment_paths["models_cache"].write_text('{"fetched_at":"after","models":[]}\n', encoding="utf-8")
            environment_sha256 = module.validate_environment_snapshot(environment)
        self.assertEqual(environment_sha256, module.sha256_text(module.canonical_json(environment)))

    def test_environment_snapshot_rejects_skill_plugin_and_marketplace_drift(self):
        for changed_catalog in ["skill", "plugin", "marketplace"]:
            with self.subTest(changed_catalog=changed_catalog), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marketplace_root = root / "marketplace"
                marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
                marketplace_plugin = marketplace_root / "plugins" / "marketplace-plugin" / ".codex-plugin" / "plugin.json"
                marketplace_manifest.parent.mkdir(parents=True)
                marketplace_plugin.parent.mkdir(parents=True)
                marketplace_manifest.write_text('{"name":"test-marketplace","plugins":[]}\n', encoding="utf-8")
                marketplace_plugin.write_text('{"name":"marketplace-plugin"}\n', encoding="utf-8")
                plan_path, plan, _ = self.build_suite(root)
                environment, environment_paths = self.make_environment(root, "codex-home", marketplace_root=marketplace_root)
                for run_plan in plan["runs"]:
                    run_plan["environment"] = environment
                self.write_json(plan_path, plan)
                changed_path = marketplace_manifest if changed_catalog == "marketplace" else environment_paths[changed_catalog]
                changed_path.write_text("changed\n", encoding="utf-8")
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
            self.assertIn("environment_catalog_drift", manifest["gate"]["failures"])
            self.assertEqual(manifest["acceptance_status"], "fail")

    def test_environment_snapshot_retries_recent_system_skill_refresh_until_frozen_catalog_returns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, environment_paths = self.make_environment(root, "codex-home")
            expected_catalog = module.catalog_snapshot(Path(environment["codex_home"]), Path(environment["config_path"]))
            refreshing_system_skill = Path(environment["skills_catalog_root"]) / ".system" / "refreshing" / "SKILL.md"
            refreshing_system_skill.parent.mkdir(parents=True)
            refreshing_system_skill.write_text("refreshing\n", encoding="utf-8")
            transient_catalog = dict(expected_catalog)
            transient_catalog["skills_catalog_sha256"] = "0" * 64
            with mock.patch.object(module, "catalog_snapshot", side_effect=[transient_catalog, expected_catalog]) as catalog_snapshot_mock, mock.patch.object(module.time, "sleep"):
                environment_sha256 = module.validate_environment_snapshot(environment)
        self.assertEqual(catalog_snapshot_mock.call_count, 2)
        self.assertEqual(environment_sha256, module.sha256_text(module.canonical_json(environment)))

    def test_catalog_snapshot_rejects_unreadable_duplicate_and_escaping_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "missing-catalog-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text("model = 'test'\n", encoding="utf-8")
            with self.assertRaisesRegex(module.BenchmarkGateError, "catalog_root_unreadable"):
                module.catalog_snapshot(codex_home, config_path)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _ = self.make_environment(root, "codex-home")
            duplicate_config = Path(environment["config_path"])
            duplicate_config.write_text(f"[marketplaces.one]\nsource_type = 'local'\nsource = '{environment['skills_catalog_root']}'\n", encoding="utf-8")
            with self.assertRaisesRegex(module.BenchmarkGateError, "catalog_root_duplicate"):
                module.catalog_snapshot(Path(environment["codex_home"]), duplicate_config)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _ = self.make_environment(root, "codex-home")
            outside_skill = root / "outside-SKILL.md"
            outside_skill.write_text("# Outside\n", encoding="utf-8")
            escaping_skill = Path(environment["skills_catalog_root"]) / "escape" / "SKILL.md"
            escaping_skill.parent.mkdir()
            escaping_skill.symlink_to(outside_skill)
            with self.assertRaisesRegex(module.BenchmarkGateError, "catalog_path_escape"):
                module.catalog_snapshot(Path(environment["codex_home"]), Path(environment["config_path"]))

    def test_pair_validation_requires_equal_visible_catalogs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, plan, _ = self.build_suite(root)
            direct_environment, _ = self.make_environment(root, "direct-home", skill_text="# Shared\n")
            global_environment, _ = self.make_environment(root, "global-home", skill_text="# Different\n")
            for run_plan in plan["runs"]:
                run_plan["environment"] = direct_environment if run_plan["arm"] == "direct" else global_environment
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_pair_catalog_mismatch"):
                module.validate_plan(plan)

    def test_pair_validation_requires_equal_config_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, plan, _ = self.build_suite(root)
            direct_environment, _ = self.make_environment(root, "direct-home", skill_text="# Shared\n")
            global_environment, _ = self.make_environment(root, "global-home", skill_text="# Shared\n")
            global_environment["config_sha256"] = "0" * 64
            for run_plan in plan["runs"]:
                run_plan["environment"] = direct_environment if run_plan["arm"] == "direct" else global_environment
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_pair_environment_mismatch"):
                module.validate_plan(plan)

    def test_pair_validation_requires_equal_runtime_context_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, plan, _ = self.build_suite(root)
            direct_environment, _ = self.make_environment(root, "direct-home", skill_text="# Shared\n")
            global_environment, _ = self.make_environment(root, "global-home", skill_text="# Shared\n")
            global_environment["memories_sha256"] = "0" * 64
            for run_plan in plan["runs"]:
                run_plan["environment"] = direct_environment if run_plan["arm"] == "direct" else global_environment
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_pair_environment_mismatch"):
                module.validate_plan(plan)

    def test_optional_prompt_path_rejects_post_plan_prompt_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, plan, _ = self.build_suite(root)
            prompt_paths = {}
            for tier in module.TIERS:
                prompt_path = root / f"{tier}.txt"
                prompt_text = f"{tier} prompt"
                prompt_path.write_text(prompt_text, encoding="utf-8")
                prompt_paths[tier] = prompt_path
                prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                for run_plan in plan["runs"]:
                    if run_plan["tier"] == tier:
                        run_plan["prompt_path"] = str(prompt_path)
                        run_plan["prompt_sha256"] = prompt_sha256
                        run_plan["receipts"][0]["workload_prompt_sha256"] = prompt_sha256
                        receipt_path = root / run_plan["receipts"][0]["path"]
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                        receipt["workload_prompt_sha256"] = prompt_sha256
                        execution_prompt = prompt_text if run_plan["arm"] == "direct" else module.auto_benchmark_execution_prompt(prompt_text)
                        receipt["prompt_sha256"] = hashlib.sha256(execution_prompt.encode("utf-8")).hexdigest()
                        self.write_json(receipt_path, receipt)
            self.write_json(plan_path, plan)
            prompt_paths["simple"].write_text("changed simple prompt", encoding="utf-8")
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-direct.json").read_text(encoding="utf-8"))
        self.assertIn("prompt_hash_mismatch", manifest["gate"]["failures"])
        self.assertEqual(manifest["acceptance_status"], "fail")

    def test_unreceipted_retry_fallback_repair_pair_and_role_are_rejected(self):
        cases = [
            ("uncensused", lambda evidence, receipt: evidence["launched_session_ids"].append("missing-session"), "evidence_session_census"),
            ("retry", lambda evidence, receipt: evidence["retry_session_ids"].append(evidence["launched_session_ids"][0]), "retry_not_allowed"),
            ("fallback", lambda evidence, receipt: evidence["fallback_session_ids"].append(evidence["launched_session_ids"][0]), "fallback_not_allowed"),
            ("repair", lambda evidence, receipt: evidence["repair_session_ids"].append(evidence["launched_session_ids"][0]), "repair_not_allowed"),
            ("pair", lambda evidence, receipt: receipt.update(effective_pair="wrong|low"), "receipt_pair_mismatch"),
            ("role", lambda evidence, receipt: receipt.update(node_role="verification"), "receipt_role_mismatch"),
            ("node_type", lambda evidence, receipt: receipt.update(node_type="direct-task"), "receipt_node_type_mismatch"),
            ("entry_context", lambda evidence, receipt: receipt.update(entry_context_active=True), "receipt_entry_context_active"),
            ("authorization", lambda evidence, receipt: receipt.update(authorization_source="benchmark-direct"), "receipt_authorization_source_mismatch"),
            ("benchmark_id", lambda evidence, receipt: receipt.update(benchmark_run_id="benchmark-other-run"), "receipt_benchmark_run_id_mismatch"),
            ("workload_id", lambda evidence, receipt: receipt.update(workload_id="other-run"), "receipt_workload_id_mismatch"),
            ("prompt_binding", lambda evidence, receipt: receipt.update(benchmark_prompt_file_verified=False), "receipt_benchmark_prompt_unverified"),
            ("adaptive_launch", lambda evidence, receipt: receipt.update(benchmark_auto_workspace_count=2), "receipt_auto_launch_unverified"),
            ("result_source", lambda evidence, receipt: receipt.update(benchmark_result_source="unverified_handoff"), "receipt_result_source_invalid"),
            ("execution_prompt", lambda evidence, receipt: receipt.update(prompt_sha256="0" * 64), "receipt_execution_prompt_mismatch"),
            ("result_ready_event", lambda evidence, receipt: receipt.update(result_ready_clock="child-local"), "receipt_result_ready_event_invalid"),
            ("result_duplicate", lambda evidence, receipt: receipt.update(duplicate_result_detected=True), "receipt_result_not_frozen"),
        ]
        for label, mutate, expected_failure in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan_path, _, run_paths = self.build_suite(root)
                target_paths = run_paths["simple-1-global"]
                evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
                receipt = json.loads(target_paths["receipt"].read_text(encoding="utf-8"))
                mutate(evidence, receipt)
                self.write_json(target_paths["evidence"], evidence)
                self.write_json(target_paths["receipt"], receipt)
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
            self.assertIn(expected_failure, manifest["gate"]["failures"])
            self.assertEqual(manifest["acceptance_status"], "fail")

    def test_runtime_descendant_census_rejects_hidden_root_and_incomplete_child(self):
        cases = ["hidden_omitted", "hidden_root", "incomplete_child"]
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan_path, _, run_paths = self.build_suite(root)
                target_paths = run_paths["simple-1-global"]
                evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
                if case_name == "hidden_omitted":
                    evidence["state_snapshot"]["after_thread_count"] += 1
                    evidence["state_snapshot"]["after_thread_ids_sha256"] = "b" * 64
                else:
                    main_thread_id = evidence["runtime_sessions"][0]["thread_id"]
                    child_pair = "gpt-5.6-sol|ultra"
                    child_source_kind = "root" if case_name == "hidden_root" else "subagent"
                    child_parent = None if case_name == "hidden_root" else main_thread_id
                    child_session = self.make_runtime_session(f"{case_name}-session", child_pair, 50, child_parent, child_source_kind, case_name != "incomplete_child")
                    evidence["runtime_sessions"].append(child_session)
                    evidence["launched_session_ids"].append(child_session["thread_id"])
                    evidence["state_snapshot"]["after_thread_count"] += 1
                    evidence["state_snapshot"]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json(sorted(evidence["launched_session_ids"])))
                self.write_json(target_paths["evidence"], evidence)
                module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
                manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
            expected_failure = {"hidden_omitted": "evidence_state_snapshot_delta", "hidden_root": "runtime_session_tree", "incomplete_child": "runtime_session_incomplete"}[case_name]
            self.assertIn(expected_failure, manifest["gate"]["failures"])
            self.assertEqual(manifest["acceptance_status"], "fail")

    def test_completed_mixed_pair_descendant_is_counted_without_entry_pair_leakage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, _, run_paths = self.build_suite(root)
            target_paths = run_paths["simple-1-global"]
            evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
            main_thread_id = evidence["runtime_sessions"][0]["thread_id"]
            child_session = self.make_runtime_session("child-session", "gpt-5.6-luna|low", 50, main_thread_id, "subagent", True)
            evidence["runtime_sessions"].append(child_session)
            evidence["launched_session_ids"].append(child_session["thread_id"])
            evidence["state_snapshot"]["after_thread_count"] += 1
            evidence["state_snapshot"]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json(sorted(evidence["launched_session_ids"])))
            evidence["foreground_sessions"].append(self.make_foreground_session(child_session))
            evidence["foreground_state_snapshot"]["after_thread_count"] += 1
            evidence["foreground_state_snapshot"]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json(sorted(session["thread_id"] for session in evidence["foreground_sessions"])))
            self.write_json(target_paths["evidence"], evidence)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "pass")
        self.assertEqual(manifest["runtime_session_count"], 3)
        self.assertEqual(manifest["runtime_descendant_session_count"], 2)
        self.assertEqual(manifest["unreceipted_descendant_count"], 2)
        self.assertEqual(manifest["result_producer_pair"], "gpt-5.6-luna|max")
        self.assertEqual(manifest["executed_pairs"], ["gpt-5.6-luna|max", "gpt-5.6-luna|max", "gpt-5.6-luna|low"])
        self.assertEqual(manifest["logical_total_tokens"], 451)

    def test_receipt_backed_mixed_pair_ending_root_is_allowed_and_excluded_from_task_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path, plan, run_paths = self.build_suite(root)
            target_run = next(run_plan for run_plan in plan["runs"] if run_plan["run_id"] == "simple-1-global")
            target_paths = run_paths[target_run["run_id"]]
            result_message = target_paths["result"].read_text(encoding="utf-8").rstrip("\n")
            ending_path = target_paths["receipt"].with_name("ending-receipt.json")
            ending_pair = "gpt-5.6-luna|low"
            ending_workload_sha256 = "b" * 64
            ending_receipt = self.make_receipt(ending_path, "ending-session", ending_pair, "ending", result_message, 70, ending_workload_sha256, "ending-workload", "global")
            ending_receipt.update({"node_type": "locked-route-node", "authorization_source": "outside-entry-context"})
            self.write_json(ending_path, ending_receipt)
            target_run["receipts"].append({"path": self.relative(root, ending_path), "pair": ending_pair, "role": "ending", "bind_result": False, "workload_prompt_sha256": ending_workload_sha256})
            evidence = json.loads(target_paths["evidence"].read_text(encoding="utf-8"))
            ending_session = self.make_runtime_session("ending-session", ending_pair, 70, None, "root", True)
            evidence["runtime_sessions"].append(ending_session)
            evidence["launched_session_ids"].append(ending_session["thread_id"])
            evidence["state_snapshot"]["after_thread_count"] += 1
            evidence["state_snapshot"]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json(sorted(evidence["launched_session_ids"])))
            self.write_json(target_paths["evidence"], evidence)
            self.write_json(plan_path, plan)
            module.evaluate_suite(plan_path, root / "manifests", root / "summary.json")
            manifest = json.loads((root / "manifests" / "simple-1-global.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["acceptance_status"], "pass")
        self.assertEqual(manifest["result_producer_pair"], "gpt-5.6-luna|max")
        self.assertEqual(manifest["runtime_root_session_count"], 2)
        self.assertEqual(manifest["unreceipted_descendant_count"], 1)
        self.assertEqual(manifest["logical_total_tokens"], 401)
        self.assertEqual(manifest["executed_pairs"], ["gpt-5.6-luna|max", "gpt-5.6-luna|max", ending_pair])

    def test_foreground_census_rejects_incomplete_unknown_mismatched_and_excess_tokens(self):
        cases = ["incomplete", "unknown", "mismatched", "excess"]
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, _, run_paths = self.build_suite(root)
                evidence = json.loads(run_paths["simple-1-direct"]["evidence"].read_text(encoding="utf-8"))
                if case_name == "incomplete":
                    evidence["foreground_state_snapshot"].update({"after_complete": False, "after_thread_count": None, "after_thread_ids_sha256": None})
                    expected_failure = "evidence_foreground_state_snapshot_incomplete"
                elif case_name == "unknown":
                    main_thread_id = evidence["foreground_main_thread_id"]
                    unknown_session = self.make_foreground_session(self.make_runtime_session("unknown-session", "gpt-5.6-sol|ultra", 1, main_thread_id, "subagent"))
                    evidence["foreground_sessions"].append(unknown_session)
                    evidence["foreground_state_snapshot"]["after_thread_count"] += 1
                    evidence["foreground_state_snapshot"]["after_thread_ids_sha256"] = module.sha256_text(module.canonical_json(sorted(session["thread_id"] for session in evidence["foreground_sessions"])))
                    expected_failure = "evidence_foreground_unknown_session"
                elif case_name == "mismatched":
                    evidence["foreground_sessions"][0]["model"] = "gpt-5.6-luna"
                    expected_failure = "evidence_foreground_session_mismatch"
                else:
                    evidence["foreground_sessions"][0]["tokens_used"] = evidence["runtime_sessions"][0]["tokens_used"] + 1
                    expected_failure = "evidence_foreground_tokens_exceed_final"
                with self.assertRaisesRegex(module.BenchmarkGateError, expected_failure):
                    module.validate_evidence(evidence, "simple-1-direct", "direct")

    def test_plan_and_evidence_cannot_supply_acceptance_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, plan, run_paths = self.build_suite(root)
            plan["runs"][0]["acceptance_status"] = "pass"
            with self.assertRaisesRegex(module.BenchmarkGateError, "plan_run_keys"):
                module.validate_plan(plan)
            evidence = json.loads(run_paths["simple-1-direct"]["evidence"].read_text(encoding="utf-8"))
            evidence["acceptance_status"] = "pass"
            with self.assertRaisesRegex(module.BenchmarkGateError, "evidence_contract"):
                module.validate_evidence(evidence, "simple-1-direct", "direct")

    def test_legacy_simple_direct_and_global_outputs_fail_the_frozen_expected(self):
        for arm in module.ARMS:
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                expected_path = root / "expected.json"
                result_path = root / "result.json"
                evidence_path = root / "evidence.json"
                receipt_path = root / "receipt.json"
                self.write_json(expected_path, LEGACY_SIMPLE_EXPECTED)
                result_message = json.dumps(LEGACY_SIMPLE_ACTUAL, separators=(",", ":"))
                result_path.write_text(result_message + "\n", encoding="utf-8")
                thread_id = f"legacy-{arm}"
                pair = module.ARM_ENTRY_PAIRS[arm]
                self.make_receipt(receipt_path, thread_id, pair, "result-producer", result_message, 100, "a" * 64, f"legacy-{arm}", arm, 1_000_000_000 + 10 * 1_000_000)
                runtime_sessions = [self.make_runtime_session(thread_id, pair, 100)]
                if arm == "global":
                    runtime_sessions.append(self.make_runtime_session(f"{thread_id}-adaptive", "gpt-5.6-luna|max", 50, thread_id, "subagent"))
                foreground_sessions = [self.make_foreground_session(runtime_session) for runtime_session in runtime_sessions]
                self.make_evidence(evidence_path, f"legacy-{arm}", [runtime_session["thread_id"] for runtime_session in runtime_sessions], 10, 20, pair, 100, runtime_sessions, foreground_sessions, thread_id)
                run_plan = {"run_id": f"legacy-{arm}", "pair_id": "legacy-simple", "tier": "simple", "repeat_index": 1, "arm": arm, "order_index": 1 if arm == "direct" else 2, "prompt_sha256": "a" * 64, "expected_result_path": "expected.json", "expected_sha256": hashlib.sha256(expected_path.read_bytes()).hexdigest(), "result_path": "result.json", "evidence_path": "evidence.json", "receipts": [{"path": "receipt.json", "pair": pair, "role": "result-producer", "bind_result": True, "workload_prompt_sha256": "a" * 64}], "selected_entry_pair": pair, "entry_execution_mode": "executed"}
                manifest = module.evaluate_run(root, "legacy-suite", "b" * 64, run_plan)
            self.assertEqual(manifest["completion"], "complete")
            self.assertEqual(manifest["acceptance_status"], "fail")
            self.assertIn("result_not_exact", manifest["gate"]["failures"])


if __name__ == "__main__":
    unittest.main()
