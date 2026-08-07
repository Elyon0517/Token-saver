#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parents[1] / "assets" / "graduated-route-fixtures.json"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_module("validate_graduated_routes")
dispatcher = load_module("task_route_dispatcher")


class GraduatedRouteLifecycleTests(unittest.TestCase):
    def test_fixture_uses_result_first_timing_and_schema_two_phases(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for scenario in fixture["scenarios"]:
            self.assertEqual(scenario["timing_evidence"], "wall_clock_to_first_result_excluding_ending")
        plan = fixture["admitted_dispatcher_template"]["dispatcher_plan"]
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual({node["phase"] for node in plan["nodes"]}, {"result", "ending"})
        self.assertNotIn("mini_verify_node", plan)
        implementation = next(node for node in plan["nodes"] if node["id"] == "implementation")
        self.assertEqual(implementation["candidate_ladder"], dispatcher.normal_adaptive_pair_texts())
        self.assertEqual(len(implementation["candidate_ladder"]), sum(len(efforts) for efforts in dispatcher.ACTIVE_MODEL_EFFORTS.values()))
        self.assertEqual(implementation["hard_floor"], dispatcher.MODEL_ROLE_PAIRS["floor"])

    def test_run_plan_release_then_ending_dispatches_the_producer_verifier(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        template = fixture["admitted_dispatcher_template"]["dispatcher_plan"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_dir = root / "work" / "cache" / "route"
            entry_model, entry_effort = dispatcher.routing_history_module.parse_pair(dispatcher.MODEL_ROLE_PAIRS["floor"])
            plan = validator.materialize_dispatcher_plan(template, cache_dir, entry_model, entry_effort)
            implementation = next(node for node in plan["nodes"] if node["id"] == "implementation")
            self.assertEqual(implementation["routing_recommendation"]["selection_basis"], "dual_model_history")
            locked_proof = implementation["routing_recommendation"]
            current_recommendation = {"selected_pair": locked_proof["selected_pair"], "trial": locked_proof["trial"]}
            calls = []

            def fake_run_node(node, node_cache_dir, completed, state_db, workdir, codex_bin="codex", skills_root=None):
                calls.append(node["id"])
                result_path = node_cache_dir / f"{node['id']}-result.md"
                receipt_path = node_cache_dir / f"{node['id']}-receipt.json"
                marker = "ENDING_TASK=PASS\n" if node["phase"] == "ending" else "RESULT=approved\n"
                result_path.write_text(marker, encoding="utf-8")
                receipt_path.write_text(json.dumps({"status": "pass", "thread_id": node["id"]}), encoding="utf-8")
                return {"id": node["id"], "phase": node["phase"], "skill": node["skill"], "model": node["model"], "effort": node["effort"], "status": "pass", "receipt_path": str(receipt_path), "result_path": str(result_path), "worker_identity": node["id"]}

            with patch.object(dispatcher.routing_history_module, "recommend_route", side_effect=AssertionError("legacy local recommendation used")), patch.object(dispatcher, "_obsidian_recommendation_and_proof", return_value=(current_recommendation, locked_proof)), patch.object(dispatcher, "run_node", side_effect=fake_run_node), patch.object(dispatcher, "_run_record", return_value={"status": "recorded"}):
                manifest = dispatcher.run_plan(plan, entry_model, entry_effort, root)
            self.assertEqual(calls, ["design", "implementation"])
            self.assertEqual(manifest["ending_nodes_pending"], ["ending-real"])
            handoff_path = Path(manifest["ending_handoff_path"])
            with patch.object(dispatcher, "run_node", side_effect=AssertionError("Ending ran before release")):
                unreleased = dispatcher.run_ending_handoff(handoff_path)
            self.assertEqual(unreleased["status"], "fail")
            self.assertTrue(any("not released" in failure for failure in unreleased["failures"]))

            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            release = dispatcher._release_main_result(handoff)
            self.assertEqual(release["status"], "pass")
            with patch.object(dispatcher, "run_node", side_effect=fake_run_node), patch.object(dispatcher, "_run_record", return_value={"status": "recorded"}):
                ending = dispatcher.run_ending_handoff(handoff_path)
            self.assertEqual(ending["status"], "pass")
            self.assertEqual(ending["routing_learning"], {"status": "recorded"})
            self.assertEqual(calls[:2], ["design", "implementation"])
            self.assertEqual(calls[2:], ["ending-real"])


if __name__ == "__main__":
    unittest.main()
