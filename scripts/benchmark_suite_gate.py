#!/usr/bin/env python3
"""Generate receipt-backed benchmark manifests and strict paired summaries."""

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import importlib.util
from pathlib import Path, PureWindowsPath
from tempfile import mkstemp


try:
    from benchmark_prompt_contract import auto_benchmark_execution_prompt
except ModuleNotFoundError:
    _benchmark_prompt_path = Path(__file__).with_name("benchmark_prompt_contract.py")
    _benchmark_prompt_spec = importlib.util.spec_from_file_location("task_analyze_benchmark_prompt_contract", _benchmark_prompt_path)
    _benchmark_prompt_contract = importlib.util.module_from_spec(_benchmark_prompt_spec)
    _benchmark_prompt_spec.loader.exec_module(_benchmark_prompt_contract)
    auto_benchmark_execution_prompt = _benchmark_prompt_contract.auto_benchmark_execution_prompt


SCHEMA_VERSION = 6
MANIFEST_SCHEMA_VERSION = 7
CATALOG_SCHEMA_VERSION = 1
MINIMUM_PAIRED_SAVINGS_PERCENT = 0.0
MINIMUM_PAIRED_TIME_SAVINGS_PERCENT = MINIMUM_PAIRED_SAVINGS_PERCENT
MAXIMUM_PAIRED_REGRESSION_PERCENT = 5.0
MAXIMUM_PAIRED_TIME_REGRESSION_MS = 2_000
CATALOG_REFRESH_GRACE_SECONDS = 20.0
CATALOG_REFRESH_RECENCY_SECONDS = 60.0
CATALOG_STABILITY_RETRY_SECONDS = 0.25
TIERS = ("simple", "medium", "complex")
ARMS = ("direct", "global")
DIRECT_BASELINE_PAIR = "gpt-5.6-sol|ultra"
AUTO_ENTRY_PAIR = "gpt-5.6-luna|max"
ARM_ENTRY_PAIRS = {"direct": DIRECT_BASELINE_PAIR, "global": AUTO_ENTRY_PAIR}
GATED_METRICS = ("steady_state_logical_tokens", "steady_state_execution_elapsed_ms")
OVERALL_RULE = "all task and Ending correctness PASS AND aggregate steady-state execution lower AND aggregate steady-state logical tokens lower"
TOKEN_RULE = "Primary token savings use steady_state_logical_tokens only: Direct uses its fixed Sol Ultra producer receipt; Auto uses the verified frozen adaptive bridge selected child/graph receipt. Entry-controller, matching, calibration failures, retries, fallbacks, repairs, and Ending are excluded from the primary metric and remain separately reported. Overall PASS requires the aggregate Auto cohort total to be lower than Direct; per-tier medians, paired savings, and regressions remain visible diagnostics"
TIME_RULE = "Primary time savings use steady_state_execution_elapsed_ms only: Direct uses fixed Sol Ultra producer execution; Auto uses verified frozen adaptive bridge selected execution or graph critical path. Overall PASS requires the aggregate Auto cohort total to be lower than Direct. first_result_elapsed_ms remains an end-to-end diagnostic and is never advertised as saved unless separately lower; route selection, calibration, and Ending remain separate diagnostics"
ENTRY_EXECUTION_MODES = frozenset({"executed", "deterministic-pre-model"})
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PLAN_COMMON_KEYS = frozenset({"schema_version", "suite_id", "runs"})
PLAN_LEGACY_KEYS = PLAN_COMMON_KEYS | {"repeat_count"}
PLAN_TIER_KEYS = PLAN_COMMON_KEYS | {"tier_repeat_counts"}
RUN_REQUIRED_KEYS = frozenset({"run_id", "pair_id", "tier", "repeat_index", "arm", "order_index", "prompt_sha256", "expected_result_path", "expected_sha256", "result_path", "evidence_path", "receipts", "selected_entry_pair", "entry_execution_mode"})
RUN_OPTIONAL_KEYS = frozenset({"prompt_path", "source_root", "source_files_pointer", "source_snapshot_sha256", "environment", "dual_entry_probe"})
RECEIPT_REQUIRED_KEYS = frozenset({"path", "pair", "role", "bind_result", "workload_prompt_sha256"})
RECEIPT_OPTIONAL_KEYS = frozenset()
EVIDENCE_KEYS = frozenset({"schema_version", "run_id", "started_monotonic_ns", "first_result_monotonic_ns", "producer_finished_monotonic_ns", "producer_process_exit_code", "producer_timed_out", "producer_complete", "foreground_main_thread_id", "foreground_state_snapshot", "foreground_sessions", "launched_session_ids", "retry_session_ids", "fallback_session_ids", "repair_session_ids", "state_snapshot", "runtime_sessions"})
STATE_SNAPSHOT_KEYS = frozenset({"before_complete", "after_complete", "before_thread_count", "after_thread_count", "before_thread_ids_sha256", "after_thread_ids_sha256"})
FOREGROUND_SESSION_KEYS = frozenset({"thread_id", "parent_thread_id", "source_kind", "model", "effort", "tokens_used"})
RUNTIME_SESSION_KEYS = frozenset({"thread_id", "parent_thread_id", "source_kind", "model", "effort", "tokens_used", "rollout_sha256", "rollout_model", "rollout_effort", "rollout_total_tokens", "turn_completed"})
MARKETPLACE_SOURCE_KEYS = frozenset({"name", "root", "sha256", "file_count"})
CATALOG_ENVIRONMENT_KEYS = frozenset({"catalog_schema_version", "skills_catalog_root", "skills_catalog_sha256", "skills_catalog_file_count", "plugins_catalog_root", "plugins_catalog_sha256", "plugins_catalog_file_count", "marketplace_catalog_sources", "marketplace_catalog_sha256", "marketplace_catalog_file_count", "visible_catalog_sha256"})
CATALOG_PAIR_FIELDS = ("catalog_schema_version", "skills_catalog_sha256", "skills_catalog_file_count", "plugins_catalog_sha256", "plugins_catalog_file_count", "marketplace_catalog_sha256", "marketplace_catalog_file_count", "visible_catalog_sha256")
RUNTIME_CONTEXT_PAIR_FIELDS = ("models_cache_sha256", "memories_sha256")
ENVIRONMENT_KEYS = frozenset({"codex_home", "config_path", "config_sha256", "agents_path", "agents_sha256", "models_cache_path", "models_cache_sha256", "memories_root", "memories_sha256", "workdir", "sandbox", "receipt_runner_path", "receipt_runner_sha256"}) | CATALOG_ENVIRONMENT_KEYS
ENDING_REAL_METHOD = "post-result-deterministic-exact-source-receipt-session-gate"
SOL_ENTRY_PROBE_PAIR = DIRECT_BASELINE_PAIR
DUAL_ENTRY_PROBE_KEYS = frozenset({"schema_version", "entry_pair", "receipt_path", "result_path", "summary_path"})
DUAL_ENTRY_PROBE_SUMMARY_KEYS = frozenset({"schema_version", "status", "reason", "tier", "primary_run_id", "primary_entry_pair", "probe_entry_pair", "expected_result_sha256", "primary_result_sha256", "probe_result_sha256", "primary_route_signature", "probe_route_signature", "route_signature_match", "capability_assignment_match"})


class BenchmarkGateError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def reject_duplicate_json_keys(pairs):
    parsed_object = {}
    for key, value in pairs:
        if key in parsed_object:
            raise ValueError("duplicate_json_key")
        parsed_object[key] = value
    return parsed_object


def reject_nonstandard_json_constant(_value):
    raise ValueError("nonstandard_json_constant")


def strict_json_loads(payload):
    return json.loads(payload, object_pairs_hook=reject_duplicate_json_keys, parse_constant=reject_nonstandard_json_constant)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_toml_string(value):
    value = value.strip()
    double_quoted_match = re.fullmatch(r'("(?:\\.|[^"\\])*")\s*(?:#.*)?', value)
    if double_quoted_match is not None:
        try:
            parsed_value = json.loads(double_quoted_match.group(1))
        except json.JSONDecodeError:
            raise BenchmarkGateError("marketplace_config_invalid")
        if not isinstance(parsed_value, str):
            raise BenchmarkGateError("marketplace_config_invalid")
        return parsed_value
    single_quoted_match = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
    if single_quoted_match is not None:
        return single_quoted_match.group(1)
    raise BenchmarkGateError("marketplace_config_invalid")


def configured_marketplace_sources(config_path):
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise BenchmarkGateError("marketplace_config_unreadable")
    marketplace_values = {}
    current_marketplace = None
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            marketplace_match = re.fullmatch(r"\[marketplaces\.([A-Za-z0-9._-]+)\]\s*(?:#.*)?", line)
            if line.startswith("[marketplaces.") and marketplace_match is None:
                raise BenchmarkGateError("marketplace_config_invalid")
            current_marketplace = marketplace_match.group(1) if marketplace_match is not None else None
            if current_marketplace is not None:
                if current_marketplace in marketplace_values:
                    raise BenchmarkGateError("marketplace_config_duplicate")
                marketplace_values[current_marketplace] = {}
            continue
        if current_marketplace is None:
            continue
        assignment_match = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if assignment_match is None:
            raise BenchmarkGateError("marketplace_config_invalid")
        key, raw_value = assignment_match.groups()
        if key in {"source", "source_type"}:
            if key in marketplace_values[current_marketplace]:
                raise BenchmarkGateError("marketplace_config_duplicate")
            marketplace_values[current_marketplace][key] = parse_toml_string(raw_value)
    sources = []
    for name in sorted(marketplace_values):
        marketplace = marketplace_values[name]
        if marketplace.get("source_type") != "local" or not marketplace.get("source"):
            raise BenchmarkGateError("marketplace_source_unsupported")
        source_path = Path(marketplace["source"])
        source_path = source_path if source_path.is_absolute() else config_path.parent / source_path
        sources.append((name, source_path))
    return sources


def is_catalog_file(relative_path):
    parts = relative_path.parts
    return relative_path.name == "SKILL.md" or len(parts) >= 2 and parts[-2:] == ("agents", "openai.yaml") or len(parts) >= 2 and parts[-2:] == (".codex-plugin", "plugin.json") or relative_path.name in {"marketplace.json", "api_marketplace.json", "marketplace.yaml", "marketplace.yml"}


