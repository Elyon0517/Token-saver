#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_effort.py"
SPEC = importlib.util.spec_from_file_location("session_effort", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SessionEffortTests(unittest.TestCase):
    def write_session(self, root, session_id, project):
        session_path = root / "2026" / "08" / "03" / f"rollout-{session_id}.jsonl"
        session_path.parent.mkdir(parents=True)
        events = [
            {"type": "session_meta", "payload": {"session_id": session_id, "cwd": str(project)}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "max"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "My request for Codex: implement the SVG corner trace."}},
            {"type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-5.6-luna", "effort": "max"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "My request for Codex: still wrong, fix the SVG corner line."}},
        ]
        session_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def test_repeated_failure_escalates_and_records_user_effort(self):
        session_id = "019fc8e5-87da-7082-90b9-6d505404d229"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            self.write_session(root / "sessions", session_id, project)
            store = root / "events.jsonl"
            prompt = "Again, fix the SVG corner line."
            summary = module.assess_session(prompt, project, project_key="demo", task_type="code", module="svg-repair", operation="fix", modality="mixed", complexity_score=65, task_summary=prompt, session_id=session_id, sessions_root=root / "sessions", local_store=store)
            self.assertTrue(summary["failure_recorded"])
            self.assertEqual(summary["state"], "repeated_failure")
            self.assertEqual(summary["last_model_pair"], "gpt-5.6-luna|max")
            self.assertEqual(summary["route_class"], "core_solving_short_difficult")
            self.assertEqual(summary["preferred_solving_pair"], "gpt-5.6-terra|low")
            self.assertEqual(module.next_escalation_pair(summary["last_model_pair"], ["gpt-5.6-luna|max", "gpt-5.6-terra|max", "gpt-5.6-sol|max", "gpt-5.6-sol|ultra"]), "gpt-5.6-terra|max")
            written = module.record_session_effort(summary, project_key="demo", task_type="code", module="svg-repair", complexity_score=65, complexity_band="complex", selected_pair="gpt-5.6-terra|max", requested_pair="gpt-5.6-luna|max", local_store=store)
            self.assertEqual(written["status"], "written")
            next_summary = module.assess_session("Still wrong, repair the SVG corner line again.", project, project_key="demo", task_type="code", module="svg-repair", operation="fix", modality="mixed", complexity_score=65, task_summary="Still wrong, repair the SVG corner line again.", session_id=session_id, sessions_root=root / "sessions", local_store=store)
            self.assertEqual(next_summary["last_model_pair"], "gpt-5.6-terra|max")

    def test_new_topic_does_not_inherit_failure(self):
        session_id = "019fc8e5-87da-7082-90b9-6d505404d229"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            self.write_session(root / "sessions", session_id, project)
            summary = module.assess_session("Implement the database migration for invoices.", project, project_key="demo", task_type="code", module="billing", session_id=session_id, sessions_root=root / "sessions", local_store=root / "events.jsonl")
        self.assertFalse(summary["failure_recorded"])
        self.assertEqual(summary["state"], "solved_or_new_topic")
        self.assertEqual(summary["same_task_turns"], 0)

    def test_session_and_explicit_task_scope_are_hashed_and_isolated(self):
        first_session = module.session_key("019fc8e5-87da-7082-90b9-6d505404d229")
        second_session = module.session_key("019fc8e5-87da-7082-90b9-6d505404d230")
        first_task = module.task_scope_key("demo", "code", "pipeline", "step-one")
        second_task = module.task_scope_key("demo", "code", "pipeline", "step-two")
        self.assertNotEqual(first_session, second_session)
        self.assertNotEqual(first_task, second_task)
        self.assertTrue(module.scope_matches({"codex_session_key": first_session, "task_scope_key": first_task}, session_key_value=first_session, task_scope=first_task))
        related = module.scope_relation({"codex_session_key": first_session, "task_scope_key": first_task}, session_key_value=second_session, task_scope=first_task)
        self.assertTrue(related["matched"])
        self.assertEqual(related["reason"], "related_task_scope")
        self.assertFalse(module.scope_matches({"codex_session_key": first_session, "task_scope_key": first_task}, session_key_value=first_session, task_scope=second_task))
        self.assertFalse(module.scope_matches({"codex_session_key": first_session, "task_scope_key": first_task}, session_key_value=second_session, task_scope=second_task))
        self.assertFalse(module.scope_matches({"session_key": "", "task_scope_key": ""}, session_key_value=first_session))

    def test_related_task_group_crosses_sessions_only_with_explicit_group_evidence(self):
        first_session = module.session_key("019fc8e5-87da-7082-90b9-6d505404d229")
        second_session = module.session_key("019fc8e5-87da-7082-90b9-6d505404d230")
        group = module.task_group_key("demo", "shared-route", "related-step")
        relation = module.scope_relation({"codex_session_key": first_session, "task_name": "related-step", "task_group": "shared-route", "task_group_key": group}, session_key_value=second_session, task_group_key_value=group)
        self.assertTrue(relation["matched"])
        self.assertEqual(relation["reason"], "related_task_group")
        isolated = module.scope_relation({"codex_session_key": first_session, "task_group_key": module.task_group_key("demo", "other-route", "other-step")}, session_key_value=second_session, task_group_key_value=group)
        self.assertFalse(isolated["matched"])
        self.assertEqual(isolated["reason"], "unrelated_session")

    def test_escalation_stops_at_sol_ultra(self):
        pairs = ["gpt-5.6-luna|max", "gpt-5.6-terra|max", "gpt-5.6-sol|max", "gpt-5.6-sol|ultra"]
        self.assertEqual(module.next_escalation_pair("gpt-5.6-luna|max", pairs), "gpt-5.6-terra|max")
        self.assertEqual(module.next_escalation_pair("gpt-5.6-terra|max", pairs), "gpt-5.6-sol|max")
        self.assertEqual(module.next_escalation_pair("gpt-5.6-sol|max", pairs), "gpt-5.6-sol|ultra")
        self.assertIsNone(module.next_escalation_pair("gpt-5.6-sol|ultra", pairs))

    def test_task_class_selects_cheaper_image_and_max_long_solving_routes(self):
        image = module.classify_task("Read the image and compare the sleeve shape.", operation="inspect", modality="image", complexity_score=20)
        medium_task = module.classify_task("Read the design, compare all corners, and fix the SVG.", operation="fix", modality="mixed", complexity_score=55)
        long_task = module.classify_task("Trace all outlines first, then inspect every intersection, update multiple files, run tests, and export the final result.", operation="fix", modality="mixed", complexity_score=65)
        frontier_task = module.classify_task("Comprehensively read and understand massive information from many sources, synthesize tradeoffs, choose an architecture, and write the final plan.", operation="analyze", modality="text", complexity_score=70)
        explicit_entry = module.classify_task("Use gpt-5.6-luna|max as the entry model, then fix this difficult SVG.", operation="fix", modality="mixed", complexity_score=65)
        generic_code = module.classify_task("Implement the database migration and run tests.", task_type="code", operation="implement", modality="text", complexity_score=55)
        simple_prose = module.classify_task("Write a short email confirming the meeting.", task_type="general", operation="write", modality="text", complexity_score=10)
        self.assertEqual(image["solving_surface"], "image_inspection")
        self.assertEqual(image["preferred_solving_pair"], "gpt-5.6-luna|low")
        self.assertEqual(medium_task["estimated_effort"], "medium")
        self.assertEqual(medium_task["preferred_solving_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(long_task["task_length"], "long")
        self.assertEqual(long_task["preferred_solving_pair"], "gpt-5.6-terra|max")
        self.assertEqual(frontier_task["model_family"], "gpt-5.6-sol")
        self.assertEqual(frontier_task["estimated_effort"], "max")
        self.assertEqual(frontier_task["preferred_solving_pair"], "gpt-5.6-sol|max")
        self.assertEqual(explicit_entry["explicit_route_hint"], "gpt-5.6-luna|max")
        self.assertEqual(explicit_entry["preferred_solving_pair"], "gpt-5.6-terra|low")
        self.assertEqual(generic_code["preferred_solving_pair"], "gpt-5.6-terra|medium")
        self.assertEqual(simple_prose["preferred_solving_pair"], "gpt-5.6-luna|low")

    def test_solving_route_keeps_effort_class_when_model_strengthens(self):
        pairs = ["gpt-5.6-luna|low", "gpt-5.6-luna|max", "gpt-5.6-terra|low", "gpt-5.6-terra|max", "gpt-5.6-sol|low", "gpt-5.6-sol|max"]
        short_core = {"preferred_solving_pair": "gpt-5.6-terra|low", "route_reason": "short_difficult_core_solving_light_route", "last_model_source": "context"}
        long_core = {"preferred_solving_pair": "gpt-5.6-terra|max", "route_reason": "long_core_solving_max_route", "last_model_source": "context"}
        image = {"preferred_solving_pair": "gpt-5.6-luna|low", "route_reason": "cheaper_image_inspection_route", "last_model_source": "context"}
        self.assertEqual(module.solve_route_pair(short_core, "gpt-5.6-luna|max", pairs)["pair"], "gpt-5.6-terra|low")
        self.assertEqual(module.solve_route_pair(short_core, "gpt-5.6-terra|low", pairs)["pair"], "gpt-5.6-sol|low")
        self.assertEqual(module.solve_route_pair(long_core, "gpt-5.6-luna|max", pairs)["pair"], "gpt-5.6-terra|max")
        self.assertEqual(module.solve_route_pair(image, "gpt-5.6-luna|max", pairs)["pair"], "gpt-5.6-luna|low")
        self.assertEqual(module.solve_route_pair(short_core, "gpt-5.6-luna|max", ["gpt-5.6-luna|max"])["pair"], "gpt-5.6-terra|low")


if __name__ == "__main__":
    unittest.main()
