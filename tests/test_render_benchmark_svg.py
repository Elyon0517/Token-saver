#!/usr/bin/env python3
import html
import importlib.util
import json
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_benchmark_svg.py"
MODULE_SPEC = importlib.util.spec_from_file_location("render_benchmark_svg", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class RenderBenchmarkSvgTests(unittest.TestCase):
    def public_document(self):
        tasks = []
        for index, tier in enumerate(module.benchmark_public_export.benchmark_suite_gate.TIERS, start=1):
            pair_count = module.benchmark_public_export.MINIMUM_PUBLIC_PAIR_COUNT
            direct_totals = {"steady_state_logical_tokens": 600003 * index, "steady_state_execution_elapsed_ms": 120003 * index, "first_result_elapsed_ms": 120003 * index, "total_wall_elapsed_ms": 126003 * index}
            global_totals = {"steady_state_logical_tokens": 300003 * index, "steady_state_execution_elapsed_ms": 60003 * index, "first_result_elapsed_ms": 60003 * index, "total_wall_elapsed_ms": 66003 * index}
            direct_medians = {"steady_state_logical_tokens": 100000.5 * index, "steady_state_execution_elapsed_ms": 20000.5 * index, "first_result_elapsed_ms": 20000.5 * index, "total_wall_elapsed_ms": 21000.5 * index}
            global_medians = {"steady_state_logical_tokens": 50000.5 * index, "steady_state_execution_elapsed_ms": 10000.5 * index, "first_result_elapsed_ms": 10000.5 * index, "total_wall_elapsed_ms": 11000.5 * index}
            savings = {"steady_state_logical_tokens": 50.123 + index, "steady_state_execution_elapsed_ms": 40.234 + index, "first_result_elapsed_ms": 40.234 + index, "total_wall_elapsed_ms": 45.345 + index}
            paired_wins = {"steady_state_logical_tokens": pair_count - 1, "steady_state_execution_elapsed_ms": pair_count, "first_result_elapsed_ms": pair_count, "total_wall_elapsed_ms": pair_count}
            metric_gate = {"aggregate_global_lower": True, "raw_global_median_lower": True, "minimum_paired_savings_percent": 0.0, "paired_savings_median_meets_threshold": True, "strict_majority_better": True, "strict_majority_required": True, "maximum_pair_regression_percent": 5.0, "regression_bound_required": False, "worst_pair_regression_within_limit": True, "worst_pair_savings_percent": -1.0, "status": "pass"}
            metric_gates = {metric: {**metric_gate, "strict_majority_better": paired_wins[metric] > pair_count / 2, "strict_majority_required": metric != "steady_state_logical_tokens"} for metric in module.GATED_METRIC_KEYS}
            metric_gates["steady_state_execution_elapsed_ms"].update({"worst_pair_regression_within_limit": True, "worst_pair_savings_percent": 1.0, "maximum_pair_regression_ms": module.benchmark_public_export.benchmark_suite_gate.MAXIMUM_PAIRED_TIME_REGRESSION_MS, "worst_pair_regression_ms": 0, "material_pair_regression_count": 0})
            tasks.append({"tier": tier, "label": module.benchmark_public_export.TASK_LABELS[tier], "status": "pass", "optimization_status": "pass", "failures": [], "performance_diagnostics": [], "pair_count": pair_count, "run_count": pair_count * 2, "direct_totals": direct_totals, "global_totals": global_totals, "direct_medians": direct_medians, "global_medians": global_medians, "paired_savings_percent_medians": savings, "paired_wins": paired_wins, "metric_gates": metric_gates})
        tier_repeat_counts = {tier: module.benchmark_public_export.MINIMUM_PUBLIC_PAIR_COUNT for tier in module.benchmark_public_export.benchmark_suite_gate.TIERS}
        expected_run_count = sum(tier_repeat_counts.values()) * 2
        configuration = {"config_hash_equal": True, "config_sha256": "b" * 64, "agents_sha256": {"direct": "c" * 64, "global": "d" * 64}, "runtime_context_hash_equal": True, "models_cache_sha256": "3" * 64, "memories_sha256": "4" * 64, "catalog_hash_equal": True, "catalog_schema_version": 1, "catalog_sha256": {"skills": "e" * 64, "plugins": "f" * 64, "marketplaces": "1" * 64, "visible": "2" * 64}, "catalog_file_counts": {"skills": 10, "plugins": 20, "marketplaces": 30, "marketplace_sources": 2}}
        cohort_metric_gates = {}
        for metric in module.GATED_METRIC_KEYS:
            direct_total = sum(task["direct_totals"][metric] for task in tasks)
            global_total = sum(task["global_totals"][metric] for task in tasks)
            cohort_metric_gates[metric] = {"direct_total": direct_total, "global_total": global_total, "aggregate_global_lower": True, "status": "pass"}
        return {"schema_version": module.benchmark_public_export.PUBLIC_SCHEMA_VERSION, "evidence_scope": "sanitized frozen real Direct versus Auto empirical cohort", "suite_id": "benchmark-suite-svg-test", "plan_sha256": "a" * 64, "overall_status": "pass", "all_correct": True, "expected_run_count": expected_run_count, "entry_pairs": module.benchmark_public_export.benchmark_suite_gate.ARM_ENTRY_PAIRS, "tier_repeat_counts": tier_repeat_counts, "rules": {"tokens": module.benchmark_public_export.TOKEN_RULE, "time": module.benchmark_public_export.TIME_RULE, "overall": module.benchmark_public_export.OVERALL_RULE, "minimum_pairs_per_tier": module.benchmark_public_export.MINIMUM_PUBLIC_PAIR_COUNT}, "configuration": configuration, "execution_integrity": {"complete_runs": expected_run_count, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "runtime_session_count": 18, "runtime_root_count": 12, "runtime_descendant_count": 6, "task_session_count": expected_run_count, "controller_tokens_excluded": 300, "multi_session_run_count": 6, "sol_entry_probe_count": 3, "sol_entry_probe_pass_count": 3}, "cohort_metric_gates": cohort_metric_gates, "tasks": tasks, "caveats": {"tokens": "Logical runtime tokens are not billing tokens.", "first_result": "Ending Real is excluded from first-result time.", "generalization": "Empirical cohort, not a universal guarantee."}}

    def scalar_values(self, value):
        if isinstance(value, dict):
            for child_value in value.values():
                yield from self.scalar_values(child_value)
        elif isinstance(value, list):
            for child_value in value:
                yield from self.scalar_values(child_value)
        else:
            yield value

    def test_desktop_and_mobile_are_accessible_deterministic_and_include_all_exported_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "public.json"
            desktop_path = root / "desktop.svg"
            mobile_path = root / "mobile.svg"
            second_desktop_path = root / "desktop-second.svg"
            second_mobile_path = root / "mobile-second.svg"
            document = self.public_document()
            input_path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            module.render_svgs(input_path, desktop_path, mobile_path)
            module.render_svgs(input_path, second_desktop_path, second_mobile_path)
            desktop_text = desktop_path.read_text(encoding="utf-8")
            mobile_text = mobile_path.read_text(encoding="utf-8")
            second_desktop_text = second_desktop_path.read_text(encoding="utf-8")
            second_mobile_text = second_mobile_path.read_text(encoding="utf-8")
            desktop_root = ElementTree.fromstring(desktop_text)
            mobile_root = ElementTree.fromstring(mobile_text)
            desktop_mode = stat.S_IMODE(desktop_path.stat().st_mode)
            mobile_mode = stat.S_IMODE(mobile_path.stat().st_mode)
        self.assertEqual(desktop_root.attrib["width"], "1200")
        self.assertEqual(desktop_root.attrib["height"], "760")
        self.assertEqual(mobile_root.attrib["width"], "720")
        self.assertEqual(mobile_root.attrib["height"], "1260")
        self.assertEqual(desktop_root.attrib["role"], "img")
        self.assertIsNotNone(desktop_root.find("{http://www.w3.org/2000/svg}title"))
        self.assertIsNotNone(desktop_root.find("{http://www.w3.org/2000/svg}desc"))
        self.assertIn(module.DIRECT_COLOR, desktop_text)
        self.assertIn(module.GLOBAL_COLOR, desktop_text)
        self.assertIn("Direct", desktop_text)
        self.assertIn("Auto", desktop_text)
        desktop_visible_text = " ".join("".join(element.itertext()) for element in desktop_root.findall(".//{http://www.w3.org/2000/svg}text"))
        mobile_visible_text = " ".join("".join(element.itertext()) for element in mobile_root.findall(".//{http://www.w3.org/2000/svg}text"))
        self.assertIn("12 complete · 18 sessions (12 roots) · 0 retry/fallback/repair", desktop_visible_text)
        self.assertIn("12 complete · 18 sessions (12 roots) · 0 retry/fallback/repair", mobile_visible_text)
        self.assertEqual(desktop_visible_text.count("CORRECT · 2 pairs · 4 runs"), 3)
        self.assertEqual(mobile_visible_text.count("CORRECT · 2 pairs · 4 runs"), 3)
        self.assertIn("12/12 correct · aggregate steady-state saved 50.0% tokens · 50.0% execution", desktop_visible_text)
        self.assertIn("12/12 correct · aggregate steady-state saved 50.0% tokens · 50.0% execution", mobile_visible_text)
        self.assertIn("wins 2/2", desktop_visible_text)
        self.assertIn("First-result is end-to-end diagnostic", mobile_visible_text)
        self.assertIn("STEADY-STATE EXECUTION", desktop_visible_text)
        self.assertIn("Ending excluded", mobile_visible_text)
        desktop_metadata = desktop_root.find("{http://www.w3.org/2000/svg}metadata")
        mobile_metadata = mobile_root.find("{http://www.w3.org/2000/svg}metadata")
        self.assertEqual(json.loads(desktop_metadata.text), document)
        self.assertEqual(json.loads(mobile_metadata.text), document)
        self.assertEqual(desktop_text, second_desktop_text)
        self.assertEqual(mobile_text, second_mobile_text)
        for value in self.scalar_values(document):
            encoded_value = html.escape(json.dumps(value, ensure_ascii=False)[1:-1] if isinstance(value, str) else json.dumps(value), quote=True)
            self.assertIn(encoded_value, desktop_text)
            self.assertIn(encoded_value, mobile_text)
        self.assertEqual(desktop_mode, 0o644)
        self.assertEqual(mobile_mode, 0o644)

    def test_valid_all_correct_strategy_failure_renders_fail_state_and_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "public.json"
            desktop_path = root / "desktop.svg"
            mobile_path = root / "mobile.svg"
            document = self.public_document()
            medium = document["tasks"][1]
            document["overall_status"] = "fail"
            medium["optimization_status"] = "fail"
            medium["performance_diagnostics"] = ["steady_state_aggregate_loss"]
            medium["global_totals"]["steady_state_execution_elapsed_ms"] = 480006
            medium["metric_gates"]["steady_state_execution_elapsed_ms"]["aggregate_global_lower"] = False
            medium["metric_gates"]["steady_state_execution_elapsed_ms"]["status"] = "fail"
            steady_time_cohort = document["cohort_metric_gates"]["steady_state_execution_elapsed_ms"]
            steady_time_cohort["global_total"] = steady_time_cohort["direct_total"]
            steady_time_cohort["aggregate_global_lower"] = False
            steady_time_cohort["status"] = "fail"
            input_path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            module.render_svgs(input_path, desktop_path, mobile_path)
            desktop_text = desktop_path.read_text(encoding="utf-8")
            mobile_text = mobile_path.read_text(encoding="utf-8")
        self.assertIn("Real A/B benchmark · FAIL", desktop_text)
        self.assertIn("CORRECT · TIER REGRESSION · 2 pairs · 4 runs", desktop_text)
        self.assertIn("Medium: steady-state", desktop_text)
        self.assertNotIn("steady state aggregate loss", " ".join("".join(element.itertext()) for element in ElementTree.fromstring(desktop_text).findall(".//{http://www.w3.org/2000/svg}text")))
        self.assertIn("Strategy gate FAIL", mobile_text)

    def test_strict_v5_public_contract_rejects_schema_gate_and_integrity_drift(self):
        cases = (("top_level_extra", "public_json_schema"), ("rules_missing_minimum", "public_rule_contract"), ("rules_wrong_minimum", "public_rule_contract"), ("rules_wrong_token", "public_rule_contract"), ("rules_wrong_time", "public_rule_contract"), ("rules_wrong_overall", "public_rule_contract"), ("tier_pair_count", "public_pair_count_contract"), ("task_pair_count", "public_task_contract"), ("expected_run_count", "public_run_count_contract"), ("integrity_missing_field", "public_execution_integrity"), ("integrity_incomplete", "public_execution_integrity"), ("integrity_retry", "public_execution_integrity"), ("metric_gate_legacy", "public_metric_gate_contract"), ("metric_gate_threshold", "public_metric_gate_contract"), ("metric_gate_threshold_status", "public_metric_gate_contract"), ("metric_gate_majority_status", "public_metric_gate_contract"), ("time_floor_missing", "public_metric_gate_contract"), ("time_floor_wrong", "public_metric_gate_contract"), ("time_tail_required", "public_metric_gate_contract"), ("time_material_invalid", "public_metric_gate_contract"), ("time_tail_inconsistent", "public_metric_gate_contract"), ("time_material_exceeds_losses", "public_metric_gate_contract"), ("time_wins_tie", "public_metric_gate_contract"), ("raw_time_loss", "public_metric_gate_contract"), ("total_token_loss", "public_metric_gate_contract"), ("configuration_extra", "public_configuration_contract"), ("catalog_hash_false", "public_configuration_contract"), ("catalog_hash_invalid", "public_configuration_contract"), ("catalog_count_invalid", "public_configuration_contract"), ("caveat_missing", "public_caveat_contract"))
        for case_name, expected_error in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                input_path = root / "public.json"
                desktop_path = root / "desktop.svg"
                mobile_path = root / "mobile.svg"
                document = self.public_document()
                if case_name == "top_level_extra":
                    document["unexpected"] = True
                elif case_name == "rules_missing_minimum":
                    document["rules"].pop("minimum_pairs_per_tier")
                elif case_name == "rules_wrong_minimum":
                    document["rules"]["minimum_pairs_per_tier"] = 5
                elif case_name == "rules_wrong_token":
                    document["rules"]["tokens"] = "weaker token rule"
                elif case_name == "rules_wrong_time":
                    document["rules"]["time"] = "weaker time rule"
                elif case_name == "rules_wrong_overall":
                    document["rules"]["overall"] = "suite aggregate"
                elif case_name == "tier_pair_count":
                    document["tier_repeat_counts"]["simple"] = 1
                elif case_name == "task_pair_count":
                    document["tasks"][0]["pair_count"] = 5
                elif case_name == "expected_run_count":
                    document["expected_run_count"] -= 1
                elif case_name == "integrity_missing_field":
                    document["execution_integrity"].pop("repair_count")
                elif case_name == "integrity_incomplete":
                    document["execution_integrity"]["complete_runs"] -= 1
                elif case_name == "integrity_retry":
                    document["execution_integrity"]["retry_count"] = 1
                elif case_name == "metric_gate_legacy":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"].pop("strict_majority_better")
                elif case_name == "metric_gate_threshold":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["minimum_paired_savings_percent"] = 4.0
                elif case_name == "metric_gate_threshold_status":
                    document["tasks"][1]["metric_gates"]["steady_state_execution_elapsed_ms"]["paired_savings_median_meets_threshold"] = False
                elif case_name == "metric_gate_majority_status":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["strict_majority_better"] = False
                elif case_name == "time_floor_missing":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"].pop("maximum_pair_regression_ms")
                elif case_name == "time_floor_wrong":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["maximum_pair_regression_ms"] = 3000
                elif case_name == "time_tail_required":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["regression_bound_required"] = True
                elif case_name == "time_material_invalid":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["material_pair_regression_count"] = -1
                elif case_name == "time_tail_inconsistent":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["worst_pair_regression_within_limit"] = False
                elif case_name == "time_material_exceeds_losses":
                    document["tasks"][0]["metric_gates"]["steady_state_execution_elapsed_ms"]["material_pair_regression_count"] = 2
                elif case_name == "token_majority_diagnostic":
                    document["tasks"][0]["metric_gates"]["logical_total_tokens"]["strict_majority_better"] = False
                elif case_name == "time_wins_tie":
                    document["tasks"][0]["paired_wins"]["steady_state_execution_elapsed_ms"] = 1
                elif case_name == "token_regression_limit":
                    document["tasks"][0]["metric_gates"]["logical_total_tokens"]["worst_pair_savings_percent"] = -6.0
                elif case_name == "raw_time_loss":
                    document["tasks"][1]["global_medians"]["steady_state_execution_elapsed_ms"] = document["tasks"][1]["direct_medians"]["steady_state_execution_elapsed_ms"]
                elif case_name == "total_token_loss":
                    document["tasks"][0]["global_totals"]["steady_state_logical_tokens"] = document["tasks"][0]["direct_totals"]["steady_state_logical_tokens"]
                elif case_name == "configuration_extra":
                    document["configuration"]["unexpected"] = True
                elif case_name == "catalog_hash_false":
                    document["configuration"]["catalog_hash_equal"] = False
                elif case_name == "catalog_hash_invalid":
                    document["configuration"]["catalog_sha256"]["visible"] = "invalid"
                elif case_name == "catalog_count_invalid":
                    document["configuration"]["catalog_file_counts"]["skills"] = -1
                elif case_name == "caveat_missing":
                    document["caveats"].pop("generalization")
                else:
                    self.fail(f"unhandled case: {case_name}")
                input_path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                with self.assertRaises(module.BenchmarkSvgError) as raised:
                    module.render_svgs(input_path, desktop_path, mobile_path)
                self.assertEqual(raised.exception.code, expected_error)
                self.assertFalse(desktop_path.exists())
                self.assertFalse(mobile_path.exists())

    def test_private_or_nonpassing_input_fails_before_svg_write(self):
        cases = [("privacy", "/private/raw/result.json"), ("status", "fail")]
        for case_name, changed_value in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                input_path = root / "public.json"
                desktop_path = root / "desktop.svg"
                mobile_path = root / "mobile.svg"
                document = self.public_document()
                if case_name == "privacy":
                    document["debug"] = changed_value
                else:
                    document["overall_status"] = changed_value
                input_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(module.BenchmarkSvgError):
                    module.render_svgs(input_path, desktop_path, mobile_path)
                desktop_exists = desktop_path.exists()
                mobile_exists = mobile_path.exists()
            self.assertFalse(desktop_exists)
            self.assertFalse(mobile_exists)


if __name__ == "__main__":
    unittest.main()
