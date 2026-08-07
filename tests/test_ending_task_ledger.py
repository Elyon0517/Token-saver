import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "ending_task_ledger.py"
SPEC = importlib.util.spec_from_file_location("ending_task_ledger", SCRIPT_PATH)
LEDGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEDGER)


class EndingTaskLedgerTests(unittest.TestCase):
    def producer_receipt(self, root, project_name="project", **context_updates):
        project = root / project_name
        project.mkdir(exist_ok=True)
        context = {"project_root": str(project.resolve()), "task_type": "code", "module": "runtime", "file": "script.py", "symbol": "run", "code_kind": "python", "operation": "edit", "modality": "text", "complexity": "easy", "complexity_score": 12, "complexity_band": "small", "risk": "low", "ambiguity": "low", "task_summary": "Edit one function."}
        context.update(context_updates)
        pair = "gpt-5.3-codex-spark|low"
        receipt = {
            "status": "pass",
            "result_published": True,
            "turn_completed": True,
            "model_match": True,
            "effort_match": True,
            "node_type": "locked-route-node",
            "node_role": "result-producer",
            "requested_model": "gpt-5.3-codex-spark",
            "requested_effort": "low",
            "requested_pair": pair,
            "executed_pair": pair,
            "priority_attempt_pair": pair,
            "operational_failure_pairs": [],
            "workload_prompt_sha256": "1" * 64,
            "tokens": {"total_tokens": 101},
            "process_elapsed_ms": 250,
            "model_learning_context": context,
            "route_attempts": [{"status": "pass", "executed_pair": pair, "model_match": True, "effort_match": True}],
        }
        path = root / "producer-receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return project, path

    def test_passed_lifecycle_opens_final_gate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding="utf-8")
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Implemented the requested script change", project, "runtime", ["script.py"], store=store)
            pending = LEDGER.audit_lifecycle(started["lifecycle_id"], store)
            passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Independent Real Verify passed", ["Focused test passed"], store=store)
            audit = LEDGER.audit_lifecycle(started["lifecycle_id"], store)
            self.assertEqual(pending["terminal_status"], "pending")
            self.assertEqual(passed["lifecycle_status"], "passed")
            self.assertTrue(audit["final_gate_passed"])
            self.assertEqual(audit["chain"], [started["lifecycle_id"]])

    def test_terminal_ending_records_personal_memory_result_and_no_candidate_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, store=store)
            no_candidates = {"status": "no-candidates", "written": False, "candidates": 0}
            with patch.object(LEDGER, "_record_personal_memory_candidates", return_value=no_candidates) as record:
                passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", ["Focused test passed"], store=store)
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        record.assert_called_once_with(state, "pass", None)
        self.assertEqual(passed["personal_memory"], no_candidates)
        self.assertEqual(state["events"][-1]["personal_memory"], no_candidates)

    def test_terminal_ending_can_persist_a_candidate_file_result(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            candidate_file = project / "Cache" / "tests" / "ending-memory" / "candidates.json"
            candidate_file.parent.mkdir(parents=True)
            candidate_file.write_text(json.dumps({"candidates": [{"kind": "preference", "area": "ui", "statement": "Prefer compact status layouts.", "evidence": "Explicit user request.", "basis": "explicit_user_request", "confidence": "high", "source": "ending"}]}), encoding="utf-8")
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, store=store)
            captured = {"status": "written", "written": True, "candidates": 1, "vault": "ready"}
            with patch.object(LEDGER, "_record_personal_memory_candidates", return_value=captured) as record:
                passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", store=store, memory_candidates_file=candidate_file)
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        record.assert_called_once_with(state, "pass", candidate_file)
        self.assertEqual(passed["personal_memory"]["candidates"], 1)
        self.assertEqual(state["personal_memory"]["status"], "written")

    def test_memory_candidate_file_must_stay_inside_project_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, store=root / "store")
            with self.assertRaisesRegex(ValueError, "inside the lifecycle project root"):
                LEDGER._record_personal_memory_candidates(started | {"cwd": str(project)}, "pass", root / "outside.json")

    def test_failure_is_logged_before_repair_and_repair_has_own_ending(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            original = LEDGER.start_lifecycle("code", root, "Original task result", store=store)
            LEDGER.record_event(original["lifecycle_id"], "fail", "Real Verify found a correctness error", ["Expected 2 but observed 1"], "value-mismatch", store)
            repair = LEDGER.start_lifecycle("repair", root, "Repair the verified value mismatch", repair_of_lifecycle_id=original["lifecycle_id"], store=store)
            before_reverify = LEDGER.audit_lifecycle(original["lifecycle_id"], store)
            LEDGER.record_event(repair["lifecycle_id"], "pass", "A different Ending verifier passed the repaired result", ["Regression passed"], store=store)
            after_reverify = LEDGER.audit_lifecycle(original["lifecycle_id"], store)
            events = [json.loads(line) for line in (store / "index.jsonl").read_text(encoding="utf-8").splitlines()]
            failure_index = next(index for index, event in enumerate(events) if event["event"] == "fail")
            repair_index = next(index for index, event in enumerate(events) if event["event"] == "repair_started")
            self.assertEqual(before_reverify["terminal_status"], "pending")
            self.assertLess(failure_index, repair_index)
            self.assertEqual(after_reverify["terminal_status"], "passed")
            self.assertEqual(after_reverify["chain"], [original["lifecycle_id"], repair["lifecycle_id"]])

    def test_repair_requires_a_failed_parent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            original = LEDGER.start_lifecycle("text", root, "Original result", store=store)
            with self.assertRaisesRegex(ValueError, "failed parent"):
                LEDGER.start_lifecycle("repair", root, "Invalid early repair", repair_of_lifecycle_id=original["lifecycle_id"], store=store)

    def test_root_wide_repair_attempts_normalize_siblings_and_enforce_the_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            original = LEDGER.start_lifecycle("code", root, "Original result", store=store, max_repair_attempts=3)
            LEDGER.record_event(original["lifecycle_id"], "fail", "First verification failed", store=store)
            repair = LEDGER.start_lifecycle("repair", root, "First repair", repair_of_lifecycle_id=original["lifecycle_id"], store=store)
            LEDGER.record_event(repair["lifecycle_id"], "blocked", "Repair infrastructure failed", store=store)
            sibling = LEDGER.start_lifecycle("repair", root, "Second repair sibling", repair_of_lifecycle_id=original["lifecycle_id"], store=store)
            LEDGER.record_event(sibling["lifecycle_id"], "blocked", "Second repair infrastructure failed", store=store)
            third = LEDGER.start_lifecycle("repair", root, "Third repair from blocked child", repair_of_lifecycle_id=sibling["lifecycle_id"], store=store)
            LEDGER.record_event(third["lifecycle_id"], "fail", "Third repair verification failed", store=store)
            before_states = sorted((store / "lifecycles").glob("*.json"))
            with self.assertRaisesRegex(ValueError, "repair attempt limit exceeded"):
                LEDGER.start_lifecycle("repair", root, "Disallowed fourth repair", repair_of_lifecycle_id=original["lifecycle_id"], store=store)
            audit = LEDGER.audit_lifecycle(original["lifecycle_id"], store)
            repair_state = json.loads((store / "lifecycles" / f"{repair['lifecycle_id']}.json").read_text(encoding="utf-8"))
            sibling_state = json.loads((store / "lifecycles" / f"{sibling['lifecycle_id']}.json").read_text(encoding="utf-8"))
            third_state = json.loads((store / "lifecycles" / f"{third['lifecycle_id']}.json").read_text(encoding="utf-8"))
            root_state = json.loads((store / "lifecycles" / f"{original['lifecycle_id']}.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (store / "index.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(audit["terminal_status"], "blocked")
            self.assertEqual(audit["status"], "blocked")
            self.assertFalse(audit["final_gate_passed"])
            self.assertEqual(audit["root_lifecycle_id"], original["lifecycle_id"])
            self.assertEqual(audit["chain"], [original["lifecycle_id"], repair["lifecycle_id"], sibling["lifecycle_id"], third["lifecycle_id"]])
            self.assertEqual(audit["descendants"], [repair["lifecycle_id"], sibling["lifecycle_id"], third["lifecycle_id"]])
            self.assertEqual([repair_state["attempt_index"], sibling_state["attempt_index"], third_state["attempt_index"]], [1, 2, 3])
            self.assertEqual(repair_state["status"], "blocked")
            self.assertEqual(sibling_state["status"], "blocked")
            self.assertEqual(third_state["status"], "failed")
            self.assertEqual(root_state["status"], "blocked")
            self.assertEqual(root_state["events"][-1]["error_fingerprint"], "repair-attempt-limit-exceeded")
            self.assertEqual(before_states, sorted((store / "lifecycles").glob("*.json")))
            self.assertEqual(sum(event["event"] == "started" for event in events), 4)

    def test_bound_pass_records_model_result_before_terminal_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, "runtime", ["script.py"], store=store, producer_receipt=receipt)
            learned = {"status": "written", "written": True, "record_id": "record-1", "model_switch": {"status": "rebuilt", "records": 1}}
            with patch.object(LEDGER, "_record_bound_model_result", return_value=learned) as record:
                passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", ["Test passed"], store=store)
                duplicate = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", ["Test passed"], store=store)
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1:], ("pass", "none", "Real verification passed", 1))
        self.assertEqual(record.call_args.kwargs["ending_attempt_number"], 1)
        self.assertEqual(passed["model_learning"], learned)
        self.assertEqual(passed["model_learning"]["model_switch"]["status"], "rebuilt")
        self.assertEqual(state["events"][-1]["model_learning"], learned)
        self.assertEqual(state["producer_binding"]["status"], "recorded")
        self.assertEqual(passed["model_assessment"]["pass_shape"], "first_attempt_pass")
        self.assertEqual(passed["model_assessment"]["attempt_count"], 1)
        self.assertEqual(passed["model_assessment"]["model_suitability"], "suitable")
        self.assertEqual(duplicate["status"], "duplicate")

    def test_retry_pass_reports_original_pair_failure_and_verified_recovery(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            store = root / "store"
            original = LEDGER.start_lifecycle("verification", project, "First Ending", project, store=store, producer_receipt=receipt, selected_pair="gpt-5.6-luna|low")
            failed_learning = {
                "status": "written",
                "written": True,
                "pair": "gpt-5.6-luna|low",
                "next_pair": "gpt-5.6-terra|medium",
                "next_pair_direction": "upgrade",
                "model_record_document": "Projects/project/Model Routing/Tests and Verification.md",
                "model_record_link": "[[Projects/project/Model Routing/Tests and Verification]]",
            }
            passed_learning = {
                "status": "written",
                "written": True,
                "pair": "gpt-5.6-terra|medium",
                "next_pair": "gpt-5.6-terra|medium",
                "next_pair_direction": "freeze",
                "recovery_from_pair": "gpt-5.6-luna|low",
                "ending_attempt_number": 2,
                "model_record_document": "Projects/project/Model Routing/Tests and Verification.md",
                "model_record_link": "[[Projects/project/Model Routing/Tests and Verification]]",
                "obsidian": {"status": "written"},
            }
            with patch.object(LEDGER, "_record_bound_model_result", return_value=failed_learning) as failed_record:
                failed = LEDGER.record_event(original["lifecycle_id"], "fail", "First check found a mismatch", ["mismatch"], "mismatch", store, "correctness")
            repair = LEDGER.start_lifecycle("verification", project, "Fresh Ending after repair", project, repair_of_lifecycle_id=original["lifecycle_id"], store=store, producer_receipt=receipt, selected_pair="gpt-5.6-terra|medium")
            with patch.object(LEDGER, "_record_bound_model_result", return_value=passed_learning) as passed_record:
                passed = LEDGER.record_event(repair["lifecycle_id"], "pass", "Second check passed", ["regression pass"], store=store)
            audit = LEDGER.audit_lifecycle(original["lifecycle_id"], store)
        self.assertEqual(failed["model_assessment"]["model_suitability"], "too_weak_for_verified_result")
        self.assertEqual(failed_record.call_args.kwargs["ending_attempt_number"], 1)
        self.assertEqual(passed_record.call_args.kwargs["ending_attempt_number"], 2)
        self.assertEqual(passed_record.call_args.kwargs["prior_quality_failure_count"], 1)
        self.assertEqual(passed["model_assessment"]["pass_shape"], "retry_pass")
        self.assertEqual(passed["model_assessment"]["attempt_count"], 2)
        self.assertEqual(passed["model_assessment"]["model_suitability"], "initial_pair_too_weak_recovered")
        self.assertEqual(passed["model_assessment"]["routing_action"], "reuse_lowest_successful_recovery_pair")
        self.assertEqual(passed["model_assessment"]["next_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(passed["model_assessment"]["model_record_link"], "[[Projects/project/Model Routing/Tests and Verification]]")
        self.assertEqual(audit["attempt_count"], 2)

    def test_bound_receipt_accepts_current_extended_routing_context(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(
                root,
                step_kind="local-test",
                capability_tags=["model-routing", "persistent-end-task"],
                capability_fingerprint="a" * 64,
                entry_model="gpt-5.6-sol",
                entry_effort="max",
                entry_pair="gpt-5.6-sol|max",
                entry_source="explicit",
            )
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, "runtime", ["script.py"], store=root / "store", producer_receipt=receipt)
            state = json.loads(Path(started["local"]["state"]).read_text(encoding="utf-8"))
        context = state["producer_binding"]["model_learning_context"]
        self.assertEqual(context["step_kind"], "local-test")
        self.assertEqual(context["capability_tags"], ["model-routing", "persistent-end-task"])
        self.assertEqual(context["entry_pair"], "gpt-5.6-sol|max")

    def test_ending_keeps_its_own_score_and_pair_while_learning_from_producer(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            plan = project / "ending-plan.json"
            plan.write_text(json.dumps({"verification_required": True}), encoding="utf-8")
            started = LEDGER.start_lifecycle(
                "verification",
                project,
                "Run independent acceptance",
                project,
                "runtime",
                ["script.py"],
                store=root / "store",
                producer_receipt=receipt,
                complexity_score=68,
                complexity_band="complex",
                verification_required=True,
                verification_plan=plan,
                ending_check_id="acceptance",
                selected_pair="gpt-5.6-terra|high",
            )
            state = json.loads(Path(started["local"]["state"]).read_text(encoding="utf-8"))
        self.assertEqual(state["complexity_score"], 68)
        self.assertEqual(state["complexity_band"], "complex")
        self.assertEqual(state["producer_binding"]["model_learning_context"]["complexity_score"], 12)
        self.assertEqual(state["model_disclosure"]["current_pair"], "gpt-5.6-terra|high")
        self.assertEqual(state["model_disclosure"]["model_evidence"], "task_assignment")

    def test_real_bound_ending_writes_canonical_model_switch_projection_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            (home / "Documents" / "Muse").mkdir(parents=True)
            project, receipt = self.producer_receipt(home / "Documents" / "Muse", project_name="MuseAI")
            (project / "script.py").write_text("value = 1\n", encoding="utf-8")
            store = root / "store"
            vault = root / "vault"
            (vault / "Projects" / "MuseAI").mkdir(parents=True)
            (vault / "Projects" / "MuseAI" / "Model Switch.md").write_text("# Model Switch\n", encoding="utf-8")
            previous_vault = os.environ.get("CODEX_OBSIDIAN_VAULT")
            previous_local_store = os.environ.get("CODEX_MODEL_ROUTING_MEMORY")
            os.environ["CODEX_OBSIDIAN_VAULT"] = str(vault)
            os.environ["CODEX_MODEL_ROUTING_MEMORY"] = str(root / "model-routing-memory" / "events.jsonl")
            try:
                with patch("pathlib.Path.home", return_value=home):
                    started = LEDGER.start_lifecycle("code", project, "Result is ready", project, "runtime", ["script.py"], store=store, producer_receipt=receipt)
                    passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", ["Focused integration passed"], store=store)
                learned = passed["model_learning"]
                canonical_vault = vault.resolve()
                switch_path = canonical_vault / "Projects" / "MuseAI" / "Model Switch.md"
                record_path = canonical_vault / learned["model_record_document"]
                record_before = record_path.read_bytes()
                switch_before = switch_path.read_bytes()

                duplicate = LEDGER.record_event(started["lifecycle_id"], "pass", "Real verification passed", ["Focused integration passed"], store=store)

                self.assertEqual(learned["obsidian_note"], "Projects/MuseAI/Model Switch.md")
                self.assertEqual(learned["model_record_document"], "Projects/MuseAI/Model Routing/Normal Script Update.md")
                self.assertTrue(record_path.is_file())
                self.assertIn("# MuseAI · Normal Script Update", record_before.decode("utf-8"))
                self.assertIn(learned["record_id"], record_before.decode("utf-8"))
                self.assertIn("[[Projects/MuseAI/Model Routing/Normal Script Update]]", switch_before.decode("utf-8"))
                self.assertEqual(duplicate["status"], "duplicate")
                self.assertEqual(record_path.read_bytes(), record_before)
                self.assertEqual(switch_path.read_bytes(), switch_before)
                self.assertFalse(any(canonical_vault.rglob("ModelExperience/*.md")))
                self.assertFalse(any(project.rglob("model_experience.json")))
            finally:
                if previous_vault is None:
                    os.environ.pop("CODEX_OBSIDIAN_VAULT", None)
                else:
                    os.environ["CODEX_OBSIDIAN_VAULT"] = previous_vault
                if previous_local_store is None:
                    os.environ.pop("CODEX_MODEL_ROUTING_MEMORY", None)
                else:
                    os.environ["CODEX_MODEL_ROUTING_MEMORY"] = previous_local_store

    def test_bound_fail_requires_class_and_unavailable_memory_still_records_local_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, producer_receipt=receipt, store=store)
            with self.assertRaisesRegex(ValueError, "explicit failure_class"):
                LEDGER.record_event(started["lifecycle_id"], "fail", "Verification failed", store=store)
            unavailable = {"status": "unavailable", "written": False, "reason": "obsidian_vault_unavailable"}
            with patch.object(LEDGER, "_record_bound_model_result", return_value=unavailable) as record:
                result = LEDGER.record_event(started["lifecycle_id"], "fail", "Verification found an error", ["Mismatch"], store=store, failure_class="correctness")
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1:], ("fail", "correctness", "Verification found an error", 1))
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["lifecycle_status"], "failed")
        self.assertTrue(result["repair_required"])
        self.assertEqual(result["repair_handoff"]["action"], "blocked_origin_session_unavailable")
        self.assertTrue(result["repair_handoff"]["requires_origin_session"])
        self.assertFalse(result["final_gate_passed"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["producer_binding"]["status"], "unavailable")
        self.assertEqual(state["model_learning"], unavailable)

    def test_failure_dispatches_contextual_prompt_to_original_source_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            plan = project / "ending-plan.json"
            plan.write_text(json.dumps({"verification_required": True}), encoding="utf-8")
            store = root / "store"
            started = LEDGER.start_lifecycle("verification", project, "Verify the requested artifact", project, store=store, verification_required=True, verification_plan=plan, ending_check_id="artifact", project_id="project-123", origin_thread_id="source-session-001", origin_host_id="host-local")
            failed = LEDGER.record_event(started["lifecycle_id"], "fail", "The final artifact differs from the original request", ["Command exited 0; acceptance mismatch: approved line is missing."], "acceptance-mismatch", store=store, failure_class="correctness")
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
            repair = LEDGER.start_lifecycle("verification", project, "Fresh Ending after source-session repair", project, repair_of_lifecycle_id=started["lifecycle_id"], store=store, verification_required=True, verification_plan=plan, ending_check_id="artifact")
            repair_state = json.loads((store / "lifecycles" / f"{repair['lifecycle_id']}.json").read_text(encoding="utf-8"))
        handoff = failed["repair_handoff"]
        self.assertEqual(handoff["action"], "send_repair_prompt_to_origin_session_then_fresh_ending")
        self.assertEqual(handoff["repair_dispatch"]["tool"], "codex_app__send_message_to_thread")
        self.assertEqual(handoff["repair_dispatch"]["arguments"]["threadId"], "source-session-001")
        self.assertEqual(handoff["repair_dispatch"]["arguments"]["hostId"], "host-local")
        self.assertIn("missing", handoff["repair_prompt"])
        self.assertIn("Start a fresh Ending", handoff["repair_prompt"])
        self.assertEqual(state["origin_session"], {"thread_id": "source-session-001", "host_id": "host-local"})
        self.assertEqual(repair_state["origin_session"], state["origin_session"])
        self.assertEqual(repair_state["project_id"], "project-123")

    def test_final_failed_repair_is_immediately_blocked_at_the_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            original = LEDGER.start_lifecycle("verification", root, "Verify the requested artifact", root, store=store, max_repair_attempts=1, project_id="project-123", origin_thread_id="source-session-001", origin_host_id="host-local")
            LEDGER.record_event(original["lifecycle_id"], "fail", "Initial Ending failed", ["The acceptance check failed."], "acceptance-mismatch", store=store, failure_class="correctness")
            repair = LEDGER.start_lifecycle("verification", root, "Fresh Ending after source-session repair", root, repair_of_lifecycle_id=original["lifecycle_id"], store=store)
            final_failure = LEDGER.record_event(repair["lifecycle_id"], "fail", "The repaired result still differs", ["The acceptance check still fails."], "acceptance-mismatch", store=store, failure_class="correctness")
            audit = LEDGER.audit_lifecycle(original["lifecycle_id"], store)
            state = json.loads((store / "lifecycles" / f"{repair['lifecycle_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(final_failure["lifecycle_status"], "blocked")
        self.assertEqual(final_failure["repair_handoff"]["action"], "repair_limit_exhausted")
        self.assertEqual(audit["status"], "blocked")
        self.assertEqual(audit["terminal_status"], "blocked")
        self.assertTrue(any(event["event"] == "blocked" and event["error_fingerprint"] == "repair-attempt-limit-exceeded" for event in state["events"]))

    def test_verification_required_lifecycle_binds_real_plan_and_model_pair(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan = root / "ending-plan.json"
            plan.write_text(json.dumps({"verification_required": True}), encoding="utf-8")
            started = LEDGER.start_lifecycle("code", root, "Run real tests", complexity_score=60, complexity_band="complex", verification_required=True, verification_plan=plan, ending_check_id="unit", selected_pair="gpt-5.6-terra|ultra", store=root / "store")
            LEDGER.record_event(started["lifecycle_id"], "note", "Model disclosure remains audit history", store=root / "store")
            state = json.loads(Path(started["local"]["state"]).read_text(encoding="utf-8"))
        self.assertTrue(started["verification_required"])
        self.assertEqual(started["verification_plan"], str(plan.resolve()))
        self.assertEqual(state["ending_check_id"], "unit")
        self.assertEqual(state["selected_pair"], "gpt-5.6-terra|ultra")
        self.assertEqual(state["model_disclosure"], {"assigned_pair": "gpt-5.6-terra|ultra", "current_pair": "gpt-5.6-terra|ultra", "model_evidence": "task_assignment", "requested_pair": "gpt-5.6-terra|ultra", "resolved_pair": "gpt-5.6-terra|ultra", "effective_pair": "gpt-5.6-terra|ultra", "previous_pair": "same as current", "route_change": "no_switch", "switch_summary": "No model switch", "reason": "Best-known pair used; receipt not available.", "effective_evidence_level": "UNVERIFIED (no runtime receipt)"})
        self.assertEqual(state["events"][0]["model_disclosure"], state["model_disclosure"])

    def test_receipt_effective_pair_overrides_assignment_and_keeps_resolved_pair(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload.update({"requested_pair": "gpt-5.6-sol|ultra", "resolved_pair": "gpt-5.6-sol|ultra", "executed_pair": "gpt-5.3-codex-spark|medium"})
            receipt_payload["route_attempts"] = [{"status": "pass", "executed_pair": "gpt-5.3-codex-spark|medium", "model_match": True, "effort_match": True}]
            receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, selected_pair="gpt-5.6-sol|ultra", producer_receipt=receipt, store=root / "store")
            state = json.loads(Path(started["local"]["state"]).read_text(encoding="utf-8"))
        disclosure = state["model_disclosure"]
        self.assertEqual(disclosure["assigned_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(disclosure["model_evidence"], "runtime_receipt")
        self.assertEqual(disclosure["requested_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(disclosure["resolved_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(disclosure["effective_pair"], "gpt-5.3-codex-spark|medium")
        self.assertEqual(disclosure["current_pair"], "gpt-5.3-codex-spark|medium")
        self.assertEqual(disclosure["previous_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(disclosure["route_change"], "operational_fallback")
        self.assertEqual(disclosure["reason"], "Runtime receipt conflicts with resolved pair.")
        self.assertEqual(disclosure["effective_evidence_level"], "runtime_receipt")

    def test_unknown_model_disclosure_uses_an_explicit_unknown_pair(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            started = LEDGER.start_lifecycle("code", root, "Result is ready", store=root / "store")
            state = json.loads(Path(started["local"]["state"]).read_text(encoding="utf-8"))
        self.assertEqual(state["model_disclosure"]["current_pair"], "unknown|unknown")
        self.assertEqual(state["model_disclosure"]["model_evidence"], "unavailable")
        self.assertEqual(state["model_disclosure"]["requested_pair"], "unknown|unknown")
        self.assertEqual(state["model_disclosure"]["resolved_pair"], "unknown|unknown")
        self.assertEqual(state["model_disclosure"]["effective_pair"], "unknown|unknown")
        self.assertEqual(state["model_disclosure"]["effective_evidence_level"], "unavailable")
        self.assertEqual(state["model_disclosure"]["previous_pair"], "none")
        self.assertEqual(state["model_disclosure"]["switch_summary"], "No model switch")
        self.assertEqual(state["model_disclosure"]["reason"], "Previous-model provenance unavailable: no assignment or receipt.")

    def test_unbound_ending_assignment_records_non_learning_project_observation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            (project / "structure.md").write_text("verified\n", encoding="utf-8")
            store = root / "store"
            started = LEDGER.start_lifecycle(
                "verification",
                project,
                "Verify the structure record",
                project,
                "structure-record",
                ["structure.md"],
                store=store,
                complexity_score=18,
                selected_pair="gpt-5.6-luna|medium",
                model_evidence="task_assignment",
            )
            observation = {
                "status": "written",
                "written": True,
                "learning_eligible": False,
                "pair": "gpt-5.6-luna|medium",
                "next_pair": "gpt-5.6-luna|medium",
                "next_pair_direction": "no_switch",
                "model_suitability": "suitable_observed_no_runtime_receipt",
                "routing_action": "record_only_require_receipted_evidence_before_model_movement",
                "obsidian": {"status": "written"},
                "model_record_document": "Projects/project/Model Routing/Tests and Verification.md",
                "model_record_link": "[[Projects/project/Model Routing/Tests and Verification]]",
            }
            with patch.object(LEDGER, "_record_unbound_model_observation", return_value=observation) as record:
                passed = LEDGER.record_event(started["lifecycle_id"], "pass", "Independent file check passed", ["structure.md exists"], store=store)
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        record.assert_called_once()
        self.assertEqual(passed["model_learning"], observation)
        self.assertEqual(passed["model_assessment"]["producer_pair"], "unknown|unknown")
        self.assertEqual(passed["model_assessment"]["ending_pair"], "gpt-5.6-luna|medium")
        self.assertEqual(passed["model_assessment"]["model_record_pair"], "gpt-5.6-luna|medium")
        self.assertEqual(passed["model_assessment"]["model_record_status"], "written")
        self.assertEqual(passed["model_assessment"]["routing_action"], "record_only_require_receipted_evidence_before_model_movement")
        self.assertFalse(state["model_learning"]["learning_eligible"])

    def test_runtime_receipt_evidence_requires_a_validated_receipt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "requires a validated producer receipt"):
                LEDGER.start_lifecycle("code", root, "Result is ready", selected_pair="gpt-5.6-terra|ultra", model_evidence="runtime_receipt", store=root / "store")

    def test_unregistered_broad_model_switch_is_a_successful_learning_noop(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root)
            store = root / "store"
            started = LEDGER.start_lifecycle("code", project, "Result is ready", project, producer_receipt=receipt, store=store)
            no_op = {"status": "no-op", "written": False, "reason": "unregistered_or_missing_broad_model_switch"}
            with patch.object(LEDGER, "_record_bound_model_result", return_value=no_op):
                result = LEDGER.record_event(started["lifecycle_id"], "fail", "Verification found an error", ["Mismatch"], store=store, failure_class="correctness")
            state = json.loads((store / "lifecycles" / f"{started['lifecycle_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["lifecycle_status"], "failed")
        self.assertEqual(result["model_learning"], no_op)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["producer_binding"]["status"], "no-op")
        self.assertEqual(state["model_learning"], no_op)

    def test_bound_receipt_rejects_unsanitized_or_unknown_learning_context(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project, receipt = self.producer_receipt(root, task_summary="unsafe\nsummary", raw_prompt="secret")
            with self.assertRaisesRegex(ValueError, "exact sanitized"):
                LEDGER.start_lifecycle("code", project, "Result is ready", project, producer_receipt=receipt, store=root / "store")


if __name__ == "__main__":
    unittest.main()