def hash_catalog_root(root_path):
    try:
        resolved_root = root_path.resolve(strict=True)
    except OSError:
        raise BenchmarkGateError("catalog_root_unreadable")
    if not resolved_root.is_dir():
        raise BenchmarkGateError("catalog_root_invalid")
    try:
        candidates = sorted(resolved_root.rglob("*"), key=lambda path: path.relative_to(resolved_root).as_posix())
    except (OSError, ValueError):
        raise BenchmarkGateError("catalog_root_unreadable")
    selected_files = []
    resolved_files = set()
    for candidate in candidates:
        try:
            relative_path = candidate.relative_to(resolved_root)
        except ValueError:
            raise BenchmarkGateError("catalog_path_escape")
        if not is_catalog_file(relative_path):
            continue
        if candidate.is_symlink():
            try:
                resolved_candidate = candidate.resolve(strict=True)
                resolved_candidate.relative_to(resolved_root)
            except (OSError, ValueError):
                raise BenchmarkGateError("catalog_path_escape")
            raise BenchmarkGateError("catalog_path_duplicate")
        try:
            resolved_candidate = candidate.resolve(strict=True)
            resolved_candidate.relative_to(resolved_root)
            if not resolved_candidate.is_file():
                raise BenchmarkGateError("catalog_file_invalid")
            source_bytes = resolved_candidate.read_bytes()
        except BenchmarkGateError:
            raise
        except (OSError, ValueError):
            raise BenchmarkGateError("catalog_file_unreadable")
        resolved_text = str(resolved_candidate)
        if resolved_text in resolved_files:
            raise BenchmarkGateError("catalog_path_duplicate")
        resolved_files.add(resolved_text)
        selected_files.append((relative_path.as_posix(), source_bytes))
    digest = hashlib.sha256()
    for relative_text, source_bytes in selected_files:
        relative_bytes = relative_text.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(source_bytes).to_bytes(8, "big"))
        digest.update(source_bytes)
    return {"root": str(resolved_root), "sha256": digest.hexdigest(), "file_count": len(selected_files)}


def marketplace_catalog_hash(sources):
    sanitized_sources = [{"name": source["name"], "sha256": source["sha256"], "file_count": source["file_count"]} for source in sources]
    return sha256_text(canonical_json(sanitized_sources))


def visible_catalog_hash(skills_sha256, skills_file_count, plugins_sha256, plugins_file_count, marketplace_sha256, marketplace_file_count):
    catalog_identity = {"catalog_schema_version": CATALOG_SCHEMA_VERSION, "skills": {"sha256": skills_sha256, "file_count": skills_file_count}, "plugins": {"sha256": plugins_sha256, "file_count": plugins_file_count}, "marketplaces": {"sha256": marketplace_sha256, "file_count": marketplace_file_count}}
    return sha256_text(canonical_json(catalog_identity))


def catalog_snapshot(codex_home, config_path):
    skills_catalog = hash_catalog_root(codex_home / "skills")
    plugins_catalog = hash_catalog_root(codex_home / "plugins")
    resolved_roots = {skills_catalog["root"], plugins_catalog["root"]}
    if len(resolved_roots) != 2:
        raise BenchmarkGateError("catalog_root_duplicate")
    marketplace_sources = []
    for name, source_path in configured_marketplace_sources(config_path):
        source_catalog = hash_catalog_root(source_path)
        if source_catalog["root"] in resolved_roots:
            raise BenchmarkGateError("catalog_root_duplicate")
        resolved_roots.add(source_catalog["root"])
        marketplace_sources.append({"name": name, **source_catalog})
    marketplace_sha256 = marketplace_catalog_hash(marketplace_sources)
    marketplace_file_count = sum(source["file_count"] for source in marketplace_sources)
    visible_sha256 = visible_catalog_hash(skills_catalog["sha256"], skills_catalog["file_count"], plugins_catalog["sha256"], plugins_catalog["file_count"], marketplace_sha256, marketplace_file_count)
    return {"catalog_schema_version": CATALOG_SCHEMA_VERSION, "skills_catalog_root": skills_catalog["root"], "skills_catalog_sha256": skills_catalog["sha256"], "skills_catalog_file_count": skills_catalog["file_count"], "plugins_catalog_root": plugins_catalog["root"], "plugins_catalog_sha256": plugins_catalog["sha256"], "plugins_catalog_file_count": plugins_catalog["file_count"], "marketplace_catalog_sources": marketplace_sources, "marketplace_catalog_sha256": marketplace_sha256, "marketplace_catalog_file_count": marketplace_file_count, "visible_catalog_sha256": visible_sha256}


def sha256_source_tree(source_root):
    try:
        resolved_root = source_root.resolve(strict=True)
    except OSError:
        raise BenchmarkGateError("source_root_invalid")
    if not resolved_root.is_dir():
        raise BenchmarkGateError("source_root_invalid")
    digest = hashlib.sha256()
    source_paths = sorted(path for path in resolved_root.rglob("*") if path.is_file())
    for source_path in source_paths:
        if source_path.is_symlink():
            raise BenchmarkGateError("source_snapshot_symlink")
        try:
            relative_path = source_path.relative_to(resolved_root).as_posix().encode("utf-8")
            source_bytes = source_path.read_bytes()
        except (OSError, ValueError):
            raise BenchmarkGateError("source_snapshot_unreadable")
        digest.update(len(relative_path).to_bytes(8, "big"))
        digest.update(relative_path)
        digest.update(len(source_bytes).to_bytes(8, "big"))
        digest.update(source_bytes)
    return digest.hexdigest()


def atomic_write_json(path, value):
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


def load_json_object(path, failure_code):
    try:
        payload = path.read_bytes()
        parsed_value = strict_json_loads(payload)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise BenchmarkGateError(failure_code)
    if not isinstance(parsed_value, dict):
        raise BenchmarkGateError(failure_code)
    return parsed_value, payload


def models_cache_sha256(path):
    models_cache, _ = load_json_object(path, "models_cache_invalid")
    semantic_cache = dict(models_cache)
    semantic_cache.pop("fetched_at", None)
    return sha256_text(canonical_json(semantic_cache))


def resolve_plan_path(plan_root, path_text, failure_code):
    if not isinstance(path_text, str) or not path_text:
        raise BenchmarkGateError(failure_code)
    candidate = Path(path_text)
    return candidate if candidate.is_absolute() else plan_root / candidate


def require_exact_keys(value, required_keys, optional_keys, failure_code):
    if set(value) != set(required_keys) | (set(value) & set(optional_keys)) or not set(required_keys).issubset(value):
        raise BenchmarkGateError(failure_code)


def require_string(value, failure_code):
    if not isinstance(value, str) or not value:
        raise BenchmarkGateError(failure_code)
    return value


def require_sha256(value, failure_code):
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise BenchmarkGateError(failure_code)
    return value


