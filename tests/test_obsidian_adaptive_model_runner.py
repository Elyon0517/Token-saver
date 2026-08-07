#!/usr/bin/env python3
import importlib.util
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "obsidian_adaptive_model_runner.py"
SPEC = importlib.util.spec_from_file_location("obsidian_adaptive_model_runner", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def recommendation(pair="gpt-5.6-terra|medium", fallback_pair="gpt-5.6-terra|high"):
    model, effort = pair.split("|", 1)
    return {
        "source": "local_and_obsidian_model_history",
        "memory_available": True,
        "selected_pair": pair,
        "selected_model": model,
        "selected_effort": effort,
        "attempt_pair": pair,
        "active_fallback_pair": fallback_pair,
        "attempt_trial": True,
        "attempt_reason": "repeated_real_pass_one_rung_down",
        "attempt_calibration_state": "provisional",
        "trial": True,
        "reason": "real_pass_one_rung_down",
        "calibration_state": "provisional",
        "specificity": "symbol",
        "matched_records": 2,
        "project_key": "example-123",
    }


class ObsidianAdaptiveRunnerTests(unittest.TestCase):
    def arguments(self, root):
        project = root / "project"
        project.mkdir()
        return SimpleNamespace(
            project_root=project,
            task_type="code",
            module="module",
            file="src/a.py",
            symbol="A.run",
            code_kind="python",
            operation="edit",
            modality="text",
            complexity="easy",
            complexity_score=12,
            risk="low",
            ambiguity="low",
            task_summary="Edit one method.",
            vault=root / "vault",
            ladder=module.obsidian_model_memory.DEFAULT_LADDER,
            workload_id="workload",
            receipt_output=root / "receipt.json",
            result_output=root / "result.md",
            workdir=project,
            state_db=root / "state.db",
            codex_bin="codex",
            sandbox="read-only",
            allow_fallback=[],
            ignore_user_config=True,
            timeout=60,
            emit_result=True,
            entry_model="gpt-5.6-sol",
            entry_effort="ultra",
            cache_root=project / "Cache" / "task-analyze",
        )

    def test_executes_exact_obsidian_selected_pair_and_returns_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))

            def fake_run(receipt_args, prompt):
                self.assertEqual((receipt_args.model, receipt_args.effort), ("gpt-5.6-terra", "medium"))
                receipt_args.result_output.write_text("RESULT", encoding="utf-8")
                return {"status": "pass", "requested_pair": "gpt-5.6-terra|medium", "result_published": True, "result_ready_monotonic_ns": time.monotonic_ns(), "process_elapsed_ms": 12, "tokens": {"total_tokens": 34}}

            with patch.object(module, "_recommend", return_value=recommendation()), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run):
                result = module.run(args, "Do the work")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["memory_source"], "local_and_obsidian_model_history")
        self.assertEqual(result["entry_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(result["selected_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(result["result"], "RESULT")

    def test_recommendation_receives_resolved_entry_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.resolved_entry_model = "gpt-5.6-luna"
            args.resolved_entry_effort = "max"
            with patch.object(module.obsidian_model_memory, "recommend_model", return_value=recommendation()) as recommend:
                module._recommend(args)
        self.assertEqual(recommend.call_args.kwargs["entry_model"], "gpt-5.6-luna")
        self.assertEqual(recommend.call_args.kwargs["entry_effort"], "max")

    def test_zero_argument_stdin_fast_path_derives_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "fixture"
            workdir.mkdir()
            with patch.dict(module.os.environ, {"CODEX_HOME": str(root / "codex-home")}, clear=False):
                args = module.resolve_fast_path_args(module.parse_args(["--workdir", str(workdir)]), "  Implement one function.\nRun tests.  ")
        self.assertEqual(args.project_root, workdir.resolve())
        self.assertEqual(args.task_type, "code")
        self.assertEqual(args.module, "fixture")
        self.assertEqual(args.task_summary, "Implement one function. Run tests.")
        self.assertEqual(args.complexity, "easy")
        self.assertIsInstance(args.complexity_score, int)
        self.assertEqual(args.complexity_band, module.obsidian_model_memory.complexity_band(args.complexity_score))
        self.assertRegex(args.workload_id, r"^fast-[0-9a-f]{16}$")
        self.assertEqual(args.receipt_output.parent, args.result_output.parent)
        expected_output_root = workdir / "Cache" / "task-analyze" / "adaptive-producer" / args.workload_id
        self.assertEqual(args.receipt_output.parent, expected_output_root.resolve())
        self.assertNotIn("codex-home", str(args.receipt_output))
        self.assertEqual(args.sandbox, "workspace-write")
        self.assertTrue(args.emit_result)

    def test_route_ready_event_is_flushed_before_producer_call(self):
        class FlushTrackingStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1
                super().flush()

        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            stream = FlushTrackingStream()
            adaptive = recommendation()

            def fake_run(receipt_args, prompt):
                self.assertTrue(stream.getvalue().splitlines())
                receipt_args.result_output.write_text("RESULT", encoding="utf-8")
                return {"status": "pass", "requested_pair": "gpt-5.6-terra|medium", "result_published": True, "result_ready_monotonic_ns": time.monotonic_ns(), "process_elapsed_ms": 12, "tokens": {"total_tokens": 34}}

            with patch.object(module.sys, "stdout", stream), patch.object(module, "_recommend", return_value=adaptive), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run):
                result = module.run(args, "Do the work")
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            event = events[0]
            notice_event = events[1]
        expected_notice = module._model_route_notice(args, adaptive)
        self.assertEqual(event, {"schema_version": 1, "stage": "route-ready", "complexity_score": 12, "complexity_band": "small", "entry_pair": "gpt-5.6-sol|ultra", "entry_source": "explicit", "selected_pair": "gpt-5.6-terra|medium", "attempt_pair": "gpt-5.6-terra|medium", "active_fallback_pair": "gpt-5.6-terra|high", "switch_direction": "no_switch", "switch_change": "initial->gpt-5.6-terra|medium", "receipt_path": str(args.receipt_output), "result_path": str(args.result_output), "result_pending": True, "user_visible_message": expected_notice["message"], "model_route_notice": expected_notice})
        self.assertEqual(notice_event, {"schema_version": 1, "stage": "model-switch-notice", "user_visible": True, **expected_notice})
        self.assertGreaterEqual(stream.flush_count, 1)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["model_route_notice"], expected_notice)

    def test_repeated_failure_notice_names_the_higher_model_and_task_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.resolved_entry_model = "gpt-5.6-luna"
            args.resolved_entry_effort = "max"
            adaptive = recommendation(pair="gpt-5.6-terra|low", fallback_pair="gpt-5.6-terra|medium")
            adaptive.update({"step_kind": "debugging", "session_effort": {"failure_recorded": True, "user_effort": 10, "last_model_pair": "gpt-5.6-luna|max", "solving_surface": "core_solving", "step_estimate": 1, "estimated_effort": "low", "model_difficulty": "difficult", "information_burden": "low"}, "session_escalation": {"applied": True, "from_pair": "gpt-5.6-luna|max", "to_pair": "gpt-5.6-terra|low"}})
            notice = module._model_route_notice(args, adaptive)
        self.assertEqual(notice["kind"], "session_model_escalation")
        self.assertIn("increased the model to gpt-5.6-terra|low", notice["message"])
        self.assertIn("core solving / debugging part", notice["message"])
        self.assertIn("Entry model remains gpt-5.6-luna|max", notice["message"])
        self.assertEqual(notice["estimated_steps"], 1)

    def test_graph_notice_lists_each_modelled_task_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.resolved_entry_model = "gpt-5.6-luna"
            args.resolved_entry_effort = "max"
            plan = {"nodes": [{"id": "source-1", "phase": "result", "model": "gpt-5.6-luna", "effort": "low", "step_kind": "analysis", "dependencies": []}, {"id": "merge-result", "phase": "result", "model": "gpt-5.6-terra", "effort": "medium", "step_kind": "integration", "dependencies": ["source-1"]}, {"id": "ending-verify", "phase": "ending", "model": "gpt-5.6-terra", "effort": "high", "dependencies": ["merge-result"]}]}
            notice = module._graph_model_route_notice(args, plan, recommendation(), recommendation(pair="gpt-5.6-terra|medium"))
        self.assertEqual(notice["kind"], "graph_model_route")
        self.assertEqual([part["node_id"] for part in notice["parts"]], ["source-1", "merge-result", "ending-verify"])
        self.assertIn("each of 3 task parts", notice["message"])

    def test_explicit_route_arguments_keep_read_only_and_emit_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = ["--project-root", str(root), "--task-type", "code", "--module", "module", "--workload-id", "explicit", "--receipt-output", str(root / "receipt.json"), "--result-output", str(root / "result.txt")]
            args = module.resolve_fast_path_args(module.parse_args(argv), "Do work")
        self.assertEqual(args.workload_id, "explicit")
        self.assertEqual(args.sandbox, "read-only")
        self.assertFalse(args.emit_result)
        self.assertEqual(args.complexity, "easy")

    def test_fast_path_infers_numeric_and_multifile_complexity(self):
        with tempfile.TemporaryDirectory() as temporary:
            numeric = module.resolve_fast_path_args(
                module.parse_args(["--workdir", temporary]),
                "Use Decimal, ROUND_HALF_UP cents, tax, and percent calculations.",
            )
            multifile = module.resolve_fast_path_args(
                module.parse_args(["--workdir", temporary]),
                "Complete the six-file store quote pipeline.",
            )
            explicit = module.resolve_fast_path_args(
                module.parse_args(["--workdir", temporary, "--complexity", "easy"]),
                "Complete the six-file store quote pipeline.",
            )
        self.assertEqual(numeric.complexity, "complex")
        self.assertEqual(multifile.complexity, "complex")
        self.assertEqual(explicit.complexity, "easy")

    def test_explicit_numeric_score_overrides_inference_and_drives_band(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary, "--complexity-score", "9"]), "Complete a multi-file architecture migration.")
        self.assertEqual(args.complexity_score, 9)
        self.assertEqual(args.complexity, "easy")
        self.assertEqual(module.obsidian_model_memory.complexity_band(args.complexity_score), "small")
        self.assertEqual(module.infer_operation("Please change one text value"), "edit")

    def test_complex_global_lifecycle_request_is_not_masked_by_small_edit_wording(self):
        prompt = "Update global skills so small edits use Spark, add model routing downgrade and upgrade, build real verification tests, split independent Ending tasks, record Obsidian history, and run repair loops with fresh verifiers."
        score = module.infer_complexity_score(prompt)
        self.assertGreaterEqual(score, 75)
        self.assertEqual(module.obsidian_model_memory.complexity_band(score), "advanced")

    def test_one_local_typo_remains_small(self):
        score = module.infer_complexity_score("Fix one typo in a single Python function.")
        self.assertLessEqual(score, 24)
        self.assertEqual(module.obsidian_model_memory.complexity_band(score), "small")

    def test_simple_question_is_classified_for_spark_without_explicit_metadata(self):
        prompt = "What is 7 times 8?"
        with tempfile.TemporaryDirectory() as temporary:
            args = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), prompt)
        self.assertEqual(args.task_type, "question")
        self.assertEqual(args.operation, "answer")
        self.assertLessEqual(args.complexity_score, 24)
        self.assertEqual(args.complexity_band, "small")

    def test_independent_read_only_sources_enable_safe_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            prompt = "Complete two independent source audits. Do not edit files.\n- a.py\n- b.py\nReturn JSON."
            sources = module.scheduled_source_paths(prompt, root)
            compressed = module.scheduled_source_paths("Audit independent a.py, b.py. Read-only, no edits.", root)
            dependent = module.scheduled_source_paths("Complete a two-file pipeline. Do not edit files.\n- a.py\n- b.py", root)
        self.assertEqual(sources, ["a.py", "b.py"])
        self.assertEqual(compressed, ["a.py", "b.py"])
        self.assertEqual(dependent, [])

    def test_exact_expression_contract_does_not_fan_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            prompt = "Audit independent a.py and b.py. Read-only. Return exactly JSON; copy each exact expression, preserve key order and the exact literal."
            sources = module.scheduled_source_paths(prompt, root)
        self.assertEqual(sources, [])

    def test_explicit_latency_source_graph_overrides_mislabeled_easy_complexity(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.complexity = "easy"
            (args.workdir / "a.py").write_text("A = 1\n", encoding="utf-8")
            (args.workdir / "b.py").write_text("B = 2\n", encoding="utf-8")
            with patch.object(module, "_recommend", return_value=recommendation()), patch.object(module, "_run_scheduled_graph", return_value={"status": "pass"}) as scheduled:
                result = module.run(args, "Audit independent a.py, b.py. Read-only, no edits. Must run in parallel for latency.")
        self.assertEqual(result["status"], "pass")
        scheduled.assert_called_once()

    def test_schedule_admission_prefers_one_producer_for_small_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = ["a.py", "b.py", "c.py"]
            for source in sources:
                (root / source).write_text("VALUE = 1\n", encoding="utf-8")
            decision = module.schedule_admission("Audit independent a.py, b.py, c.py. Read-only, no edits.", root, sources)
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["decision"], "single_adaptive_producer")
        self.assertEqual(decision["reason"], "single_producer_lower_estimated_logical_tokens")
        self.assertLess(decision["estimated_single_input_tokens"], decision["estimated_scheduled_input_tokens"])

    def test_schedule_admission_uses_graph_for_context_pressure_or_explicit_latency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = ["a.py", "b.py", "c.py"]
            for source in sources:
                (root / source).write_text("X" * 70000, encoding="utf-8")
            pressure = module.schedule_admission("Audit independent a.py, b.py, c.py. Read-only, no edits.", root, sources)
            for source in sources:
                (root / source).write_text("X", encoding="utf-8")
            latency = module.schedule_admission("Audit independent a.py, b.py, c.py. Read-only, no edits. Parallel is required for latency-critical delivery.", root, sources)
        self.assertTrue(pressure["admitted"])
        self.assertEqual(pressure["reason"], "single_producer_context_budget_exceeded")
        self.assertTrue(latency["admitted"])
        self.assertEqual(latency["reason"], "explicit_parallel_latency_contract")

    def test_scheduled_plan_uses_parallel_priority_branches_and_adaptive_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.complexity = "complex"
            (args.workdir / "a.py").write_text("A = 1\n", encoding="utf-8")
            (args.workdir / "b.py").write_text("B = 2\n", encoding="utf-8")
            proof = {"selected_pair": "gpt-5.6-terra|medium", "attempt_pair": "gpt-5.6-terra|medium", "active_fallback_pair": "gpt-5.6-terra|high", "trial": False, "reason": "shared_cold_start", "profile_fingerprint": "fingerprint", "calibration_state": "cold_start", "best_pair": None, "selection_basis": "shared_cold_start"}
            adaptive = {"selected_pair": "gpt-5.6-terra|medium", "trial": False}
            with patch.object(module.task_route_dispatcher, "_obsidian_recommendation_and_proof", return_value=(adaptive, proof)):
                plan, merge_recommendation = module._scheduled_plan(
                    args,
                    "Complete two independent source audits. Do not edit files.\n- a.py\n- b.py",
                    ["a.py", "b.py"],
                    "gpt-5.6-sol",
                    "ultra",
                    recommendation(),
                )
        result_nodes = [node for node in plan["nodes"] if node["phase"] == "result"]
        self.assertEqual(plan["topology"], "parallel")
        self.assertEqual(plan["entry"], {"model": "gpt-5.6-sol", "effort": "ultra"})
        self.assertEqual([node["source_allowlist"] for node in result_nodes[:-1]], [["a.py"], ["b.py"]])
        self.assertEqual([(node["model"], node["effort"]) for node in result_nodes[:-1]], [("gpt-5.3-codex-spark", "low"), ("gpt-5.3-codex-spark", "low")])
        self.assertTrue(all(node["priority_producer"] is True for node in result_nodes[:-1]))
        self.assertIn("Omit unsupported fields", result_nodes[0]["prompt"])
        self.assertIn("Prefer direct defining-source facts", result_nodes[-1]["prompt"])
        self.assertEqual((result_nodes[-1]["model"], result_nodes[-1]["effort"]), ("gpt-5.6-terra", "medium"))
        self.assertEqual(result_nodes[-1]["routing_recommendation"]["attempt_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(merge_recommendation, adaptive)

    def test_exact_owned_three_source_schedule_fuses_final_source_with_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.complexity = "complex"
            sources = ["a.py", "b.py", "c.py"]
            for source in sources:
                (args.workdir / source).write_text(f"VALUE = {source!r}\n", encoding="utf-8")
            prompt = """Complete three independent read-only source audits. Do not edit files.
a.py
b.py
c.py
Return one minified JSON object.

alpha is owned only by a.py
- value: exact assignment

beta is owned only by b.py
- value: exact assignment

gamma is owned only by c.py
- value: exact assignment

source_files must list all sources in order."""
            proof = {"selected_pair": "gpt-5.6-terra|medium", "attempt_pair": "gpt-5.6-terra|medium", "active_fallback_pair": "gpt-5.6-terra|high", "trial": False, "reason": "shared_cold_start", "profile_fingerprint": "fingerprint", "calibration_state": "cold_start", "best_pair": None, "selection_basis": "shared_cold_start"}
            adaptive = {"selected_pair": "gpt-5.6-terra|medium", "trial": False}
            with patch.object(module.task_route_dispatcher, "_obsidian_recommendation_and_proof", return_value=(adaptive, proof)):
                plan, _ = module._scheduled_plan(args, prompt, sources, "gpt-5.6-sol", "ultra", recommendation())
        result_nodes = [node for node in plan["nodes"] if node["phase"] == "result"]
        self.assertEqual(plan["topology"], "mixed")
        self.assertEqual(plan["schedule_mode"], "parallel_sources_fused_final")
        self.assertEqual(plan["parallel_branch_count"], 2)
        self.assertEqual(plan["fused_source"], "c.py")
        self.assertEqual(len(result_nodes), 3)
        self.assertEqual([node["source_allowlist"] for node in result_nodes], [["a.py"], ["b.py"], ["c.py"]])
        self.assertEqual(result_nodes[-1]["dependencies"], ["source-1", "source-2"])
        self.assertTrue(result_nodes[-1]["fuses_owned_source_with_dependencies"])
        self.assertNotIn("reads_dependency_results_only", result_nodes[-1])
        self.assertIn("Dependency results own every other section", result_nodes[-1]["prompt"])
        self.assertEqual((result_nodes[-1]["model"], result_nodes[-1]["effort"]), ("gpt-5.6-terra", "medium"))

    def test_exact_expression_schedule_raises_branch_quality(self):
        exact = "Return exactly one JSON object. Copy the exact expression, preserve key order, and preserve the exact literal."
        relaxed = "Summarize two independent files as JSON."
        self.assertEqual(module._scheduled_branch_pair(exact, "gpt-5.6-luna|low"), ("gpt-5.6-terra", "medium"))
        self.assertEqual(module._scheduled_branch_pair(relaxed, "gpt-5.6-luna|low"), ("gpt-5.6-luna", "low"))

    def test_exact_expression_single_producer_uses_frontier_quality_guard(self):
        base = recommendation()
        exact = "Return exactly JSON; copy the exact expression, preserve key order, and preserve the exact literal."
        guarded = module._exact_contract_recommendation(exact, base)
        self.assertEqual(guarded["selected_pair"], "gpt-5.6-sol|high")
        self.assertEqual(guarded["attempt_pair"], "gpt-5.6-sol|high")
        self.assertEqual(guarded["attempt_reason"], "exact_expression_quality_guard")
        self.assertIsNone(guarded["active_fallback_pair"])
        self.assertEqual(base["selected_pair"], "gpt-5.6-terra|medium")

    def test_scheduled_branch_receives_only_its_owned_contract(self):
        prompt = """Complete independent source audits. Do not edit files.
- a.py
- b.py
Return exactly one JSON object.

alpha is owned only by a.py and uses key value.
- value is the exact assignment.

beta is owned only by b.py and uses key name.
- name is the exact function name.

source_files must list both sources."""
        first = module._scheduled_branch_prompt(prompt, "a.py")
        second = module._scheduled_branch_prompt(prompt, "b.py")
        self.assertIn("alpha is owned only by a.py", first)
        self.assertNotIn("beta is owned only by b.py", first)
        self.assertIn("beta is owned only by b.py", second)
        self.assertNotIn("alpha is owned only by a.py", second)
        self.assertIn("Return exactly one JSON object", first)
        self.assertIn("Do not read another source", first)

    def test_owned_contract_handles_long_prefix_offsets_as_search_positions(self):
        prefix = "Global rule. " * 40
        prompt = f"""{prefix}
alpha is owned only by a.py and uses key value.
- value is exact.

beta is owned only by b.py and uses key name.
- name is exact.

source_files must list both sources."""
        contract = module._owned_source_contract(prompt, "a.py")
        self.assertIn("alpha is owned only by a.py", contract)
        self.assertNotIn("beta is owned only by b.py", contract)

    def test_fast_path_summary_respects_memory_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), "word " * 200)
        self.assertEqual(len(args.task_summary), 280)

    def test_fast_path_classifies_read_only_source_audit_as_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), "Audit independent a.py and b.py. Read-only, no edits.")
        self.assertEqual(args.task_type, "question")

    def test_fast_path_identity_is_stable_per_project_and_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), "Do A")
            second = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), "Do A")
            different = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary]), "Do B")
            different_metadata = module.resolve_fast_path_args(module.parse_args(["--workdir", temporary, "--module", "other"]), "Do A")
        self.assertEqual(first.workload_id, second.workload_id)
        self.assertNotEqual(first.workload_id, different.workload_id)
        self.assertNotEqual(first.workload_id, different_metadata.workload_id)

    def test_main_zero_argument_path_resolves_before_run_without_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            with patch.object(module.Path, "cwd", return_value=workdir), patch.object(module.sys, "stdin", io.StringIO("Implement one function")), patch.object(module.sys, "stdout", io.StringIO()), patch.object(module, "_recommend") as recommend, patch.object(module, "run", return_value={"status": "pass"}) as execute:
                status = module.main([])
        self.assertEqual(status, 0)
        recommend.assert_not_called()
        args, prompt = execute.call_args.args
        self.assertEqual(prompt, "Implement one function")
        self.assertEqual(args.task_type, "code")
        self.assertEqual(args.sandbox, "workspace-write")
        self.assertTrue(args.emit_result)

    def test_receipt_and_summary_embed_only_sanitized_model_learning_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.task_summary = "  Edit one method.\nKeep behavior stable.  "

            def fake_run(receipt_args, prompt):
                receipt_args.result_output.write_text("RESULT", encoding="utf-8")
                return {"status": "pass", "requested_pair": "gpt-5.6-terra|medium", "effective_pair": "gpt-5.6-terra|medium", "result_published": True, "turn_completed": True, "model_match": True, "effort_match": True, "result_ready_monotonic_ns": time.monotonic_ns(), "process_elapsed_ms": 12, "tokens": {"total_tokens": 34}}

            with patch.object(module, "_recommend", return_value=recommendation()), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run):
                result = module.run(args, "SECRET RAW PROMPT MUST NOT BE STORED")
            receipt = json.loads(args.receipt_output.read_text(encoding="utf-8"))
        expected_fields = {"project_root", "task_type", "module", "file", "symbol", "code_kind", "operation", "modality", "complexity", "complexity_score", "complexity_band", "risk", "ambiguity", "task_name", "task_group", "task_scope_key", "task_group_key", "codex_session_key", "task_summary", "step_kind", "capability_tags", "capability_fingerprint", "entry_model", "entry_effort", "entry_pair", "entry_source"}
        self.assertEqual(set(result["model_learning_context"]), expected_fields)
        self.assertEqual(receipt["model_learning_context"], result["model_learning_context"])
        self.assertEqual(result["model_learning_context"]["task_summary"], "Edit one method. Keep behavior stable.")
        self.assertEqual(result["model_learning_context"]["step_kind"], "implementation")
        self.assertRegex(result["model_learning_context"]["capability_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["entry_pair"], "gpt-5.6-sol|ultra")
        self.assertNotIn("SECRET RAW PROMPT", json.dumps(receipt))
        self.assertNotIn("SECRET RAW PROMPT", json.dumps(result))

    def test_receipt_args_use_an_exact_supported_route_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            receipt_args = module._receipt_args(args, ("gpt-5.3-codex-spark", "low"))
        self.assertEqual(receipt_args.route_marker, "LOCKED_ROUTE_NODE")
        self.assertIn(receipt_args.route_marker, module.model_execution_receipt.ROUTE_MARKERS)

    def test_blocked_boundary_does_not_launch_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            blocked = recommendation()
            blocked.update({"selected_pair": None, "attempt_pair": None, "reason": "quality_boundary_exhausted", "calibration_state": "blocked"})
            with patch.object(module, "_recommend", return_value=blocked), patch.object(module.model_execution_receipt, "run_receipt") as execute:
                result = module.run(args, "Do the work")
        self.assertEqual(result["status"], "blocked")
        execute.assert_not_called()

    def test_missing_obsidian_memory_uses_shared_cold_start_instead_of_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            unavailable = recommendation()
            unavailable["memory_available"] = False
            def fake_run(receipt_args, prompt):
                receipt_args.result_output.write_text("COLD START RESULT", encoding="utf-8")
                return {"status": "pass", "requested_pair": "gpt-5.6-terra|medium", "effective_pair": "gpt-5.6-terra|medium", "result_published": True, "turn_completed": True, "model_match": True, "effort_match": True, "result_ready_monotonic_ns": time.monotonic_ns(), "process_elapsed_ms": 12, "tokens": {"total_tokens": 34}}
            with patch.object(module, "_recommend", return_value=unavailable), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run) as execute:
                result = module.run(args, "Do the work")
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["memory_available"])
        self.assertTrue(result["result"].startswith("Complexity: 12/100 (small) · Model: gpt-5.6-terra|medium · Route: no switch\nEvidence: runtime receipt"))
        self.assertTrue(result["result"].endswith("COLD START RESULT"))
        execute.assert_called_once()

    def test_failed_execution_is_operational_and_not_quality_learning(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            failed = {"status": "fail", "requested_pair": "gpt-5.6-terra|medium", "result_published": False, "process_elapsed_ms": 5, "tokens": {}}
            with patch.object(module, "_recommend", return_value=recommendation()), patch.object(module.model_execution_receipt, "run_receipt", return_value=failed):
                result = module.run(args, "Do the work")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["reason"], "producer_operational_failure")
        self.assertEqual(result["ending_real_status"], "not_started")

    def test_selected_pair_pre_execution_failure_falls_back_once_to_stronger_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            calls = []

            def fake_run(receipt_args, prompt):
                pair = f"{receipt_args.model}|{receipt_args.effort}"
                calls.append(pair)
                if pair == "gpt-5.6-terra|medium":
                    return {
                        "status": "fail",
                        "failure_class": "availability",
                        "requested_pair": pair,
                        "turn_completed": False,
                        "model_match": False,
                        "effort_match": False,
                        "tokens": {"total_tokens": 0},
                        "process_elapsed_ms": 2,
                        "route_attempts": [{"requested_pair": pair, "tokens": {"total_tokens": 0}}],
                    }
                receipt_args.result_output.write_text("FALLBACK RESULT", encoding="utf-8")
                return {
                    "status": "pass",
                    "requested_pair": pair,
                    "effective_pair": pair,
                    "turn_completed": True,
                    "model_match": True,
                    "effort_match": True,
                    "result_published": True,
                    "result_ready_monotonic_ns": time.monotonic_ns(),
                    "process_elapsed_ms": 7,
                    "tokens": {"total_tokens": 20},
                    "route_attempts": [{"requested_pair": pair, "effective_pair": pair, "tokens": {"total_tokens": 20}}],
                }

            adaptive = recommendation()
            with patch.object(module, "_recommend", return_value=adaptive), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run):
                result = module.run(args, "Do the work")
            receipt = __import__("json").loads(args.receipt_output.read_text(encoding="utf-8"))
        self.assertEqual(calls, ["gpt-5.6-terra|medium", "gpt-5.6-terra|high"])
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["result"].startswith("Complexity: 12/100 (small) · Model: gpt-5.6-terra|high · Route: fallback"))
        self.assertIn("Model path: gpt-5.6-terra|medium -> gpt-5.6-terra|high", result["result"])
        self.assertTrue(result["result"].endswith("FALLBACK RESULT"))
        self.assertEqual(receipt["operational_failure_pairs"], ["gpt-5.6-terra|medium"])
        self.assertEqual(len(receipt["route_attempts"]), 2)

    def test_selected_pair_published_result_never_foreground_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            calls = []

            def fake_run(receipt_args, prompt):
                pair = f"{receipt_args.model}|{receipt_args.effort}"
                calls.append(pair)
                receipt_args.result_output.write_text("ADAPTIVE RESULT", encoding="utf-8")
                return {"status": "pass", "requested_pair": pair, "effective_pair": pair, "turn_completed": True, "model_match": True, "effort_match": True, "result_published": True, "result_ready_monotonic_ns": time.monotonic_ns(), "process_elapsed_ms": 3, "tokens": {"total_tokens": 9}, "route_attempts": [{"requested_pair": pair, "effective_pair": pair, "tokens": {"total_tokens": 9}}]}

            adaptive = recommendation()
            with patch.object(module, "_recommend", return_value=adaptive), patch.object(module.model_execution_receipt, "run_receipt", side_effect=fake_run):
                result = module.run(args, "Do the work")
        self.assertEqual(calls, ["gpt-5.6-terra|medium"])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["ending_real_status"], "pending")


if __name__ == "__main__":
    unittest.main()
