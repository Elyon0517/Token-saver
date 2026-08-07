#!/usr/bin/env python3
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "task_route_dispatcher.py"
MODULE_SPEC = importlib.util.spec_from_file_location("task_route_dispatcher", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class TestSmartTaskSegmentation(unittest.TestCase):
    def setUp(self):
        self.fixture_vault = tempfile.TemporaryDirectory()
        self.fixture_owner_roots = {}
        (Path(self.fixture_vault.name) / "Skills").mkdir()
        self.original_registered_owner = module.obsidian_model_memory.project_change_memory._registered_owner

        def fixture_registered_owner(record_root):
            root = Path(record_root).expanduser().resolve()
            return self.fixture_owner_roots.get(root, self.original_registered_owner(record_root))

        self.owner_patch = patch.object(module.obsidian_model_memory.project_change_memory, "_registered_owner", side_effect=fixture_registered_owner)
        self.vault_patch = patch.dict(os.environ, {"CODEX_OBSIDIAN_VAULT": self.fixture_vault.name, "CODEX_MODEL_ROUTING_MEMORY": str(Path(self.fixture_vault.name) / "model-routing-memory" / "events.jsonl")}, clear=False)
        self.owner_patch.start()
        self.vault_patch.start()

    def tearDown(self):
        self.vault_patch.stop()
        self.owner_patch.stop()
        self.fixture_vault.cleanup()

    def register_fixture_owner(self, cache_dir):
        cache_path = Path(cache_dir).expanduser().resolve()
        project_root = next(parent.parent for parent in (cache_path, *cache_path.parents) if parent.name == "work")
        self.fixture_owner_roots[project_root] = "Global Codex Skills"

    def _refresh_recommendation(self, node):
        pairs = module.routing_history_module.canonical_pairs(node["candidate_ladder"])
        static_pair = module.routing_history_module.parse_pair(node["static_suggestion"])
        hard_floor = module.routing_history_module.parse_pair(node["hard_floor"])
        fingerprint = module.routing_history_module.profile_fingerprint(node["routing_condition"], pairs, static_pair, hard_floor)
        node["routing_recommendation"] = {"selected_pair": f"{node['model']}|{node['effort']}", "trial": node["trial"], "reason": "shared_cold_start", "profile_fingerprint": fingerprint, "calibration_state": "cold_start", "best_pair": None, "selection_basis": "dual_model_history"}

    def _result_node(self, cache_dir, node_id="work", *, complexity_score=16, complexity_band="small", purpose="implement"):
        return {
            "id": node_id,
            "phase": "result",
            "skill": "code-skill",
            "model": module.PRIORITY_PRODUCER_CONFIG["id"],
            "effort": module.PRIORITY_PRODUCER_CONFIG["effort_by_complexity"]["easy"],
            "dependencies": [],
            "prompt": f"return PASS for {node_id}",
            "sandbox": "workspace-write",
            "execution_domain": "python",
            "task_summary": f"Return a result for {node_id}.",
            "routing_condition": {
                "task_family": "tiny_code",
                "artifact": "patch",
                "scope": "single",
                "ambiguity": "low",
                "modality": "text",
                "risk": "low",
                "complexity": "easy",
                "owning_skill": "code-skill",
                "project_family": "global",
                "verification_shape": "real",
                "execution_domain": "python",
            },
            "candidate_ladder": module.normal_adaptive_pair_texts(),
            "static_suggestion": module.MODEL_ROLE_PAIRS["weak_default"],
            "hard_floor": module.MODEL_ROLE_PAIRS["weak_default"],
            "trial": False,
            "selection_basis": "spark_priority",
            "priority_producer": True,
            "complexity_score": complexity_score,
            "complexity_band": complexity_band,
            "refresh": True,
            "source_allowlist": ["a.txt"],
            "routing_recommendation": {"selected_pair": f"{module.PRIORITY_PRODUCER_CONFIG['id']}|{module.PRIORITY_PRODUCER_CONFIG['effort_by_complexity']['easy']}", "trial": False, "reason": "shared_cold_start", "profile_fingerprint": "legacy", "calibration_state": "cold_start", "best_pair": None, "selection_basis": "dual_model_history"},
        }

    def _build_dynamic_plan(self, cache_dir):
        cache_dir = Path(cache_dir)
        if cache_dir.name == "cache" or "work" not in cache_dir.parts:
            cache_dir = cache_dir / "work" / "cache" / "route"
        self.register_fixture_owner(cache_dir)
        floor_model, floor_effort = module.routing_history_module.parse_pair(module.MODEL_ROLE_PAIRS["weak_default"])
        advanced = {
            "schema_version": 2,
            "complexity": "easy",
            "complexity_score": 76,
            "complexity_band": "advanced",
            "topology": "mixed",
            "routing_mode": "dynamic_task_graph",
            "cache_dir": str(cache_dir),
            "entry": {"model": "gpt-5.6-terra", "effort": "low"},
            "nodes": [],
            "main_result_node": "design",
        }
        writing = self._result_node(cache_dir, "writing", complexity_score=16, complexity_band="small", purpose="implement")
        testing = self._result_node(cache_dir, "testing", complexity_score=16, complexity_band="small", purpose="test")
        writing["id"] = "writing"
        testing["id"] = "testing"
        writing["allow_fallback"] = [module.MODEL_ROLE_PAIRS["weak_default"]]
        testing["selection_basis"] = "adaptive_quality"
        testing.pop("priority_producer")
        testing["model"] = floor_model
        testing["effort"] = floor_effort
        testing["complexity_score"] = 26
        testing["complexity_band"] = module.complexity_band(26)
        testing["spark_exception_category"] = "quality_failure"
        testing["spark_exception_reason"] = "Previous quality failure suppresses Spark for this test stage."
        design = self._result_node(cache_dir, "design", complexity_score=60, complexity_band="advanced", purpose="design")
        design.pop("priority_producer")
        design["selection_basis"] = "adaptive_quality"
        design["dependencies"] = ["writing", "testing"]
        design["model"] = floor_model
        design["effort"] = floor_effort
        design["complexity_band"] = module.complexity_band(60)
        integration = self._result_node(cache_dir, "integration", complexity_score=45, complexity_band="standard", purpose="integration")
        integration.pop("priority_producer")
        integration["selection_basis"] = "adaptive_quality"
        integration["dependencies"] = ["design"]
        integration["model"] = floor_model
        integration["effort"] = floor_effort
        integration["complexity_score"] = 45
        integration["complexity_band"] = module.complexity_band(45)
        ending_model, ending_effort = module.score_role_pair(42).split("|", 1)
        ending = {
            "id": "ending-verify",
            "phase": "ending",
            "skill": "verify-skill",
            "model": ending_model,
            "effort": ending_effort,
            "dependencies": ["integration"],
            "prompt": "Run ending verification.",
            "sandbox": "read-only",
            "complexity_score": 42,
            "complexity_band": module.complexity_band(42),
            "selection_basis": "ending_score_role",
        }
        advanced["nodes"] = [writing, testing, design, integration, ending]
        advanced["main_result_node"] = "integration"
        for node in (writing, testing, design, integration):
            self._refresh_recommendation(node)
        advanced["decomposition"] = {
            "policy": module.DECOMPOSITION_POLICY,
            "stage_inventory": [
                {"stage_id": "stage-writing", "node_id": "writing", "logical_stage_ids": ["stage-writing"], "purpose": "implement", "score": 16, "band": module.complexity_band(16), "model_intent": f"{writing['model']}|{writing['effort']}", "operation": "code", "dependencies": [], "inputs": ["in.writing"], "outputs": ["out.writing"], "stop_condition": "must return result", "coupling": "independent", "parallelizable": True, "objective_scope": "app", "mutable_state": [], "failure_escalation": "quality_upgrade_one_notch", "external_side_effects": False, "deterministic_merge_node": "design"},
                {"stage_id": "stage-testing", "node_id": "testing", "logical_stage_ids": ["stage-testing"], "purpose": "test", "score": testing["complexity_score"], "band": testing["complexity_band"], "model_intent": f"{testing['model']}|{testing['effort']}", "operation": "execute", "dependencies": [], "inputs": ["in.testing"], "outputs": ["out.testing"], "stop_condition": "must return result", "coupling": "independent", "parallelizable": True, "objective_scope": "app", "mutable_state": [], "failure_escalation": "quality_upgrade_one_notch", "external_side_effects": False, "deterministic_merge_node": "design"},
                {"stage_id": "stage-design", "node_id": "design", "logical_stage_ids": ["stage-design"], "purpose": "design", "score": design["complexity_score"], "band": design["complexity_band"], "model_intent": f"{design['model']}|{design['effort']}", "operation": "write", "dependencies": ["writing", "testing"], "inputs": ["in.design"], "outputs": ["out.design"], "stop_condition": "must return result", "coupling": "linear", "parallelizable": False, "objective_scope": "app", "mutable_state": [], "failure_escalation": "quality_upgrade_one_notch", "external_side_effects": False},
                {"stage_id": "stage-integration", "node_id": "integration", "logical_stage_ids": ["stage-integration"], "purpose": "integration", "score": integration["complexity_score"], "band": integration["complexity_band"], "model_intent": f"{integration['model']}|{integration['effort']}", "operation": "execute", "dependencies": ["design"], "inputs": ["in.integration"], "outputs": ["out.integration"], "stop_condition": "must return result", "coupling": "linear", "parallelizable": False, "objective_scope": "app", "mutable_state": ["state.integration"], "failure_escalation": "quality_upgrade_one_notch", "external_side_effects": False},
            ],
        }
        return advanced

    def test_parent_advanced_task_split_with_spark_and_higher_quality_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertEqual(failures, [])

    def test_stage_inventory_missing_duplicate_and_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            result_nodes = [node for node in plan["nodes"] if node.get("phase") == "result"]
            plan["decomposition"]["policy"] = "unsafe"
            duplicate = dict(plan["decomposition"]["stage_inventory"][0])
            plan["decomposition"]["stage_inventory"].append(duplicate)
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertIn("decomposition.policy must be max_safe", failures)
        self.assertIn("decomposition covers result node more than once:", " ".join(failures))

        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            expected = {node["id"] for node in plan["nodes"] if node.get("phase") == "result"}
            plan["decomposition"]["stage_inventory"] = [plan["decomposition"]["stage_inventory"][0]]
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertTrue(any("decomposition must cover every result node" in failure for failure in failures))
        missing = expected - {node["node_id"] for node in plan["decomposition"]["stage_inventory"]}
        self.assertTrue(missing)

    def test_missing_continuity_reason_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            plan["decomposition"]["stage_inventory"][0]["logical_stage_ids"] = ["s1", "s2"]
            plan["decomposition"]["stage_inventory"][0]["coupling"] = "linear"
            plan["decomposition"]["stage_inventory"][0]["parallelizable"] = False
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertTrue(any("continuity_reason" in failure for failure in failures))

    def test_small_non_spark_result_rejected_without_exception_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            node = plan["nodes"][0]
            node["selection_basis"] = "adaptive_quality"
            node["complexity_score"] = 16
            node["complexity_band"] = "small"
            node["source_allowlist"] = ["a.txt"]
            plan["decomposition"]["stage_inventory"][0]["operation"] = "code"
            plan["decomposition"]["stage_inventory"][0]["external_side_effects"] = False
            node["spark_exception_reason"] = ""
            if "spark_exception_category" in node:
                node.pop("spark_exception_category")
            node["spark_exception_category"] = None
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertTrue(any("eligible small task segment must use Spark or declare spark_exception_reason" in failure for failure in failures))

    def test_small_non_spark_result_requires_allowed_exception_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            node = plan["nodes"][0]
            stage = plan["decomposition"]["stage_inventory"][0]
            node.pop("priority_producer")
            node["selection_basis"] = "adaptive_quality"
            node["model"], node["effort"] = module.routing_history_module.parse_pair(module.MODEL_ROLE_PAIRS["weak_default"])
            node["spark_exception_reason"] = "Prior evidence requires a quality route."
            node["spark_exception_category"] = "not-an-exception"
            stage["model_intent"] = f"{node['model']}|{node['effort']}"
            self._refresh_recommendation(node)
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertTrue(any("spark exception category must be one of" in failure for failure in failures))

    def test_parallel_wave_rejects_shared_state_or_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = self._build_dynamic_plan(Path(temp_dir))
            stage_a = plan["decomposition"]["stage_inventory"][0]
            stage_b = plan["decomposition"]["stage_inventory"][1]
            stage_a.update({"node_id": "writing", "coupling": "independent", "parallelizable": True, "objective_scope": "scope-shared", "deterministic_merge_node": "design", "inputs": ["same.in"], "mutable_state": ["state.shared"]})
            stage_b.update({"node_id": "testing", "coupling": "independent", "parallelizable": True, "objective_scope": "scope-shared", "deterministic_merge_node": "design", "inputs": ["same.in"], "mutable_state": ["state.shared"]})
            failures = module.validate_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir))
        self.assertTrue(any("must have disjoint inputs" in failure for failure in failures))

    def test_model_switch_summary_in_run_plan_includes_requested_and_pending_endings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "work" / "cache" / "route"
            plan = self._build_dynamic_plan(cache_dir)
            plan["topology"] = "parallel"
            plan["nodes"] = [node for node in plan["nodes"] if node.get("phase") == "result"] + [plan["nodes"][-1]]
            result_nodes = [node for node in plan["nodes"] if node.get("phase") == "result"]
            for node in result_nodes:
                if node.get("priority_producer") is not True:
                    node["selection_basis"] = "adaptive_quality"

            def fake_run_node(node, cache_dir, completed, state_db, workdir, codex_bin="codex", skills_root=None):
                pair = f"{node['model']}|{node['effort']}"
                result_path = Path(cache_dir) / f"{node['id']}-result.md"
                result_path.write_text("RESULT=ok")
                return {
                    "id": node["id"],
                    "phase": node["phase"],
                    "skill": node["skill"],
                    "requested_model": node["model"],
                    "requested_effort": node["effort"],
                    "requested_pair": pair,
                    "resolved_model": node["model"],
                    "resolved_effort": node["effort"],
                    "resolved_pair": pair,
                    "effective_model": node["model"],
                    "effective_effort": node["effort"],
                    "effective_pair": pair,
                    "model_evidence_source": "runtime_receipt",
                    "evidence_level": "runtime_receipt",
                    "failure_class": None,
                    "operational_fallback": False,
                    "model": node["model"],
                    "effort": node["effort"],
                    "workload_id": f"task-route-{node['id']}",
                    "status": "pass",
                    "receipt_path": str(cache_dir / f"{node['id']}-receipt.json"),
                    "result_path": str(result_path),
                    "result_published": True,
                    "result_ready_monotonic_ns": 1000,
                    "receipt_failure_after_result": False,
                    "worker_identity": "sha256:000",
                    "tokens": {"total_tokens": 12},
                    "process_elapsed_ms": 8,
                    "complexity_score": node["complexity_score"],
                    "complexity_band": node["complexity_band"],
                    "selection_basis": node["selection_basis"],
                    "dependencies": list(node.get("dependencies", [])),
                }

            with patch.object(module, "run_node", side_effect=fake_run_node):
                manifest = module.run_plan(plan, "gpt-5.6-terra", "low", Path(temp_dir), history_path=Path(temp_dir) / "history.json")
        self.assertEqual(manifest["status"], "pass", manifest["failures"])
        summary = manifest["model_switch_summary"]
        self.assertIn("nodes", summary)
        self.assertIn("aggregate", summary)
        pending = [node for node in summary["nodes"] if node.get("status") == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["node_id"], "ending-verify")
        self.assertTrue(any(item["node_id"] == "writing" and item["requested_pair"] and item["resolved_pair"] for item in summary["nodes"]))
        self.assertIn("dependency_wave", summary["nodes"][0])
        self.assertIn("same_wave_siblings", summary["nodes"][0]["relations"])
        self.assertIn("process_elapsed_ms", summary["nodes"][0])
        self.assertIn("spark_usage_nodes", summary["aggregate"])

    def test_model_switch_summary_quality_and_operational_fallback_counts(self):
        nodes = [
            {"id": "result-a", "model": "gpt-5.6-terra", "effort": "low", "complexity_score": 16, "complexity_band": "small", "dependencies": [], "phase": "result"},
            {"id": "ending", "model": "gpt-5.6-terra", "effort": "low", "complexity_score": 42, "complexity_band": "standard", "dependencies": ["result-a"], "phase": "ending"},
        ]
        plan = {
            "routing_mode": "dynamic_task_graph",
            "nodes": nodes,
            "decomposition": {
                "policy": module.DECOMPOSITION_POLICY,
                "stage_inventory": [
                    {
                        "stage_id": "s1",
                        "node_id": "result-a",
                        "logical_stage_ids": ["s1"],
                        "purpose": "write",
                        "score": 16,
                        "band": "small",
                        "model_intent": "gpt-5.6-terra|low",
                        "operation": "code",
                        "dependencies": [],
                        "inputs": ["a.in"],
                        "outputs": ["a.out"],
                        "stop_condition": "success",
                        "coupling": "linear",
                        "parallelizable": False,
                        "objective_scope": "s",
                        "mutable_state": [],
                        "failure_escalation": "quality_upgrade_one_notch",
                        "external_side_effects": False,
                    },
                ],
            },
        }
        records = [
            {"id": "result-a", "model": "gpt-5.6-terra", "effort": "low", "status": "pass", "requested_pair": "gpt-5.6-terra|low", "resolved_pair": "gpt-5.6-luna|low", "effective_pair": "gpt-5.6-luna|low", "model_evidence_source": "runtime_receipt", "evidence_level": "runtime_receipt", "tokens": {"total_tokens": 11}, "process_elapsed_ms": 10, "failure_class": "quality", "operational_fallback": False},
            {"id": "ending", "model": "gpt-5.6-terra", "effort": "low", "status": "pending", "requested_pair": "gpt-5.6-terra|low", "model_evidence_source": "unavailable", "evidence_level": "unavailable", "tokens": {}, "process_elapsed_ms": None},
        ]
        summary = module.build_model_switch_summary(plan, records, {"model": "gpt-5.6-terra", "effort": "low"}, ending_quality_failure_nodes=["result-a"])
        self.assertEqual(summary["nodes"][0]["failure_class"], "quality")
        self.assertEqual(summary["aggregate"]["quality_failure_nodes"], 1)
        records[1]["operational_fallback"] = True
        summary = module.build_model_switch_summary(plan, records, {"model": "gpt-5.6-terra", "effort": "low"})
        self.assertEqual(summary["aggregate"]["operational_fallback_nodes"], 1)

    def test_model_switch_summary_preserves_known_assignment_without_runtime_receipt(self):
        plan = {"routing_mode": "dynamic_task_graph", "nodes": [{"id": "assigned", "model": "gpt-5.6-terra", "effort": "medium", "complexity_score": 36, "complexity_band": "standard", "dependencies": [], "phase": "result"}], "decomposition": {"policy": module.DECOMPOSITION_POLICY, "stage_inventory": []}}
        summary = module.build_model_switch_summary(plan, [{"id": "assigned", "status": "pass", "requested_pair": "gpt-5.6-terra|medium", "model_evidence_source": "task_assignment"}], {"model": "gpt-5.6-terra", "effort": "low"})
        node_summary = summary["nodes"][0]
        self.assertEqual(node_summary["requested_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(node_summary["evidence_level"], "UNVERIFIED (no runtime receipt)")
        self.assertEqual(node_summary["route_change"], "freeze")

    def test_ending_manifest_marks_non_targeted_quality_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            plan = self._build_dynamic_plan(temp_root / "work" / "cache" / "route")
            main_node = next(node for node in plan["nodes"] if node.get("id") == "integration")
            main_node["dependencies"] = []
            plan["main_result_node"] = "integration"
            plan["nodes"] = [main_node]
            plan["decomposition"]["stage_inventory"] = [stage for stage in plan["decomposition"]["stage_inventory"] if stage.get("node_id") == "integration"]
            ending_model, ending_effort = module.score_role_pair(42).split("|", 1)
            ending_verify = {
                "id": "ending-verify",
                "phase": "ending",
                "skill": "verify-skill",
                "model": ending_model,
                "effort": ending_effort,
                "dependencies": ["integration"],
                "prompt": "Run ending verify.",
                "sandbox": "read-only",
                "complexity_score": 42,
                "complexity_band": module.complexity_band(42),
                "selection_basis": "ending_score_role",
            }
            plan["nodes"].append(ending_verify)

            with tempfile.TemporaryDirectory() as temp_route:
                handoff_path = Path(temp_route) / "ending-handoff.json"
                route_run_id = "route-test"
                cache_dir = Path(temp_route) / "work" / "cache" / "route"
                cache_dir.mkdir(parents=True, exist_ok=True)
                ending_manifest_path = cache_dir / "ending-dispatch-manifest.json"
                release_path = module._ending_release_path(cache_dir, route_run_id)
                release_record = {"route_run_id": route_run_id, "main_result_node": "integration"}
                release_record["released_at"] = "2020-01-01T00:00:00"
                module._write_release_record(release_path, release_record)
                completed = [{
                    "id": "integration",
                    "status": "pass",
                    "model": main_node["model"],
                    "effort": main_node["effort"],
                    "receipt_path": str(cache_dir / "integration-receipt.json"),
                    "result_path": str(cache_dir / "integration-result.md"),
                    "result_published": True,
                    "result_ready_monotonic_ns": 1000,
                    "dependencies": [],
                    "requested_model": main_node["model"],
                    "requested_effort": main_node["effort"],
                }]
                handoff_path.write_text(json.dumps({
                    "schema_version": module.DISPATCH_SCHEMA_VERSION,
                    "cwd": str(temp_root),
                    "state_db": str(temp_root / "state.sqlite"),
                    "entry": {"model": "gpt-5.6-terra", "effort": "low"},
                    "route_run_id": route_run_id,
                    "plan": plan,
                    "completed": completed,
                    "main_result_node": "integration",
                    "cache_dir": str(cache_dir),
                    "released": True,
                    "release_path": str(release_path),
                    "ending_manifest_path": str(ending_manifest_path),
                }))

                def fake_run_node(node, cache_dir, completed, state_db, workdir, codex_bin="codex", skills_root=None):
                    return {
                        "id": node["id"],
                        "phase": "ending",
                        "status": "fail",
                        "skill": node["skill"],
                        "requested_model": node["model"],
                        "requested_effort": node["effort"],
                        "requested_pair": f"{node['model']}|{node['effort']}",
                        "resolved_model": node["model"],
                        "resolved_effort": node["effort"],
                        "resolved_pair": f"{node['model']}|{node['effort']}",
                        "effective_model": node["model"],
                        "effective_effort": node["effort"],
                        "effective_pair": f"{node['model']}|{node['effort']}",
                        "model_evidence_source": "runtime_receipt",
                        "evidence_level": "runtime_receipt",
                        "failure_class": "quality",
                        "operational_fallback": False,
                        "model": node["model"],
                        "effort": node["effort"],
                        "workload_id": f"task-route-{node['id']}",
                        "receipt_path": str(cache_dir / f"{node['id']}-receipt.json"),
                        "result_path": str(cache_dir / f"{node['id']}-result.md"),
                        "result_published": False,
                        "result_ready_monotonic_ns": 1000,
                        "receipt_failure_after_result": False,
                        "worker_identity": "sha256:000",
                        "tokens": {"total_tokens": 1},
                        "process_elapsed_ms": 4,
                        "complexity_score": node.get("complexity_score", 42),
                        "complexity_band": node.get("complexity_band", "standard"),
                        "selection_basis": node.get("selection_basis", "ending_score_role"),
                        "dependencies": list(node.get("dependencies", [])),
                    }

                with patch.object(module, "run_node", side_effect=fake_run_node):
                    manifest = module.run_ending_handoff(handoff_path)
        self.assertEqual(manifest["status"], "fail")
        self.assertIn("model_switch_summary", manifest)
        summary = manifest["model_switch_summary"]
        self.assertGreater(summary["aggregate"]["quality_failure_nodes"], 0, manifest)


if __name__ == "__main__":
    unittest.main()
