import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "obsidian_model_memory.py"
SPEC = importlib.util.spec_from_file_location("obsidian_model_memory", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ObsidianModelMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.local_store = self.root / "model-routing-memory" / "events.jsonl"
        self.local_store_patcher = mock.patch.dict(os.environ, {"CODEX_MODEL_ROUTING_MEMORY": str(self.local_store), "CODEX_THREAD_ID": "", "CODEX_SESSION_ID": "", "CODEX_TASK_NAME": "", "CODEX_TASK_GROUP": ""})
        self.local_store_patcher.start()
        self.path_home_patcher = mock.patch.object(module.Path, "home", lambda: self.home)
        self.path_home_patcher.start()
        self.project = self.home / "Documents" / "YofaGames" / "ThisIsMyOregon" / "ExampleProject"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "example.py").write_text("result = 1\n", encoding="utf-8")
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "Projects" / "ThisIsMyOregon").mkdir(parents=True)
        self.broad_page = self.vault / "Projects" / "ThisIsMyOregon" / "Model Switch.md"
        self.broad_page.write_text("# Model Switch\n", encoding="utf-8")
        self.broad_index = self.vault / "Projects" / "ThisIsMyOregon" / "index.md"
        self.broad_index.write_text("# ThisIsMyOregon\n", encoding="utf-8")
        self.receipt = self.root / "receipt.json"

    def tearDown(self):
        self.path_home_patcher.stop()
        self.local_store_patcher.stop()
        self.temporary.cleanup()

    def write_receipt(self, pair, path=None, context=None):
        target = path or self.receipt
        receipt = {"status": "pass", "result_published": True, "turn_completed": True, "model_match": True, "effort_match": True, "requested_pair": pair, "executed_pair": pair, "priority_attempt_pair": pair, "workload_prompt_sha256": "1" * 64, "tokens": {"total_tokens": 101}, "process_elapsed_ms": 1001}
        if context is not None:
            receipt.update({"node_type": "locked-route-node", "node_role": "result-producer", "model_learning_context": context, "route_attempts": [{"status": "pass", "executed_pair": pair, "model_match": True, "effort_match": True}]})
        target.write_text(json.dumps(receipt), encoding="utf-8")
        return target

    def quality_record(self, pair, *, status="pass", workload="1", tokens=100, elapsed=1000):
        return {
            "pair": pair,
            "receipt_status": "pass",
            "turn_completed": True,
            "model_match": True,
            "effort_match": True,
            "real_status": status,
            "failure_class": "none" if status == "pass" else "correctness",
            "workload_prompt_sha256": workload * 64,
            "total_tokens": tokens,
            "process_ms": elapsed,
        }

    def write_local_history(self, records, prefix="history"):
        self.local_store.parent.mkdir(parents=True, exist_ok=True)
        self.local_store.write_text(
            "".join(
                json.dumps({"local_model_memory_schema": 1, "event": "model-result", "event_id": f"{prefix}-{index}", "record": record}) + "\n"
                for index, record in enumerate(records)
            ),
            encoding="utf-8",
        )

    def active(self, records):
        shared, pairs = module.load_shared_ladder()
        query = {"task_type": "code", "complexity": "easy"}
        return module._active_recommendation(shared, pairs, query, records), pairs

    def record(self, recorded_at=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)):
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        self.write_receipt(recommendation["attempt_pair"])
        return module.record_model_result(self.project, "code", "example-module", self.receipt, "pass", "none", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault, recorded_at=recorded_at)

    def test_write_uses_one_category_page_with_one_structured_record(self):
        written = self.record()
        page = self.vault / written["obsidian_note"]
        category = self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Normal Script Update.md"
        self.assertEqual(written["status"], "written")
        self.assertEqual(written["model_record_document"], "Projects/ThisIsMyOregon/Model Routing/Normal Script Update.md")
        self.assertEqual(written["model_record_link"], "[[Projects/ThisIsMyOregon/Model Routing/Normal Script Update]]")
        self.assertEqual(category.read_text(encoding="utf-8").count("<!-- model-experience: "), 1)
        self.assertIn("[[Projects/ThisIsMyOregon/Model Routing/Normal Script Update]]", page.read_text(encoding="utf-8"))
        self.assertFalse(any(self.vault.rglob("ModelExperience/*.md")))
        self.assertFalse(any(self.vault.rglob(".model-experience.lock")))

    def test_receipt_replay_is_byte_idempotent(self):
        first = self.record()
        page = self.vault / first["obsidian_note"]
        before = page.read_bytes()
        replay = self.record(datetime(2026, 7, 13, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(page.read_bytes(), before)

    def test_sequential_path_backed_writes_and_stress_stay_on_one_page(self):
        first = self.record()
        page = self.vault / first["obsidian_note"]
        for index in range(1, 100):
            receipt = self.root / f"receipt-{index}.json"
            recommendation = module.recommend_model(self.project, "code", "example-module", file_value=Path("src") / f"example-{index}.py", symbol=f"Example.run{index}", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Sequential Path-backed write.", vault=self.vault)
            receipt.write_text(json.dumps({"status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "requested_pair": recommendation["attempt_pair"], "executed_pair": recommendation["attempt_pair"], "priority_attempt_pair": recommendation["attempt_pair"], "workload_prompt_sha256": f"{index:064x}", "tokens": {"total_tokens": index}, "process_elapsed_ms": index}), encoding="utf-8")
            written = module.record_model_result(self.project, "code", "example-module", receipt, "pass", "none", file_value=Path("src") / f"example-{index}.py", symbol=f"Example.run{index}", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Sequential Path-backed write.", vault=self.vault, recorded_at=datetime(2026, 7, 13, 12, 0, index % 60, tzinfo=timezone.utc))
            self.assertEqual(written["status"], "written")
        category = self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Normal Script Update.md"
        self.assertEqual(category.read_text(encoding="utf-8").count("<!-- model-experience: "), 100)
        self.assertFalse(any(self.vault.rglob("*.graph-index.json")))

    def test_reader_scopes_records_from_its_single_page(self):
        self.record()
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        self.assertEqual(recommendation["matched_records"], 1)
        self.assertEqual(recommendation["specificity"], "symbol")
        self.assertEqual(recommendation["local_record_count"], 1)
        self.assertEqual(recommendation["obsidian_record_count"], 1)
        self.assertEqual(recommendation["merged_record_count"], 1)
        self.assertEqual(recommendation["selection_basis"], "local_and_obsidian")

    def test_environment_session_becomes_active_scope_without_explicit_api_argument(self):
        session_id = "019fc8e5-87da-7082-90b9-6d505404d229"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": session_id}, clear=False):
            recommendation = module.recommend_model(self.project, "code", "environment-scope", task_summary="Use the active Codex session.", vault=self.vault)
        self.assertTrue(recommendation["scope_enforced"])
        self.assertEqual(recommendation["codex_session_key"], module.session_effort.session_key(session_id))

    def test_session_and_task_scope_never_cross_step_records(self):
        session_one = "019fc8e5-87da-7082-90b9-6d505404d229"
        session_two = "019fc8e5-87da-7082-90b9-6d505404d230"
        query = module._query(self.project, "code", "example-module", "src/example.py", "Example.run", "python", "edit", "text", "easy", 35, "low", "low", "scoped task", "implementation", None, "step-one")
        records = []
        for session_id, task_name, pair in ((session_one, "step-one", "gpt-5.6-terra|high"), (session_two, "step-two", "gpt-5.6-sol|high")):
            records.append({"model_experience_schema": 1, "record_id": f"{task_name}-record", "event_id": f"{task_name}-event", "project_key": query["project"]["key"], "project_owner": "ThisIsMyOregon", "task_type": query["task_type"], "task_name": task_name, "task_scope_key": module.session_effort.task_scope_key(query["project"]["key"], query["task_type"], query["module"], task_name), "codex_session_key": module.session_effort.session_key(session_id), "session_key": module.session_effort.session_key(session_id), "task_summary": query["task_summary"], "module": query["module"], "file": query["file"], "symbol": query["symbol"], "code_kind": query["code_kind"], "operation": query["operation"], "modality": query["modality"], "complexity": query["complexity"], "complexity_score": query["complexity_score"], "complexity_band": query["complexity_band"], "risk": query["risk"], "ambiguity": query["ambiguity"], "step_kind": query["step_kind"], "capability_tags": query["capability_tags"], "capability_fingerprint": query["capability_fingerprint"], "pair": pair, "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none", "recorded_at": "2026-08-06T12:00:00Z"})
        self.write_local_history(records, prefix="scoped")
        first = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", complexity_score=35, risk="low", ambiguity="low", task_summary="scoped task", task_name="step-one", session_id=session_one, vault=self.vault)
        second = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", complexity_score=35, risk="low", ambiguity="low", task_summary="scoped task", task_name="step-two", session_id=session_two, vault=self.vault)
        self.assertTrue(first["scope_enforced"])
        self.assertTrue(second["scope_enforced"])
        self.assertEqual(first["matched_records"], 1)
        self.assertEqual(second["matched_records"], 1)
        self.assertEqual(first["task_name"], "step-one")
        self.assertEqual(second["task_name"], "step-two")

    def test_related_session_group_is_read_with_evidence_but_unrelated_session_isolated(self):
        current_session = "019fc8e5-87da-7082-90b9-6d505404d231"
        related_session = "019fc8e5-87da-7082-90b9-6d505404d232"
        unrelated_session = "019fc8e5-87da-7082-90b9-6d505404d233"
        query = module._query(self.project, "code", "example-module", "src/example.py", "Example.run", "python", "edit", "text", "easy", 35, "low", "low", "related task", "implementation", None, "current-step", "shared-route")
        shared_group_key = module.session_effort.task_group_key(query["project"]["key"], "shared-route", "related-step")
        unrelated_group_key = module.session_effort.task_group_key(query["project"]["key"], "other-route", "unrelated-step")
        common = {"model_experience_schema": 1, "project_key": query["project"]["key"], "project_owner": "ThisIsMyOregon", "task_type": query["task_type"], "task_summary": query["task_summary"], "module": query["module"], "file": query["file"], "symbol": query["symbol"], "code_kind": query["code_kind"], "operation": query["operation"], "modality": query["modality"], "complexity": query["complexity"], "complexity_score": query["complexity_score"], "complexity_band": query["complexity_band"], "risk": query["risk"], "ambiguity": query["ambiguity"], "step_kind": query["step_kind"], "capability_tags": query["capability_tags"], "capability_fingerprint": query["capability_fingerprint"], "pair": "gpt-5.6-terra|high", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none", "recorded_at": "2026-08-06T12:00:00Z"}
        related = {**common, "record_id": "related-record", "event_id": "related-event", "task_name": "related-step", "task_group": "shared-route", "task_group_key": shared_group_key, "task_scope_key": module.session_effort.task_scope_key(query["project"]["key"], query["task_type"], query["module"], "related-step"), "codex_session_key": module.session_effort.session_key(related_session), "session_key": module.session_effort.session_key(related_session)}
        unrelated = {**common, "record_id": "unrelated-record", "event_id": "unrelated-event", "task_name": "unrelated-step", "task_group": "other-route", "task_group_key": unrelated_group_key, "task_scope_key": module.session_effort.task_scope_key(query["project"]["key"], query["task_type"], query["module"], "unrelated-step"), "codex_session_key": module.session_effort.session_key(unrelated_session), "session_key": module.session_effort.session_key(unrelated_session), "pair": "gpt-5.6-sol|high"}
        self.write_local_history([related, unrelated], prefix="related")
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", complexity_score=35, risk="low", ambiguity="low", task_summary="related task", task_name="current-step", task_group="shared-route", session_id=current_session, vault=self.vault)
        related_key = module.session_effort.session_key(related_session)
        self.assertEqual(recommendation["matched_records"], 1)
        self.assertEqual(recommendation["related_session_count"], 1)
        self.assertEqual(recommendation["related_session_keys"], [related_key])
        self.assertEqual(recommendation["scope_relation_counts"].get("related_task_group"), 1)
        self.assertEqual(recommendation["related_session_evidence"][0]["source_session_key"], related_key)
        self.assertEqual(recommendation["related_session_evidence"][0]["relation_reason"], "related_task_group")




    def test_local_first_write_survives_vault_outage_and_reconciles_later(self):
        unavailable_vault = self.root / "unavailable-vault"
        recommendation = module.recommend_model(self.project, "code", "offline-routing", file_value="src/example.py", symbol="Example.offline", code_kind="python", operation="edit", modality="text", complexity_score=35, risk="low", ambiguity="low", task_summary="Record offline model routing result.", vault=unavailable_vault)
        self.write_receipt(recommendation["attempt_pair"])
        written = module.record_model_result(self.project, "code", "offline-routing", self.receipt, "pass", "none", file_value="src/example.py", symbol="Example.offline", code_kind="python", operation="edit", modality="text", complexity_score=35, risk="low", ambiguity="low", task_summary="Record offline model routing result.", vault=unavailable_vault, outcome_reason="Focused offline verification passed.", verification_count=1)
        self.assertTrue(written["local"]["written"])
        self.assertEqual(written["obsidian"]["status"], "pending")
        self.assertEqual(written["pending_projection_count"], 1)
        self.assertEqual(len(module._read_local_records()), 1)
        reconciled = module.reconcile_local_model_history(self.project, vault=self.vault)
        self.assertEqual(reconciled["projected"], 1)
        self.assertEqual(reconciled["pending"], 0)
        projected = module._read_project_records(self.broad_page)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["event_id"], written["event_id"])
        self.assertEqual(projected[0]["outcome_reason"], "Focused offline verification passed.")

    def test_failure_then_stronger_pass_records_recovery_route_and_reasons(self):
        context = {"file_value": "src/example.py", "symbol": "Example.recover", "code_kind": "python", "operation": "feature", "modality": "text", "complexity_score": 35, "risk": "medium", "ambiguity": "low", "task_summary": "Recover a failed model routing task.", "vault": self.vault}
        first = module.recommend_model(self.project, "code", "recovery-routing", **context)
        self.write_receipt(first["attempt_pair"])
        failed = module.record_model_result(self.project, "code", "recovery-routing", self.receipt, "fail", "correctness", outcome_reason="Focused verification found an incorrect result.", verification_count=1, **context)
        second = module.recommend_model(self.project, "code", "recovery-routing", **context)
        self.assertEqual(second["attempt_reason"], "quality_failure_one_rung_up")
        self.assertNotEqual(second["attempt_pair"], first["attempt_pair"])
        second_receipt = self.root / "second-receipt.json"
        self.write_receipt(second["attempt_pair"], path=second_receipt)
        passed = module.record_model_result(self.project, "code", "recovery-routing", second_receipt, "pass", "none", outcome_reason="Regression verification passed after the model upgrade.", verification_count=2, ending_attempt_number=2, prior_quality_failure_count=1, **context)
        records = [record for record in module._read_local_records() if record["module"] == "recovery-routing"]
        self.assertEqual(len(records), 2)
        self.assertEqual(failed["next_pair"], second["attempt_pair"])
        self.assertEqual(passed["recovery_from_pair"], first["attempt_pair"])
        self.assertEqual(passed["ending_pass_shape"], "retry_pass")
        self.assertEqual(passed["ending_attempt_number"], 2)
        self.assertEqual(passed["next_pair_reason"], "verified_recovery_pair_retained")
        self.assertEqual(passed["model_suitability"], "initial_pair_too_weak_recovered")
        self.assertEqual(passed["routing_action"], "reuse_lowest_successful_recovery_pair")
        self.assertEqual(records[-1]["completed_pair"], second["attempt_pair"])
        self.assertEqual(records[-1]["outcome_reason"], "Regression verification passed after the model upgrade.")
        recommendation = module.recommend_model(self.project, "code", "recovery-routing", **context)
        self.assertEqual(recommendation["local_record_count"], 2)
        self.assertEqual(recommendation["obsidian_record_count"], 2)
        self.assertEqual(recommendation["merged_record_count"], 2)

    def test_first_real_pass_retains_pair_and_second_matching_pass_sets_one_rung_down(self):
        context = {"file_value": "src/example.py", "symbol": "Example.calibrate", "code_kind": "python", "operation": "feature", "modality": "text", "complexity_score": 35, "risk": "medium", "ambiguity": "low", "task_summary": "Calibrate a bounded model route.", "vault": self.vault}
        first_route = module.recommend_model(self.project, "code", "calibration-routing", **context)
        first_receipt = self.write_receipt(first_route["attempt_pair"], path=self.root / "calibration-first.json")
        first = module.record_model_result(self.project, "code", "calibration-routing", first_receipt, "pass", "none", ending_attempt_number=1, **context)
        second_route = module.recommend_model(self.project, "code", "calibration-routing", **context)
        second_receipt = self.write_receipt(second_route["attempt_pair"], path=self.root / "calibration-second.json")
        second_payload = json.loads(second_receipt.read_text(encoding="utf-8"))
        second_payload["workload_prompt_sha256"] = "2" * 64
        second_receipt.write_text(json.dumps(second_payload), encoding="utf-8")
        second = module.record_model_result(self.project, "code", "calibration-routing", second_receipt, "pass", "none", ending_attempt_number=1, **context)
        _, pairs = module.load_shared_ladder()
        expected_lower = pairs[pairs.index(first_route["attempt_pair"]) - 1]
        self.assertEqual(first["matched_pass_count_after"], 1)
        self.assertEqual(first["next_pair"], first_route["attempt_pair"])
        self.assertEqual(first["next_pair_reason"], "first_real_pass_retain_collecting_evidence")
        self.assertEqual(first["model_suitability"], "suitable")
        self.assertEqual(first["routing_action"], "retain_until_second_matching_first_pass")
        self.assertEqual(second["matched_pass_count_after"], 2)
        self.assertEqual(second["next_pair"], expected_lower)
        self.assertEqual(second["next_pair_direction"], "downgrade")
        self.assertEqual(second["next_pair_reason"], "two_matching_real_passes_trial_one_rung_down")
        self.assertEqual(second["model_suitability"], "suitable_downgrade_candidate")
        self.assertEqual(second["routing_action"], "trial_downgrade_one_rung_next_matching_task")

    def test_pre_result_timeout_records_neutral_operational_fallback(self):
        context = {"file_value": "src/example.py", "symbol": "Example.timeout", "code_kind": "python", "operation": "design", "modality": "text", "complexity_score": 82, "risk": "high", "ambiguity": "high", "task_summary": "Record a timed out routing stage.", "vault": self.vault}
        recommendation = module.recommend_model(self.project, "code-design", "timeout-routing", **context)
        receipt = {"status": "fail", "turn_completed": False, "model_match": False, "effort_match": False, "requested_pair": "gpt-5.6-sol|high", "tokens": {"total_tokens": 20}, "process_elapsed_ms": 300000}
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        recorded = module.record_model_result(self.project, "code-design", "timeout-routing", self.receipt, "fail", "timeout", outcome_reason="The routed stage timed out before publishing.", **context)
        self.assertEqual(recorded["switch_direction"], "operational_fallback")
        self.assertEqual(recorded["next_pair"], recommendation["attempt_pair"])
        self.assertEqual(recorded["outcome_reason"], "The routed stage timed out before publishing.")

    def test_shared_page_ignores_another_project_record(self):
        foreign = {"model_experience_schema": 1, "project_key": "other-project", "task_type": "code", "module": "example-module", "file": "src/example.py", "symbol": "Example.run", "code_kind": "python", "operation": "edit", "modality": "text", "complexity": "easy", "risk": "low", "ambiguity": "low", "pair": "gpt-5.6-terra|high", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none"}
        self.broad_page.write_text("# Model Switch\n\n<!-- model-experience: " + json.dumps(foreign) + " -->\n", encoding="utf-8")
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Isolation test.", vault=self.vault)
        self.assertEqual(recommendation["matched_records"], 0)

    def test_registered_project_move_reuses_old_model_learning(self):
        old_root = self.home / "Documents" / "YofaGames" / "XNews"
        current_root = self.home / "Documents" / "PythonProject" / "XNews"
        (old_root / "src").mkdir(parents=True)
        (current_root / "src").mkdir(parents=True)
        page = self.vault / "Projects" / "XNews" / "Model Switch.md"
        page.parent.mkdir(parents=True)
        old_key = module.project_change_memory._project_identity(old_root)["key"]
        old_record = {"model_experience_schema": 1, "project_key": old_key, "task_type": "code", "module": "feed", "file": "src/feed.py", "symbol": "Feed.run", "code_kind": "python", "operation": "edit", "modality": "text", "complexity": "easy", "risk": "low", "ambiguity": "low", "pair": "gpt-5.6-terra|high", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none", "recorded_at": "2026-07-15T12:00:00Z"}
        page.write_text("# Model Switch\n\n<!-- model-experience: " + json.dumps(old_record) + " -->\n", encoding="utf-8")
        recommendation = module.recommend_model(current_root, "code", "feed", file_value="src/feed.py", symbol="Feed.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Update feed parser.", vault=self.vault)
        self.assertEqual(recommendation["matched_records"], 1)
        self.assertEqual(recommendation["specificity"], "symbol")
        self.assertEqual(recommendation["matched_records"], 1)
        self.assertEqual(recommendation["specificity"], "symbol")


    def test_compact_recommendation_is_bounded_route_capsule(self):
        self.record()
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        capsule = module._compact_recommendation(recommendation)
        capsule_bytes = len(json.dumps(capsule, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(capsule["status"], "ready")
        self.assertEqual(capsule["attempt_pair"], recommendation["attempt_pair"])
        self.assertEqual(capsule["capability_fingerprint"], recommendation["capability_fingerprint"])
        self.assertLess(capsule_bytes, 4096)
        self.assertIn("route_capsule", capsule)

    def test_rebuild_hides_foreign_rows_but_preserves_structured_record(self):
        self.record()
        own = module._read_project_records(self.broad_page)[0]
        foreign = dict(own, record_id="foreign", event_id="foreign-event", project_key="unrelated-project", project_owner="Unrelated", module="foreign-module")
        self.broad_page.write_text("# Model Switch\n\n" + "\n".join("<!-- model-experience: " + json.dumps(record) + " -->" for record in (own, foreign)) + "\n", encoding="utf-8")
        result = module.rebuild_model_switches(self.project, vault=self.vault)
        text = self.broad_page.read_text(encoding="utf-8")
        records = module._read_project_records(self.broad_page)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["page_records"], 2)
        self.assertNotIn("| foreign-module |", text)
        self.assertEqual({record.get("record_id") for record in records}, {own.get("record_id"), "foreign"})

    def test_same_name_project_rebuild_no_op_with_local_only_clone(self):
        root_one = self.home / "Documents" / "Muse" / "SVGDrawer"
        root_two = self.root / "other" / "SVGDrawer"
        root_one.mkdir(parents=True)
        root_two.mkdir(parents=True)
        shared_page = self.vault / "Projects" / "SVGDrawer" / "Model Switch.md"
        shared_page.parent.mkdir(parents=True)
        foreign = {"model_experience_schema": 1, "project_key": module.project_change_memory._project_identity(root_one)["key"], "task_type": "code", "module": "root-one", "file": "src/one.py", "symbol": "run", "code_kind": "python", "operation": "edit", "modality": "text", "complexity": "easy", "risk": "low", "ambiguity": "low", "pair": "gpt-5.6-terra|high", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none"}
        shared_page.write_text("# Model Switch\n\n<!-- model-experience: " + json.dumps(foreign) + " -->\n", encoding="utf-8")
        result = module.rebuild_model_switches(root_two, vault=self.vault)
        records = module._read_project_records(shared_page)
        self.assertEqual(result["status"], "no-op")
        self.assertEqual(records, [foreign])

    def test_registered_owner_status_reports_missing_model_switch_without_crash(self):
        status = module.memory_status(self.project, vault=self.vault)
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["memory_available"])
        self.assertIsNone(status["reason"])
        self.broad_page.unlink()
        status = module.memory_status(self.project, vault=self.vault)
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["memory_available"])
        self.assertEqual(status["reason"], "configured_model_switch_missing")
        self.assertEqual(status["model_switch_owner"], "ThisIsMyOregon")

    def test_first_receipt_backed_record_lazily_creates_broad_page_and_links_index(self):
        self.broad_page.unlink()
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        self.write_receipt(recommendation["attempt_pair"], path=self.root / "first.json")
        result = module.record_model_result(self.project, "code", "example-module", self.root / "first.json", "pass", "none", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        self.assertEqual(result["status"], "written")
        text = self.broad_page.read_text(encoding="utf-8")
        index = self.broad_index.read_text(encoding="utf-8")
        category = self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Normal Script Update.md"
        self.assertEqual(category.read_text(encoding="utf-8").count("<!-- model-experience: "), 1)
        self.assertIn("- [[Projects/ThisIsMyOregon/Model Switch|Model Switch]] — Compact entry to native linked model-routing categories.", index)
        self.assertEqual(result["model_switch_document"], "Projects/ThisIsMyOregon/Model Switch.md")
        self.assertEqual(result["model_switch_link"], "[[Projects/ThisIsMyOregon/Model Switch]]")

    def test_source_checkout_recommendation_exposes_global_model_switch_link(self):
        source_root = self.home / "Documents" / "AIProject" / "qin-codex-skills"
        source_root.mkdir(parents=True)
        (self.vault / "Skills").mkdir()
        recommendation = module.recommend_model(source_root, "code", "global-skill-routing", code_kind="python", operation="audit", complexity_score=82, risk="low", ambiguity="medium", task_summary="Audit the global skill route.", vault=self.vault)
        self.assertEqual(recommendation["model_switch_status"], "pending")
        self.assertEqual(recommendation["model_switch_document"], "Skills/Model Switch.md")
        self.assertEqual(recommendation["model_switch_link"], "[[Skills/Model Switch]]")

    def test_real_absolute_nested_svgdrawer_mapping_is_more_specific_than_muse(self):
        query = {"project": {"name": "skill", "root": str(self.home / "Documents" / "Muse" / "SVGDrawer" / "skill"), "key": "svgdrawer-test"}}
        (self.vault / "Projects" / "SVGDrawer").mkdir(parents=True)
        (self.vault / "Projects" / "SVGDrawer" / "Model Switch.md").write_text("# Model Switch\n", encoding="utf-8")
        _, page = module._memory_root(query, self.vault)
        self.assertEqual(page.resolve(), (self.vault / "Projects" / "SVGDrawer" / "Model Switch.md").resolve())

    def test_unknown_root_cannot_create_a_broad_page(self):
        unknown = self.root / "Desktop"
        unknown.mkdir()
        before = list((self.vault / "Projects").rglob("*.md"))
        result = module.rebuild_model_switches(unknown, vault=self.vault)
        self.assertEqual(result["status"], "no-op")
        self.assertEqual(list((self.vault / "Projects").rglob("*.md")), before)

    def test_categories_are_fixed(self):
        self.assertEqual(len(module.MODEL_SWITCH_CATEGORIES), 6)
        self.assertEqual(module._task_category({"task_type": "code", "code_kind": "python", "operation": "edit"}), "normal-script-update")

    def test_standard_score_cold_start_executes_recommended_quality_pair(self):
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Cold start.", vault=self.vault)
        self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["attempt_pair"], recommendation["selected_pair"])
        self.assertEqual(recommendation["active_fallback_pair"], "gpt-5.6-terra|high")
        self.assertEqual(recommendation["complexity_score"], 35)
        self.assertEqual(recommendation["priority_producer_scope"], "bounded_text_code_and_scheduled_independent_sources")

    def test_sol_ultra_entry_routes_down_to_contextual_cold_start(self):
        recommendation = module.recommend_model(self.project, "code", "entry-aware", operation="work", complexity_score=35, task_summary="Route from a frontier entry.", entry_model="gpt-5.6-sol", entry_effort="ultra", vault=self.vault)
        self.assertEqual(recommendation["entry_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(recommendation["entry_anchor_pair"], "gpt-5.6-sol|ultra")
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["switch_direction"], "downgrade")
        self.assertEqual(recommendation["reason"], "shared_cold_start")
        self.assertEqual(recommendation["entry_route_reason"], "contextual_static_below_entry")

    def test_luna_max_entry_starts_at_entry_without_history(self):
        recommendation = module.recommend_model(self.project, "code", "entry-aware", operation="work", complexity_score=35, task_summary="Route from a lower entry.", entry_model="gpt-5.6-luna", entry_effort="max", vault=self.vault)
        self.assertEqual(recommendation["entry_pair"], "gpt-5.6-luna|max")
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-luna|max")
        self.assertEqual(recommendation["switch_direction"], "no_switch")
        self.assertEqual(recommendation["reason"], "entry_anchored_cold_start")

    def test_matching_task_and_difficulty_reuse_local_boundary_across_project_roots(self):
        foreign_project = self.root / "archived" / "ExampleProject"
        foreign_project.mkdir(parents=True)
        foreign_key = module.project_change_memory._project_identity(foreign_project)["key"]
        common = {"model_experience_schema": 1, "project_key": foreign_key, "task_type": "code", "module": "portable-routing", "file": "", "symbol": "", "code_kind": "python", "operation": "work", "modality": "text", "complexity": "easy", "complexity_score": 42, "complexity_band": "standard", "risk": "low", "ambiguity": "low", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True}
        records = [
            {**common, "pair": "gpt-5.6-luna|max", "real_status": "fail", "failure_class": "correctness"},
            {**common, "pair": "gpt-5.6-terra|low", "real_status": "fail", "failure_class": "correctness"},
            {**common, "pair": "gpt-5.6-terra|medium", "real_status": "pass", "failure_class": "none"},
        ]
        self.local_store.parent.mkdir(parents=True)
        self.local_store.write_text("".join(json.dumps({"local_model_memory_schema": 1, "event": "model-result", "event_id": f"transfer-{index}", "record": record}) + "\n" for index, record in enumerate(records)), encoding="utf-8")
        recommendation = module.recommend_model(self.project, "code", "portable-routing", code_kind="python", operation="work", modality="text", complexity_score=42, risk="low", ambiguity="low", task_summary="Reuse a matching historical boundary.", entry_model="gpt-5.6-luna", entry_effort="max", vault=self.vault)
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["selection_basis"], "local_transfer_history")
        self.assertEqual(recommendation["specificity"], "cross_project_module")
        self.assertEqual(recommendation["transfer_record_count"], 3)
        self.assertEqual(recommendation["switch_direction"], "upgrade")

    def test_cross_project_history_requires_matching_task_difficulty(self):
        foreign = {"model_experience_schema": 1, "project_key": "foreign-project", "task_type": "code", "module": "portable-routing", "file": "", "symbol": "", "code_kind": "python", "operation": "work", "modality": "text", "complexity": "complex", "complexity_score": 68, "complexity_band": "complex", "risk": "low", "ambiguity": "low", "pair": "gpt-5.6-terra|high", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none"}
        self.local_store.parent.mkdir(parents=True)
        self.local_store.write_text(json.dumps({"local_model_memory_schema": 1, "event": "model-result", "event_id": "wrong-band", "record": foreign}) + "\n", encoding="utf-8")
        recommendation = module.recommend_model(self.project, "code", "portable-routing", code_kind="python", operation="work", modality="text", complexity_score=42, risk="low", ambiguity="low", task_summary="Do not transfer a different difficulty.", entry_model="gpt-5.6-luna", entry_effort="max", vault=self.vault)
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-luna|max")
        self.assertEqual(recommendation["transfer_record_count"], 0)

    def test_verified_boundary_overrides_both_low_and_high_entries(self):
        project = module.project_change_memory._project_identity(self.project)
        context = {"model_experience_schema": 1, "project_key": project["key"], "project_owner": "ThisIsMyOregon", "task_type": "code", "module": "stable-entry-aware", "file": "src/example.py", "symbol": "Example.route", "code_kind": "python", "operation": "work", "modality": "text", "complexity": "easy", "complexity_score": 35, "complexity_band": "standard", "risk": "low", "ambiguity": "low", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True}
        failed = {**context, "pair": "gpt-5.6-terra|medium", "real_status": "fail", "failure_class": "correctness"}
        passed = {**context, "pair": "gpt-5.6-terra|high", "real_status": "pass", "failure_class": "none"}
        self.broad_page.write_text("# Model Switch\n\n" + "\n".join("<!-- model-experience: " + json.dumps(record) + " -->" for record in (failed, passed)) + "\n", encoding="utf-8")
        common = {"file_value": "src/example.py", "symbol": "Example.route", "code_kind": "python", "operation": "work", "complexity_score": 35, "task_summary": "Reuse the verified routing boundary.", "vault": self.vault}
        low = module.recommend_model(self.project, "code", "stable-entry-aware", entry_model="gpt-5.6-luna", entry_effort="low", **common)
        high = module.recommend_model(self.project, "code", "stable-entry-aware", entry_model="gpt-5.6-sol", entry_effort="ultra", **common)
        self.assertEqual(low["attempt_pair"], "gpt-5.6-terra|high")
        self.assertEqual(low["switch_direction"], "upgrade")
        self.assertEqual(high["attempt_pair"], "gpt-5.6-terra|high")
        self.assertEqual(high["switch_direction"], "downgrade")
        self.assertEqual(low["calibration_state"], "frozen")

    def test_similar_image_control_step_reuses_recovered_model_from_both_entries(self):
        foreign_project = self.root / "archived" / "ImageController"
        foreign_project.mkdir(parents=True)
        profile = module.task_capability_profile(
            "code", "python", "work", "mixed", 42, "low", "low",
            "Control code that generates images through a local tool.",
        )
        common = {
            "model_experience_schema": 1,
            "project_key": module.project_change_memory._project_identity(foreign_project)["key"],
            "task_type": "code",
            "task_summary": "Control code that generates images through a local tool.",
            "module": "legacy-image-controller",
            "file": "",
            "symbol": "",
            "code_kind": "python",
            "operation": "work",
            "modality": "mixed",
            "complexity": "easy",
            "complexity_score": 42,
            "complexity_band": "standard",
            "risk": "low",
            "ambiguity": "low",
            "step_kind": profile["step_kind"],
            "capability_tags": profile["capability_tags"],
            "capability_fingerprint": profile["capability_fingerprint"],
            "receipt_status": "pass",
            "turn_completed": True,
            "model_match": True,
            "effort_match": True,
        }
        self.write_local_history([
            {**common, "pair": "gpt-5.6-luna|max", "real_status": "fail", "failure_class": "correctness"},
            {**common, "pair": "gpt-5.6-terra|medium", "real_status": "pass", "failure_class": "none"},
        ], prefix="image-control")
        scope = {
            "code_kind": "python",
            "operation": "work",
            "modality": "mixed",
            "complexity_score": 42,
            "risk": "low",
            "ambiguity": "low",
            "task_summary": "用代码控制图片生成，并调用本地工具。",
            "step_kind": "image-generation-control",
            "capability_tags": ["image-generation", "tool-control"],
            "vault": self.vault,
        }
        low_entry = module.recommend_model(self.project, "code", "new-image-controller", entry_model="gpt-5.6-luna", entry_effort="max", **scope)
        high_entry = module.recommend_model(self.project, "code", "new-image-controller", entry_model="gpt-5.6-sol", entry_effort="ultra", **scope)
        for recommendation in (low_entry, high_entry):
            self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-terra|medium")
            self.assertEqual(recommendation["reason"], "verified_quality_boundary")
            self.assertEqual(recommendation["calibration_state"], "frozen")
            self.assertEqual(recommendation["selection_basis"], "local_transfer_history")
            self.assertEqual(recommendation["transfer_record_count"], 2)
            self.assertEqual(recommendation["step_kind"], "image-generation-control")
        self.assertEqual(low_entry["switch_direction"], "upgrade")
        self.assertEqual(high_entry["switch_direction"], "downgrade")

    def test_compound_implementation_and_local_test_steps_reuse_separate_models(self):
        project = module.project_change_memory._project_identity(self.project)
        implementation_profile = module.task_capability_profile("code", "python", "implement", "text", 38, "low", "low", "Implement the bounded parser change.")
        test_profile = module.task_capability_profile("code", "python", "test", "text", 18, "low", "low", "Run the local pytest regression suite.")
        common = {
            "model_experience_schema": 1,
            "project_key": project["key"],
            "project_owner": "ThisIsMyOregon",
            "task_type": "code",
            "module": "compound-parser",
            "file": "src/example.py",
            "symbol": "",
            "code_kind": "python",
            "modality": "text",
            "risk": "low",
            "ambiguity": "low",
            "receipt_status": "pass",
            "turn_completed": True,
            "model_match": True,
            "effort_match": True,
            "real_status": "pass",
            "failure_class": "none",
        }
        implementation = {**common, "task_summary": "Implement the bounded parser change.", "operation": "implement", "complexity": "easy", "complexity_score": 38, "complexity_band": "standard", "pair": "gpt-5.6-terra|medium", "step_kind": implementation_profile["step_kind"], "capability_tags": implementation_profile["capability_tags"], "capability_fingerprint": implementation_profile["capability_fingerprint"]}
        local_test = {**common, "task_summary": "Run the local pytest regression suite.", "operation": "test", "complexity": "easy", "complexity_score": 18, "complexity_band": "small", "pair": "gpt-5.6-luna|low", "step_kind": test_profile["step_kind"], "capability_tags": test_profile["capability_tags"], "capability_fingerprint": test_profile["capability_fingerprint"]}
        self.write_local_history([implementation, local_test], prefix="compound")
        implementation_route = module.recommend_model(self.project, "code", "compound-parser", file_value="src/example.py", code_kind="python", operation="implement", complexity_score=38, task_summary="Implement the bounded parser change.", entry_model="gpt-5.6-sol", entry_effort="ultra", vault=self.vault)
        test_route = module.recommend_model(self.project, "code", "compound-parser", file_value="src/example.py", code_kind="python", operation="test", complexity_score=18, task_summary="Run the local pytest regression suite.", entry_model="gpt-5.6-sol", entry_effort="ultra", vault=self.vault)
        self.assertEqual(implementation_route["attempt_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(implementation_route["step_kind"], "implementation")
        self.assertEqual(test_route["attempt_pair"], "gpt-5.6-luna|low")
        self.assertEqual(test_route["step_kind"], "local-test")
        self.assertNotEqual(implementation_route["capability_fingerprint"], test_route["capability_fingerprint"])
        self.assertEqual(implementation_route["switch_direction"], "downgrade")
        self.assertEqual(test_route["switch_direction"], "downgrade")

    def test_nearby_but_different_capability_does_not_reuse_image_control_history(self):
        foreign_project = self.root / "archived" / "ImageMetadata"
        foreign_project.mkdir(parents=True)
        profile = module.task_capability_profile("code", "python", "work", "text", 42, "low", "low", "Control code that generates images.")
        foreign = {"model_experience_schema": 1, "project_key": module.project_change_memory._project_identity(foreign_project)["key"], "task_type": "code", "task_summary": "Control code that generates images.", "module": "image-pipeline", "file": "", "symbol": "", "code_kind": "python", "operation": "work", "modality": "text", "complexity": "easy", "complexity_score": 42, "complexity_band": "standard", "risk": "low", "ambiguity": "low", "pair": "gpt-5.6-terra|medium", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "pass", "failure_class": "none", "step_kind": profile["step_kind"], "capability_tags": profile["capability_tags"], "capability_fingerprint": profile["capability_fingerprint"]}
        self.write_local_history([foreign], prefix="nearby")
        recommendation = module.recommend_model(self.project, "code", "image-pipeline", code_kind="python", operation="work", modality="text", complexity_score=42, task_summary="Update image metadata labels.", entry_model="gpt-5.6-luna", entry_effort="max", vault=self.vault)
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-luna|max")
        self.assertEqual(recommendation["transfer_record_count"], 0)
        self.assertEqual(recommendation["reason"], "entry_anchored_cold_start")

    def test_record_projects_entry_pair_with_task_difficulty(self):
        recommendation = module.recommend_model(self.project, "code", "record-entry", operation="work", complexity_score=35, task_summary="Record entry-aware routing.", entry_model="gpt-5.6-luna", entry_effort="max", vault=self.vault)
        self.write_receipt(recommendation["attempt_pair"])
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt.update({"entry_model": "gpt-5.6-luna", "entry_effort": "max", "entry_pair": "gpt-5.6-luna|max", "entry_source": "explicit"})
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        recorded = module.record_model_result(self.project, "code", "record-entry", self.receipt, "pass", "none", operation="work", complexity_score=35, task_summary="Record entry-aware routing.", vault=self.vault)
        record = next(record for record in module._read_project_records(self.broad_page) if record["module"] == "record-entry")
        self.assertEqual(recorded["entry_pair"], "gpt-5.6-luna|max")
        self.assertEqual(record["entry_anchor_pair"], "gpt-5.6-luna|max")
        self.assertEqual(record["complexity_band"], "standard")
        self.assertEqual(record["step_kind"], "implementation")
        self.assertRegex(record["capability_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertIn("code-authoring", record["capability_tags"])
        self.assertTrue(any("record-entry" in page.read_text(encoding="utf-8") for page in (self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing").glob("*.md")))

    def test_small_edit_score_uses_spark_priority_with_quality_fallback(self):
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity_score=12, risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)
        self.assertEqual(recommendation["complexity_band"], "small")
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.3-codex-spark|low")
        self.assertEqual(recommendation["selected_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["active_fallback_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["switch_direction"], "downgrade")

    def test_small_question_uses_spark_priority_with_quality_fallback(self):
        recommendation = module.recommend_model(self.project, "question", "example-module", operation="answer", modality="text", complexity_score=8, risk="low", ambiguity="low", task_summary="What is seven times eight?", vault=self.vault)
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.3-codex-spark|low")
        self.assertEqual(recommendation["selected_pair"], "gpt-5.6-luna|low")
        self.assertEqual(recommendation["active_fallback_pair"], "gpt-5.6-luna|low")

    def test_spark_verify_failure_suppresses_matching_score_band_and_upgrades(self):
        project = module.project_change_memory._project_identity(self.project)
        failed = {"model_experience_schema": 1, "project_key": project["key"], "task_type": "code", "module": "other-module", "file": "", "symbol": "", "code_kind": "python", "operation": "edit", "modality": "text", "complexity": "easy", "complexity_score": 18, "complexity_band": "small", "risk": "low", "ambiguity": "low", "pair": "gpt-5.3-codex-spark|low", "receipt_status": "pass", "turn_completed": True, "model_match": True, "effort_match": True, "real_status": "fail", "failure_class": "correctness"}
        self.broad_page.write_text("# Model Switch\n\n<!-- model-experience: " + json.dumps(failed) + " -->\n", encoding="utf-8")
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity_score=8, risk="low", ambiguity="low", task_summary="Edit another bounded Python method.", vault=self.vault)
        self.assertEqual(recommendation["priority_verdict"], "fail")
        self.assertEqual(recommendation["attempt_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(recommendation["switch_direction"], "upgrade")
        self.assertEqual(recommendation["attempt_reason"], "spark_verify_failure_upgrade")

    def test_one_real_pass_collects_evidence_and_two_passes_downgrade_one_rung(self):
        first, pairs = self.active([self.quality_record("gpt-5.6-terra|medium")])
        second, _ = self.active([
            self.quality_record("gpt-5.6-terra|medium", workload="1"),
            self.quality_record("gpt-5.6-terra|medium", workload="2"),
        ])
        self.assertEqual(first["selected_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(first["reason"], "real_pass_collecting_evidence")
        self.assertFalse(first["trial"])
        self.assertEqual(second["selected_pair"], pairs[pairs.index("gpt-5.6-terra|medium") - 1])
        self.assertEqual(second["reason"], "repeated_real_pass_one_rung_down")
        self.assertTrue(second["trial"])

    def test_quality_failure_upgrades_exactly_one_rung(self):
        active, pairs = self.active([self.quality_record("gpt-5.6-terra|medium", status="fail")])
        self.assertEqual(active["selected_pair"], pairs[pairs.index("gpt-5.6-terra|medium") + 1])
        self.assertEqual(active["reason"], "quality_failure_one_rung_up")

    def test_like_for_like_cost_is_diagnostic_and_never_overrides_lowest_correct_pair(self):
        records = [
            self.quality_record("gpt-5.6-luna|low", workload="1", tokens=200, elapsed=100),
            self.quality_record("gpt-5.6-terra|medium", workload="1", tokens=100, elapsed=500),
        ]
        token_diagnostic, pairs = self.active(records)
        self.assertEqual(token_diagnostic["selected_pair"], "gpt-5.6-luna|low")
        self.assertEqual(token_diagnostic["reason"], "verified_floor_retained")
        self.assertEqual(token_diagnostic["cost_evidence"]["status"], "like_for_like")
        records[0].update(total_tokens=100, process_ms=300)
        records[1].update(total_tokens=100, process_ms=500)
        time_diagnostic, _ = self.active(records)
        self.assertEqual(time_diagnostic["selected_pair"], "gpt-5.6-luna|low")
        records[0]["process_ms"] = records[1]["process_ms"] = 500
        tie_diagnostic, _ = self.active(records)
        self.assertEqual(tie_diagnostic["selected_pair"], "gpt-5.6-luna|low")
        recovered, _ = self.active([
            self.quality_record(pairs[0], status="fail", workload="1", tokens=20, elapsed=20),
            self.quality_record(pairs[1], workload="1", tokens=300, elapsed=300),
            self.quality_record(pairs[2], workload="1", tokens=30, elapsed=30),
        ])
        self.assertEqual(recovered["selected_pair"], pairs[1])
        self.assertEqual(recovered["reason"], "verified_quality_boundary")

    def test_bound_historical_failure_records_once_after_recommendation_advances(self):
        pair = "gpt-5.6-terra|high"
        context = {"project_root": str(self.project.resolve()), "task_type": "documentation-instructions", "module": "example-module", "file": "src/example.py", "symbol": "Example.run", "code_kind": "python", "operation": "repair", "modality": "text", "complexity": "complex", "risk": "high", "ambiguity": "low", "task_summary": "Record a bound historical failure."}
        first_receipt = self.write_receipt(pair, self.root / "historical-one.json", context)
        first_binding = {"receipt_sha256": module.hashlib.sha256(first_receipt.read_bytes()).hexdigest(), "model_learning_context": context, "executed_pair": pair}
        first = module.record_model_result(self.project, "documentation-instructions", "example-module", first_receipt, "fail", "correctness", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="repair", modality="text", complexity="complex", risk="high", ambiguity="low", task_summary="Record a bound historical failure.", vault=self.vault, bound_receipt=first_binding)
        advanced = module.recommend_model(self.project, "documentation-instructions", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="repair", modality="text", complexity="complex", risk="high", ambiguity="low", task_summary="Record a bound historical failure.", vault=self.vault)
        second_receipt = self.write_receipt(pair, self.root / "historical-two.json", context)
        second_payload = json.loads(second_receipt.read_text(encoding="utf-8"))
        second_payload["workload_prompt_sha256"] = "2" * 64
        second_receipt.write_text(json.dumps(second_payload), encoding="utf-8")
        second_binding = {"receipt_sha256": module.hashlib.sha256(second_receipt.read_bytes()).hexdigest(), "model_learning_context": context, "executed_pair": pair}
        second = module.record_model_result(self.project, "documentation-instructions", "example-module", second_receipt, "fail", "correctness", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="repair", modality="text", complexity="complex", risk="high", ambiguity="low", task_summary="Record a bound historical failure.", vault=self.vault, bound_receipt=second_binding)
        replay = module.record_model_result(self.project, "documentation-instructions", "example-module", second_receipt, "fail", "correctness", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="repair", modality="text", complexity="complex", risk="high", ambiguity="low", task_summary="Record a bound historical failure.", vault=self.vault, bound_receipt=second_binding)
        final = module.recommend_model(self.project, "documentation-instructions", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="repair", modality="text", complexity="complex", risk="high", ambiguity="low", task_summary="Record a bound historical failure.", vault=self.vault)
        self.assertEqual(first["status"], "written")
        self.assertEqual(advanced["selected_pair"], "gpt-5.6-terra|xhigh")
        self.assertEqual(advanced["attempt_pair"], advanced["selected_pair"])
        self.assertEqual(second["status"], "written")
        self.assertEqual(replay["status"], "duplicate")
        self.assertEqual(final["selected_pair"], "gpt-5.6-terra|xhigh")
        self.assertEqual(final["attempt_pair"], final["selected_pair"])


    def test_native_wikilinks_form_project_shared_and_cross_project_edges(self):
        self.record()
        category_page = self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Normal Script Update.md"
        shared_page = self.vault / "Skills" / "Model Routing" / "Normal Script Update.md"
        shared_index = self.vault / "Skills" / "Model Routing" / "index.md"
        switch_text = self.broad_page.read_text(encoding="utf-8")
        category_text = category_page.read_text(encoding="utf-8")
        shared_text = shared_page.read_text(encoding="utf-8")
        self.assertIn("[[Projects/ThisIsMyOregon/Model Switch|Model Switch]]", self.broad_index.read_text(encoding="utf-8"))
        self.assertIn("[[Projects/ThisIsMyOregon/Model Routing/Normal Script Update]]", switch_text)
        self.assertIn("[[Projects/ThisIsMyOregon/index]]", category_text)
        self.assertIn("[[Projects/ThisIsMyOregon/Model Switch]]", category_text)
        self.assertIn("[[Skills/Model Routing/Normal Script Update]]", category_text)
        self.assertIn("[[Projects/ThisIsMyOregon/Model Routing/Normal Script Update]]", shared_text)
        self.assertIn("[[Skills/Model Routing/Normal Script Update]]", shared_index.read_text(encoding="utf-8"))
        self.assertIn("| Task | Step / capability | Score |", category_text)
        self.assertNotIn("<!-- model-experience: ", switch_text)
        route = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Edit one bounded Python method.", vault=self.vault)["route_capsule"]
        self.assertEqual(route["mode"], "obsidian_native_wikilinks")
        self.assertEqual(route["current_source_document"], "Projects/ThisIsMyOregon/Model Routing/Normal Script Update.md")
        self.assertLessEqual(route["pages_read"], 2)

    def test_unreceipted_assignment_is_projected_but_never_moves_routing(self):
        scope = {
            "file_value": "src/example.py",
            "code_kind": "general",
            "operation": "verify",
            "modality": "text",
            "complexity_score": 18,
            "risk": "low",
            "ambiguity": "low",
            "task_summary": "Verify one generated structure record.",
            "step_kind": "verification",
            "capability_tags": ["local-test"],
        }
        before = module.recommend_model(self.project, "verification", "structure-record", vault=self.vault, **scope)
        observed = module.record_model_observation(
            self.project,
            "verification",
            "structure-record",
            "gpt-5.6-luna|medium",
            "pass",
            "none",
            observation_id="ending-structure-record-1",
            model_evidence="task_assignment",
            vault=self.vault,
            ending_attempt_number=1,
            outcome_reason="Independent file check passed.",
            verification_count=1,
            **scope,
        )
        replay = module.record_model_observation(
            self.project,
            "verification",
            "structure-record",
            "gpt-5.6-luna|medium",
            "pass",
            "none",
            observation_id="ending-structure-record-1",
            model_evidence="task_assignment",
            vault=self.vault,
            ending_attempt_number=1,
            outcome_reason="Independent file check passed.",
            verification_count=1,
            **scope,
        )
        after = module.recommend_model(self.project, "verification", "structure-record", vault=self.vault, **scope)
        category = self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Tests and Verification.md"
        envelope = json.loads(self.local_store.read_text(encoding="utf-8").splitlines()[0])
        record = envelope["record"]
        self.assertEqual(observed["status"], "written")
        self.assertEqual(replay["status"], "duplicate")
        self.assertFalse(observed["learning_eligible"])
        self.assertEqual(observed["model_record_link"], "[[Projects/ThisIsMyOregon/Model Routing/Tests and Verification]]")
        self.assertEqual(record["receipt_status"], "unavailable")
        self.assertEqual(record["model_evidence"], "task_assignment")
        self.assertFalse(record["learning_eligible"])
        self.assertEqual(record["routing_action"], "record_only_require_receipted_evidence_before_model_movement")
        self.assertEqual(after["attempt_pair"], before["attempt_pair"])
        self.assertIn("task_assignment / pass / 1 / first_attempt_pass", category.read_text(encoding="utf-8"))

    def test_migration_preserves_foreign_records_and_keeps_model_switch_compact(self):
        own = self.quality_record("gpt-5.6-terra|medium")
        own.update(model_experience_schema=1, project_key=module.project_change_memory._project_identity(self.project)["key"], task_type="code", module="example-module", file="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low")
        foreign = dict(own, project_key="foreign-project", record_id="foreign-record")
        self.broad_page.write_text("# Model Switch\n\n<!-- model-experience: " + json.dumps(own) + " -->\n<!-- model-experience: " + json.dumps(foreign) + " -->\n", encoding="utf-8")
        rebuilt = module.rebuild_model_switches(self.project, vault=self.vault)
        switch_text = self.broad_page.read_text(encoding="utf-8")
        category_text = (self.vault / "Projects" / "ThisIsMyOregon" / "Model Routing" / "Normal Script Update.md").read_text(encoding="utf-8")
        self.assertEqual(rebuilt["records"], 1)
        self.assertIn("foreign-record", switch_text)
        self.assertNotIn('"project_key":"' + module.project_change_memory._project_identity(self.project)["key"] + '"', switch_text)
        self.assertIn("example-module", category_text)

    def test_shared_category_reads_only_exact_fingerprint_project_page(self):
        other_root = self.home / "Documents" / "Muse" / "SVGDrawer"
        other_root.mkdir(parents=True)
        other_owner = "SVGDrawer"
        other_record = self.quality_record("gpt-5.6-terra|high")
        other_record.update(model_experience_schema=1, project_key=module.project_change_memory._project_identity(other_root)["key"], project_owner=other_owner, task_type="code", module="example-module", file="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low")
        profile = module._record_capability_profile(other_record)
        other_record["capability_fingerprint"] = profile["capability_fingerprint"]
        other_page = self.vault / "Projects" / other_owner / "Model Routing" / "Normal Script Update.md"
        other_page.parent.mkdir(parents=True)
        other_page.write_text(module._render_category_page(self.vault, other_owner, "normal-script-update", [other_record]), encoding="utf-8")
        module._refresh_shared_category(self.vault, "normal-script-update")
        recommendation = module.recommend_model(self.project, "code", "example-module", file_value="src/example.py", symbol="Example.run", code_kind="python", operation="edit", modality="text", complexity="easy", risk="low", ambiguity="low", task_summary="Exact cross-project routing.", vault=self.vault)
        self.assertEqual(recommendation["specificity"], "cross_project_symbol")
        self.assertEqual(recommendation["matched_records"], 1)
        self.assertIn("[[Projects/SVGDrawer/Model Routing/Normal Script Update]]", (self.vault / recommendation["route_capsule"]["shared_document"]).read_text(encoding="utf-8"))

    def test_compact_cli_excludes_graph_index_commands(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            module.parse_args(["graph-index-status", "--project-root", str(self.project)])



    def test_global_skills_uses_model_routing_records_pages(self):
        source_root = self.home / "Documents" / "AIProject" / "qin-codex-skills"
        source_root.mkdir(parents=True)
        (self.vault / "Skills").mkdir()
        recommendation = module.recommend_model(source_root, "code", "global-routing", code_kind="python", operation="edit", complexity_score=35, task_summary="Record global skills routing.", vault=self.vault)
        receipt = self.write_receipt(recommendation["attempt_pair"], self.root / "global-receipt.json")
        written = module.record_model_result(source_root, "code", "global-routing", receipt, "pass", "none", code_kind="python", operation="edit", complexity_score=35, task_summary="Record global skills routing.", vault=self.vault)
        category = self.vault / "Skills" / "Model Routing Records" / "Normal Script Update.md"
        self.assertEqual(written["status"], "written")
        self.assertTrue(category.exists())
        self.assertIn("[[Skills/Model Routing/Normal Script Update]]", category.read_text(encoding="utf-8"))
        routed = module.recommend_model(source_root, "code", "global-routing", code_kind="python", operation="edit", complexity_score=35, task_summary="Record global skills routing.", vault=self.vault)
        self.assertEqual(routed["obsidian_record_count"], 1)
        self.assertEqual(routed["route_capsule"]["current_source_document"], "Skills/Model Routing Records/Normal Script Update.md")


if __name__ == "__main__":
    unittest.main()