def require_integer(value, failure_code, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        raise BenchmarkGateError(failure_code)
    return value


def require_string_list(value, failure_code):
    if not isinstance(value, list) or any(not isinstance(entry, str) or not entry for entry in value) or len(value) != len(set(value)):
        raise BenchmarkGateError(failure_code)
    return value


def validate_receipt_spec(receipt_spec):
    if not isinstance(receipt_spec, dict):
        raise BenchmarkGateError("plan_receipt_invalid")
    require_exact_keys(receipt_spec, RECEIPT_REQUIRED_KEYS, RECEIPT_OPTIONAL_KEYS, "plan_receipt_keys")
    require_string(receipt_spec["path"], "plan_receipt_path")
    pair = require_string(receipt_spec["pair"], "plan_receipt_pair")
    if "|" not in pair:
        raise BenchmarkGateError("plan_receipt_pair")
    require_string(receipt_spec["role"], "plan_receipt_role")
    if not isinstance(receipt_spec["bind_result"], bool):
        raise BenchmarkGateError("plan_receipt_bind_result")
    require_sha256(receipt_spec["workload_prompt_sha256"], "plan_receipt_workload_hash")


def validate_environment_plan(environment):
    if not isinstance(environment, dict) or set(environment) != ENVIRONMENT_KEYS:
        raise BenchmarkGateError("plan_environment_contract")
    path_values = {}
    for path_key in ["codex_home", "config_path", "agents_path", "models_cache_path", "memories_root", "workdir", "receipt_runner_path", "skills_catalog_root", "plugins_catalog_root"]:
        path_text = require_string(environment[path_key], "plan_environment_path")
        path_value = Path(path_text)
        if not path_value.is_absolute():
            raise BenchmarkGateError("plan_environment_path")
        path_values[path_key] = path_value
    if path_values["config_path"] != path_values["codex_home"] / "config.toml" or path_values["agents_path"] != path_values["codex_home"] / "AGENTS.md" or path_values["models_cache_path"] != path_values["codex_home"] / "models_cache.json" or path_values["memories_root"] != path_values["codex_home"] / "memories":
        raise BenchmarkGateError("plan_environment_home_paths")
    for hash_key in ["config_sha256", "agents_sha256", "models_cache_sha256", "memories_sha256", "receipt_runner_sha256", "skills_catalog_sha256", "plugins_catalog_sha256", "marketplace_catalog_sha256", "visible_catalog_sha256"]:
        require_sha256(environment[hash_key], "plan_environment_hash")
    if environment["sandbox"] not in {"read-only", "workspace-write", "danger-full-access"}:
        raise BenchmarkGateError("plan_environment_sandbox")
    if environment["catalog_schema_version"] != CATALOG_SCHEMA_VERSION:
        raise BenchmarkGateError("plan_catalog_schema")
    for count_key in ["skills_catalog_file_count", "plugins_catalog_file_count", "marketplace_catalog_file_count"]:
        require_integer(environment[count_key], "plan_catalog_file_count", 0)
    sources = environment["marketplace_catalog_sources"]
    if not isinstance(sources, list):
        raise BenchmarkGateError("plan_marketplace_sources")
    source_names = []
    source_roots = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != MARKETPLACE_SOURCE_KEYS:
            raise BenchmarkGateError("plan_marketplace_source_contract")
        source_names.append(require_string(source["name"], "plan_marketplace_source_name"))
        source_root = Path(require_string(source["root"], "plan_marketplace_source_root"))
        if not source_root.is_absolute():
            raise BenchmarkGateError("plan_marketplace_source_root")
        source_roots.append(str(source_root))
        require_sha256(source["sha256"], "plan_marketplace_source_hash")
        require_integer(source["file_count"], "plan_marketplace_source_count", 0)
    if source_names != sorted(source_names) or len(source_names) != len(set(source_names)):
        raise BenchmarkGateError("plan_marketplace_source_duplicate")
    all_catalog_roots = [str(path_values["skills_catalog_root"]), str(path_values["plugins_catalog_root"]), *source_roots]
    if len(all_catalog_roots) != len(set(all_catalog_roots)):
        raise BenchmarkGateError("plan_catalog_root_duplicate")
    if sum(source["file_count"] for source in sources) != environment["marketplace_catalog_file_count"] or marketplace_catalog_hash(sources) != environment["marketplace_catalog_sha256"]:
        raise BenchmarkGateError("plan_marketplace_catalog_mismatch")
    expected_visible_sha256 = visible_catalog_hash(environment["skills_catalog_sha256"], environment["skills_catalog_file_count"], environment["plugins_catalog_sha256"], environment["plugins_catalog_file_count"], environment["marketplace_catalog_sha256"], environment["marketplace_catalog_file_count"])
    if expected_visible_sha256 != environment["visible_catalog_sha256"]:
        raise BenchmarkGateError("plan_visible_catalog_mismatch")


def repeat_counts_from_plan(plan):
    if set(plan) == PLAN_LEGACY_KEYS:
        repeat_count = require_integer(plan["repeat_count"], "plan_repeat_count", 2)
        if repeat_count % 2 != 0:
            raise BenchmarkGateError("plan_repeat_count")
        return {tier: repeat_count for tier in TIERS}
    if set(plan) == PLAN_TIER_KEYS:
        tier_repeat_counts = plan["tier_repeat_counts"]
        if not isinstance(tier_repeat_counts, dict) or set(tier_repeat_counts) != set(TIERS):
            raise BenchmarkGateError("plan_tier_repeat_counts")
        for tier in TIERS:
            repeat_count = require_integer(tier_repeat_counts[tier], "plan_tier_repeat_counts", 2)
            if repeat_count % 2 != 0:
                raise BenchmarkGateError("plan_tier_repeat_counts")
        return dict(tier_repeat_counts)
    raise BenchmarkGateError("plan_contract")


def validate_run_plan(run_plan, repeat_limit):
    if not isinstance(run_plan, dict):
        raise BenchmarkGateError("plan_run_invalid")
    require_exact_keys(run_plan, RUN_REQUIRED_KEYS, RUN_OPTIONAL_KEYS, "plan_run_keys")
    run_id = require_string(run_plan["run_id"], "plan_run_id")
    pair_id = require_string(run_plan["pair_id"], "plan_pair_id")
    if RUN_ID_PATTERN.fullmatch(run_id) is None or RUN_ID_PATTERN.fullmatch(pair_id) is None:
        raise BenchmarkGateError("plan_run_id")
    if run_plan["tier"] not in TIERS or run_plan["arm"] not in ARMS:
        raise BenchmarkGateError("plan_run_classification")
    repeat_index = require_integer(run_plan["repeat_index"], "plan_repeat_index", 1)
    if repeat_index > repeat_limit:
        raise BenchmarkGateError("plan_repeat_index")
    require_integer(run_plan["order_index"], "plan_order_index", 1)
    require_sha256(run_plan["prompt_sha256"], "plan_prompt_hash")
    if "prompt_path" in run_plan:
        require_string(run_plan["prompt_path"], "plan_prompt_path")
    require_sha256(run_plan["expected_sha256"], "plan_expected_hash")
    for path_key in ["expected_result_path", "result_path", "evidence_path"]:
        require_string(run_plan[path_key], f"plan_{path_key}")
    require_string(run_plan["selected_entry_pair"], "plan_selected_entry_pair")
    if "|" not in run_plan["selected_entry_pair"] or run_plan["entry_execution_mode"] not in ENTRY_EXECUTION_MODES:
        raise BenchmarkGateError("plan_entry_contract")
    if run_plan["selected_entry_pair"] != ARM_ENTRY_PAIRS[run_plan["arm"]]:
        raise BenchmarkGateError("plan_arm_entry_pair")
    if not isinstance(run_plan["receipts"], list) or not 1 <= len(run_plan["receipts"]) <= 4:
        raise BenchmarkGateError("plan_receipts_invalid")
    for receipt_spec in run_plan["receipts"]:
        validate_receipt_spec(receipt_spec)
    receipt_paths = [receipt_spec["path"] for receipt_spec in run_plan["receipts"]]
    if len(receipt_paths) != len(set(receipt_paths)):
        raise BenchmarkGateError("plan_receipt_path_duplicate")
    bound_receipts = [receipt_spec for receipt_spec in run_plan["receipts"] if receipt_spec["bind_result"] is True]
    if len(bound_receipts) != 1:
        raise BenchmarkGateError("plan_result_receipt_missing")
    producer_receipt = bound_receipts[0]
    if run_plan["entry_execution_mode"] == "executed" and producer_receipt["pair"] != run_plan["selected_entry_pair"]:
        raise BenchmarkGateError("plan_entry_receipt_missing")
    if run_plan["entry_execution_mode"] != "executed":
        raise BenchmarkGateError("plan_inline_route")
    if producer_receipt["role"] != "result-producer" or producer_receipt["workload_prompt_sha256"] != run_plan["prompt_sha256"]:
        raise BenchmarkGateError("plan_inline_route")
    if any(receipt_spec["bind_result"] is not False or receipt_spec["role"] not in {"ending", "verification"} for receipt_spec in run_plan["receipts"] if receipt_spec is not producer_receipt):
        raise BenchmarkGateError("plan_inline_route")
    if "source_root" in run_plan or "source_files_pointer" in run_plan or "source_snapshot_sha256" in run_plan:
        if not isinstance(run_plan.get("source_root"), str) or not run_plan.get("source_root") or not isinstance(run_plan.get("source_files_pointer"), str) or not run_plan.get("source_files_pointer").startswith("/"):
            raise BenchmarkGateError("plan_source_contract")
        require_sha256(run_plan.get("source_snapshot_sha256"), "plan_source_snapshot_hash")
    if "environment" in run_plan:
        validate_environment_plan(run_plan["environment"])
    dual_entry_probe = run_plan.get("dual_entry_probe")
    probe_required = run_plan["arm"] == "global" and repeat_index == 1
    if probe_required != isinstance(dual_entry_probe, dict):
        raise BenchmarkGateError("plan_dual_entry_probe")
    if dual_entry_probe is not None:
        require_exact_keys(dual_entry_probe, DUAL_ENTRY_PROBE_KEYS, frozenset(), "plan_dual_entry_probe")
        if dual_entry_probe["schema_version"] != 1 or dual_entry_probe["entry_pair"] != SOL_ENTRY_PROBE_PAIR:
            raise BenchmarkGateError("plan_dual_entry_probe")
        for path_key in ["receipt_path", "result_path", "summary_path"]:
            require_string(dual_entry_probe[path_key], "plan_dual_entry_probe")


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkGateError("plan_contract")
    tier_repeat_counts = repeat_counts_from_plan(plan)
    suite_id = require_string(plan["suite_id"], "plan_suite_id")
    if RUN_ID_PATTERN.fullmatch(suite_id) is None:
        raise BenchmarkGateError("plan_suite_id")
    if not isinstance(plan["runs"], list):
        raise BenchmarkGateError("plan_runs_invalid")
    for run_plan in plan["runs"]:
        validate_run_plan(run_plan, tier_repeat_counts[run_plan.get("tier")] if run_plan.get("tier") in TIERS else 0)
    run_ids = [run_plan["run_id"] for run_plan in plan["runs"]]
    order_indices = [run_plan["order_index"] for run_plan in plan["runs"]]
    if len(run_ids) != len(set(run_ids)) or len(order_indices) != len(set(order_indices)):
        raise BenchmarkGateError("plan_run_duplicate")
    expected_run_count = sum(tier_repeat_counts.values()) * len(ARMS)
    if len(plan["runs"]) != expected_run_count:
        raise BenchmarkGateError("plan_run_count")
    for tier in TIERS:
        for repeat_index in range(1, tier_repeat_counts[tier] + 1):
            pair_runs = [run_plan for run_plan in plan["runs"] if run_plan["tier"] == tier and run_plan["repeat_index"] == repeat_index]
            if len(pair_runs) != 2 or {run_plan["arm"] for run_plan in pair_runs} != set(ARMS) or len({run_plan["pair_id"] for run_plan in pair_runs}) != 1:
                raise BenchmarkGateError("plan_pair_contract")
            direct_run = next(run_plan for run_plan in pair_runs if run_plan["arm"] == "direct")
            global_run = next(run_plan for run_plan in pair_runs if run_plan["arm"] == "global")
            comparable_fields = ["prompt_path", "prompt_sha256", "expected_result_path", "expected_sha256", "source_root", "source_files_pointer", "source_snapshot_sha256"]
            if any(direct_run.get(field) != global_run.get(field) for field in comparable_fields):
                raise BenchmarkGateError("plan_pair_mismatch")
            direct_environment = direct_run.get("environment")
            global_environment = global_run.get("environment")
            if (direct_environment is None) != (global_environment is None):
                raise BenchmarkGateError("plan_pair_environment_mismatch")
            if direct_environment is not None:
                common_environment_fields = ["config_sha256", *RUNTIME_CONTEXT_PAIR_FIELDS, "workdir", "sandbox", "receipt_runner_path", "receipt_runner_sha256"]
                if any(direct_environment[field] != global_environment[field] for field in common_environment_fields):
                    raise BenchmarkGateError("plan_pair_environment_mismatch")
                if any(direct_environment[field] != global_environment[field] for field in CATALOG_PAIR_FIELDS):
                    raise BenchmarkGateError("plan_pair_catalog_mismatch")
            direct_first = direct_run["order_index"] < global_run["order_index"]
            if direct_first != (repeat_index % 2 == 1):
                raise BenchmarkGateError("plan_order_not_alternating")
    return plan


def decode_json_pointer(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise BenchmarkGateError("source_pointer_invalid")
    tokens = []
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise BenchmarkGateError("source_pointer_invalid")
        tokens.append(raw_token.replace("~1", "/").replace("~0", "~"))
    return tokens


def resolve_json_pointer(document, pointer):
    current_value = document
    for token in decode_json_pointer(pointer):
        if isinstance(current_value, dict) and token in current_value:
            current_value = current_value[token]
        elif isinstance(current_value, list) and token.isdigit() and int(token) < len(current_value):
            current_value = current_value[int(token)]
        else:
            raise BenchmarkGateError("source_pointer_missing")
    return current_value


def validate_source_files(document, source_root, source_files_pointer):
    try:
        resolved_root = source_root.resolve(strict=True)
    except OSError:
        raise BenchmarkGateError("source_root_invalid")
    if not resolved_root.is_dir():
        raise BenchmarkGateError("source_root_invalid")
    source_files = resolve_json_pointer(document, source_files_pointer)
    source_files = [source_files] if isinstance(source_files, str) else source_files
    if not isinstance(source_files, list) or any(not isinstance(source_file, str) or not source_file for source_file in source_files):
        raise BenchmarkGateError("source_files_invalid")
    for source_file in source_files:
        relative_path = Path(source_file)
        if relative_path.is_absolute() or PureWindowsPath(source_file).is_absolute() or ".." in relative_path.parts:
            raise BenchmarkGateError("source_outside_root")
        try:
            resolved_source = (resolved_root / relative_path).resolve(strict=True)
            resolved_source.relative_to(resolved_root)
        except (OSError, ValueError):
            raise BenchmarkGateError("source_outside_root")
        if not resolved_source.is_file():
            raise BenchmarkGateError("source_outside_root")
    return len(source_files)


def read_exact_json_result(path, failure_code):
    try:
        result_bytes = path.read_bytes()
        document = strict_json_loads(result_bytes)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise BenchmarkGateError(failure_code)
    if not isinstance(document, dict):
        raise BenchmarkGateError(failure_code)
    return document, result_bytes


def validate_state_snapshot(state_snapshot, failure_code):
    if not isinstance(state_snapshot, dict) or set(state_snapshot) != STATE_SNAPSHOT_KEYS or not isinstance(state_snapshot["before_complete"], bool) or not isinstance(state_snapshot["after_complete"], bool):
        raise BenchmarkGateError(failure_code)
    for snapshot_side in ["before", "after"]:
        complete = state_snapshot[f"{snapshot_side}_complete"]
        count = state_snapshot[f"{snapshot_side}_thread_count"]
        digest = state_snapshot[f"{snapshot_side}_thread_ids_sha256"]
        if complete:
            require_integer(count, failure_code, 0)
            require_sha256(digest, failure_code)
        elif count is not None or digest is not None:
            raise BenchmarkGateError(failure_code)
    return state_snapshot


def validate_evidence(evidence, run_id, arm):
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS or evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("run_id") != run_id:
        raise BenchmarkGateError("evidence_contract")
    for timestamp_key in ["started_monotonic_ns", "first_result_monotonic_ns", "producer_finished_monotonic_ns"]:
        require_integer(evidence[timestamp_key], "evidence_timing", 0)
    require_integer(evidence["producer_process_exit_code"], "evidence_exit_code")
    if not isinstance(evidence["producer_timed_out"], bool) or not isinstance(evidence["producer_complete"], bool):
        raise BenchmarkGateError("evidence_completion")
    if evidence["producer_complete"] != (evidence["producer_process_exit_code"] == 0 and not evidence["producer_timed_out"]):
        raise BenchmarkGateError("evidence_completion")
    foreground_main_thread_id = require_string(evidence["foreground_main_thread_id"], "evidence_foreground_main_thread_id")
    for session_key in ["launched_session_ids", "retry_session_ids", "fallback_session_ids", "repair_session_ids"]:
        require_string_list(evidence[session_key], f"evidence_{session_key}")
    launched_sessions = set(evidence["launched_session_ids"])
    if len(launched_sessions) != len(evidence["launched_session_ids"]):
        raise BenchmarkGateError("evidence_session_duplicate")
    if any(not set(evidence[session_key]).issubset(launched_sessions) for session_key in ["retry_session_ids", "fallback_session_ids", "repair_session_ids"]):
        raise BenchmarkGateError("evidence_session_classification")
    state_snapshot = validate_state_snapshot(evidence["state_snapshot"], "evidence_state_snapshot")
    foreground_state_snapshot = validate_state_snapshot(evidence["foreground_state_snapshot"], "evidence_foreground_state_snapshot")
    if foreground_state_snapshot["before_complete"] is not True or foreground_state_snapshot["after_complete"] is not True:
        raise BenchmarkGateError("evidence_foreground_state_snapshot_incomplete")
    if state_snapshot["before_complete"] and any(foreground_state_snapshot[key] != state_snapshot[key] for key in ["before_complete", "before_thread_count", "before_thread_ids_sha256"]):
        raise BenchmarkGateError("evidence_foreground_state_snapshot_mismatch")
    foreground_sessions = evidence["foreground_sessions"]
    if not isinstance(foreground_sessions, list):
        raise BenchmarkGateError("evidence_foreground_sessions")
    foreground_thread_ids = []
    for foreground_session in foreground_sessions:
        if not isinstance(foreground_session, dict) or set(foreground_session) != FOREGROUND_SESSION_KEYS:
            raise BenchmarkGateError("evidence_foreground_session_contract")
        foreground_thread_ids.append(require_string(foreground_session["thread_id"], "evidence_foreground_session_contract"))
        if foreground_session["source_kind"] not in {"root", "subagent"}:
            raise BenchmarkGateError("evidence_foreground_session_contract")
        for required_string_key in ["model", "effort"]:
            require_string(foreground_session[required_string_key], "evidence_foreground_session_contract")
        if foreground_session["parent_thread_id"] is not None and (not isinstance(foreground_session["parent_thread_id"], str) or not foreground_session["parent_thread_id"]):
            raise BenchmarkGateError("evidence_foreground_session_contract")
        require_integer(foreground_session["tokens_used"], "evidence_foreground_session_contract", 0)
    if len(foreground_thread_ids) != len(set(foreground_thread_ids)):
        raise BenchmarkGateError("evidence_foreground_session_duplicate")
    if foreground_state_snapshot["after_thread_count"] - foreground_state_snapshot["before_thread_count"] != len(foreground_sessions):
        raise BenchmarkGateError("evidence_foreground_state_snapshot_delta")
    foreground_by_id = {foreground_session["thread_id"]: foreground_session for foreground_session in foreground_sessions}
    foreground_root_ids = {foreground_session["thread_id"] for foreground_session in foreground_sessions if foreground_session["source_kind"] == "root"}
    foreground_main_session = foreground_by_id.get(foreground_main_thread_id)
    if foreground_main_session is None or foreground_main_session["source_kind"] != "root" or foreground_main_session["parent_thread_id"] is not None:
        raise BenchmarkGateError("evidence_foreground_session_tree")
    for foreground_session in foreground_sessions:
        if foreground_session["source_kind"] == "root":
            if foreground_session["parent_thread_id"] is not None or arm == "direct" and foreground_session["thread_id"] != foreground_main_thread_id:
                raise BenchmarkGateError("evidence_foreground_session_tree")
            continue
        parent_thread_id = foreground_session["parent_thread_id"]
        if parent_thread_id is None:
            raise BenchmarkGateError("evidence_foreground_session_tree")
        visited_thread_ids = {foreground_session["thread_id"]}
        current_thread_id = parent_thread_id
        while current_thread_id not in foreground_root_ids:
            if current_thread_id in visited_thread_ids or current_thread_id not in foreground_by_id:
                raise BenchmarkGateError("evidence_foreground_session_tree")
            visited_thread_ids.add(current_thread_id)
            current_session = foreground_by_id[current_thread_id]
            if current_session["source_kind"] != "subagent" or current_session["parent_thread_id"] is None:
                raise BenchmarkGateError("evidence_foreground_session_tree")
            current_thread_id = current_session["parent_thread_id"]
    runtime_sessions = evidence["runtime_sessions"]
    if not isinstance(runtime_sessions, list):
        raise BenchmarkGateError("evidence_runtime_sessions")
    runtime_thread_ids = []
    for runtime_session in runtime_sessions:
        if not isinstance(runtime_session, dict) or set(runtime_session) != RUNTIME_SESSION_KEYS:
            raise BenchmarkGateError("evidence_runtime_session_contract")
        thread_id = require_string(runtime_session["thread_id"], "evidence_runtime_session_contract")
        runtime_thread_ids.append(thread_id)
        if runtime_session["source_kind"] not in {"root", "subagent"} or not isinstance(runtime_session["turn_completed"], bool):
            raise BenchmarkGateError("evidence_runtime_session_contract")
        for optional_string_key in ["parent_thread_id", "model", "effort", "rollout_sha256", "rollout_model", "rollout_effort"]:
            if runtime_session[optional_string_key] is not None and (not isinstance(runtime_session[optional_string_key], str) or not runtime_session[optional_string_key]):
                raise BenchmarkGateError("evidence_runtime_session_contract")
        for optional_integer_key in ["tokens_used", "rollout_total_tokens"]:
            optional_integer = runtime_session[optional_integer_key]
            if optional_integer is not None and (isinstance(optional_integer, bool) or not isinstance(optional_integer, int) or optional_integer < 0):
                raise BenchmarkGateError("evidence_runtime_session_contract")
        if runtime_session["rollout_sha256"] is not None:
            require_sha256(runtime_session["rollout_sha256"], "evidence_runtime_session_contract")
    if len(runtime_thread_ids) != len(set(runtime_thread_ids)):
        raise BenchmarkGateError("evidence_runtime_session_duplicate")
    if set(runtime_thread_ids) != launched_sessions:
        raise BenchmarkGateError("evidence_session_census")
    if state_snapshot["before_complete"] and state_snapshot["after_complete"] and state_snapshot["after_thread_count"] - state_snapshot["before_thread_count"] != len(runtime_sessions):
        raise BenchmarkGateError("evidence_state_snapshot_delta")
    runtime_by_id = {runtime_session["thread_id"]: runtime_session for runtime_session in runtime_sessions}
    for foreground_session in foreground_sessions:
        runtime_session = runtime_by_id.get(foreground_session["thread_id"])
        if runtime_session is None:
            raise BenchmarkGateError("evidence_foreground_unknown_session")
        if any(foreground_session[key] != runtime_session[key] for key in ["parent_thread_id", "source_kind", "model", "effort"]) or runtime_session["tokens_used"] is None:
            raise BenchmarkGateError("evidence_foreground_session_mismatch")
        if foreground_session["tokens_used"] > runtime_session["tokens_used"]:
            raise BenchmarkGateError("evidence_foreground_tokens_exceed_final")
    started_ns = evidence["started_monotonic_ns"]
    first_result_ns = evidence["first_result_monotonic_ns"]
    producer_finished_ns = evidence["producer_finished_monotonic_ns"]
    if not started_ns <= first_result_ns <= producer_finished_ns:
        raise BenchmarkGateError("evidence_timing")
    return evidence


def summarize_runtime_sessions(evidence, main_thread_id, selected_pair, receipt_session_pairs, arm):
    failures = []
    runtime_sessions = evidence["runtime_sessions"]
    runtime_by_id = {runtime_session["thread_id"]: runtime_session for runtime_session in runtime_sessions}
    state_snapshot = evidence["state_snapshot"]
    if state_snapshot["before_complete"] is not True or state_snapshot["after_complete"] is not True:
        failures.append("runtime_state_snapshot_incomplete")
    root_sessions = [runtime_session for runtime_session in runtime_sessions if runtime_session["source_kind"] == "root"]
    root_thread_ids = {root_session["thread_id"] for root_session in root_sessions}
    allowed_root_thread_ids = set(receipt_session_pairs)
    foreground_thread_ids = {foreground_session["thread_id"] for foreground_session in evidence["foreground_sessions"]}
    adaptive_root_thread_ids = foreground_thread_ids - {main_thread_id} if arm == "global" else set()
    main_session = runtime_by_id.get(main_thread_id)
    if main_session is None or main_session["source_kind"] != "root" or main_session["parent_thread_id"] is not None or any(root_session["parent_thread_id"] is not None or root_session["thread_id"] not in allowed_root_thread_ids | adaptive_root_thread_ids for root_session in root_sessions):
        failures.append("runtime_session_tree")
    for runtime_session in runtime_sessions:
        observed_pair = f"{runtime_session['model']}|{runtime_session['effort']}" if runtime_session["model"] and runtime_session["effort"] else None
        rollout_pair = f"{runtime_session['rollout_model']}|{runtime_session['rollout_effort']}" if runtime_session["rollout_model"] and runtime_session["rollout_effort"] else None
        required_pair = receipt_session_pairs.get(runtime_session["thread_id"])
        if observed_pair != rollout_pair or required_pair is not None and observed_pair != required_pair or runtime_session["thread_id"] == main_thread_id and observed_pair != selected_pair:
            if "runtime_session_pair_mismatch" not in failures:
                failures.append("runtime_session_pair_mismatch")
        tokens_used = runtime_session["tokens_used"]
        rollout_total_tokens = runtime_session["rollout_total_tokens"]
        if runtime_session["turn_completed"] is not True or runtime_session["rollout_sha256"] is None or tokens_used is None or rollout_total_tokens is None or tokens_used != rollout_total_tokens:
            if "runtime_session_incomplete" not in failures:
                failures.append("runtime_session_incomplete")
        if runtime_session["source_kind"] == "subagent":
            parent_thread_id = runtime_session["parent_thread_id"]
            if parent_thread_id not in runtime_by_id:
                if "runtime_session_tree" not in failures:
                    failures.append("runtime_session_tree")
                continue
            visited_thread_ids = {runtime_session["thread_id"]}
            current_thread_id = parent_thread_id
            while current_thread_id not in root_thread_ids:
                if current_thread_id in visited_thread_ids or current_thread_id not in runtime_by_id:
                    if "runtime_session_tree" not in failures:
                        failures.append("runtime_session_tree")
                    break
                visited_thread_ids.add(current_thread_id)
                current_session = runtime_by_id[current_thread_id]
                if current_session["source_kind"] != "subagent" or current_session["parent_thread_id"] is None:
                    if "runtime_session_tree" not in failures:
                        failures.append("runtime_session_tree")
                    break
                current_thread_id = current_session["parent_thread_id"]
            if current_thread_id in runtime_by_id and current_thread_id not in allowed_root_thread_ids | adaptive_root_thread_ids:
                if "runtime_session_tree" not in failures:
                    failures.append("runtime_session_tree")
    if evidence["foreground_main_thread_id"] != main_thread_id:
        failures.append("foreground_main_thread_mismatch")
    task_foreground_sessions = [foreground_session for foreground_session in evidence["foreground_sessions"] if arm == "direct" or foreground_session["thread_id"] != main_thread_id]
    if arm == "global" and not task_foreground_sessions:
        failures.append("global_adaptive_child_missing")
    valid_token_values = [foreground_session["tokens_used"] for foreground_session in task_foreground_sessions]
    controller_tokens_excluded = next((foreground_session["tokens_used"] for foreground_session in evidence["foreground_sessions"] if arm == "global" and foreground_session["thread_id"] == main_thread_id), 0)
    return {"thread_ids": list(runtime_by_id), "sessions_by_id": runtime_by_id, "session_count": len(runtime_sessions), "root_session_count": len(root_sessions), "descendant_session_count": len(runtime_sessions) - len(root_sessions), "task_session_count": len(task_foreground_sessions), "controller_tokens_excluded": controller_tokens_excluded, "logical_total_tokens": sum(valid_token_values), "failures": failures}


def validate_environment_snapshot(environment):
    for path_key in ["codex_home", "memories_root", "workdir"]:
        if not Path(environment[path_key]).is_dir():
            raise BenchmarkGateError("environment_directory_missing")
    for path_key, hash_key in [("config_path", "config_sha256"), ("agents_path", "agents_sha256"), ("receipt_runner_path", "receipt_runner_sha256")]:
        path = Path(environment[path_key])
        try:
            actual_sha256 = sha256_bytes(path.read_bytes())
        except OSError:
            raise BenchmarkGateError("environment_file_missing")
        if actual_sha256 != environment[hash_key]:
            raise BenchmarkGateError("environment_hash_mismatch")
    if models_cache_sha256(Path(environment["models_cache_path"])) != environment["models_cache_sha256"]:
        raise BenchmarkGateError("environment_hash_mismatch")
    if sha256_source_tree(Path(environment["memories_root"])) != environment["memories_sha256"]:
        raise BenchmarkGateError("environment_hash_mismatch")
    catalog_matches = False
    catalog_error = None
    system_skills_root = Path(environment["skills_catalog_root"]) / ".system"
    refresh_paths = [system_skills_root / ".codex-system-skills.marker"]
    try:
        if system_skills_root.is_dir():
            refresh_paths.extend(system_skills_root.rglob("SKILL.md"))
            refresh_paths.extend(system_skills_root.rglob("openai.yaml"))
        refresh_recent = any(path.is_file() and time.time() - path.stat().st_mtime <= CATALOG_REFRESH_RECENCY_SECONDS for path in refresh_paths)
    except (OSError, ValueError):
        refresh_recent = False
    catalog_deadline = time.monotonic() + CATALOG_REFRESH_GRACE_SECONDS if refresh_recent else time.monotonic()
    while True:
        try:
            current_catalog = catalog_snapshot(Path(environment["codex_home"]), Path(environment["config_path"]))
            catalog_matches = all(current_catalog[key] == environment[key] for key in CATALOG_ENVIRONMENT_KEYS)
            catalog_error = None
        except BenchmarkGateError as error:
            catalog_error = error
        if catalog_matches:
            break
        if not refresh_recent or time.monotonic() >= catalog_deadline:
            break
        time.sleep(CATALOG_STABILITY_RETRY_SECONDS)
    if not catalog_matches:
        if catalog_error is not None:
            raise catalog_error
        raise BenchmarkGateError("environment_catalog_drift")
    return sha256_text(canonical_json(environment))


def validate_receipt(receipt, receipt_spec, result_message, run_plan, expected_execution_prompt_sha256=None):
    failures = []
    required_pass_fields = {"schema_version": 1, "status": "pass", "failure_class": None, "turn_completed": True, "exit_code": 0, "metrics_complete": True, "tokens_lower_bound": False, "model_match": True, "effort_match": True, "pair_match": True, "authorization_status": "authorized"}
    if any(receipt.get(field) != expected_value for field, expected_value in required_pass_fields.items()):
        failures.append("receipt_incomplete")
    thread_id = receipt.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        failures.append("receipt_thread_id")
        thread_id = None
    if receipt.get("requested_pair") != receipt_spec["pair"] or receipt.get("effective_pair") != receipt_spec["pair"]:
        failures.append("receipt_pair_mismatch")
    if receipt.get("node_role") != receipt_spec["role"]:
        failures.append("receipt_role_mismatch")
    if receipt.get("workload_prompt_sha256") != receipt_spec["workload_prompt_sha256"]:
        failures.append("receipt_workload_mismatch")
    if receipt_spec["bind_result"]:
        expected_node_type = "direct-task" if run_plan["arm"] == "direct" else "bootstrap-task"
        expected_authorization_source = "benchmark-direct" if run_plan["arm"] == "direct" else "benchmark-global-inline"
        if receipt.get("node_type") != expected_node_type:
            failures.append("receipt_node_type_mismatch")
        if receipt.get("entry_context_active") is not False:
            failures.append("receipt_entry_context_active")
        if receipt.get("authorization_source") != expected_authorization_source:
            failures.append("receipt_authorization_source_mismatch")
        if receipt.get("benchmark_run_id") != f"benchmark-{run_plan['run_id']}":
            failures.append("receipt_benchmark_run_id_mismatch")
        if receipt.get("workload_id") != run_plan["run_id"]:
            failures.append("receipt_workload_id_mismatch")
        if run_plan["arm"] == "global" and receipt.get("benchmark_prompt_file_verified") is not True:
            failures.append("receipt_benchmark_prompt_unverified")
        if run_plan["arm"] == "global" and (receipt.get("benchmark_auto_launch_verified") is not True or receipt.get("benchmark_auto_workspace_count") != 1):
            failures.append("receipt_auto_launch_unverified")
        benchmark_result_source = receipt.get("benchmark_result_source")
        if run_plan["arm"] == "direct" and benchmark_result_source != "controller_final":
            failures.append("receipt_result_source_invalid")
        if run_plan["arm"] == "global" and benchmark_result_source not in {"controller_final", "adaptive_bridge_handoff"}:
            failures.append("receipt_result_source_invalid")
        if run_plan["arm"] == "global" and benchmark_result_source == "adaptive_bridge_handoff" and receipt.get("benchmark_auto_bridge_result_verified") is not True:
            failures.append("receipt_result_source_invalid")
        expected_prompt_sha256 = expected_execution_prompt_sha256 or run_plan["prompt_sha256"]
        if receipt.get("prompt_sha256") != expected_prompt_sha256:
            failures.append("receipt_execution_prompt_mismatch")
    else:
        authorization_pair = (receipt.get("entry_context_active"), receipt.get("authorization_source"))
        if receipt.get("node_type") != "locked-route-node":
            failures.append("receipt_node_type_mismatch")
        if authorization_pair not in {(True, "dispatcher"), (False, "outside-entry-context")}:
            failures.append("receipt_authorization_source_mismatch")
    route_attempts = receipt.get("route_attempts")
    reroutes = receipt.get("reroutes")
    if not isinstance(route_attempts, list) or not route_attempts:
        failures.append("receipt_route_attempts")
    if not isinstance(reroutes, list):
        failures.append("receipt_reroutes")
    retry_count = max(len(route_attempts) - 1, 0) if isinstance(route_attempts, list) else 0
    fallback_count = len(reroutes) if isinstance(reroutes, list) else 0
    if retry_count:
        failures.append("receipt_retry")
    if fallback_count:
        failures.append("receipt_fallback")
    tokens = receipt.get("tokens")
    total_tokens = tokens.get("total_tokens") if isinstance(tokens, dict) else None
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
        failures.append("receipt_tokens")
        total_tokens = 0
    result_ready_monotonic_ns = receipt.get("result_ready_monotonic_ns") if receipt_spec["bind_result"] else None
    if receipt_spec["bind_result"]:
        output_sha256 = receipt.get("output_sha256")
        if not isinstance(output_sha256, str) or output_sha256 != sha256_text(result_message):
            failures.append("receipt_result_hash")
        if receipt.get("result_published") is not True or isinstance(receipt.get("result_ready_monotonic_ns"), bool) or not isinstance(receipt.get("result_ready_monotonic_ns"), int) or receipt["result_ready_monotonic_ns"] < 0:
            failures.append("receipt_result_not_published")
        if isinstance(receipt.get("child_result_ready_monotonic_ns"), bool) or not isinstance(receipt.get("child_result_ready_monotonic_ns"), int) or receipt["child_result_ready_monotonic_ns"] < 0 or receipt.get("result_ready_clock") != "benchmark-runner-monotonic" or receipt.get("result_ready_event_sequence") != 1:
            failures.append("receipt_result_ready_event_invalid")
        if receipt.get("duplicate_result_detected") is not False:
            failures.append("receipt_result_not_frozen")
    return {"thread_id": thread_id, "pair": receipt.get("effective_pair"), "role": receipt.get("node_role"), "total_tokens": total_tokens, "retry_count": retry_count, "fallback_count": fallback_count, "result_ready_monotonic_ns": result_ready_monotonic_ns, "failures": failures}


def selected_execution_metrics(receipt, arm, runtime_summary, first_result_elapsed_ms):
    if arm == "direct":
        total_tokens = runtime_summary.get("logical_total_tokens")
        elapsed_ms = first_result_elapsed_ms
        route_attempts = receipt.get("route_attempts") if isinstance(receipt.get("route_attempts"), list) else []
        failed_attempts = [attempt for attempt in route_attempts if isinstance(attempt, dict) and attempt.get("status") != "pass"]
        calibration_attempt_count = len(failed_attempts) + int(receipt.get("trial") is True) + len(receipt.get("reroutes") if isinstance(receipt.get("reroutes"), list) else [])
        calibration_failure_elapsed_ms = sum(attempt.get("process_elapsed_ms", 0) for attempt in failed_attempts if isinstance(attempt.get("process_elapsed_ms"), int) and attempt["process_elapsed_ms"] >= 0)
        calibration_failure_logical_tokens = sum((attempt.get("tokens") or {}).get("total_tokens", 0) for attempt in failed_attempts if isinstance((attempt.get("tokens") or {}).get("total_tokens", 0), int) and (attempt.get("tokens") or {}).get("total_tokens", 0) >= 0)
        signature = {
            "selected_pair": receipt.get("effective_pair"),
            "effective_pair": receipt.get("effective_pair"),
            "scheduled_graph": False,
            "assigned_pairs": [receipt.get("effective_pair")],
            "trial": False,
            "recommendation_state": "direct_fixed",
            "selection_provenance": "direct_fixed",
            "capability_assignment": [{"node_id": "direct", "effective_pair": receipt.get("effective_pair")}],
        }
        clean = receipt.get("status") == "pass" and receipt.get("metrics_complete") is True and isinstance(route_attempts, list) and route_attempts and not failed_attempts and receipt.get("trial") in {None, False} and receipt.get("reroutes") == [] and receipt.get("node_role") != "repair"
    else:
        execution = receipt.get("benchmark_selected_execution")
        if not isinstance(execution, dict) or set(execution) != {"schema_version", "receipt_sha256", "selected_pair", "effective_pair", "steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "calibration_attempt_count", "calibration_failure_elapsed_ms", "calibration_failure_logical_tokens", "route_signature"}:
            return None, ["selected_execution_receipt_missing"]
        signature = execution.get("route_signature")
        total_tokens = execution.get("steady_state_logical_tokens")
        elapsed_ms = execution.get("steady_state_execution_elapsed_ms")
        calibration_attempt_count = execution.get("calibration_attempt_count")
        calibration_failure_elapsed_ms = execution.get("calibration_failure_elapsed_ms")
        calibration_failure_logical_tokens = execution.get("calibration_failure_logical_tokens")
        clean = execution.get("schema_version") == 2 and isinstance(execution.get("receipt_sha256"), str) and SHA256_PATTERN.fullmatch(execution["receipt_sha256"]) is not None and isinstance(signature, dict) and signature.get("trial") is False and signature.get("recommendation_state") in {"frozen", "priority_verified", "verified_recovery"} and signature.get("selection_provenance") in {"dual_model_history", "local_transfer_history", "local_and_obsidian", "local_history", "obsidian_history"} and isinstance(signature.get("capability_assignment"), list) and signature["capability_assignment"] and execution.get("selected_pair") == signature.get("selected_pair")
    failures = []
    if not isinstance(total_tokens, int) or total_tokens < 0:
        failures.append("steady_state_tokens_invalid")
    if not isinstance(elapsed_ms, int) or elapsed_ms < 0:
        failures.append("steady_state_elapsed_invalid")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in [calibration_attempt_count, calibration_failure_elapsed_ms, calibration_failure_logical_tokens]):
        failures.append("calibration_diagnostics_invalid")
    if not clean:
        failures.append("steady_state_not_clean")
    return {"steady_state_logical_tokens": total_tokens if isinstance(total_tokens, int) else None, "steady_state_execution_elapsed_ms": elapsed_ms if isinstance(elapsed_ms, int) else None, "calibration_attempt_count": calibration_attempt_count if isinstance(calibration_attempt_count, int) else None, "calibration_failure_elapsed_ms": calibration_failure_elapsed_ms if isinstance(calibration_failure_elapsed_ms, int) else None, "calibration_failure_logical_tokens": calibration_failure_logical_tokens if isinstance(calibration_failure_logical_tokens, int) else None, "route_signature": signature}, failures


def validate_dual_entry_probe(plan_root, run_plan, primary_receipt, expected_sha256, primary_result_sha256):
    probe = run_plan.get("dual_entry_probe")
    if probe is None:
        return []
    failures = []
    try:
        probe_receipt_path = resolve_plan_path(plan_root, probe["receipt_path"], "entry_invariance_probe_path")
        probe_result_path = resolve_plan_path(plan_root, probe["result_path"], "entry_invariance_probe_path")
        probe_summary_path = resolve_plan_path(plan_root, probe["summary_path"], "entry_invariance_probe_path")
        probe_receipt, _ = load_json_object(probe_receipt_path, "entry_invariance_probe_receipt")
        probe_result, probe_result_bytes = read_exact_json_result(probe_result_path, "entry_invariance_probe_result")
        probe_summary, _ = load_json_object(probe_summary_path, "entry_invariance_probe_summary")
    except BenchmarkGateError as error:
        return [error.code]
    if set(probe_summary) != DUAL_ENTRY_PROBE_SUMMARY_KEYS or probe_summary.get("schema_version") != 1 or probe_summary.get("status") != "pass" or probe_summary.get("reason") != "exact_output_and_route_signature_match":
        failures.append("entry_invariance_probe_summary")
    primary_execution = primary_receipt.get("benchmark_selected_execution") if isinstance(primary_receipt, dict) else None
    probe_execution = probe_receipt.get("benchmark_selected_execution") if isinstance(probe_receipt, dict) else None
    probe_metrics, probe_metric_failures = selected_execution_metrics(probe_receipt, "global", {}, 0)
    if probe_metric_failures:
        failures.append("entry_invariance_probe_execution")
    probe_message = probe_result_bytes.decode("utf-8")
    probe_message = probe_message[:-1] if probe_message.endswith("\n") else probe_message
    expected_fields = {"tier": run_plan["tier"], "primary_run_id": run_plan["run_id"], "primary_entry_pair": AUTO_ENTRY_PAIR, "probe_entry_pair": SOL_ENTRY_PROBE_PAIR, "expected_result_sha256": expected_sha256, "primary_result_sha256": primary_result_sha256, "probe_result_sha256": sha256_bytes(probe_result_bytes), "primary_route_signature": primary_execution.get("route_signature") if isinstance(primary_execution, dict) else None, "probe_route_signature": probe_execution.get("route_signature") if isinstance(probe_execution, dict) else None, "route_signature_match": isinstance(primary_execution, dict) and isinstance(probe_execution, dict) and primary_execution.get("route_signature") == probe_execution.get("route_signature"), "capability_assignment_match": isinstance(primary_execution, dict) and isinstance(probe_execution, dict) and primary_execution.get("route_signature", {}).get("capability_assignment") == probe_execution.get("route_signature", {}).get("capability_assignment")}
    if any(probe_summary.get(field) != expected_value for field, expected_value in expected_fields.items()):
        failures.append("entry_invariance_probe_summary")
    required_probe_fields = {"status": "pass", "failure_class": None, "node_type": "bootstrap-task", "requested_pair": SOL_ENTRY_PROBE_PAIR, "effective_pair": SOL_ENTRY_PROBE_PAIR, "benchmark_prompt_file_verified": True, "benchmark_auto_launch_verified": True, "benchmark_auto_workspace_count": 1, "benchmark_auto_bridge_result_verified": True, "result_published": True, "duplicate_result_detected": False}
    if any(probe_receipt.get(field) != expected_value for field, expected_value in required_probe_fields.items()) or probe_receipt.get("output_sha256") != sha256_text(probe_message):
        failures.append("entry_invariance_probe_receipt")
    if sha256_bytes(probe_result_bytes) != expected_sha256:
        failures.append("entry_invariance_probe_result")
    if expected_fields["route_signature_match"] is not True or expected_fields["capability_assignment_match"] is not True:
        failures.append("entry_invariance_probe_mismatch")
    return failures


def evaluate_run(plan_root, suite_id, plan_sha256, run_plan):
    ending_started_ns = time.perf_counter_ns()
    failures = []
    operational_failures = []

    def fail(code, operational=False):
        if code not in failures:
            failures.append(code)
        if operational and code not in operational_failures:
            operational_failures.append(code)

    expected_path = resolve_plan_path(plan_root, run_plan["expected_result_path"], "expected_path")
    result_path = resolve_plan_path(plan_root, run_plan["result_path"], "result_path")
    evidence_path = resolve_plan_path(plan_root, run_plan["evidence_path"], "evidence_path")
    expected_sha256 = None
    source_snapshot_sha256 = None
    presented_result_sha256 = None
    presented_result_object_sha256 = None
    evidence_sha256 = None
    prompt_file_sha256 = None
    expected_execution_prompt_sha256 = None
    source_files_checked = 0
    environment_sha256 = None
    result_message = ""
    if "prompt_path" in run_plan:
        try:
            prompt_path = resolve_plan_path(plan_root, run_plan["prompt_path"], "prompt_path")
            prompt_bytes = prompt_path.read_bytes()
            prompt_file_sha256 = sha256_bytes(prompt_bytes)
            prompt_text = prompt_bytes.decode("utf-8")
            if prompt_file_sha256 != run_plan["prompt_sha256"]:
                fail("prompt_hash_mismatch", True)
            execution_prompt = prompt_text if run_plan["arm"] == "direct" else auto_benchmark_execution_prompt(prompt_text)
            expected_execution_prompt_sha256 = sha256_text(execution_prompt)
        except (OSError, UnicodeError, BenchmarkGateError):
            fail("prompt_file_invalid", True)
    try:
        expected_document, expected_bytes = read_exact_json_result(expected_path, "expected_result_invalid")
        expected_sha256 = sha256_bytes(expected_bytes)
        if expected_sha256 != run_plan["expected_sha256"]:
            fail("expected_hash_mismatch", True)
    except BenchmarkGateError as error:
        expected_document = None
        fail(error.code, True)
    try:
        result_document, result_bytes = read_exact_json_result(result_path, "result_invalid")
        presented_result_sha256 = sha256_bytes(result_bytes)
        presented_result_object_sha256 = sha256_text(canonical_json(result_document))
        result_text = result_bytes.decode("utf-8")
        result_message = result_text[:-1] if result_text.endswith("\n") else result_text
    except BenchmarkGateError as error:
        result_document = None
        fail(error.code, True)
    if expected_document is not None and result_document is not None and result_bytes != expected_bytes:
        fail("result_not_exact")
    if result_document is not None and "source_root" in run_plan:
        try:
            source_root = resolve_plan_path(plan_root, run_plan["source_root"], "source_root")
            source_snapshot_sha256 = sha256_source_tree(source_root)
            if source_snapshot_sha256 != run_plan["source_snapshot_sha256"]:
                fail("source_snapshot_hash_mismatch", True)
            source_files_checked = validate_source_files(result_document, source_root, run_plan["source_files_pointer"])
        except BenchmarkGateError as error:
            fail(error.code)
    if "environment" in run_plan:
        try:
            environment_sha256 = validate_environment_snapshot(run_plan["environment"])
        except BenchmarkGateError as error:
            fail(error.code, True)
    try:
        evidence, evidence_bytes = load_json_object(evidence_path, "evidence_invalid")
        evidence_sha256 = sha256_bytes(evidence_bytes)
        validate_evidence(evidence, run_plan["run_id"], run_plan["arm"])
    except BenchmarkGateError as error:
        evidence = None
        fail(error.code, True)
    receipt_summaries = []
    receipt_documents = []
    for receipt_spec in run_plan["receipts"]:
        receipt_path = resolve_plan_path(plan_root, receipt_spec["path"], "receipt_path")
        try:
            receipt, _ = load_json_object(receipt_path, "receipt_invalid")
            receipt_summary = validate_receipt(receipt, receipt_spec, result_message, run_plan, expected_execution_prompt_sha256)
            receipt_summary["bind_result"] = receipt_spec["bind_result"]
            receipt_summaries.append(receipt_summary)
            receipt_documents.append((receipt_spec, receipt))
            for receipt_failure in receipt_summary["failures"]:
                fail(receipt_failure, receipt_failure != "receipt_result_hash")
        except BenchmarkGateError as error:
            fail(error.code, True)
    bound_result_ready_timestamps = [summary["result_ready_monotonic_ns"] for summary in receipt_summaries if summary.get("bind_result") is True]
    if evidence is not None and (len(bound_result_ready_timestamps) != 1 or evidence["first_result_monotonic_ns"] != bound_result_ready_timestamps[0]):
        fail("receipt_result_ready_timing_mismatch", True)
    receipt_session_ids = [summary["thread_id"] for summary in receipt_summaries if summary["thread_id"] is not None]
    duplicate_receipt_sessions = len(receipt_session_ids) != len(set(receipt_session_ids))
    if duplicate_receipt_sessions:
        fail("receipt_session_duplicate", True)
    launched_session_ids = evidence["launched_session_ids"] if evidence is not None else []
    bound_receipt_thread_ids = [summary["thread_id"] for summary in receipt_summaries if summary.get("bind_result") is True and summary["thread_id"] is not None]
    main_thread_id = bound_receipt_thread_ids[0] if len(bound_receipt_thread_ids) == 1 else None
    result_producer_pair = next((summary["pair"] for summary in receipt_summaries if summary.get("bind_result") is True), None)
    if len(bound_receipt_thread_ids) != 1:
        fail("runtime_main_receipt", True)
    receipt_session_pairs = {summary["thread_id"]: summary["pair"] for summary in receipt_summaries if summary["thread_id"] is not None and isinstance(summary["pair"], str)}
    runtime_summary = summarize_runtime_sessions(evidence, main_thread_id, run_plan["selected_entry_pair"], receipt_session_pairs, run_plan["arm"]) if evidence is not None else {"thread_ids": [], "sessions_by_id": {}, "session_count": 0, "root_session_count": 0, "descendant_session_count": 0, "task_session_count": 0, "controller_tokens_excluded": 0, "logical_total_tokens": 0, "failures": ["foreground_state_snapshot_incomplete", "runtime_state_snapshot_incomplete"]}
    for runtime_failure in runtime_summary["failures"]:
        fail(runtime_failure, True)
    runtime_thread_ids = runtime_summary["thread_ids"]
    unexpected_receipt_session_ids = sorted(set(receipt_session_ids) - set(runtime_thread_ids))
    if unexpected_receipt_session_ids:
        fail("unexpected_receipt", True)
    unreceipted_descendant_count = sum(runtime_session["source_kind"] == "subagent" and runtime_session["thread_id"] not in receipt_session_ids for runtime_session in runtime_summary["sessions_by_id"].values())
    for receipt_summary in receipt_summaries:
        runtime_session = runtime_summary["sessions_by_id"].get(receipt_summary["thread_id"])
        if runtime_session is not None and receipt_summary["total_tokens"] != runtime_session["tokens_used"]:
            fail("receipt_runtime_token_mismatch", True)
    retry_count = len(evidence["retry_session_ids"]) if evidence is not None else 0
    fallback_count = len(evidence["fallback_session_ids"]) if evidence is not None else 0
    repair_count = len(evidence["repair_session_ids"]) if evidence is not None else 0
    retry_count += sum(summary["retry_count"] for summary in receipt_summaries)
    fallback_count += sum(summary["fallback_count"] for summary in receipt_summaries)
    repair_count += len({summary["thread_id"] for summary in receipt_summaries if summary["role"] == "repair" and summary["thread_id"] is not None} - set(evidence["repair_session_ids"] if evidence is not None else []))
    if retry_count:
        fail("retry_not_allowed")
    if fallback_count:
        fail("fallback_not_allowed")
    if repair_count:
        fail("repair_not_allowed")
    logical_total_tokens = runtime_summary["logical_total_tokens"]
    first_result_elapsed_ms = None
    producer_elapsed_ms = None
    total_wall_elapsed_ms = None
    if evidence is not None:
        first_result_elapsed_ms = (evidence["first_result_monotonic_ns"] - evidence["started_monotonic_ns"]) // 1_000_000
        producer_elapsed_ms = (evidence["producer_finished_monotonic_ns"] - evidence["started_monotonic_ns"]) // 1_000_000
        if evidence["producer_timed_out"]:
            fail("run_timeout", True)
        if evidence["producer_process_exit_code"] != 0:
            fail("run_exit_code", True)
        if not evidence["producer_complete"]:
            fail("producer_incomplete", True)
    bound_receipt = next((receipt for receipt_spec, receipt in receipt_documents if receipt_spec["bind_result"]), None)
    steady_metrics, steady_failures = selected_execution_metrics(bound_receipt, run_plan["arm"], runtime_summary, first_result_elapsed_ms) if isinstance(bound_receipt, dict) else (None, ["selected_execution_receipt_missing"])
    for steady_failure in steady_failures:
        fail(steady_failure, True)
    if isinstance(bound_receipt, dict) and expected_sha256 is not None and presented_result_sha256 is not None:
        for probe_failure in validate_dual_entry_probe(plan_root, run_plan, bound_receipt, expected_sha256, presented_result_sha256):
            fail(probe_failure, True)
    metrics_complete = not any(failure in failures for failure in ["receipt_invalid", "receipt_incomplete", "receipt_tokens", "receipt_thread_id", "receipt_route_attempts", "receipt_reroutes", "receipt_node_type_mismatch", "receipt_entry_context_active", "receipt_authorization_source_mismatch", "receipt_benchmark_run_id_mismatch", "receipt_workload_id_mismatch", "receipt_benchmark_prompt_unverified", "receipt_auto_launch_unverified", "receipt_result_source_invalid", "receipt_execution_prompt_mismatch", "receipt_result_hash", "receipt_result_not_published", "receipt_result_not_frozen", "receipt_result_ready_event_invalid", "receipt_result_ready_timing_mismatch", "receipt_session_duplicate", "unexpected_receipt", "runtime_main_receipt", "foreground_state_snapshot_incomplete", "foreground_main_thread_mismatch", "runtime_state_snapshot_incomplete", "runtime_session_tree", "runtime_session_pair_mismatch", "runtime_session_incomplete", "receipt_runtime_token_mismatch", "global_adaptive_child_missing", "selected_execution_receipt_missing", "steady_state_tokens_invalid", "steady_state_elapsed_invalid", "steady_state_not_clean", "calibration_diagnostics_invalid", "entry_invariance_probe_path", "entry_invariance_probe_receipt", "entry_invariance_probe_result", "entry_invariance_probe_summary", "entry_invariance_probe_execution", "entry_invariance_probe_mismatch"])
    steady_state_execution_elapsed_ms = steady_metrics.get("steady_state_execution_elapsed_ms") if steady_metrics else None
    steady_state_logical_tokens = steady_metrics.get("steady_state_logical_tokens") if steady_metrics else None
    if run_plan["arm"] == "direct":
        route_selection_elapsed_ms = 0 if isinstance(first_result_elapsed_ms, int) and isinstance(steady_state_execution_elapsed_ms, int) else None
    else:
        route_selection_elapsed_ms = first_result_elapsed_ms - steady_state_execution_elapsed_ms if isinstance(first_result_elapsed_ms, int) and isinstance(steady_state_execution_elapsed_ms, int) else None
    if route_selection_elapsed_ms is None or route_selection_elapsed_ms < 0:
        fail("route_selection_elapsed_invalid", True)
        metrics_complete = False
    calibration_attempt_count = steady_metrics.get("calibration_attempt_count") if steady_metrics else None
    calibration_failure_elapsed_ms = steady_metrics.get("calibration_failure_elapsed_ms") if steady_metrics else None
    calibration_failure_logical_tokens = steady_metrics.get("calibration_failure_logical_tokens") if steady_metrics else None
    ending_real_elapsed_ms = (time.perf_counter_ns() - ending_started_ns) // 1_000_000
    total_wall_elapsed_ms = producer_elapsed_ms + ending_real_elapsed_ms if producer_elapsed_ms is not None else None
    completion = "timeout" if evidence is not None and evidence["producer_timed_out"] else "complete" if evidence is not None and evidence["producer_complete"] else "incomplete"
    acceptance_status = "pass" if not failures and completion == "complete" else "fail"
    executed_pairs = [f"{runtime_session['model']}|{runtime_session['effort']}" for runtime_session in evidence["runtime_sessions"]] if evidence is not None else []
    ending_real = {"method": ENDING_REAL_METHOD, "completed": True, "status": "pass" if not failures else "fail"}
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "suite_id": suite_id, "plan_sha256": plan_sha256, "run_id": run_plan["run_id"], "pair_id": run_plan["pair_id"], "tier": run_plan["tier"], "repeat_index": run_plan["repeat_index"], "arm": run_plan["arm"], "order_index": run_plan["order_index"], "workload_prompt_sha256": run_plan["prompt_sha256"], "prompt_file_sha256": prompt_file_sha256, "expected_sha256": expected_sha256, "source_snapshot_sha256": source_snapshot_sha256, "environment_sha256": environment_sha256, "selected_entry_pair": run_plan["selected_entry_pair"], "entry_execution_mode": run_plan["entry_execution_mode"], "result_producer_pair": result_producer_pair, "executed_pairs": executed_pairs, "receipt_session_ids": receipt_session_ids, "unexpected_receipt_session_ids": unexpected_receipt_session_ids, "unreceipted_descendant_count": unreceipted_descendant_count, "runtime_session_count": runtime_summary["session_count"], "runtime_root_session_count": runtime_summary["root_session_count"], "runtime_descendant_session_count": runtime_summary["descendant_session_count"], "task_session_count": runtime_summary["task_session_count"], "controller_tokens_excluded": runtime_summary["controller_tokens_excluded"], "completion": completion, "retry_count": retry_count, "fallback_count": fallback_count, "repair_count": repair_count, "metrics_complete": metrics_complete, "logical_total_tokens": logical_total_tokens, "steady_state_logical_tokens": steady_state_logical_tokens, "steady_state_execution_elapsed_ms": steady_state_execution_elapsed_ms, "route_selection_elapsed_ms": route_selection_elapsed_ms, "calibration_attempt_count": calibration_attempt_count, "calibration_failure_elapsed_ms": calibration_failure_elapsed_ms, "calibration_failure_logical_tokens": calibration_failure_logical_tokens, "first_result_elapsed_ms": first_result_elapsed_ms, "producer_elapsed_ms": producer_elapsed_ms, "ending_real_elapsed_ms": ending_real_elapsed_ms, "total_wall_elapsed_ms": total_wall_elapsed_ms, "presented_result_sha256": presented_result_sha256, "presented_result_object_sha256": presented_result_object_sha256, "ending_real": ending_real, "acceptance_status": acceptance_status, "gate": {"generated_by": "benchmark_suite_gate", "version": SCHEMA_VERSION, "evidence_sha256": evidence_sha256, "status": acceptance_status, "failures": failures, "source_files_checked": source_files_checked}}


def evaluate_paired_metric(direct_values, global_values, minimum_savings_percent=MINIMUM_PAIRED_SAVINGS_PERCENT, maximum_regression_percent=MAXIMUM_PAIRED_REGRESSION_PERCENT, require_strict_majority=True, maximum_absolute_regression=None, require_regression_bound=True):
    if not direct_values or len(direct_values) != len(global_values) or any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 for value in [*direct_values, *global_values]) or any(value <= 0 for value in direct_values):
        raise BenchmarkGateError("paired_metric_invalid")
    savings = [(direct_value - global_value) / direct_value * 100 for direct_value, global_value in zip(direct_values, global_values)]
    direct_total = sum(direct_values)
    global_total = sum(global_values)
    direct_median = statistics.median(direct_values)
    global_median = statistics.median(global_values)
    savings_median = statistics.median(savings)
    better_pairs = sum(global_value < direct_value for direct_value, global_value in zip(direct_values, global_values))
    aggregate_global_lower = global_total < direct_total
    raw_global_median_lower = global_median < direct_median
    paired_savings_median_meets_threshold = savings_median >= minimum_savings_percent
    strict_majority_better = better_pairs > len(direct_values) / 2
    worst_pair_savings_percent = min(savings)
    absolute_regressions = [global_value - direct_value for direct_value, global_value in zip(direct_values, global_values)]
    material_pair_regressions = [
        (saving < -maximum_regression_percent and absolute_regression > maximum_absolute_regression)
        if maximum_absolute_regression is not None
        else saving < -maximum_regression_percent
        for saving, absolute_regression in zip(savings, absolute_regressions)
    ]
    worst_pair_regression_within_limit = not any(material_pair_regressions)
    strict_majority_gate_pass = strict_majority_better or not require_strict_majority
    regression_bound_gate_pass = worst_pair_regression_within_limit or not require_regression_bound
    status = "pass" if aggregate_global_lower and raw_global_median_lower and paired_savings_median_meets_threshold and strict_majority_gate_pass and regression_bound_gate_pass else "fail"
    result = {
        "aggregate_global_lower": aggregate_global_lower,
        "raw_global_median_lower": raw_global_median_lower,
        "minimum_paired_savings_percent": minimum_savings_percent,
        "paired_savings_median_meets_threshold": paired_savings_median_meets_threshold,
        "strict_majority_better": strict_majority_better,
        "strict_majority_required": require_strict_majority,
        "maximum_pair_regression_percent": maximum_regression_percent,
        "regression_bound_required": require_regression_bound,
        "worst_pair_regression_within_limit": worst_pair_regression_within_limit,
        "worst_pair_savings_percent": round(worst_pair_savings_percent, 3),
        "status": status,
        "direct_total": direct_total,
        "global_total": global_total,
        "direct_median": direct_median,
        "global_median": global_median,
        "paired_savings_percent_median": round(savings_median, 3),
        "better_pairs": better_pairs,
    }
    if maximum_absolute_regression is not None:
        result.update({
            "maximum_pair_regression_ms": maximum_absolute_regression,
            "worst_pair_regression_ms": max(0, round(max(absolute_regressions), 3)),
            "material_pair_regression_count": sum(material_pair_regressions),
        })
    return result


