#!/usr/bin/env python3
"""Export a sanitized public benchmark summary from frozen passing evidence."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path, PureWindowsPath
from tempfile import mkstemp


TASK_LABELS = {"simple": "simple constant lookup", "medium": "medium one-method audit", "complex": "complex multi-file workflow graph"}
MINIMUM_PUBLIC_PAIR_COUNT = 2
PUBLIC_SCHEMA_VERSION = 8
FORBIDDEN_PUBLIC_KEYS = frozenset({"prompt", "raw_prompt", "result", "raw_result", "thread_id", "thread_ids", "session_id", "session_ids", "receipt", "receipt_path", "receipt_paths", "receipt_session_ids", "codex_home", "config_path", "agents_path", "models_cache_path", "memories_root", "workdir", "source_root", "evidence_path", "skills_catalog_root", "plugins_catalog_root", "marketplace_catalog_sources"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUMMARY_KEYS = frozenset({"schema_version", "suite_id", "plan_sha256", "repeat_count", "tier_repeat_counts", "overall_status", "all_correct", "overall_rule", "time_rule", "token_rule", "cohort_metric_gates", "tiers"})
MANIFEST_KEYS = frozenset({"schema_version", "suite_id", "plan_sha256", "run_id", "pair_id", "tier", "repeat_index", "arm", "order_index", "workload_prompt_sha256", "prompt_file_sha256", "expected_sha256", "source_snapshot_sha256", "environment_sha256", "selected_entry_pair", "entry_execution_mode", "result_producer_pair", "executed_pairs", "receipt_session_ids", "unexpected_receipt_session_ids", "unreceipted_descendant_count", "runtime_session_count", "runtime_root_session_count", "runtime_descendant_session_count", "task_session_count", "controller_tokens_excluded", "completion", "retry_count", "fallback_count", "repair_count", "metrics_complete", "logical_total_tokens", "steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "route_selection_elapsed_ms", "calibration_attempt_count", "calibration_failure_elapsed_ms", "calibration_failure_logical_tokens", "first_result_elapsed_ms", "producer_elapsed_ms", "ending_real_elapsed_ms", "total_wall_elapsed_ms", "presented_result_sha256", "presented_result_object_sha256", "ending_real", "acceptance_status", "gate"})
MANIFEST_GATE_KEYS = frozenset({"generated_by", "version", "evidence_sha256", "status", "failures", "source_files_checked"})
ENDING_REAL_KEYS = frozenset({"method", "completed", "status"})


class PublicExportError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_gate_module():
    gate_path = Path(__file__).with_name("benchmark_suite_gate.py")
    module_spec = importlib.util.spec_from_file_location("benchmark_public_export_gate", gate_path)
    gate_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(gate_module)
    return gate_module


benchmark_suite_gate = load_gate_module()
OVERALL_RULE = benchmark_suite_gate.OVERALL_RULE
TOKEN_RULE = benchmark_suite_gate.TOKEN_RULE
TIME_RULE = benchmark_suite_gate.TIME_RULE


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def load_json_object(path, failure_code):
    try:
        payload = path.read_bytes()
        document = benchmark_suite_gate.strict_json_loads(payload)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise PublicExportError(failure_code)
    if not isinstance(document, dict):
        raise PublicExportError(failure_code)
    return document, payload


def atomic_write_public_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def private_strings_from_evidence(plan, manifests):
    private_strings = set()

    def visit(value, key=None):
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, child_key)
        elif isinstance(value, list):
            for child_value in value:
                visit(child_value, key)
        elif isinstance(value, str):
            is_absolute_path = Path(value).is_absolute() or PureWindowsPath(value).is_absolute()
            is_private_identifier = key in {"thread_id", "thread_ids", "session_id", "session_ids", "receipt_session_ids"}
            if is_absolute_path or is_private_identifier:
                private_strings.add(value)

    visit(plan)
    visit(manifests)
    return private_strings


def validate_public_privacy(public_document, private_strings):
    def visit(value, key=None):
        if key in FORBIDDEN_PUBLIC_KEYS:
            raise PublicExportError("public_privacy_violation")
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, child_key)
        elif isinstance(value, list):
            for child_value in value:
                visit(child_value, key)
        elif isinstance(value, str):
            if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or any(private_value and private_value in value for private_value in private_strings):
                raise PublicExportError("public_privacy_violation")

    visit(public_document)


def load_manifests(manifest_dir):
    if not manifest_dir.is_dir():
        raise PublicExportError("manifest_directory_missing")
    manifest_paths = sorted(manifest_dir.glob("*.json"))
    manifests = []
    for manifest_path in manifest_paths:
        manifest, _ = load_json_object(manifest_path, "manifest_invalid")
        if manifest_path.name != f"{manifest.get('run_id')}.json":
            raise PublicExportError("manifest_filename_mismatch")
        manifests.append(manifest)
    return manifests


def require_sha256(value, failure_code):
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise PublicExportError(failure_code)
    return value


def validate_manifest(manifest, run_plan, suite_id, plan_sha256):
    if set(manifest) != MANIFEST_KEYS:
        raise PublicExportError("manifest_schema_mismatch")
    required_values = {"schema_version": benchmark_suite_gate.MANIFEST_SCHEMA_VERSION, "suite_id": suite_id, "plan_sha256": plan_sha256, "run_id": run_plan["run_id"], "pair_id": run_plan["pair_id"], "tier": run_plan["tier"], "repeat_index": run_plan["repeat_index"], "arm": run_plan["arm"], "order_index": run_plan["order_index"], "acceptance_status": "pass", "completion": "complete", "metrics_complete": True, "retry_count": 0, "fallback_count": 0, "repair_count": 0, "selected_entry_pair": run_plan["selected_entry_pair"], "result_producer_pair": run_plan["selected_entry_pair"], "workload_prompt_sha256": run_plan["prompt_sha256"], "expected_sha256": run_plan["expected_sha256"], "source_snapshot_sha256": run_plan["source_snapshot_sha256"]}
    if any(manifest.get(field) != expected_value for field, expected_value in required_values.items()):
        raise PublicExportError("manifest_contract_mismatch")
    if manifest.get("unexpected_receipt_session_ids") != []:
        raise PublicExportError("manifest_session_coverage_failure")
    runtime_session_count = manifest.get("runtime_session_count")
    runtime_root_session_count = manifest.get("runtime_root_session_count")
    runtime_descendant_session_count = manifest.get("runtime_descendant_session_count")
    invalid_runtime_counts = isinstance(runtime_session_count, bool) or not isinstance(runtime_session_count, int) or runtime_session_count < 1 or isinstance(runtime_root_session_count, bool) or not isinstance(runtime_root_session_count, int) or runtime_root_session_count < 1 or isinstance(runtime_descendant_session_count, bool) or not isinstance(runtime_descendant_session_count, int) or runtime_descendant_session_count != runtime_session_count - runtime_root_session_count
    if invalid_runtime_counts or run_plan["arm"] == "direct" and runtime_root_session_count != 1:
        raise PublicExportError("manifest_runtime_session_count_invalid")
    unreceipted_descendant_count = manifest.get("unreceipted_descendant_count")
    if isinstance(unreceipted_descendant_count, bool) or not isinstance(unreceipted_descendant_count, int) or not 0 <= unreceipted_descendant_count <= runtime_descendant_session_count:
        raise PublicExportError("manifest_session_coverage_failure")
    task_session_count = manifest.get("task_session_count")
    controller_tokens_excluded = manifest.get("controller_tokens_excluded")
    if isinstance(task_session_count, bool) or not isinstance(task_session_count, int) or task_session_count < 1 or task_session_count > runtime_session_count:
        raise PublicExportError("manifest_task_session_count_invalid")
    if isinstance(controller_tokens_excluded, bool) or not isinstance(controller_tokens_excluded, int) or controller_tokens_excluded < 0:
        raise PublicExportError("manifest_controller_tokens_invalid")
    if run_plan["arm"] == "direct" and (task_session_count != runtime_session_count or controller_tokens_excluded != 0):
        raise PublicExportError("manifest_controller_accounting_invalid")
    if run_plan["arm"] == "global" and (task_session_count != runtime_session_count - 1 or controller_tokens_excluded <= 0):
        raise PublicExportError("manifest_controller_accounting_invalid")
    executed_pairs = manifest.get("executed_pairs")
    if not isinstance(executed_pairs, list) or len(executed_pairs) != runtime_session_count or run_plan["selected_entry_pair"] not in executed_pairs or any(not isinstance(pair, str) or "|" not in pair for pair in executed_pairs):
        raise PublicExportError("manifest_runtime_pair_invalid")
    ending_real = manifest.get("ending_real")
    if not isinstance(ending_real, dict) or set(ending_real) != ENDING_REAL_KEYS or ending_real != {"method": benchmark_suite_gate.ENDING_REAL_METHOD, "completed": True, "status": "pass"}:
        raise PublicExportError("manifest_ending_real_failure")
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or set(gate) != MANIFEST_GATE_KEYS or gate.get("generated_by") != "benchmark_suite_gate" or gate.get("version") != benchmark_suite_gate.SCHEMA_VERSION or gate.get("status") != "pass" or gate.get("failures") != []:
        raise PublicExportError("manifest_gate_failure")
    require_sha256(gate.get("evidence_sha256"), "manifest_gate_evidence_invalid")
    if isinstance(gate.get("source_files_checked"), bool) or not isinstance(gate.get("source_files_checked"), int) or gate["source_files_checked"] < 0:
        raise PublicExportError("manifest_gate_source_count_invalid")
    if any(not isinstance(manifest.get(key), int) or manifest[key] < 0 for key in ["logical_total_tokens", "steady_state_logical_tokens", "calibration_attempt_count", "calibration_failure_elapsed_ms", "calibration_failure_logical_tokens"]):
        raise PublicExportError("manifest_token_metric_invalid")
    for timing_key in ["steady_state_execution_elapsed_ms", "route_selection_elapsed_ms", "first_result_elapsed_ms", "producer_elapsed_ms", "ending_real_elapsed_ms", "total_wall_elapsed_ms"]:
        if not isinstance(manifest.get(timing_key), int) or manifest[timing_key] < 0:
            raise PublicExportError("manifest_time_metric_invalid")
    if manifest["first_result_elapsed_ms"] > manifest["producer_elapsed_ms"] or manifest["route_selection_elapsed_ms"] != manifest["first_result_elapsed_ms"] - manifest["steady_state_execution_elapsed_ms"] or manifest["total_wall_elapsed_ms"] != manifest["producer_elapsed_ms"] + manifest["ending_real_elapsed_ms"]:
        raise PublicExportError("manifest_time_metric_invalid")
    expected_environment_sha256 = sha256_bytes(benchmark_suite_gate.canonical_json(run_plan["environment"]).encode("utf-8"))
    if manifest.get("environment_sha256") != expected_environment_sha256:
        raise PublicExportError("manifest_environment_mismatch")
    for hash_key in ["prompt_file_sha256", "expected_sha256", "source_snapshot_sha256", "environment_sha256", "presented_result_sha256", "presented_result_object_sha256"]:
        require_sha256(manifest.get(hash_key), "manifest_result_proof_invalid")


def validate_configuration_hashes(plan):
    config_hashes = {arm: set() for arm in benchmark_suite_gate.ARMS}
    agents_hashes = {arm: set() for arm in benchmark_suite_gate.ARMS}
    runtime_context_hashes = {field: {arm: set() for arm in benchmark_suite_gate.ARMS} for field in benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS}
    catalog_values = {field: {arm: set() for arm in benchmark_suite_gate.ARMS} for field in benchmark_suite_gate.CATALOG_PAIR_FIELDS}
    marketplace_source_counts = {arm: set() for arm in benchmark_suite_gate.ARMS}
    for run_plan in plan["runs"]:
        environment = run_plan.get("environment")
        if not isinstance(environment, dict):
            raise PublicExportError("plan_environment_missing")
        config_hashes[run_plan["arm"]].add(require_sha256(environment.get("config_sha256"), "plan_config_hash_invalid"))
        agents_hashes[run_plan["arm"]].add(require_sha256(environment.get("agents_sha256"), "plan_agents_hash_invalid"))
        for field in benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS:
            runtime_context_hashes[field][run_plan["arm"]].add(require_sha256(environment.get(field), "plan_runtime_context_hash_invalid"))
        for field in benchmark_suite_gate.CATALOG_PAIR_FIELDS:
            catalog_values[field][run_plan["arm"]].add(environment.get(field))
        marketplace_sources = environment.get("marketplace_catalog_sources")
        if not isinstance(marketplace_sources, list):
            raise PublicExportError("plan_marketplace_sources_invalid")
        marketplace_source_counts[run_plan["arm"]].add(len(marketplace_sources))
    if any(len(config_hashes[arm]) != 1 or len(agents_hashes[arm]) != 1 for arm in benchmark_suite_gate.ARMS) or any(len(runtime_context_hashes[field][arm]) != 1 for field in benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS for arm in benchmark_suite_gate.ARMS):
        raise PublicExportError("plan_configuration_cohort_mismatch")
    if any(len(catalog_values[field][arm]) != 1 for field in benchmark_suite_gate.CATALOG_PAIR_FIELDS for arm in benchmark_suite_gate.ARMS) or any(len(marketplace_source_counts[arm]) != 1 for arm in benchmark_suite_gate.ARMS):
        raise PublicExportError("plan_catalog_cohort_mismatch")
    direct_config_sha256 = next(iter(config_hashes["direct"]))
    global_config_sha256 = next(iter(config_hashes["global"]))
    if direct_config_sha256 != global_config_sha256:
        raise PublicExportError("plan_config_hash_not_equal")
    direct_runtime_context = {field: next(iter(runtime_context_hashes[field]["direct"])) for field in benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS}
    global_runtime_context = {field: next(iter(runtime_context_hashes[field]["global"])) for field in benchmark_suite_gate.RUNTIME_CONTEXT_PAIR_FIELDS}
    if direct_runtime_context != global_runtime_context:
        raise PublicExportError("plan_runtime_context_hash_not_equal")
    direct_catalog = {field: next(iter(catalog_values[field]["direct"])) for field in benchmark_suite_gate.CATALOG_PAIR_FIELDS}
    global_catalog = {field: next(iter(catalog_values[field]["global"])) for field in benchmark_suite_gate.CATALOG_PAIR_FIELDS}
    if direct_catalog != global_catalog or next(iter(marketplace_source_counts["direct"])) != next(iter(marketplace_source_counts["global"])):
        raise PublicExportError("plan_catalog_hash_not_equal")
    catalog_sha256 = {"skills": direct_catalog["skills_catalog_sha256"], "plugins": direct_catalog["plugins_catalog_sha256"], "marketplaces": direct_catalog["marketplace_catalog_sha256"], "visible": direct_catalog["visible_catalog_sha256"]}
    catalog_file_counts = {"skills": direct_catalog["skills_catalog_file_count"], "plugins": direct_catalog["plugins_catalog_file_count"], "marketplaces": direct_catalog["marketplace_catalog_file_count"], "marketplace_sources": next(iter(marketplace_source_counts["direct"]))}
    return {"config_hash_equal": True, "config_sha256": direct_config_sha256, "agents_sha256": {"direct": next(iter(agents_hashes["direct"])), "global": next(iter(agents_hashes["global"]))}, "runtime_context_hash_equal": True, **direct_runtime_context, "catalog_hash_equal": True, "catalog_schema_version": direct_catalog["catalog_schema_version"], "catalog_sha256": catalog_sha256, "catalog_file_counts": catalog_file_counts}


def validate_summary(summary, plan, manifests, plan_sha256, tier_repeat_counts):
    if set(summary) != SUMMARY_KEYS:
        raise PublicExportError("summary_schema_mismatch")
    if summary.get("schema_version") != benchmark_suite_gate.SCHEMA_VERSION or summary.get("suite_id") != plan["suite_id"] or summary.get("plan_sha256") != plan_sha256 or summary.get("overall_status") not in {"pass", "fail"} or not isinstance(summary.get("all_correct"), bool):
        raise PublicExportError("summary_identity_or_status_failure")
    uniform_repeat_count = next(iter(set(tier_repeat_counts.values()))) if len(set(tier_repeat_counts.values())) == 1 else None
    if summary.get("repeat_count") != uniform_repeat_count:
        raise PublicExportError("summary_repeat_count_mismatch")
    if summary.get("tier_repeat_counts") != tier_repeat_counts or summary.get("overall_rule") != OVERALL_RULE or summary.get("token_rule") != TOKEN_RULE or summary.get("time_rule") != TIME_RULE:
        raise PublicExportError("summary_rule_or_count_mismatch")
    summary_tiers = summary.get("tiers")
    if not isinstance(summary_tiers, dict) or set(summary_tiers) != set(benchmark_suite_gate.TIERS):
        raise PublicExportError("summary_tier_contract")
    recomputed_tiers = {tier: benchmark_suite_gate.aggregate_tier(tier, tier_repeat_counts[tier], manifests) for tier in benchmark_suite_gate.TIERS}
    all_correct = all(recomputed_tiers[tier]["status"] == "pass" for tier in benchmark_suite_gate.TIERS)
    direct_manifests = sorted([manifest for manifest in manifests if manifest["arm"] == "direct"], key=lambda item: item["order_index"])
    global_manifests = sorted([manifest for manifest in manifests if manifest["arm"] == "global"], key=lambda item: item["order_index"])
    cohort_metric_gates = {}
    for metric in benchmark_suite_gate.GATED_METRICS:
        direct_total = sum(manifest[metric] for manifest in direct_manifests)
        global_total = sum(manifest[metric] for manifest in global_manifests)
        cohort_metric_gates[metric] = {"direct_total": direct_total, "global_total": global_total, "aggregate_global_lower": global_total < direct_total, "status": "pass" if global_total < direct_total else "fail"}
    overall_status = "pass" if all_correct and all(gate["status"] == "pass" for gate in cohort_metric_gates.values()) else "fail"
    recomputed_summary = {"schema_version": benchmark_suite_gate.SCHEMA_VERSION, "suite_id": plan["suite_id"], "plan_sha256": plan_sha256, "repeat_count": uniform_repeat_count, "tier_repeat_counts": tier_repeat_counts, "overall_status": overall_status, "all_correct": all_correct, "overall_rule": OVERALL_RULE, "time_rule": TIME_RULE, "token_rule": TOKEN_RULE, "cohort_metric_gates": cohort_metric_gates, "tiers": recomputed_tiers}
    if benchmark_suite_gate.canonical_json(summary) != benchmark_suite_gate.canonical_json(recomputed_summary):
        raise PublicExportError("summary_manifest_recompute_mismatch")
    for tier in benchmark_suite_gate.TIERS:
        tier_summary = summary_tiers[tier]
        failures = tier_summary.get("failures")
        metric_statuses = [tier_summary.get("metric_gates", {}).get(metric, {}).get("status") for metric in benchmark_suite_gate.GATED_METRICS]
        if tier_summary.get("failed_run_ids") != [] or tier_summary.get("status") != "pass" or tier_summary.get("optimization_status") not in {"pass", "fail"} or not isinstance(failures, list) or any(not isinstance(failure, str) or not failure for failure in failures) or failures or not isinstance(tier_summary.get("performance_diagnostics"), list) or any(not isinstance(failure, str) or not failure for failure in tier_summary["performance_diagnostics"]):
            raise PublicExportError("summary_tier_failure")
        if tier_summary.get("pair_count") != tier_repeat_counts[tier] or tier_summary.get("run_count") != tier_repeat_counts[tier] * 2:
            raise PublicExportError("summary_tier_count_failure")
        if any(status not in {"pass", "fail"} for status in metric_statuses):
            raise PublicExportError("summary_metric_gate_failure")
    return summary_tiers


def build_public_export(plan_path, summary_path, manifest_dir):
    plan, plan_bytes = load_json_object(plan_path, "plan_invalid")
    summary, _ = load_json_object(summary_path, "summary_invalid")
    try:
        benchmark_suite_gate.validate_plan(plan)
        tier_repeat_counts = benchmark_suite_gate.repeat_counts_from_plan(plan)
    except benchmark_suite_gate.BenchmarkGateError as error:
        raise PublicExportError(f"plan_{error.code}")
    if any(pair_count < MINIMUM_PUBLIC_PAIR_COUNT for pair_count in tier_repeat_counts.values()):
        raise PublicExportError("public_pair_count_below_minimum")
    plan_sha256 = sha256_bytes(plan_bytes)
    manifests = load_manifests(manifest_dir)
    expected_run_count = sum(tier_repeat_counts.values()) * 2
    if len(plan["runs"]) != expected_run_count or len(manifests) != expected_run_count:
        raise PublicExportError("evidence_run_count_mismatch")
    plan_runs = {run_plan["run_id"]: run_plan for run_plan in plan["runs"]}
    manifest_runs = {manifest.get("run_id"): manifest for manifest in manifests}
    if len(manifest_runs) != len(manifests) or set(manifest_runs) != set(plan_runs):
        raise PublicExportError("manifest_run_set_mismatch")
    regenerated_manifests = []
    for run_id, run_plan in plan_runs.items():
        stored_manifest = manifest_runs[run_id]
        validate_manifest(stored_manifest, run_plan, plan["suite_id"], plan_sha256)
        regenerated_manifest = benchmark_suite_gate.evaluate_run(plan_path.parent, plan["suite_id"], plan_sha256, run_plan)
        regenerated_producer_elapsed_ms = regenerated_manifest.get("producer_elapsed_ms")
        stored_ending_real_elapsed_ms = stored_manifest["ending_real_elapsed_ms"]
        if isinstance(regenerated_producer_elapsed_ms, bool) or not isinstance(regenerated_producer_elapsed_ms, int):
            raise PublicExportError("manifest_raw_recompute_mismatch")
        if stored_manifest["total_wall_elapsed_ms"] != regenerated_producer_elapsed_ms + stored_ending_real_elapsed_ms:
            raise PublicExportError("manifest_diagnostic_arithmetic_mismatch")
        regenerated_manifest["ending_real_elapsed_ms"] = stored_ending_real_elapsed_ms
        regenerated_manifest["total_wall_elapsed_ms"] = stored_manifest["total_wall_elapsed_ms"]
        if benchmark_suite_gate.canonical_json(stored_manifest) != benchmark_suite_gate.canonical_json(regenerated_manifest):
            raise PublicExportError("manifest_raw_recompute_mismatch")
        regenerated_manifests.append(regenerated_manifest)
    manifests = regenerated_manifests
    tier_summaries = validate_summary(summary, plan, manifests, plan_sha256, tier_repeat_counts)
    entry_pairs = {arm: {run_plan["selected_entry_pair"] for run_plan in plan["runs"] if run_plan["arm"] == arm} for arm in benchmark_suite_gate.ARMS}
    expected_entry_pairs = dict(benchmark_suite_gate.ARM_ENTRY_PAIRS)
    if any(entry_pairs[arm] != {expected_entry_pairs[arm]} for arm in benchmark_suite_gate.ARMS):
        raise PublicExportError("entry_pair_mismatch")
    configuration = validate_configuration_hashes(plan)
    execution_integrity = {
        "complete_runs": sum(manifest["completion"] == "complete" for manifest in manifests),
        "retry_count": sum(manifest["retry_count"] for manifest in manifests),
        "fallback_count": sum(manifest["fallback_count"] for manifest in manifests),
        "repair_count": sum(manifest["repair_count"] for manifest in manifests),
        "runtime_session_count": sum(manifest["runtime_session_count"] for manifest in manifests),
        "runtime_root_count": sum(manifest["runtime_root_session_count"] for manifest in manifests),
        "runtime_descendant_count": sum(manifest["runtime_descendant_session_count"] for manifest in manifests),
        "task_session_count": sum(manifest["task_session_count"] for manifest in manifests),
        "controller_tokens_excluded": sum(manifest["controller_tokens_excluded"] for manifest in manifests),
        "multi_session_run_count": sum(manifest["runtime_session_count"] > 1 for manifest in manifests),
        "sol_entry_probe_count": sum("dual_entry_probe" in run_plan for run_plan in plan["runs"]),
        "sol_entry_probe_pass_count": sum("dual_entry_probe" in run_plan for run_plan in plan["runs"]),
    }
    task_summaries = []
    for tier in benchmark_suite_gate.TIERS:
        tier_summary = tier_summaries[tier]
        task_summaries.append({"tier": tier, "label": TASK_LABELS[tier], "status": tier_summary["status"], "optimization_status": tier_summary["optimization_status"], "failures": tier_summary["failures"], "performance_diagnostics": tier_summary["performance_diagnostics"], "pair_count": tier_summary["pair_count"], "run_count": tier_summary["run_count"], "direct_totals": tier_summary["direct_totals"], "global_totals": tier_summary["global_totals"], "direct_medians": tier_summary["direct_medians"], "global_medians": tier_summary["global_medians"], "paired_savings_percent_medians": tier_summary["paired_savings_percent_medians"], "paired_wins": tier_summary["paired_wins"], "metric_gates": tier_summary["metric_gates"]})
    cohort_result = "passing" if summary["overall_status"] == "pass" else "failed strategy-performance"
    public_document = {"schema_version": PUBLIC_SCHEMA_VERSION, "evidence_scope": "sanitized frozen real Direct versus Auto empirical cohort", "suite_id": plan["suite_id"], "plan_sha256": plan_sha256, "overall_status": summary["overall_status"], "all_correct": summary["all_correct"], "expected_run_count": expected_run_count, "entry_pairs": expected_entry_pairs, "tier_repeat_counts": tier_repeat_counts, "rules": {"tokens": TOKEN_RULE, "time": TIME_RULE, "overall": OVERALL_RULE, "minimum_pairs_per_tier": MINIMUM_PUBLIC_PAIR_COUNT}, "configuration": configuration, "execution_integrity": execution_integrity, "cohort_metric_gates": summary["cohort_metric_gates"], "tasks": task_summaries, "caveats": {"tokens": "Primary token savings use the clean steady-state selected execution. Entry matching, calibration failures, retries, fallbacks, repairs, and Ending are separately counted and are not hidden. Logical tokens are a usage proxy, not a billing-token or price claim.", "first_result": "First-result time is the actual end-to-end wait diagnostic. It is never advertised as saved unless its own values are lower; Ending is reported separately.", "generalization": f"This is a {cohort_result} empirical cohort for these frozen workloads and conditions, not a universal guarantee for every task or future runtime."}}
    validate_public_privacy(public_document, private_strings_from_evidence(plan, manifests))
    return public_document


def export_public_json(plan_path, summary_path, manifest_dir, output_path):
    public_document = build_public_export(plan_path, summary_path, manifest_dir)
    atomic_write_public_json(output_path, public_document)
    return public_document


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Export sanitized public JSON only after a frozen benchmark suite passes every evidence gate.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        public_document = export_public_json(args.plan, args.summary, args.manifests, args.output)
    except PublicExportError as error:
        print(json.dumps({"schema_version": 1, "status": "error", "failure": error.code}, separators=(",", ":")))
        return 1
    print(json.dumps({"schema_version": 1, "status": "pass", "output": str(args.output), "suite_id": public_document["suite_id"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
