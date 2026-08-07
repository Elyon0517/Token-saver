#!/usr/bin/env python3
"""Admit model delegation only after repeated steady-state Pareto wins."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkstemp


SCHEMA_VERSION = 2
DEFAULT_MINIMUM_PAIRED_SAMPLES = 6
DEFAULT_MINIMUM_SAVINGS_PERCENT = 0.0
DEFAULT_MAXIMUM_PAIR_REGRESSION_PERCENT = 5.0
DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[1] / "local" / "adaptive-routing" / "strategy_performance.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PAIR_PATTERN = re.compile(r"^[a-z0-9.-]+\|(low|medium|high|xhigh|max|ultra)$")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ARM_FIELDS = {"gate_status", "completion", "metrics_complete", "logical_total_tokens", "steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "route_selection_elapsed_ms", "calibration_attempt_count", "calibration_failure_elapsed_ms", "calibration_failure_logical_tokens", "first_result_elapsed_ms", "total_wall_elapsed_ms", "retry_count", "fallback_count", "repair_count", "unreceipted_descendant_count"}


class StrategyPerformanceError(ValueError):
    pass


def _load_benchmark_gate():
    gate_path = Path(__file__).with_name("benchmark_suite_gate.py")
    module_spec = importlib.util.spec_from_file_location("strategy_performance_benchmark_gate", gate_path)
    gate_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(gate_module)
    return gate_module


benchmark_suite_gate = _load_benchmark_gate()


def _atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _default_history():
    return {"schema_version": SCHEMA_VERSION, "updated_at": datetime.now(timezone.utc).isoformat(), "profiles": {}}


def load_history(path):
    if not path.exists():
        return _default_history()
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StrategyPerformanceError("strategy_history_invalid") from error
    if not isinstance(history, dict) or history.get("schema_version") != SCHEMA_VERSION or not isinstance(history.get("profiles"), dict):
        raise StrategyPerformanceError("strategy_history_invalid")
    return history


def _controlled_text(value, field, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise StrategyPerformanceError(f"{field}_invalid")
    return value


def profile_fields(args):
    return {"profile_fingerprint": _controlled_text(args.profile_fingerprint, "profile_fingerprint", SHA256_PATTERN), "entry_pair": _controlled_text(args.entry_pair, "entry_pair", PAIR_PATTERN), "config_cohort": _controlled_text(args.config_cohort, "config_cohort", SHA256_PATTERN), "sandbox_label": _controlled_text(args.sandbox_label, "sandbox_label", SLUG_PATTERN), "strategy_version": _controlled_text(args.strategy_version, "strategy_version", SLUG_PATTERN), "producer_contract_version": _controlled_text(args.producer_contract_version, "producer_contract_version", SLUG_PATTERN)}


def profile_key(fields):
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_arm(arm, field):
    if not isinstance(arm, dict) or set(arm) != ARM_FIELDS:
        raise StrategyPerformanceError(f"{field}_invalid")
    for count_field in ("logical_total_tokens", "steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "route_selection_elapsed_ms", "calibration_attempt_count", "calibration_failure_elapsed_ms", "calibration_failure_logical_tokens", "first_result_elapsed_ms", "total_wall_elapsed_ms", "retry_count", "fallback_count", "repair_count", "unreceipted_descendant_count"):
        if not isinstance(arm[count_field], int) or arm[count_field] < 0:
            raise StrategyPerformanceError(f"{field}_invalid")
    if arm["first_result_elapsed_ms"] != arm["steady_state_execution_elapsed_ms"] + arm["route_selection_elapsed_ms"]:
        raise StrategyPerformanceError(f"{field}_invalid")
    if arm["gate_status"] not in {"pass", "fail"} or arm["completion"] not in {"complete", "timeout", "incomplete"} or not isinstance(arm["metrics_complete"], bool):
        raise StrategyPerformanceError(f"{field}_invalid")
    return dict(arm)


def validate_sample(sample, workload_prompt_sha256):
    if not isinstance(sample, dict) or set(sample) != {"direct", "global"}:
        raise StrategyPerformanceError("sample_invalid")
    workload_hash = _controlled_text(workload_prompt_sha256, "workload_prompt_sha256", SHA256_PATTERN)
    direct = _validate_arm(sample["direct"], "direct")
    global_arm = _validate_arm(sample["global"], "global")
    comparable = all(arm["gate_status"] == "pass" and arm["completion"] == "complete" and arm["metrics_complete"] and arm["retry_count"] == 0 and arm["fallback_count"] == 0 and arm["repair_count"] == 0 and arm["unreceipted_descendant_count"] == 0 for arm in (direct, global_arm))
    token_savings_percent = (direct["steady_state_logical_tokens"] - global_arm["steady_state_logical_tokens"]) / direct["steady_state_logical_tokens"] * 100 if direct["steady_state_logical_tokens"] > 0 else None
    execution_savings_percent = (direct["steady_state_execution_elapsed_ms"] - global_arm["steady_state_execution_elapsed_ms"]) / direct["steady_state_execution_elapsed_ms"] * 100 if direct["steady_state_execution_elapsed_ms"] > 0 else None
    first_result_savings_percent = (direct["first_result_elapsed_ms"] - global_arm["first_result_elapsed_ms"]) / direct["first_result_elapsed_ms"] * 100 if direct["first_result_elapsed_ms"] > 0 else None
    total_wall_savings_percent = (direct["total_wall_elapsed_ms"] - global_arm["total_wall_elapsed_ms"]) / direct["total_wall_elapsed_ms"] * 100 if direct["total_wall_elapsed_ms"] > 0 else None
    strict_pareto_win = comparable and token_savings_percent is not None and execution_savings_percent is not None and min(token_savings_percent, execution_savings_percent) > 0
    return {"workload_prompt_sha256": workload_hash, "direct": direct, "global": global_arm, "comparable": comparable, "strict_pareto_win": strict_pareto_win, "token_savings_percent": token_savings_percent, "execution_savings_percent": execution_savings_percent, "first_result_savings_percent": first_result_savings_percent, "total_wall_savings_percent": total_wall_savings_percent, "recorded_at": datetime.now(timezone.utc).isoformat()}


def record_sample(args):
    fields = profile_fields(args)
    history = load_history(args.history)
    try:
        sample_payload = json.loads(args.sample.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StrategyPerformanceError("sample_invalid") from error
    sample = validate_sample(sample_payload, args.workload_prompt_sha256)
    key = profile_key(fields)
    record = history["profiles"].setdefault(key, {"profile": fields, "samples": []})
    if record.get("profile") != fields or not isinstance(record.get("samples"), list):
        raise StrategyPerformanceError("strategy_profile_invalid")
    record["samples"].append(sample)
    history["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(args.history, history)
    return recommend_mode(args, history=history)


def _cohort_summary(samples, minimum_paired_samples, minimum_savings_percent, maximum_pair_regression_percent=DEFAULT_MAXIMUM_PAIR_REGRESSION_PERCENT):
    comparable = [sample for sample in samples if sample.get("comparable")]
    strict_wins = [sample for sample in comparable if sample.get("strict_pareto_win")]
    enough_samples = len(comparable) >= minimum_paired_samples
    all_samples_comparable = bool(samples) and len(comparable) == len(samples)
    all_quality_metrics_complete = bool(samples) and all(all(arm.get("gate_status") == "pass" and arm.get("completion") == "complete" and arm.get("metrics_complete") is True for arm in (sample.get("direct", {}), sample.get("global", {}))) for sample in samples)
    correctness_failure_samples = sum(any(arm.get("gate_status") != "pass" for arm in (sample.get("direct", {}), sample.get("global", {}))) for sample in samples)
    token_wins = sum(sample["global"]["steady_state_logical_tokens"] < sample["direct"]["steady_state_logical_tokens"] for sample in comparable)
    steady_execution_faster_pairs = sum(sample["global"]["steady_state_execution_elapsed_ms"] < sample["direct"]["steady_state_execution_elapsed_ms"] for sample in comparable)
    first_result_faster_pairs = sum(sample["global"]["first_result_elapsed_ms"] < sample["direct"]["first_result_elapsed_ms"] for sample in comparable)
    total_wall_faster_pairs = sum(sample["global"]["total_wall_elapsed_ms"] < sample["direct"]["total_wall_elapsed_ms"] for sample in comparable)
    metric_names = ("steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "first_result_elapsed_ms", "total_wall_elapsed_ms")
    gated_metric_names = benchmark_suite_gate.GATED_METRICS
    metric_gates = {
        metric: benchmark_suite_gate.evaluate_paired_metric(
            [sample["direct"][metric] for sample in comparable],
            [sample["global"][metric] for sample in comparable],
            minimum_savings_percent=minimum_savings_percent,
            maximum_regression_percent=maximum_pair_regression_percent,
            require_strict_majority=metric != "steady_state_logical_tokens",
            maximum_absolute_regression=benchmark_suite_gate.MAXIMUM_PAIRED_TIME_REGRESSION_MS if metric in {"steady_state_execution_elapsed_ms", "first_result_elapsed_ms"} else None,
            require_regression_bound=False,
        )
        for metric in metric_names
    } if comparable else {}
    token_median = metric_gates.get("steady_state_logical_tokens", {}).get("paired_savings_percent_median")
    execution_median = metric_gates.get("steady_state_execution_elapsed_ms", {}).get("paired_savings_percent_median")
    first_result_median = metric_gates.get("first_result_elapsed_ms", {}).get("paired_savings_percent_median")
    total_wall_median = metric_gates.get("total_wall_elapsed_ms", {}).get("paired_savings_percent_median")
    direct_first_result_median = metric_gates.get("first_result_elapsed_ms", {}).get("direct_median")
    global_first_result_median = metric_gates.get("first_result_elapsed_ms", {}).get("global_median")
    direct_total_wall_median = metric_gates.get("total_wall_elapsed_ms", {}).get("direct_median")
    global_total_wall_median = metric_gates.get("total_wall_elapsed_ms", {}).get("global_median")
    every_pair_token_win = enough_samples and token_wins == len(comparable)
    token_majority_lower = enough_samples and metric_gates.get("steady_state_logical_tokens", {}).get("strict_majority_better") is True
    steady_execution_majority_faster = enough_samples and metric_gates.get("steady_state_execution_elapsed_ms", {}).get("strict_majority_better") is True
    first_result_majority_faster = enough_samples and metric_gates.get("first_result_elapsed_ms", {}).get("strict_majority_better") is True
    total_wall_majority_faster = enough_samples and metric_gates.get("total_wall_elapsed_ms", {}).get("strict_majority_better") is True
    raw_time_medians_pass = enough_samples and metric_gates.get("steady_state_execution_elapsed_ms", {}).get("raw_global_median_lower") is True
    savings_medians_pass = enough_samples and all(metric_gates.get(metric, {}).get("paired_savings_median_meets_threshold") is True for metric in gated_metric_names)
    aggregate_totals_pass = enough_samples and all(metric_gates.get(metric, {}).get("aggregate_global_lower") is True for metric in gated_metric_names)
    regression_bounds_pass = enough_samples and all(metric_gates.get(metric, {}).get("worst_pair_regression_within_limit") is True or metric_gates.get(metric, {}).get("regression_bound_required") is False for metric in gated_metric_names)
    metric_gates_pass = enough_samples and all(metric_gates.get(metric, {}).get("status") == "pass" for metric in gated_metric_names)
    admitted = enough_samples and all_samples_comparable and all_quality_metrics_complete and correctness_failure_samples == 0 and metric_gates_pass
    return {"paired_samples": len(samples), "comparable_samples": len(comparable), "strict_pareto_wins": len(strict_wins), "minimum_paired_samples": minimum_paired_samples, "minimum_savings_percent": minimum_savings_percent, "maximum_pair_regression_percent": maximum_pair_regression_percent, "maximum_pair_time_regression_ms": benchmark_suite_gate.MAXIMUM_PAIRED_TIME_REGRESSION_MS, "all_samples_comparable": all_samples_comparable, "all_quality_metrics_complete": all_quality_metrics_complete, "correctness_failure_samples": correctness_failure_samples, "token_wins": token_wins, "every_pair_token_win": every_pair_token_win, "token_majority_lower": token_majority_lower, "steady_execution_faster_pairs": steady_execution_faster_pairs, "steady_execution_majority_faster": steady_execution_majority_faster, "first_result_faster_pairs": first_result_faster_pairs, "total_wall_faster_pairs": total_wall_faster_pairs, "first_result_majority_faster": first_result_majority_faster, "total_wall_majority_faster": total_wall_majority_faster, "median_direct_first_result_elapsed_ms": direct_first_result_median, "median_global_first_result_elapsed_ms": global_first_result_median, "median_direct_total_wall_elapsed_ms": direct_total_wall_median, "median_global_total_wall_elapsed_ms": global_total_wall_median, "median_token_savings_percent": token_median, "median_execution_savings_percent": execution_median, "median_first_result_savings_percent": first_result_median, "median_total_wall_savings_percent": total_wall_median, "raw_time_medians_pass": raw_time_medians_pass, "savings_medians_pass": savings_medians_pass, "aggregate_totals_pass": aggregate_totals_pass, "regression_bounds_pass": regression_bounds_pass, "metric_gates": metric_gates, "admitted": admitted}


def recommend_mode(args, history=None):
    fields = profile_fields(args)
    history = history if history is not None else load_history(args.history)
    workload_hash = _controlled_text(args.workload_prompt_sha256, "workload_prompt_sha256", SHA256_PATTERN)
    maximum_pair_regression_percent = getattr(args, "maximum_pair_regression_percent", DEFAULT_MAXIMUM_PAIR_REGRESSION_PERCENT)
    if not isinstance(args.minimum_paired_samples, int) or args.minimum_paired_samples < DEFAULT_MINIMUM_PAIRED_SAMPLES or not isinstance(args.minimum_savings_percent, float) or args.minimum_savings_percent < 0 or not isinstance(maximum_pair_regression_percent, float) or maximum_pair_regression_percent <= 0:
        raise StrategyPerformanceError("admission_threshold_invalid")
    record = history["profiles"].get(profile_key(fields))
    samples = [sample for sample in record.get("samples", []) if sample.get("workload_prompt_sha256") == workload_hash] if isinstance(record, dict) else []
    summary = _cohort_summary(samples, args.minimum_paired_samples, args.minimum_savings_percent, maximum_pair_regression_percent)
    execution_mode = "delegated_adaptive" if summary["admitted"] else "inline_entry"
    reason = "repeated_steady_state_token_and_execution_win" if summary["admitted"] else "delegation_not_proven_steady_state_faster_and_smaller"
    return {"schema_version": SCHEMA_VERSION, "execution_mode": execution_mode, "reason": reason, "profile_key": profile_key(fields), "workload_prompt_sha256": workload_hash, **summary}


def add_common_arguments(parser):
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--profile-fingerprint", required=True)
    parser.add_argument("--entry-pair", required=True)
    parser.add_argument("--config-cohort", required=True)
    parser.add_argument("--sandbox-label", required=True)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--producer-contract-version", required=True)
    parser.add_argument("--workload-prompt-sha256", required=True)
    parser.add_argument("--minimum-paired-samples", type=int, default=DEFAULT_MINIMUM_PAIRED_SAMPLES)
    parser.add_argument("--minimum-savings-percent", type=float, default=DEFAULT_MINIMUM_SAVINGS_PERCENT)
    parser.add_argument("--maximum-pair-regression-percent", type=float, default=DEFAULT_MAXIMUM_PAIR_REGRESSION_PERCENT)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Require repeated steady-state token and execution wins before delegating a task.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    recommend_parser = subparsers.add_parser("recommend")
    add_common_arguments(recommend_parser)
    record_parser = subparsers.add_parser("record")
    add_common_arguments(record_parser)
    record_parser.add_argument("--sample", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        result = record_sample(args) if args.command == "record" else recommend_mode(args)
    except StrategyPerformanceError as error:
        result = {"schema_version": SCHEMA_VERSION, "execution_mode": "inline_entry", "reason": str(error), "admitted": False}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("execution_mode") in {"inline_entry", "delegated_adaptive"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