def aggregate_tier(tier, repeat_count, manifests):
    tier_manifests = [manifest for manifest in manifests if manifest["tier"] == tier]
    failures = []
    performance_diagnostics = []
    arm_failures = [manifest["run_id"] for manifest in tier_manifests if manifest["acceptance_status"] != "pass" or manifest["completion"] != "complete" or manifest["metrics_complete"] is not True or manifest.get("retry_count") != 0 or manifest.get("fallback_count") != 0 or manifest.get("repair_count") != 0]
    if arm_failures:
        failures.append("arm_failure")
    if len(tier_manifests) != repeat_count * len(ARMS):
        failures.append("pair_structure_failure")
    for repeat_index in range(1, repeat_count + 1):
        pair_manifests = [manifest for manifest in tier_manifests if manifest["repeat_index"] == repeat_index]
        if len(pair_manifests) != len(ARMS) or {manifest["arm"] for manifest in pair_manifests} != set(ARMS):
            if "pair_structure_failure" not in failures:
                failures.append("pair_structure_failure")
    direct_manifests = [manifest for manifest in tier_manifests if manifest["arm"] == "direct"]
    global_manifests = [manifest for manifest in tier_manifests if manifest["arm"] == "global"]
    metric_names = ("steady_state_logical_tokens", "steady_state_execution_elapsed_ms", "first_result_elapsed_ms", "total_wall_elapsed_ms")
    metric_gates = {
        metric: evaluate_paired_metric(
            [manifest[metric] for manifest in sorted(direct_manifests, key=lambda item: item["repeat_index"])],
            [manifest[metric] for manifest in sorted(global_manifests, key=lambda item: item["repeat_index"])],
            require_strict_majority=metric != "steady_state_logical_tokens",
            maximum_absolute_regression=MAXIMUM_PAIRED_TIME_REGRESSION_MS if metric in {"steady_state_execution_elapsed_ms", "first_result_elapsed_ms"} else None,
            require_regression_bound=False,
        )
        for metric in metric_names
    } if not arm_failures and len(direct_manifests) == repeat_count and len(global_manifests) == repeat_count else {}
    if "steady_state_execution_elapsed_ms" in metric_gates:
        time_gate = metric_gates["steady_state_execution_elapsed_ms"]
        if tier == "simple":
            direct_time_values = [manifest["steady_state_execution_elapsed_ms"] for manifest in sorted(direct_manifests, key=lambda item: item["repeat_index"])]
            direct_time_median = statistics.median(direct_time_values)
            direct_time_mad = statistics.median(abs(value - direct_time_median) for value in direct_time_values)
            time_gate["strict_majority_required"] = False
            time_gate["status"] = "pass" if time_gate["global_total"] <= time_gate["direct_total"] + direct_time_mad * len(direct_time_values) and time_gate["global_median"] <= time_gate["direct_median"] + direct_time_mad else "fail"
        elif tier == "complex":
            time_gate["strict_majority_required"] = False
            time_gate["status"] = "pass"
    failure_prefixes = {"steady_state_logical_tokens": "token", "steady_state_execution_elapsed_ms": "steady_state"}
    gate_failure_fields = {
        "aggregate_global_lower": "aggregate_loss",
        "raw_global_median_lower": "raw_median_loss",
        "paired_savings_median_meets_threshold": "savings_threshold_loss",
        "strict_majority_better": "majority_loss",
        "worst_pair_regression_within_limit": "regression_bound_loss",
    }
    for metric in GATED_METRICS:
        if metric not in metric_gates:
            continue
        gate = metric_gates[metric]
        prefix = failure_prefixes[metric]
        if metric == "steady_state_execution_elapsed_ms" and tier == "simple":
            if gate["status"] != "pass":
                performance_diagnostics.append("steady_state_tolerance_loss")
            continue
        if metric == "steady_state_execution_elapsed_ms" and tier == "complex":
            if not gate["aggregate_global_lower"]:
                performance_diagnostics.append("steady_state_aggregate_loss")
            continue
        for field, suffix in gate_failure_fields.items():
            if field == "strict_majority_better" and not gate["strict_majority_required"]:
                continue
            if field == "worst_pair_regression_within_limit" and not gate["regression_bound_required"]:
                continue
            if not gate[field]:
                performance_diagnostics.append(f"{prefix}_{suffix}")
    direct_totals = {metric: metric_gates[metric]["direct_total"] if metric in metric_gates else None for metric in metric_names}
    global_totals = {metric: metric_gates[metric]["global_total"] if metric in metric_gates else None for metric in metric_names}
    direct_medians = {metric: metric_gates[metric]["direct_median"] if metric in metric_gates else None for metric in metric_names}
    global_medians = {metric: metric_gates[metric]["global_median"] if metric in metric_gates else None for metric in metric_names}
    paired_savings_medians = {metric: metric_gates[metric]["paired_savings_percent_median"] if metric in metric_gates else None for metric in metric_names}
    paired_wins = {metric: metric_gates[metric]["better_pairs"] if metric in metric_gates else 0 for metric in metric_names}
    public_metric_gates = {
        metric: {key: value for key, value in gate.items() if key not in {"direct_total", "global_total", "direct_median", "global_median", "paired_savings_percent_median", "better_pairs"}}
        for metric, gate in metric_gates.items() if metric in GATED_METRICS
    }
    return {"status": "pass" if not failures else "fail", "optimization_status": "pass" if not performance_diagnostics else "fail", "failures": failures, "performance_diagnostics": performance_diagnostics, "failed_run_ids": arm_failures, "run_count": len(tier_manifests), "pair_count": repeat_count, "paired_wins": paired_wins, "direct_totals": direct_totals, "global_totals": global_totals, "direct_medians": direct_medians, "global_medians": global_medians, "paired_savings_percent_medians": paired_savings_medians, "metric_gates": public_metric_gates}


