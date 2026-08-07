import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "strategy_performance.py"
MODULE_SPEC = importlib.util.spec_from_file_location("strategy_performance", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


def arm(tokens, first_result_ms, total_wall_ms, gate_status="pass", steady_tokens=None, steady_execution_ms=None):
    steady_tokens = tokens if steady_tokens is None else steady_tokens
    steady_execution_ms = first_result_ms if steady_execution_ms is None else steady_execution_ms
    return {"gate_status": gate_status, "completion": "complete", "metrics_complete": True, "logical_total_tokens": tokens, "steady_state_logical_tokens": steady_tokens, "steady_state_execution_elapsed_ms": steady_execution_ms, "route_selection_elapsed_ms": first_result_ms - steady_execution_ms, "calibration_attempt_count": 0, "calibration_failure_elapsed_ms": 0, "calibration_failure_logical_tokens": 0, "first_result_elapsed_ms": first_result_ms, "total_wall_elapsed_ms": total_wall_ms, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "unreceipted_descendant_count": 0}


class StrategyPerformanceTests(unittest.TestCase):
    def arguments(self, history, workload_hash="a" * 64):
        return SimpleNamespace(history=history, profile_fingerprint="b" * 64, entry_pair="gpt-5.6-sol|ultra", config_cohort="c" * 64, sandbox_label="danger-full-access", strategy_version="inline-v1", producer_contract_version="producer-v1", workload_prompt_sha256=workload_hash, minimum_paired_samples=6, minimum_savings_percent=5.0)

    def write_sample(self, path, direct=None, global_arm=None):
        path.write_text(json.dumps({"direct": direct or arm(100, 100, 120), "global": global_arm or arm(70, 70, 80)}), encoding="utf-8")

    def test_missing_history_fails_closed_to_inline(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            recommendation = module.recommend_mode(self.arguments(Path(temporary_directory) / "missing.json"))
            self.assertEqual(recommendation["execution_mode"], "inline_entry")
            self.assertFalse(recommendation["admitted"])

    def test_six_repeated_pareto_wins_admit_delegation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path)
                args.sample = sample_path
                recommendation = module.record_sample(args)
            self.assertEqual(recommendation["execution_mode"], "delegated_adaptive")
            self.assertTrue(recommendation["admitted"])
            self.assertEqual(recommendation["strict_pareto_wins"], 6)

    def test_one_slower_time_outlier_is_admitted_when_medians_and_majorities_pass(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 104, 124) if index == 5 else arm(70, 70, 80))
                args.sample = sample_path
                recommendation = module.record_sample(args)
            self.assertEqual(recommendation["execution_mode"], "delegated_adaptive")
            self.assertTrue(recommendation["admitted"])
            self.assertEqual(recommendation["strict_pareto_wins"], 5)
            self.assertEqual(recommendation["first_result_faster_pairs"], 5)
            self.assertEqual(recommendation["total_wall_faster_pairs"], 5)
            self.assertTrue(recommendation["raw_time_medians_pass"])
            self.assertTrue(recommendation["savings_medians_pass"])

    def test_short_task_time_jitter_under_two_seconds_does_not_block_admission(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(
                    sample_path,
                    direct=arm(100, 15000, 15000),
                    global_arm=arm(70, 16074, 16074) if index == 5 else arm(70, 8000, 8000),
                )
                args.sample = sample_path
                recommendation = module.record_sample(args)
        time_gate = recommendation["metric_gates"]["first_result_elapsed_ms"]
        self.assertTrue(recommendation["admitted"])
        self.assertEqual(recommendation["maximum_pair_time_regression_ms"], 2000)
        self.assertEqual(time_gate["worst_pair_regression_ms"], 1074)
        self.assertEqual(time_gate["material_pair_regression_count"], 0)

    def test_material_time_regression_over_two_seconds_is_diagnostic_when_cohort_passes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(
                    sample_path,
                    direct=arm(100, 15000, 15000),
                    global_arm=arm(70, 18001, 18001) if index == 5 else arm(70, 8000, 8000),
                )
                args.sample = sample_path
                recommendation = module.record_sample(args)
        time_gate = recommendation["metric_gates"]["first_result_elapsed_ms"]
        self.assertTrue(recommendation["admitted"])
        self.assertEqual(time_gate["material_pair_regression_count"], 1)
        self.assertFalse(time_gate["worst_pair_regression_within_limit"])
        self.assertFalse(time_gate["regression_bound_required"])

    def test_ending_wall_time_is_diagnostic_and_does_not_block_admission(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 70, 1200))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertTrue(recommendation["admitted"])
        self.assertEqual(recommendation["strict_pareto_wins"], 6)
        self.assertTrue(recommendation["first_result_majority_faster"])
        self.assertFalse(recommendation["total_wall_majority_faster"])
        self.assertEqual(recommendation["metric_gates"]["total_wall_elapsed_ms"]["status"], "fail")

    def test_time_ties_fail_raw_median_and_majority_gates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 100, 120))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertFalse(recommendation["admitted"])
        self.assertFalse(recommendation["raw_time_medians_pass"])
        self.assertFalse(recommendation["first_result_majority_faster"])
        self.assertFalse(recommendation["total_wall_majority_faster"])

    def test_exactly_half_faster_pairs_fail_strict_majority_even_when_medians_pass(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 70, 80) if index < 3 else arm(70, 110, 130))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertFalse(recommendation["admitted"])
        self.assertTrue(recommendation["raw_time_medians_pass"])
        self.assertTrue(recommendation["savings_medians_pass"])
        self.assertEqual(recommendation["first_result_faster_pairs"], 3)
        self.assertEqual(recommendation["total_wall_faster_pairs"], 3)
        self.assertFalse(recommendation["first_result_majority_faster"])
        self.assertFalse(recommendation["total_wall_majority_faster"])

    def test_time_savings_median_below_threshold_fails_despite_every_pair_faster(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 96, 115))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertFalse(recommendation["admitted"])
        self.assertTrue(recommendation["raw_time_medians_pass"])
        self.assertTrue(recommendation["first_result_majority_faster"])
        self.assertTrue(recommendation["total_wall_majority_faster"])
        self.assertFalse(recommendation["savings_medians_pass"])

    def test_one_small_token_outlier_is_admitted_by_cohort_gate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(100, 70, 80) if index == 5 else arm(70, 70, 80))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertTrue(recommendation["admitted"])
        self.assertEqual(recommendation["token_wins"], 5)
        self.assertFalse(recommendation["every_pair_token_win"])
        self.assertTrue(recommendation["token_majority_lower"])
        self.assertTrue(recommendation["aggregate_totals_pass"])
        self.assertTrue(recommendation["regression_bounds_pass"])

    def test_large_token_outlier_is_diagnostic_when_cohort_still_wins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(120, 70, 80) if index == 5 else arm(70, 70, 80))
                args.sample = sample_path
                recommendation = module.record_sample(args)
        self.assertTrue(recommendation["admitted"])
        self.assertTrue(recommendation["regression_bounds_pass"])
        self.assertTrue(recommendation["metric_gates"]["steady_state_logical_tokens"]["status"] == "pass")
        self.assertLess(recommendation["metric_gates"]["steady_state_logical_tokens"]["worst_pair_savings_percent"], 0)

    def test_workload_hashes_do_not_share_admission(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(6):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path)
                args.sample = sample_path
                module.record_sample(args)
            other = self.arguments(args.history, "d" * 64)
            recommendation = module.recommend_mode(other)
            self.assertEqual(recommendation["execution_mode"], "inline_entry")

    def test_failed_gate_is_preserved_and_blocks_admission(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = self.arguments(root / "history.json")
            for index in range(7):
                sample_path = root / f"sample-{index}.json"
                self.write_sample(sample_path, global_arm=arm(70, 70, 80, gate_status="fail") if index == 2 else arm(70, 70, 80))
                args.sample = sample_path
                recommendation = module.record_sample(args)
            self.assertEqual(recommendation["execution_mode"], "inline_entry")
            self.assertEqual(recommendation["comparable_samples"], 6)
            self.assertEqual(recommendation["correctness_failure_samples"], 1)
            self.assertFalse(recommendation["all_quality_metrics_complete"])
            history = module.load_history(args.history)
            profile = next(iter(history["profiles"].values()))
            self.assertEqual(len(profile["samples"]), 7)

    def test_minimum_pair_configuration_cannot_weaken_six_pair_floor(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            args = self.arguments(Path(temporary_directory) / "history.json")
            args.minimum_paired_samples = 5
            with self.assertRaisesRegex(module.StrategyPerformanceError, "admission_threshold_invalid"):
                module.recommend_mode(args)


if __name__ == "__main__":
    unittest.main()
