#!/usr/bin/env python3
"""Render deterministic desktop and mobile SVGs from sanitized benchmark JSON."""

import argparse
import html
import importlib.util
import json
import os
from pathlib import Path
from tempfile import mkstemp


DIRECT_COLOR = "#3b82f6"
GLOBAL_COLOR = "#22c55e"
FAIL_COLOR = "#fb923c"
BACKGROUND_COLOR = "#071526"
PANEL_COLOR = "#101c31"
TEXT_COLOR = "#f8fafc"
MUTED_COLOR = "#a7b4c7"
BORDER_COLOR = "#334155"
EVIDENCE_SCOPE = "sanitized frozen real Direct versus Auto empirical cohort"
PUBLIC_KEYS = frozenset({"schema_version", "evidence_scope", "suite_id", "plan_sha256", "overall_status", "all_correct", "expected_run_count", "entry_pairs", "tier_repeat_counts", "rules", "configuration", "execution_integrity", "cohort_metric_gates", "tasks", "caveats"})
RULE_KEYS = frozenset({"tokens", "time", "overall", "minimum_pairs_per_tier"})
CONFIGURATION_KEYS = frozenset({"config_hash_equal", "config_sha256", "agents_sha256", "runtime_context_hash_equal", "models_cache_sha256", "memories_sha256", "catalog_hash_equal", "catalog_schema_version", "catalog_sha256", "catalog_file_counts"})
CATALOG_SHA_KEYS = frozenset({"skills", "plugins", "marketplaces", "visible"})
CATALOG_COUNT_KEYS = frozenset({"skills", "plugins", "marketplaces", "marketplace_sources"})
INTEGRITY_KEYS = frozenset({"complete_runs", "retry_count", "fallback_count", "repair_count", "runtime_session_count", "runtime_root_count", "runtime_descendant_count", "task_session_count", "controller_tokens_excluded", "multi_session_run_count", "sol_entry_probe_count", "sol_entry_probe_pass_count"})
TASK_KEYS = frozenset({"tier", "label", "status", "optimization_status", "failures", "performance_diagnostics", "pair_count", "run_count", "direct_totals", "global_totals", "direct_medians", "global_medians", "paired_savings_percent_medians", "paired_wins", "metric_gates"})
METRIC_KEYS = frozenset({"steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "first_result_elapsed_ms", "total_wall_elapsed_ms"})
METRIC_GATE_KEYS = frozenset({"aggregate_global_lower", "raw_global_median_lower", "minimum_paired_savings_percent", "paired_savings_median_meets_threshold", "strict_majority_better", "strict_majority_required", "maximum_pair_regression_percent", "regression_bound_required", "worst_pair_regression_within_limit", "worst_pair_savings_percent", "status"})
TIME_METRIC_GATE_KEYS = METRIC_GATE_KEYS | frozenset({"maximum_pair_regression_ms", "worst_pair_regression_ms", "material_pair_regression_count"})
STRATEGY_FAILURES = frozenset({"token_aggregate_loss", "token_raw_median_loss", "token_savings_threshold_loss", "steady_state_aggregate_loss", "steady_state_raw_median_loss", "steady_state_savings_threshold_loss", "steady_state_majority_loss", "steady_state_tolerance_loss"})
COHORT_METRIC_GATE_KEYS = frozenset({"direct_total", "global_total", "aggregate_global_lower", "status"})


class BenchmarkSvgError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_export_module():
    export_path = Path(__file__).with_name("benchmark_public_export.py")
    module_spec = importlib.util.spec_from_file_location("render_benchmark_public_export", export_path)
    export_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(export_module)
    return export_module


benchmark_public_export = load_export_module()
GATED_METRIC_KEYS = frozenset(benchmark_public_export.benchmark_suite_gate.GATED_METRICS)


def has_exact_keys(value, required_keys):
    return isinstance(value, dict) and set(value) == set(required_keys)


def is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def load_public_json(path):
    try:
        document = benchmark_public_export.benchmark_suite_gate.strict_json_loads(path.read_bytes())
        benchmark_public_export.validate_public_privacy(document, set())
    except benchmark_public_export.PublicExportError:
        raise BenchmarkSvgError("public_json_privacy_failure")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise BenchmarkSvgError("public_json_invalid")
    if not has_exact_keys(document, PUBLIC_KEYS):
        raise BenchmarkSvgError("public_json_schema")
    if document.get("schema_version") != benchmark_public_export.PUBLIC_SCHEMA_VERSION or document.get("evidence_scope") != EVIDENCE_SCOPE or document.get("overall_status") not in {"pass", "fail"} or not isinstance(document.get("all_correct"), bool):
        raise BenchmarkSvgError("public_json_status_contract")
    if not isinstance(document.get("suite_id"), str) or benchmark_public_export.benchmark_suite_gate.RUN_ID_PATTERN.fullmatch(document["suite_id"]) is None or benchmark_public_export.SHA256_PATTERN.fullmatch(str(document.get("plan_sha256", ""))) is None or not has_exact_keys(document.get("entry_pairs"), benchmark_public_export.benchmark_suite_gate.ARMS) or document["entry_pairs"] != benchmark_public_export.benchmark_suite_gate.ARM_ENTRY_PAIRS:
        raise BenchmarkSvgError("public_identity_contract")
    rules = document["rules"]
    expected_rules = {"tokens": benchmark_public_export.TOKEN_RULE, "time": benchmark_public_export.TIME_RULE, "overall": benchmark_public_export.OVERALL_RULE, "minimum_pairs_per_tier": benchmark_public_export.MINIMUM_PUBLIC_PAIR_COUNT}
    if not has_exact_keys(rules, RULE_KEYS) or rules != expected_rules:
        raise BenchmarkSvgError("public_rule_contract")
    tier_repeat_counts = document["tier_repeat_counts"]
    if not has_exact_keys(tier_repeat_counts, benchmark_public_export.benchmark_suite_gate.TIERS) or any(isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count < rules["minimum_pairs_per_tier"] for pair_count in tier_repeat_counts.values()):
        raise BenchmarkSvgError("public_pair_count_contract")
    expected_run_count = sum(tier_repeat_counts.values()) * 2
    if isinstance(document["expected_run_count"], bool) or not isinstance(document["expected_run_count"], int) or document["expected_run_count"] != expected_run_count:
        raise BenchmarkSvgError("public_run_count_contract")
    integrity = document["execution_integrity"]
    integrity_integers = has_exact_keys(integrity, INTEGRITY_KEYS) and all(not isinstance(value, bool) and isinstance(value, int) and value >= 0 for value in integrity.values())
    expected_controller_count = expected_run_count // 2
    integrity_counts_valid = integrity_integers and integrity["complete_runs"] == expected_run_count and integrity["retry_count"] == 0 and integrity["fallback_count"] == 0 and integrity["repair_count"] == 0 and integrity["runtime_session_count"] >= expected_run_count and integrity["runtime_root_count"] >= expected_run_count and integrity["runtime_root_count"] <= integrity["runtime_session_count"] and integrity["runtime_descendant_count"] == integrity["runtime_session_count"] - integrity["runtime_root_count"] and integrity["task_session_count"] == integrity["runtime_session_count"] - expected_controller_count and integrity["controller_tokens_excluded"] > 0 and integrity["multi_session_run_count"] <= expected_run_count and integrity["multi_session_run_count"] <= integrity["runtime_session_count"] - expected_run_count and integrity["sol_entry_probe_count"] == len(benchmark_public_export.benchmark_suite_gate.TIERS) and integrity["sol_entry_probe_pass_count"] == integrity["sol_entry_probe_count"]
    if not integrity_counts_valid:
        raise BenchmarkSvgError("public_execution_integrity")
    configuration = document["configuration"]
    catalog_sha256 = configuration.get("catalog_sha256") if isinstance(configuration, dict) else None
    catalog_file_counts = configuration.get("catalog_file_counts") if isinstance(configuration, dict) else None
    catalog_hashes_valid = has_exact_keys(catalog_sha256, CATALOG_SHA_KEYS) and all(benchmark_public_export.SHA256_PATTERN.fullmatch(str(catalog_sha256.get(key, ""))) is not None for key in CATALOG_SHA_KEYS)
    catalog_counts_valid = has_exact_keys(catalog_file_counts, CATALOG_COUNT_KEYS) and all(not isinstance(catalog_file_counts.get(key), bool) and isinstance(catalog_file_counts.get(key), int) and catalog_file_counts[key] >= 0 for key in CATALOG_COUNT_KEYS)
    if not has_exact_keys(configuration, CONFIGURATION_KEYS) or configuration.get("config_hash_equal") is not True or benchmark_public_export.SHA256_PATTERN.fullmatch(str(configuration.get("config_sha256", ""))) is None or not has_exact_keys(configuration.get("agents_sha256"), benchmark_public_export.benchmark_suite_gate.ARMS) or any(benchmark_public_export.SHA256_PATTERN.fullmatch(str(configuration["agents_sha256"].get(arm, ""))) is None for arm in benchmark_public_export.benchmark_suite_gate.ARMS) or configuration.get("runtime_context_hash_equal") is not True or any(benchmark_public_export.SHA256_PATTERN.fullmatch(str(configuration.get(field, ""))) is None for field in benchmark_public_export.benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS) or configuration.get("catalog_hash_equal") is not True or configuration.get("catalog_schema_version") != benchmark_public_export.benchmark_suite_gate.CATALOG_SCHEMA_VERSION or not catalog_hashes_valid or not catalog_counts_valid:
        raise BenchmarkSvgError("public_configuration_contract")
    if not has_exact_keys(document["caveats"], {"tokens", "first_result", "generalization"}) or any(not isinstance(value, str) or not value.strip() for value in document["caveats"].values()):
        raise BenchmarkSvgError("public_caveat_contract")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or [task.get("tier") for task in tasks if isinstance(task, dict)] != list(benchmark_public_export.benchmark_suite_gate.TIERS):
        raise BenchmarkSvgError("public_task_contract")
    task_statuses = []
    for task in tasks:
        tier = task["tier"]
        pair_count = tier_repeat_counts[tier]
        failures = task.get("failures")
        performance_diagnostics = task.get("performance_diagnostics")
        if not has_exact_keys(task, TASK_KEYS) or task.get("label") != benchmark_public_export.TASK_LABELS[tier] or task.get("status") not in {"pass", "fail"} or task.get("optimization_status") not in {"pass", "fail"} or not isinstance(failures, list) or len(failures) != len(set(failures)) or any(not isinstance(failure, str) or not failure for failure in failures) or bool(failures) is (task.get("status") == "pass") or not isinstance(performance_diagnostics, list) or len(performance_diagnostics) != len(set(performance_diagnostics)) or any(failure not in STRATEGY_FAILURES for failure in performance_diagnostics) or bool(performance_diagnostics) is (task.get("optimization_status") == "pass") or task.get("pair_count") != pair_count or task.get("run_count") != pair_count * 2:
            raise BenchmarkSvgError("public_task_contract")
        task_statuses.append(task["status"])
        for metric_group in ("direct_totals", "global_totals", "direct_medians", "global_medians", "paired_savings_percent_medians"):
            metrics = task.get(metric_group)
            if not has_exact_keys(metrics, METRIC_KEYS) or any(not is_number(value) or metric_group != "paired_savings_percent_medians" and value < 0 for value in metrics.values()):
                raise BenchmarkSvgError("public_metric_contract")
        if not has_exact_keys(task.get("paired_wins"), METRIC_KEYS) or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > pair_count for value in task["paired_wins"].values()):
            raise BenchmarkSvgError("public_metric_contract")
        direct_medians = task["direct_medians"]
        global_medians = task["global_medians"]
        savings = task["paired_savings_percent_medians"]
        wins = task["paired_wins"]
        direct_totals = task["direct_totals"]
        global_totals = task["global_totals"]
        if not has_exact_keys(task.get("metric_gates"), GATED_METRIC_KEYS):
            raise BenchmarkSvgError("public_metric_gate_contract")
        for metric in GATED_METRIC_KEYS:
            metric_gate = task["metric_gates"][metric]
            expected_gate_keys = TIME_METRIC_GATE_KEYS if metric == "steady_state_execution_elapsed_ms" else METRIC_GATE_KEYS
            if not has_exact_keys(metric_gate, expected_gate_keys):
                raise BenchmarkSvgError("public_metric_gate_contract")
            if metric_gate.get("status") not in {"pass", "fail"}:
                raise BenchmarkSvgError("public_metric_gate_contract")
            expected_strict_majority_better = wins[metric] > pair_count / 2
            majority_required = metric_gate.get("strict_majority_required")
            expected_majority_required = metric == "steady_state_execution_elapsed_ms" and tier == "medium"
            invalid_majority_policy = metric == "steady_state_logical_tokens" and majority_required is not False or expected_majority_required and majority_required is not True or metric == "steady_state_execution_elapsed_ms" and tier != "medium" and not isinstance(majority_required, bool)
            if invalid_majority_policy or metric_gate.get("strict_majority_better") is not expected_strict_majority_better:
                raise BenchmarkSvgError("public_metric_gate_contract")
            if metric_gate.get("minimum_paired_savings_percent") != benchmark_public_export.benchmark_suite_gate.MINIMUM_PAIRED_SAVINGS_PERCENT or metric_gate.get("maximum_pair_regression_percent") != benchmark_public_export.benchmark_suite_gate.MAXIMUM_PAIRED_REGRESSION_PERCENT:
                raise BenchmarkSvgError("public_metric_gate_contract")
            if not is_number(metric_gate.get("worst_pair_savings_percent")):
                raise BenchmarkSvgError("public_metric_gate_contract")
            expected_regression_bound_required = False
            if metric_gate.get("regression_bound_required") is not expected_regression_bound_required:
                raise BenchmarkSvgError("public_metric_gate_contract")
            if metric == "steady_state_execution_elapsed_ms":
                if metric_gate.get("maximum_pair_regression_ms") != benchmark_public_export.benchmark_suite_gate.MAXIMUM_PAIRED_TIME_REGRESSION_MS or not is_number(metric_gate.get("worst_pair_regression_ms")) or metric_gate["worst_pair_regression_ms"] < 0 or isinstance(metric_gate.get("material_pair_regression_count"), bool) or not isinstance(metric_gate.get("material_pair_regression_count"), int) or metric_gate["material_pair_regression_count"] < 0:
                    raise BenchmarkSvgError("public_metric_gate_contract")
                within_limit = metric_gate.get("worst_pair_regression_within_limit")
                material_count = metric_gate["material_pair_regression_count"]
                if not isinstance(within_limit, bool) or within_limit is not (material_count == 0) or material_count > pair_count - wins[metric]:
                    raise BenchmarkSvgError("public_metric_gate_contract")
                if material_count > 0 and (metric_gate["worst_pair_regression_ms"] <= metric_gate["maximum_pair_regression_ms"] or metric_gate["worst_pair_savings_percent"] >= -metric_gate["maximum_pair_regression_percent"]):
                    raise BenchmarkSvgError("public_metric_gate_contract")
            expected_aggregate_lower = global_totals[metric] < direct_totals[metric]
            expected_raw_median_lower = global_medians[metric] < direct_medians[metric]
            expected_threshold = savings[metric] >= metric_gate["minimum_paired_savings_percent"]
            if metric_gate.get("aggregate_global_lower") is not expected_aggregate_lower or metric_gate.get("raw_global_median_lower") is not expected_raw_median_lower or metric_gate.get("paired_savings_median_meets_threshold") is not expected_threshold:
                raise BenchmarkSvgError("public_metric_gate_contract")
            if metric == "steady_state_logical_tokens":
                expected_metric_status = "fail" if any(failure.startswith("token_") for failure in performance_diagnostics) else "pass"
            elif tier == "simple":
                expected_metric_status = "fail" if "steady_state_tolerance_loss" in performance_diagnostics else "pass"
            elif tier == "medium":
                expected_metric_status = "fail" if any(failure.startswith("steady_state_") for failure in performance_diagnostics) else "pass"
            else:
                expected_metric_status = "pass"
            if metric_gate.get("status") != expected_metric_status:
                raise BenchmarkSvgError("public_metric_gate_contract")
    cohort_metric_gates = document.get("cohort_metric_gates")
    if not has_exact_keys(cohort_metric_gates, GATED_METRIC_KEYS):
        raise BenchmarkSvgError("public_cohort_metric_gate_contract")
    for metric in GATED_METRIC_KEYS:
        cohort_gate = cohort_metric_gates[metric]
        if not has_exact_keys(cohort_gate, COHORT_METRIC_GATE_KEYS) or any(isinstance(cohort_gate.get(field), bool) or not isinstance(cohort_gate.get(field), int) or cohort_gate[field] < 0 for field in ("direct_total", "global_total")) or not isinstance(cohort_gate.get("aggregate_global_lower"), bool) or cohort_gate.get("aggregate_global_lower") is not (cohort_gate["global_total"] < cohort_gate["direct_total"]) or cohort_gate.get("status") != ("pass" if cohort_gate["aggregate_global_lower"] else "fail"):
            raise BenchmarkSvgError("public_cohort_metric_gate_contract")
    expected_overall_status = "pass" if document["all_correct"] and all(gate["status"] == "pass" for gate in cohort_metric_gates.values()) else "fail"
    if document["overall_status"] != expected_overall_status:
        raise BenchmarkSvgError("public_json_status_contract")
    return document


def atomic_write_svg(path, svg_text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(svg_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def escaped(value):
    return html.escape(str(value), quote=True)


def format_number(value):
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"


def format_seconds(milliseconds):
    return f"{milliseconds / 1000:.3f}s"


def format_entry_pairs(entry_pairs):
    direct = entry_pairs["direct"].replace("|", " | ")
    auto = entry_pairs["global"].replace("|", " | ")
    return f"Direct {direct} · Auto {auto}"


def integrity_summary(document):
    integrity = document["execution_integrity"]
    issue_count = integrity["retry_count"] + integrity["fallback_count"] + integrity["repair_count"]
    return f'{integrity["complete_runs"]} complete · {integrity["runtime_session_count"]} sessions ({integrity["runtime_root_count"]} roots) · {issue_count} retry/fallback/repair'


def metric_gate_verdict(document):
    token_savings = cohort_savings_percent(document, "steady_state_logical_tokens")
    time_savings = cohort_savings_percent(document, "steady_state_execution_elapsed_ms")
    return f'{document["expected_run_count"]}/{document["expected_run_count"]} correct · aggregate steady-state saved {token_savings:.1f}% tokens · {time_savings:.1f}% execution'


def failure_summary(document):
    failed = []
    for task in document["tasks"]:
        if task["status"] == "fail" or task["optimization_status"] == "fail":
            dimensions = []
            if task["status"] == "fail":
                dimensions.append("correctness")
            if any(failure.startswith("token_") for failure in task["performance_diagnostics"]):
                dimensions.append("tokens")
            if any(failure.startswith("steady_state_") for failure in task["performance_diagnostics"]):
                dimensions.append("steady-state")
            if any(not failure.startswith(("token_", "steady_state_")) for failure in task["performance_diagnostics"]):
                dimensions.append("other")
            failed.append(f'{task["tier"].title()}: {" + ".join(dimensions) or "gate"}')
    return "; ".join(failed)


def aggregate_savings_percent(task, metric):
    direct_total = task["direct_totals"][metric]
    return (direct_total - task["global_totals"][metric]) / direct_total * 100


def cohort_savings_percent(document, metric):
    cohort_gate = document["cohort_metric_gates"][metric]
    return (cohort_gate["direct_total"] - cohort_gate["global_total"]) / cohort_gate["direct_total"] * 100


def bar_width(value, maximum, available_width):
    return max(4, round(value / maximum * available_width)) if maximum > 0 else 4


def svg_header(width, height, document, layout):
    task_descriptions = "; ".join(f"{task['tier']} cohort steady-state totals saved {aggregate_savings_percent(task, 'steady_state_logical_tokens'):.3f}% tokens and {aggregate_savings_percent(task, 'steady_state_execution_elapsed_ms'):.3f}% execution time" for task in document["tasks"])
    metadata = html.escape(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    strategy_status = document["overall_status"].upper()
    title = f"Auto strategy real benchmark {strategy_status}, {layout} layout"
    description = f"Direct blue versus Auto green cohort totals. {task_descriptions}. All {document['expected_run_count']} runs passed correctness and evidence gates; the strategy performance gate is {strategy_status}."
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">', f'  <title id="title">{escaped(title)}</title>', f'  <desc id="desc">{escaped(description)}</desc>', f'  <metadata id="benchmark-data">{metadata}</metadata>', f'  <rect width="{width}" height="{height}" rx="30" fill="{BACKGROUND_COLOR}"/>']


def desktop_svg(document):
    width = 1200
    height = 760
    lines = svg_header(width, height, document, "desktop")
    overall_status = document["overall_status"].upper()
    overall_color = GLOBAL_COLOR if document["overall_status"] == "pass" else FAIL_COLOR
    lines.extend([f'  <text x="48" y="50" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="28" font-weight="700">Real A/B benchmark · {overall_status}</text>', f'  <text x="48" y="80" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="16">Direct Sol-ultra vs Auto Luna-max entry · lower is better · {escaped(integrity_summary(document))}</text>', f'  <rect x="906" y="34" width="16" height="16" rx="4" fill="{DIRECT_COLOR}"/><text x="930" y="48" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14">Direct</text>', f'  <rect x="1014" y="34" width="16" height="16" rx="4" fill="{GLOBAL_COLOR}"/><text x="1038" y="48" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14">Auto</text>'])
    for index, task in enumerate(document["tasks"]):
        y = 104 + index * 184
        direct_tokens = task["direct_totals"]["steady_state_logical_tokens"]
        global_tokens = task["global_totals"]["steady_state_logical_tokens"]
        direct_time = task["direct_totals"]["steady_state_execution_elapsed_ms"]
        global_time = task["global_totals"]["steady_state_execution_elapsed_ms"]
        token_maximum = max(direct_tokens, global_tokens)
        time_maximum = max(direct_time, global_time)
        token_savings = aggregate_savings_percent(task, "steady_state_logical_tokens")
        time_savings = aggregate_savings_percent(task, "steady_state_execution_elapsed_ms")
        task_color = GLOBAL_COLOR if task["status"] == "pass" and task["optimization_status"] == "pass" else FAIL_COLOR
        task_state = "CORRECT" if task["status"] == "pass" else "FAIL"
        if task["optimization_status"] == "fail":
            task_state += " · TIER REGRESSION"
        lines.extend([f'  <g transform="translate(48 {y})">', f'    <rect width="1104" height="166" rx="18" fill="{PANEL_COLOR}" stroke="{BORDER_COLOR}"/>', f'    <text x="22" y="31" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="20" font-weight="700">{escaped(task["label"])}</text>', f'    <text x="1080" y="31" text-anchor="end" fill="{task_color}" font-family="Inter,Arial,sans-serif" font-size="16" font-weight="700">{task_state} · {task["pair_count"]} pairs · {task["run_count"]} runs</text>', f'    <text x="22" y="57" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14">Steady-state saved {token_savings:.3f}% tokens · {time_savings:.3f}% execution time · wins {task["paired_wins"]["steady_state_execution_elapsed_ms"]}/{task["pair_count"]}</text>', f'    <text x="22" y="85" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14" font-weight="700">STEADY-STATE LOGICAL TOKENS</text>', f'    <text x="574" y="85" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14" font-weight="700">STEADY-STATE EXECUTION</text>', f'    <text x="22" y="111" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Direct {format_number(direct_tokens)}</text>', f'    <rect x="164" y="98" width="{bar_width(direct_tokens, token_maximum, 330)}" height="15" rx="7.5" fill="{DIRECT_COLOR}"/>', f'    <text x="22" y="140" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Auto {format_number(global_tokens)}</text>', f'    <rect x="164" y="127" width="{bar_width(global_tokens, token_maximum, 330)}" height="15" rx="7.5" fill="{GLOBAL_COLOR}"/>', f'    <text x="574" y="111" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Direct {format_seconds(direct_time)}</text>', f'    <rect x="710" y="98" width="{bar_width(direct_time, time_maximum, 330)}" height="15" rx="7.5" fill="{DIRECT_COLOR}"/>', f'    <text x="574" y="140" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Auto {format_seconds(global_time)}</text>', f'    <rect x="710" y="127" width="{bar_width(global_time, time_maximum, 330)}" height="15" rx="7.5" fill="{GLOBAL_COLOR}"/>', '  </g>'])
    footer_title = metric_gate_verdict(document) if document["overall_status"] == "pass" else f"Strategy gate FAIL · {failure_summary(document)}"
    footer_fill = "#0d2b25" if document["overall_status"] == "pass" else "#33210f"
    lines.extend([f'  <rect x="48" y="662" width="1104" height="76" rx="16" fill="{footer_fill}" stroke="{overall_color}"/>', f'  <text x="600" y="690" text-anchor="middle" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="15" font-weight="700">{escaped(footer_title)}</text>', f'  <text x="600" y="716" text-anchor="middle" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">{escaped(format_entry_pairs(document["entry_pairs"]))} · all runs correctness/evidence PASS · logical tokens are not billing tokens</text>', '</svg>'])
    return "\n".join(lines) + "\n"


def mobile_svg(document):
    width = 720
    height = 1260
    lines = svg_header(width, height, document, "mobile")
    overall_status = document["overall_status"].upper()
    overall_color = GLOBAL_COLOR if document["overall_status"] == "pass" else FAIL_COLOR
    lines.extend([f'  <text x="34" y="48" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="25" font-weight="700">Real A/B benchmark · {overall_status}</text>', f'  <text x="34" y="76" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14">Direct blue vs Auto green · {escaped(integrity_summary(document))}</text>'])
    for index, task in enumerate(document["tasks"]):
        y = 98 + index * 350
        direct_tokens = task["direct_totals"]["steady_state_logical_tokens"]
        global_tokens = task["global_totals"]["steady_state_logical_tokens"]
        direct_time = task["direct_totals"]["steady_state_execution_elapsed_ms"]
        global_time = task["global_totals"]["steady_state_execution_elapsed_ms"]
        token_maximum = max(direct_tokens, global_tokens)
        time_maximum = max(direct_time, global_time)
        task_color = GLOBAL_COLOR if task["status"] == "pass" and task["optimization_status"] == "pass" else FAIL_COLOR
        task_state = "CORRECT" if task["status"] == "pass" else "FAIL"
        if task["optimization_status"] == "fail":
            task_state += " · TIER REGRESSION"
        lines.extend([f'  <g transform="translate(34 {y})">', f'    <rect width="652" height="330" rx="18" fill="{PANEL_COLOR}" stroke="{BORDER_COLOR}"/>', f'    <text x="20" y="31" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="19" font-weight="700">{escaped(task["label"])}</text>', f'    <text x="20" y="57" fill="{task_color}" font-family="Inter,Arial,sans-serif" font-size="15" font-weight="700">{task_state} · {task["pair_count"]} pairs · {task["run_count"]} runs</text>', f'    <text x="20" y="83" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Steady-state saved {aggregate_savings_percent(task, "steady_state_logical_tokens"):.3f}% tokens · {aggregate_savings_percent(task, "steady_state_execution_elapsed_ms"):.3f}% execution</text>', f'    <text x="20" y="114" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14" font-weight="700">STEADY-STATE TOKENS</text>', f'    <text x="20" y="141" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Direct {format_number(direct_tokens)}</text>', f'    <rect x="158" y="128" width="{bar_width(direct_tokens, token_maximum, 450)}" height="15" rx="7.5" fill="{DIRECT_COLOR}"/>', f'    <text x="20" y="170" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Auto {format_number(global_tokens)}</text>', f'    <rect x="158" y="157" width="{bar_width(global_tokens, token_maximum, 450)}" height="15" rx="7.5" fill="{GLOBAL_COLOR}"/>', f'    <text x="20" y="207" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="14" font-weight="700">STEADY-STATE EXECUTION</text>', f'    <text x="20" y="234" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Direct {format_seconds(direct_time)}</text>', f'    <rect x="158" y="221" width="{bar_width(direct_time, time_maximum, 450)}" height="15" rx="7.5" fill="{DIRECT_COLOR}"/>', f'    <text x="20" y="263" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13">Auto {format_seconds(global_time)}</text>', f'    <rect x="158" y="250" width="{bar_width(global_time, time_maximum, 450)}" height="15" rx="7.5" fill="{GLOBAL_COLOR}"/>', f'    <text x="20" y="299" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="12">First-result is end-to-end diagnostic · Ending excluded</text>', '  </g>'])
    footer_title = metric_gate_verdict(document) if document["overall_status"] == "pass" else f"Strategy gate FAIL · {failure_summary(document)}"
    footer_fill = "#0d2b25" if document["overall_status"] == "pass" else "#33210f"
    lines.extend([f'  <rect x="34" y="1150" width="652" height="82" rx="16" fill="{footer_fill}" stroke="{overall_color}"/>', f'  <text x="360" y="1179" text-anchor="middle" fill="{TEXT_COLOR}" font-family="Inter,Arial,sans-serif" font-size="13" font-weight="700">{escaped(footer_title)}</text>', f'  <text x="360" y="1205" text-anchor="middle" fill="{MUTED_COLOR}" font-family="Inter,Arial,sans-serif" font-size="12">{escaped(format_entry_pairs(document["entry_pairs"]))} · all runs correct · not billing tokens</text>', '</svg>'])
    return "\n".join(lines) + "\n"


def render_svgs(input_path, desktop_path, mobile_path):
    document = load_public_json(input_path)
    atomic_write_svg(desktop_path, desktop_svg(document))
    atomic_write_svg(mobile_path, mobile_svg(document))
    return document


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Render accessible Direct-versus-Auto desktop and mobile SVGs from sanitized benchmark JSON.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--desktop", type=Path, required=True)
    parser.add_argument("--mobile", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        document = render_svgs(args.input, args.desktop, args.mobile)
    except BenchmarkSvgError as error:
        print(json.dumps({"schema_version": 1, "status": "error", "failure": error.code}, separators=(",", ":")))
        return 1
    print(json.dumps({"schema_version": 1, "status": "pass", "suite_id": document["suite_id"], "desktop": str(args.desktop), "mobile": str(args.mobile)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