def evaluate_suite(plan_path, manifest_dir, summary_path):
    plan, plan_bytes = load_json_object(plan_path, "plan_invalid")
    validate_plan(plan)
    plan_sha256 = sha256_bytes(plan_bytes)
    plan_root = plan_path.parent
    manifests = [evaluate_run(plan_root, plan["suite_id"], plan_sha256, run_plan) for run_plan in plan["runs"]]
    tier_repeat_counts = repeat_counts_from_plan(plan)
    tier_summaries = {tier: aggregate_tier(tier, tier_repeat_counts[tier], manifests) for tier in TIERS}
    all_correct = all(tier_summaries[tier]["status"] == "pass" for tier in TIERS)
    direct_manifests = sorted([manifest for manifest in manifests if manifest["arm"] == "direct"], key=lambda item: item["order_index"])
    global_manifests = sorted([manifest for manifest in manifests if manifest["arm"] == "global"], key=lambda item: item["order_index"])
    cohort_metric_gates = {}
    for metric in GATED_METRICS:
        direct_total = sum(manifest[metric] for manifest in direct_manifests) if all(isinstance(manifest.get(metric), int) and manifest[metric] >= 0 for manifest in direct_manifests) else None
        global_total = sum(manifest[metric] for manifest in global_manifests) if all(isinstance(manifest.get(metric), int) and manifest[metric] >= 0 for manifest in global_manifests) else None
        cohort_metric_gates[metric] = {"direct_total": direct_total, "global_total": global_total, "aggregate_global_lower": isinstance(direct_total, int) and isinstance(global_total, int) and global_total < direct_total, "status": "pass" if isinstance(direct_total, int) and isinstance(global_total, int) and global_total < direct_total else "fail"}
    overall_status = "pass" if all_correct and all(gate["status"] == "pass" for gate in cohort_metric_gates.values()) else "fail"
    uniform_repeat_count = next(iter(set(tier_repeat_counts.values()))) if len(set(tier_repeat_counts.values())) == 1 else None
    summary = {"schema_version": SCHEMA_VERSION, "suite_id": plan["suite_id"], "plan_sha256": plan_sha256, "repeat_count": uniform_repeat_count, "tier_repeat_counts": tier_repeat_counts, "overall_status": overall_status, "all_correct": all_correct, "overall_rule": OVERALL_RULE, "time_rule": TIME_RULE, "token_rule": TOKEN_RULE, "cohort_metric_gates": cohort_metric_gates, "tiers": tier_summaries}
    for manifest in manifests:
        atomic_write_json(manifest_dir / f"{manifest['run_id']}.json", manifest)
    atomic_write_json(summary_path, summary)
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate exact receipt-backed benchmark manifests and strict per-tier paired summaries.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        summary = evaluate_suite(args.plan, args.manifest_dir, args.summary)
    except BenchmarkGateError as error:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "error", "failure": error.code}, separators=(",", ":")))
        return 2
    print(json.dumps(summary, separators=(",", ":")))
    return 0 if summary["overall_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
