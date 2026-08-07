#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
if os.name == "nt":
    import msvcrt
elif os.name == "posix":
    import fcntl
else:
    raise RuntimeError(f"Unsupported host OS for routing-history locking: {os.name}")
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkstemp

try:
    from routing_policy import (
        EXECUTION_DOMAINS,
        EXECUTION_DOMAIN_REGISTRY_LEGACY,
        EXECUTION_DOMAIN_REGISTRY_VERSION,
        MODEL_EFFORTS,
        MODEL_ORDER,
        adaptive_pair_texts_for_profile,
        canonical_pairs,
        compare_pair,
        downgrade_pair,
        infer_execution_domain,
        is_tiny_spark_profile,
        normal_adaptive_pair_texts,
        parse_pair,
        pair_text,
        PROFILE_PRESET_VERSION,
        profile_preset_names,
        public_profile_preset_rows,
        public_execution_domain_rows,
        resolve_profile_preset,
        upgrade_pair,
    )
except ModuleNotFoundError:
    import importlib.util
    import sys

    _routing_policy_path = Path(__file__).with_name("routing_policy.py")
    _routing_policy_spec = importlib.util.spec_from_file_location("task_analyze_routing_policy", _routing_policy_path)
    _routing_policy = importlib.util.module_from_spec(_routing_policy_spec)
    sys.modules[_routing_policy_spec.name] = _routing_policy
    _routing_policy_spec.loader.exec_module(_routing_policy)
    EXECUTION_DOMAINS = _routing_policy.EXECUTION_DOMAINS
    EXECUTION_DOMAIN_REGISTRY_LEGACY = _routing_policy.EXECUTION_DOMAIN_REGISTRY_LEGACY
    EXECUTION_DOMAIN_REGISTRY_VERSION = _routing_policy.EXECUTION_DOMAIN_REGISTRY_VERSION
    MODEL_EFFORTS = _routing_policy.MODEL_EFFORTS
    MODEL_ORDER = _routing_policy.MODEL_ORDER
    adaptive_pair_texts_for_profile = _routing_policy.adaptive_pair_texts_for_profile
    canonical_pairs = _routing_policy.canonical_pairs
    compare_pair = _routing_policy.compare_pair
    downgrade_pair = _routing_policy.downgrade_pair
    infer_execution_domain = _routing_policy.infer_execution_domain
    is_tiny_spark_profile = _routing_policy.is_tiny_spark_profile
    normal_adaptive_pair_texts = _routing_policy.normal_adaptive_pair_texts
    parse_pair = _routing_policy.parse_pair
    pair_text = _routing_policy.pair_text
    PROFILE_PRESET_VERSION = _routing_policy.PROFILE_PRESET_VERSION
    profile_preset_names = _routing_policy.profile_preset_names
    public_profile_preset_rows = _routing_policy.public_profile_preset_rows
    public_execution_domain_rows = _routing_policy.public_execution_domain_rows
    resolve_profile_preset = _routing_policy.resolve_profile_preset
    upgrade_pair = _routing_policy.upgrade_pair

try:
    from skill_resolver import canonicalize_installed_skill_id
except ModuleNotFoundError:
    import importlib.util as _skill_importlib_util

    _skill_resolver_path = Path(__file__).with_name("skill_resolver.py")
    _skill_resolver_spec = _skill_importlib_util.spec_from_file_location("task_analyze_skill_canonicalizer", _skill_resolver_path)
    _skill_resolver = _skill_importlib_util.module_from_spec(_skill_resolver_spec)
    _skill_resolver_spec.loader.exec_module(_skill_resolver)
    canonicalize_installed_skill_id = _skill_resolver.canonicalize_installed_skill_id


DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[1] / "local" / "adaptive-routing" / "model_experience.json"
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
DEFAULT_SKILLS_ROOT = DEFAULT_CODEX_HOME / "skills"
DEFAULT_PLUGINS_CACHE_ROOT = DEFAULT_CODEX_HOME / "plugins" / "cache"
SCHEMA_VERSION = 3
CONTROL_FIELDS = ["task_family", "artifact", "scope", "ambiguity", "modality", "risk", "complexity", "owning_skill", "project_family", "verification_shape", "execution_domain"]
ACTIVE_VERIFICATION_SHAPES = {"real"}
HISTORY_ONLY_VERIFICATION_SHAPES = {"mini", "mini_real", "result"}
CONTROL_ENUMS = {
    "task_family": {"code", "direct", "grounded", "integration", "visual", "management", "prompt", "document", "data", "safety", "legacy", "tiny_text", "tiny_code", "command_generation", "other"},
    "artifact": {"answer", "script", "note", "report", "evidence", "document", "patch", "log", "legacy"},
    "scope": {"single", "multi", "project"},
    "ambiguity": {"low", "medium", "high"},
    "modality": {"text", "image", "mixed"},
    "risk": {"low", "medium", "high"},
    "complexity": {"easy", "complex"},
    "verification_shape": ACTIVE_VERIFICATION_SHAPES | HISTORY_ONLY_VERIFICATION_SHAPES,
    "execution_domain": set(EXECUTION_DOMAINS),
}
QUALITY_FAILURES = {"quality", "correctness"}
RUNTIME_FAILURES = {"availability", "timeout", "protocol", "telemetry", "execution", "receipt"}
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PLUGIN_SKILL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}:[a-z0-9][a-z0-9-]{0,63}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _acquire_file_lock(lock_handle):
    if os.name == "nt":
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write("\0")
            lock_handle.flush()
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(lock_handle):
    if os.name == "nt":
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_SUMMARY = [re.compile(r"```"), re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://"), re.compile(r"(?:^|\s)/[^\s]+"), re.compile(r"\b(?:api|auth|secret|password|token)[_-]?(?:key|token|secret|password)?\s*[:=]", re.I)]


def sanitize_slug(value, *, field=None):
    value = str(value).strip().lower()
    if field == "owning_skill" and PLUGIN_SKILL_PATTERN.fullmatch(value):
        return value
    if not SLUG_PATTERN.fullmatch(value):
        raise ValueError("condition values must be short lowercase slugs")
    return value


def _infer_or_get_domain(values):
    explicit = values.get("execution_domain")
    explicit_domain = None
    if explicit is not None:
        explicit_domain = str(explicit).strip().lower()
        if explicit_domain == "":
            explicit_domain = None
    if explicit_domain is None:
        return infer_execution_domain(owning_skill=values.get("owning_skill"), task_family=values.get("task_family"))
    return infer_execution_domain(
        owning_skill=values.get("owning_skill"),
        task_family=values.get("task_family"),
        explicit_domain=explicit_domain,
    )


def validate_condition(values, *, allow_history_only=False):
    if not isinstance(values, dict):
        raise ValueError("routing condition must be a mapping")
    supplied = dict(values)
    raw_explicit = supplied.get("execution_domain")
    has_explicit_domain = raw_explicit is not None and str(raw_explicit).strip() != ""
    try:
        supplied["execution_domain"] = _infer_or_get_domain(supplied)
    except ValueError as error:
        raise ValueError(f"execution_domain is unknown: {error}")
    if not allow_history_only and not EXECUTION_DOMAINS[supplied["execution_domain"]].get("active", False):
        if has_explicit_domain:
            raise ValueError(f"execution_domain must be active: {supplied['execution_domain']}")
        raise ValueError(f"execution_domain must not infer to inactive row: {supplied['execution_domain']}")
    condition = {field: sanitize_slug(supplied[field], field=field) for field in CONTROL_FIELDS}
    for field, allowed in CONTROL_ENUMS.items():
        if condition[field] not in allowed:
            raise ValueError(f"{field} is invalid")
    if not allow_history_only and condition["verification_shape"] not in ACTIVE_VERIFICATION_SHAPES:
        raise ValueError("active profiles require verification_shape=real")
    return condition


def validate_summary(summary):
    if not isinstance(summary, str) or summary != summary.strip() or "\n" in summary or "\r" in summary or not 24 <= len(summary) <= 280:
        raise ValueError("task_summary must be one line with 24-280 characters")
    if any(pattern.search(summary) for pattern in FORBIDDEN_SUMMARY):
        raise ValueError("task_summary contains private content")
    return summary


def condition_key(condition, *, allow_history_only=False):
    condition = validate_condition(condition, allow_history_only=allow_history_only)
    payload = json.dumps({field: condition[field] for field in CONTROL_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def empty_history():
    return {"schema_version": SCHEMA_VERSION, "updated_at": datetime.now(timezone.utc).isoformat(), "conditions": {}}


def _read_json(path):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_int(value):
    return value if isinstance(value, int) and value >= 0 else None


def _safe_text(value, fallback=None):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _safe_sha256(value):
    if isinstance(value, str) and SHA256_PATTERN.fullmatch(value):
        return value
    return None


def _dedupe_pairs(values):
    seen = set()
    unique = []
    for value in values:
        if isinstance(value, tuple):
            pair = value
        else:
            try:
                pair = parse_pair(value)
            except (TypeError, ValueError):
                continue
        if pair not in seen:
            seen.add(pair)
            unique.append(pair)
    return unique


def _write_locked(path, history):
    history["updated_at"] = datetime.now(timezone.utc).isoformat()
    descriptor, temporary_path = mkstemp(prefix=".model_experience-", suffix=".json", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(history, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)
    os.chmod(path, 0o600)


def _parse_optional_pairs(values):
    parsed = []
    if not isinstance(values, list):
        return []
    for value in values:
        try:
            pair = parse_pair(value)
        except (TypeError, ValueError):
            continue
        if pair not in parsed:
            parsed.append(pair)
    return parsed


def _resolve_pair_text(value, pairs, fallback):
    try:
        pair = parse_pair(value) if isinstance(value, str) else parse_pair(str(value))
        if pair in pairs:
            return pair
    except (TypeError, ValueError):
        pass
    return fallback


def _normalize_task(raw_task, pair_fallback):
    if not isinstance(raw_task, dict):
        return None
    requested = _parse_optional_pairs([raw_task.get("requested_pair")])
    resolved = _parse_optional_pairs([raw_task.get("resolved_pair")])
    effective = _parse_optional_pairs([raw_task.get("effective_pair")])
    executed = _parse_optional_pairs([raw_task.get("executed_pair")])
    run_id = _safe_text(raw_task.get("run_id"))
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        run_id = f"run_{os.urandom(8).hex()}"
    requested_pair = requested[0] if requested else pair_fallback
    resolved_pair = resolved[0] if resolved else requested_pair
    effective_pair = effective[0] if effective else resolved_pair
    executed_pair = executed[0] if executed else effective_pair
    operational_pairs = _parse_optional_pairs(raw_task.get("operational_failure_pairs", []))
    return {
        "run_id": run_id,
        "summary": _safe_text(raw_task.get("summary"), "Migrated routing-history task."),
        "requested_pair": pair_text(*requested_pair),
        "resolved_pair": pair_text(*resolved_pair),
        "effective_pair": pair_text(*effective_pair),
        "executed_pair": pair_text(*executed_pair),
        "operational_failure_pairs": canonical_pair_texts(operational_pairs),
        "receipt_status": "pass" if raw_task.get("receipt_status") == "pass" and raw_task.get("turn_completed") is True and raw_task.get("model_match") is True and raw_task.get("effort_match") is True else "fail",
        "mini_status": raw_task.get("mini_status", "unknown"),
        "real_status": raw_task.get("real_status", "unknown"),
        "effective_verdict": None,
        "allowlisted_failure_class": raw_task.get("allowlisted_failure_class", "none"),
        "turn_completed": bool(raw_task.get("turn_completed") is True),
        "model_match": bool(raw_task.get("model_match") is True),
        "effort_match": bool(raw_task.get("effort_match") is True),
        "trial": bool(raw_task.get("trial")),
        "workload_prompt_sha256": _safe_sha256(raw_task.get("workload_prompt_sha256")),
        "token_totals": {
            "input": _safe_int((raw_task.get("token_totals") or {}).get("input")),
            "cached_input": _safe_int((raw_task.get("token_totals") or {}).get("cached_input")),
            "output": _safe_int((raw_task.get("token_totals") or {}).get("output")),
            "reasoning_output": _safe_int((raw_task.get("token_totals") or {}).get("reasoning_output")),
            "total": _safe_int((raw_task.get("token_totals") or {}).get("total")),
        },
        "process_ms": _safe_int(raw_task.get("process_ms")),
        "recorded_at": _safe_text(raw_task.get("recorded_at"), datetime.now(timezone.utc).isoformat()),
    }


def canonical_pair_texts(pairs):
    return [pair_text(*pair) for pair in canonical_pairs(pairs)]


def profile_fingerprint(condition, pairs, static_pair, hard_pair):
    payload = {"condition": {field: condition[field] for field in CONTROL_FIELDS}, "candidate_ladder": canonical_pair_texts(pairs), "static_suggestion": pair_text(*static_pair), "hard_floor": pair_text(*hard_pair)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ladder_fingerprint(pairs, hard_pair):
    payload = {"candidate_ladder": canonical_pair_texts(pairs), "hard_floor": pair_text(*hard_pair)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _durable_real_verdict(task):
    if task.get("receipt_status") != "pass" or task.get("turn_completed") is not True or task.get("model_match") is not True or task.get("effort_match") is not True:
        return None
    failure_class = task.get("allowlisted_failure_class")
    if failure_class in QUALITY_FAILURES and task.get("real_status") == "fail":
        return "fail"
    operational_pairs = task.get("operational_failure_pairs") if isinstance(task.get("operational_failure_pairs"), list) else []
    if failure_class in RUNTIME_FAILURES or task.get("executed_pair") in operational_pairs:
        return None
    if task.get("real_status") == "pass":
        return "pass"
    return None


def _seed_legacy_real_evidence(history, record, condition, summary, pairs):
    existing_run_ids = {task.get("run_id") for task in record.get("tasks", []) if isinstance(task, dict)}
    for _, legacy_record in list(history["conditions"].items()):
        if legacy_record is record or not isinstance(legacy_record, dict):
            continue
        try:
            legacy_condition = validate_condition(legacy_record.get("condition"), allow_history_only=True)
        except ValueError:
            continue
        if legacy_condition["verification_shape"] not in HISTORY_ONLY_VERIFICATION_SHAPES:
            continue
        if any(legacy_condition[field] != condition[field] for field in CONTROL_FIELDS if field != "verification_shape"):
            continue
        legacy_pairs = canonical_pairs(legacy_record.get("candidate_ladder", []))
        fallback_pair = legacy_pairs[0] if legacy_pairs else pairs[0]
        source_key = condition_key(legacy_condition, allow_history_only=True)
        for raw_task in legacy_record.get("tasks", []):
            raw_run_id = _safe_text(raw_task.get("run_id")) if isinstance(raw_task, dict) else None
            if not raw_run_id or not RUN_ID_PATTERN.fullmatch(raw_run_id) or not _parse_optional_pairs([raw_task.get("requested_pair")]) or not _parse_optional_pairs([raw_task.get("executed_pair")]):
                continue
            task = _normalize_task(raw_task, fallback_pair)
            verdict = _durable_real_verdict(task)
            if verdict is None:
                continue
            executed_pair = parse_pair(task["executed_pair"])
            if executed_pair not in pairs:
                continue
            identity = json.dumps({"source": source_key, "run_id": task["run_id"], "executed_pair": task["executed_pair"], "real_verdict": verdict}, sort_keys=True, separators=(",", ":"))
            run_id = f"legacy_real_{hashlib.sha256(identity.encode()).hexdigest()[:40]}"
            if run_id in existing_run_ids:
                continue
            migrated_task = {"run_id": run_id, "summary": summary, "requested_pair": task["requested_pair"], "resolved_pair": task["resolved_pair"], "effective_pair": task["effective_pair"], "executed_pair": task["executed_pair"], "operational_failure_pairs": [], "receipt_status": "pass", "real_status": task["real_status"], "effective_verdict": verdict, "allowlisted_failure_class": task["allowlisted_failure_class"], "turn_completed": True, "model_match": True, "effort_match": True, "trial": task["trial"], "workload_prompt_sha256": None, "token_totals": {"input": None, "cached_input": None, "output": None, "reasoning_output": None, "total": None}, "process_ms": None, "recorded_at": task["recorded_at"], "evidence_origin": "legacy_real_boundary", "source_verification_shape": legacy_condition["verification_shape"]}
            record["tasks"].append(migrated_task)
            existing_run_ids.add(run_id)


def _record_for(history, condition, summary, pairs, static_pair, hard_pair):
    key = condition_key(condition)
    record = history["conditions"].setdefault(
        key,
        {
            "condition": condition,
            "summary": summary,
            "candidate_ladder": [pair_text(*pair) for pair in pairs],
            "static_suggestion": pair_text(*static_pair),
            "hard_floor": pair_text(*hard_pair),
            "success_model": None,
            "failed_model": None,
            "active_ladder_fingerprint": ladder_fingerprint(pairs, hard_pair),
            "profile_fingerprint": profile_fingerprint(condition, pairs, static_pair, hard_pair),
            "calibration_state": "cold_start",
            "best_pair": None,
            "selection_basis": "cold_start",
            "cost_evidence": {"status": "not_evaluated", "compared_pairs": [], "shared_cohort_count": 0, "shared_cohort_digest": None, "scores": {}},
            "tasks": [],
        },
    )
    record["condition"] = condition
    record["summary"] = summary
    merged_pairs = _dedupe_pairs(list(pairs) + list(record.get("candidate_ladder", [])))
    record["candidate_ladder"] = canonical_pair_texts(merged_pairs)
    record["static_suggestion"] = pair_text(*static_pair)
    record["hard_floor"] = pair_text(*hard_pair)
    active_fingerprint = ladder_fingerprint(pairs, hard_pair)
    if record.get("active_ladder_fingerprint") != active_fingerprint:
        record["calibration_state"] = "cold_start"
        record["best_pair"] = None
        record["selection_basis"] = "ladder_changed"
        record["cost_evidence"] = {"status": "not_evaluated", "compared_pairs": [], "shared_cohort_count": 0, "shared_cohort_digest": None, "scores": {}}
    record["active_ladder_fingerprint"] = active_fingerprint
    record["profile_fingerprint"] = profile_fingerprint(condition, pairs, static_pair, hard_pair)
    record.setdefault("calibration_state", "cold_start")
    record.setdefault("best_pair", None)
    record.setdefault("selection_basis", "cold_start")
    record.setdefault("cost_evidence", {"status": "not_evaluated", "compared_pairs": [], "shared_cohort_count": 0, "shared_cohort_digest": None, "scores": {}})
    _seed_legacy_real_evidence(history, record, condition, summary, pairs)
    return record


def _normalize_history_record(raw_record, raw_key):
    if not isinstance(raw_record, dict):
        return None, None
    raw_condition_payload = raw_record.get("condition")
    if not isinstance(raw_condition_payload, dict):
        return None, None
    raw_condition = {
        "task_family": raw_condition_payload.get("task_family", raw_condition_payload.get("category", "legacy")),
        "artifact": raw_condition_payload.get("artifact", "legacy"),
        "scope": raw_condition_payload.get("scope", raw_condition_payload.get("phase", "result")),
        "ambiguity": raw_condition_payload.get("ambiguity", "medium"),
        "modality": raw_condition_payload.get("modality", "text"),
        "risk": raw_condition_payload.get("risk", "low"),
        "complexity": raw_condition_payload.get("complexity", "easy"),
        "owning_skill": raw_condition_payload.get("owning_skill", "workflow-skill"),
        "project_family": raw_condition_payload.get("project_family", "legacy"),
        "verification_shape": raw_condition_payload.get("verification_shape", raw_condition_payload.get("phase", "result")),
        "execution_domain": raw_condition_payload.get("execution_domain", raw_record.get("execution_domain", None)),
    }
    try:
        condition = validate_condition(raw_condition, allow_history_only=True)
    except ValueError:
        return None, None
    pairs = canonical_pairs(raw_record.get("candidate_ladder", []))
    if not pairs:
        return None, None
    static_pair = _resolve_pair_text(raw_record.get("static_suggestion"), pairs, pairs[0])
    hard_pair = _resolve_pair_text(raw_record.get("hard_floor"), pairs, pairs[0])
    record = {"summary": _safe_text(raw_record.get("summary"), "Migrated routing-history summary."), "condition": condition}
    record["candidate_ladder"] = canonical_pair_texts(pairs)
    record["static_suggestion"] = pair_text(*static_pair)
    record["hard_floor"] = pair_text(*hard_pair)
    record["success_model"] = raw_record.get("success_model")
    record["failed_model"] = raw_record.get("failed_model")
    record["tasks"] = []
    fallback_pair = pairs[0]
    for task in raw_record.get("tasks", []):
        normalized = _normalize_task(task, fallback_pair)
        if normalized is not None:
            record["tasks"].append(normalized)
    return condition_key(condition, allow_history_only=True), record


def _legacy_history(legacy_path):
    history = empty_history()
    if not legacy_path.exists():
        return history
    with legacy_path.open(encoding="utf-8", errors="ignore") as handle:
        lines = list(handle)
    for line in lines:
        try:
            event = json.loads(line)
            requested = parse_pair(f"{event['requested_model']}|{event['requested_effort']}")
            executed = parse_pair(f"{event.get('effective_model') or event.get('resolved_model') or requested[0]}|{event.get('effective_effort') or event.get('resolved_effort') or requested[1]}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        raw_condition = {
            "task_family": event.get("task_family", event.get("category", "legacy")),
            "artifact": event.get("artifact", "legacy"),
            "scope": event.get("scope", event.get("phase", "result")),
            "ambiguity": event.get("ambiguity", "medium"),
            "modality": event.get("modality", "text"),
            "risk": event.get("risk", "low"),
            "complexity": event.get("complexity", "easy"),
            "owning_skill": event.get("owning_skill", "workflow-skill"),
            "project_family": event.get("project_family", "legacy"),
            "verification_shape": event.get("verification_shape", event.get("phase", "result")),
            "execution_domain": None,
        }
        try:
            condition = validate_condition(raw_condition, allow_history_only=True)
        except ValueError:
            continue
        key = condition_key(condition, allow_history_only=True)
        record = history["conditions"].setdefault(key, {"condition": condition, "summary": "Legacy adaptive-routing evidence imported without task content.", "candidate_ladder": [], "static_suggestion": pair_text(*executed), "hard_floor": pair_text(*executed), "success_model": None, "failed_model": None, "tasks": []})
        for pair in (requested, executed):
            text = pair_text(*pair)
            if text not in record["candidate_ladder"]:
                record["candidate_ladder"].append(text)
        status = event.get("verify_status") if event.get("verify_status") in {"pass", "fail"} else "unknown"
        failure_class = event.get("failure_class", "none")
        task = {
            "run_id": f"legacy_{os.urandom(8).hex()}",
            "summary": "Legacy adaptive-routing evidence imported without task content.",
            "requested_pair": pair_text(*requested),
            "resolved_pair": pair_text(*requested),
            "effective_pair": pair_text(*executed),
            "executed_pair": pair_text(*executed),
            "operational_failure_pairs": [],
            "receipt_status": "pass" if event.get("receipt_status") == "pass" and event.get("turn_completed") is True else "fail",
            "mini_status": status if event.get("verify_level") == "mini" else "unknown",
            "real_status": status if event.get("verify_level") == "real" else "unknown",
            "effective_verdict": None,
            "allowlisted_failure_class": failure_class if failure_class in QUALITY_FAILURES | RUNTIME_FAILURES else "none",
            "turn_completed": bool(event.get("turn_completed") is True or event.get("receipt_status") == "pass"),
            "model_match": True,
            "effort_match": True,
            "trial": bool(event.get("trial")),
            "workload_prompt_sha256": None,
            "token_totals": {"input": None, "cached_input": None, "output": None, "reasoning_output": None, "total": None},
            "process_ms": None,
            "recorded_at": event.get("recorded_at") or datetime.now(timezone.utc).isoformat(),
        }
        record["tasks"].append(task)
    for record in history["conditions"].values():
        record["candidate_ladder"] = canonical_pair_texts(record["candidate_ladder"])
        recompute_bounds(record)
    return history


def _history_from_schema2(raw_history):
    if not isinstance(raw_history, dict) or raw_history.get("schema_version") != 2:
        return None
    conditions = raw_history.get("conditions")
    if not isinstance(conditions, dict):
        return None
    history = empty_history()
    for raw_key, raw_record in conditions.items():
        key, record = _normalize_history_record(raw_record, raw_key)
        if key is None:
            continue
        candidate_success = record.get("success_model")
        candidate_failed = record.get("failed_model")
        if key in history["conditions"]:
            existing = history["conditions"][key]
            existing_pairs = canonical_pairs(existing.get("candidate_ladder", []))
            incoming_pairs = canonical_pairs(record.get("candidate_ladder", []))
            merged_pairs = canonical_pair_texts(_dedupe_pairs(existing_pairs + incoming_pairs))
            existing["candidate_ladder"] = merged_pairs
            existing["tasks"].extend(record.get("tasks", []))
            existing["summary"] = record["summary"]
            existing["static_suggestion"] = record["static_suggestion"]
            existing["hard_floor"] = record["hard_floor"]
            if candidate_success:
                existing["success_model"] = candidate_success
            if candidate_failed:
                existing["failed_model"] = candidate_failed
            recompute_bounds(existing)
            if candidate_success:
                existing["success_model"] = candidate_success
            if candidate_failed:
                existing["failed_model"] = candidate_failed
        else:
            history["conditions"][key] = record
            recompute_bounds(record)
            if candidate_success:
                history["conditions"][key]["success_model"] = candidate_success
            if candidate_failed:
                history["conditions"][key]["failed_model"] = candidate_failed
    return history


def _history_locked(path, mutate=None):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    migrated = None
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name != "nt":
            os.chmod(lock_path, 0o600)
        _acquire_file_lock(lock)
        history = _read_json(path)
        if isinstance(history, dict) and history.get("schema_version") == SCHEMA_VERSION and isinstance(history.get("conditions"), dict):
            migrated = history
        else:
            if isinstance(history, dict) and history.get("schema_version") == 2:
                migrated = _history_from_schema2(history)
            if not isinstance(migrated, dict):
                migrated = _legacy_history(path.with_name("events.jsonl"))
        value = mutate(migrated) if mutate else None
        if mutate is not None:
            _write_locked(path, migrated)
        _release_file_lock(lock)
    return migrated, value


def load_history(path):
    if Path(path).expanduser().resolve() == DEFAULT_HISTORY_PATH.resolve():
        return _history_locked(path)[0]
    # Explicit non-default paths remain available to compatibility tests and
    # one-off legacy migration tools.
    return _history_locked(path, lambda history: history)[0]


def task_verdict(task, *, provisional=False):
    if task.get("receipt_status") != "pass" or task.get("turn_completed") is not True or task.get("model_match") is not True or task.get("effort_match") is not True:
        return None
    failure_class = task.get("allowlisted_failure_class")
    if failure_class in QUALITY_FAILURES and (
        task.get("mini_status") == "fail" or task.get("real_status") == "fail"
    ):
        return "fail"
    if failure_class in RUNTIME_FAILURES or task.get("executed_pair") in task.get("operational_failure_pairs", []):
        return None
    if task.get("real_status") == "pass":
        return "pass"
    if task.get("mini_status") == "pass" and provisional:
        return "pass"
    return None


def _strongest_pair(values):
    selected = None
    for pair in values:
        if selected is None or compare_pair(pair, selected) > 0:
            selected = pair
    return selected


def _weakest_pair(values):
    selected = None
    for pair in values:
        if selected is None or compare_pair(pair, selected) < 0:
            selected = pair
    return selected


def recompute_bounds(record, active_pairs=None):
    pairs = canonical_pairs(record["candidate_ladder"])
    run_pair_verdicts = {}
    for task in record["tasks"]:
        try:
            pair = parse_pair(task.get("executed_pair"))
        except (TypeError, ValueError):
            continue
        if pair not in pairs:
            continue
        verification_shape = record.get("condition", {}).get("verification_shape")
        verdict = task_verdict(task, provisional=verification_shape == "mini")
        task["effective_verdict"] = verdict
        key = (pair, task.get("run_id"))
        if verdict == "fail":
            run_pair_verdicts[key] = "fail"
        elif verdict == "pass" and run_pair_verdicts.get(key) != "fail":
            run_pair_verdicts[key] = "pass"
    verdicts = {}
    for (pair, _), verdict in run_pair_verdicts.items():
        if verdict == "fail":
            verdicts[pair] = "fail"
        elif verdicts.get(pair) != "fail":
            verdicts[pair] = "pass"
    failures = [pair for pair, verdict in verdicts.items() if verdict == "fail"]
    strongest_failure = _strongest_pair(failures)
    success_candidates = [
        pair for pair, verdict in verdicts.items()
        if verdict == "pass" and (strongest_failure is None or compare_pair(pair, strongest_failure) > 0)
    ]
    record["failed_model"] = pair_text(*strongest_failure) if strongest_failure is not None else None
    record["success_model"] = pair_text(*_weakest_pair(success_candidates)) if success_candidates else None
    candidate_pairs = active_pairs or pairs
    hard_pair = parse_pair(record["hard_floor"])
    hard_floor_real_pass = hard_pair in _passes_within(candidate_pairs, record)
    boundary_complete = strongest_failure is not None and record["success_model"] is not None and not _has_untested_between(strongest_failure, parse_pair(record["success_model"]), record, pairs)
    if record.get("calibration_state") == "frozen" and record.get("best_pair"):
        frozen_pair = parse_pair(record["best_pair"])
        frozen_pair_still_valid = (
            frozen_pair in candidate_pairs
            and compare_pair(frozen_pair, hard_pair) >= 0
            and _pair_verdict(record, frozen_pair) == "pass"
            and (strongest_failure is None or compare_pair(frozen_pair, strongest_failure) > 0)
        )
        if frozen_pair_still_valid and record.get("selection_basis") != "receipt_cost":
            return
    if (hard_floor_real_pass and strongest_failure is None) or boundary_complete:
        eligible_pairs = _pairs_between_bounds(candidate_pairs, hard_pair, strongest_failure)
        real_passing = _passes_within(eligible_pairs, record)
        passing_pairs = [pair for pair in eligible_pairs if pair in real_passing]
        _, cost_evidence = _like_for_like_cost_scores(record, passing_pairs)
        record["cost_evidence"] = cost_evidence
        best_pair = _pick_weakest_passing_above_failure(candidate_pairs, strongest_failure, hard_pair, record)
        record["selection_basis"] = "quality_boundary"
        record["calibration_state"] = "frozen"
        record["best_pair"] = pair_text(*best_pair) if best_pair is not None else None
    elif strongest_failure is not None:
        record["calibration_state"] = "quality_boundary"
        record["best_pair"] = record["success_model"]
        record["selection_basis"] = "quality_boundary"
        record["cost_evidence"] = _cost_evidence("not_evaluated", [])
    elif record.get("success_model"):
        record["calibration_state"] = "provisional"
        record["best_pair"] = record["success_model"]
        record["selection_basis"] = "real_pass"
        record["cost_evidence"] = _cost_evidence("not_evaluated", [])
    else:
        record["calibration_state"] = "cold_start"
        record["best_pair"] = None
        record["selection_basis"] = "cold_start"
        record["cost_evidence"] = _cost_evidence("not_evaluated", [])


def _pairs_between_bounds(pairs, hard_floor, failure_pair):
    floor = hard_floor
    def _within(pair):
        if compare_pair(pair, floor) < 0:
            return False
        if failure_pair is None:
            return True
        return compare_pair(pair, failure_pair) > 0
    return [pair for pair in pairs if _within(pair)]


def _passes_within(pairs, record):
    passing = set()
    for task in record["tasks"]:
        if task_verdict(task) != "pass" or task.get("real_status") != "pass":
            continue
        try:
            pair = parse_pair(task.get("executed_pair"))
        except (TypeError, ValueError):
            continue
        if pair in pairs:
            passing.add(pair)
    return passing


def _pair_verdict(record, target_pair):
    result = None
    for task in record["tasks"]:
        try:
            pair = parse_pair(task.get("executed_pair"))
        except (TypeError, ValueError):
            continue
        if pair != target_pair:
            continue
        verdict = task_verdict(task)
        if verdict == "fail":
            return "fail"
        if verdict == "pass":
            result = "pass"
    return result


def _has_unverified_quality_failure(record):
    return any(task.get("allowlisted_failure_class") in QUALITY_FAILURES and task.get("receipt_status") != "pass" for task in record["tasks"])


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _cost_evidence(status, pairs, *, shared_hashes=None, scores=None):
    shared_hashes = sorted(shared_hashes or [])
    digest = None
    if shared_hashes:
        digest = hashlib.sha256("\n".join(shared_hashes).encode()).hexdigest()
    serialized_scores = {}
    for pair, score in (scores or {}).items():
        serialized_scores[pair_text(*pair)] = {
            "median_total_tokens": score[0],
            "median_process_ms": score[1],
        }
    return {
        "status": status,
        "compared_pairs": [pair_text(*pair) for pair in pairs],
        "shared_cohort_count": len(shared_hashes),
        "shared_cohort_digest": digest,
        "scores": serialized_scores,
    }


def _like_for_like_cost_scores(record, passing_pairs):
    pairs = list(passing_pairs)
    if len(pairs) < 2:
        return None, _cost_evidence("insufficient_pairs", pairs)

    grouped = {pair: {} for pair in pairs}
    for task in record["tasks"]:
        if task_verdict(task) != "pass" or task.get("real_status") != "pass":
            continue
        try:
            executed_pair = parse_pair(task.get("executed_pair"))
        except (TypeError, ValueError):
            continue
        workload_hash = _safe_sha256(task.get("workload_prompt_sha256"))
        if executed_pair not in grouped or workload_hash is None:
            continue
        sample = (
            _safe_int((task.get("token_totals") or {}).get("total")),
            _safe_int(task.get("process_ms")),
        )
        grouped[executed_pair].setdefault(workload_hash, []).append(sample)

    common_hashes = set(grouped[pairs[0]])
    for pair in pairs[1:]:
        common_hashes.intersection_update(grouped[pair])
    if not common_hashes:
        return None, _cost_evidence("no_common_workload", pairs)

    per_pair_cohort_scores = {pair: [] for pair in pairs}
    for workload_hash in sorted(common_hashes):
        for pair in pairs:
            samples = grouped[pair][workload_hash]
            if any(total_tokens is None or process_ms is None for total_tokens, process_ms in samples):
                return None, _cost_evidence("incomplete_metrics", pairs, shared_hashes=common_hashes)
            per_pair_cohort_scores[pair].append(
                (
                    _median([total_tokens for total_tokens, _ in samples]),
                    _median([process_ms for _, process_ms in samples]),
                )
            )

    scores = {
        pair: (
            _median([cohort_score[0] for cohort_score in cohort_scores]),
            _median([cohort_score[1] for cohort_score in cohort_scores]),
        )
        for pair, cohort_scores in per_pair_cohort_scores.items()
    }
    return scores, _cost_evidence("like_for_like", pairs, shared_hashes=common_hashes, scores=scores)


def _recommendation(condition, record, pairs, static_pair, hard_pair, selected, reason, trial, selection_basis):
    selected_text = pair_text(*selected) if selected is not None else None
    return {"schema_version": SCHEMA_VERSION, "condition": condition, "selected_model": selected[0] if selected else None, "selected_effort": selected[1] if selected else None, "selected_pair": selected_text, "trial": trial, "reason": reason, "profile_fingerprint": record["profile_fingerprint"], "calibration_state": record["calibration_state"], "best_pair": record["best_pair"], "selection_basis": selection_basis, "static_suggestion": pair_text(*static_pair), "hard_floor": pair_text(*hard_pair), "success_model": record["success_model"], "failed_model": record["failed_model"], "samples": len(record["tasks"])}


def _first_eligible_after_failure(failure_pair, eligible_pairs):
    candidate = upgrade_pair(failure_pair, eligible_pairs)
    if candidate is None:
        return None
    return candidate


def _first_eligible_below_pair(pair, eligible_pairs, hard_floor):
    candidate = downgrade_pair(pair, eligible_pairs)
    if candidate is None or compare_pair(candidate, hard_floor) < 0:
        return None
    return candidate


def _pick_weakest_passing_above_failure(pairs, failure_pair, hard_floor, record):
    candidates = _pairs_between_bounds(pairs, hard_floor, failure_pair)
    passing_pairs = _passes_within(candidates, record)
    for pair in pairs:
        if pair in passing_pairs:
            return pair
    return None


def _weakest_untested_between(failure_pair, success_pair, record, pairs):
    if failure_pair is None or success_pair is None:
        return None
    if compare_pair(success_pair, failure_pair) <= 0:
        return None
    for pair in pairs:
        if compare_pair(pair, failure_pair) <= 0:
            continue
        if compare_pair(pair, success_pair) >= 0:
            break
        if _pair_verdict(record, pair) is None:
            return pair
    return None


def _has_untested_between(failure_pair, success_pair, record, pairs):
    return _weakest_untested_between(failure_pair, success_pair, record, pairs) is not None


def recommend_route(args):
    if Path(args.history).expanduser().resolve() == DEFAULT_HISTORY_PATH.resolve():
        raise ValueError("legacy local model_experience.json is read-only")
    condition, summary, pairs, static_pair, hard_pair = _profile(args)
    def recommend(history):
        record = _record_for(history, condition, summary, pairs, static_pair, hard_pair)
        recompute_bounds(record, pairs)
        failure_pair = parse_pair(record["failed_model"]) if record["failed_model"] else None
        success_pair = parse_pair(record["success_model"]) if record["success_model"] else None

        eligible_pairs = _pairs_between_bounds(pairs, hard_pair, failure_pair)
        if not eligible_pairs:
            reason = "quality_failure_boundary_exhausted" if failure_pair is not None else "no_bounds_use_static"
            return _recommendation(condition, record, pairs, static_pair, hard_pair, None, reason, False, "quality_boundary")

        static_eligible = static_pair in eligible_pairs
        tiny = ("gpt-5.3-codex-spark", "low") if is_tiny_spark_profile(condition["task_family"], condition["modality"], condition["risk"], condition["complexity"], condition["ambiguity"]) and ("gpt-5.3-codex-spark", "low") in eligible_pairs else None
        tiny_runtime_failure = bool(tiny and any(task.get("operational_failure_pairs") for task in record["tasks"]))

        selected = None
        reason = "no_bounds_use_static"
        trial = False

        if record["calibration_state"] == "frozen" and record.get("best_pair"):
            reason = "receipt_cost_best_verified" if record["selection_basis"] == "receipt_cost" else "verified_quality_boundary"
            return _recommendation(condition, record, pairs, static_pair, hard_pair, parse_pair(record["best_pair"]), reason, False, record["selection_basis"])
        if failure_pair is None and success_pair is None:
            if condition["risk"] == "high":
                selected = static_pair if static_eligible else pairs[-1]
                reason = "high_risk_no_autodowngrade"
            elif failure_pair is None and success_pair is None and tiny and not tiny_runtime_failure:
                selected = tiny
                reason = "tiny_spark_auto"
            else:
                selected = static_pair if static_eligible else eligible_pairs[0]
                reason = "no_bounds_use_static"
        elif failure_pair is None and success_pair is not None:
            if tiny_runtime_failure or _has_unverified_quality_failure(record):
                selected = static_pair if static_eligible else eligible_pairs[0]
                reason = "operational_failure_static_fallback" if tiny_runtime_failure else "quality_failure_receipt_unverified"
            elif condition["risk"] == "high":
                selected = static_pair if static_eligible else pairs[-1]
                reason = "high_risk_no_autodowngrade"
            elif success_pair == hard_pair:
                selected = success_pair
                reason = "verified_floor_retained"
            else:
                selected = _first_eligible_below_pair(success_pair, eligible_pairs, hard_pair)
                if selected is None:
                    selected = static_pair if static_eligible else pairs[-1]
                    reason = "success_boundary_exhausted"
                else:
                    reason = "success_boundary_trial"
                    trial = True
        elif failure_pair is not None and success_pair is None:
            selected = _first_eligible_after_failure(failure_pair, eligible_pairs)
            if selected is None:
                reason = "quality_failure_boundary_exhausted"
            else:
                reason = "failure_and_success_boundary"
                trial = True
        else:
            gap_pair = _weakest_untested_between(failure_pair, success_pair, record, pairs)
            if gap_pair is not None:
                return _recommendation(condition, record, pairs, static_pair, hard_pair, gap_pair, "quality_boundary_gap_trial", True, record["selection_basis"])
            passing_pairs = [pair for pair in eligible_pairs if pair in _passes_within(eligible_pairs, record)]
            _, cost_evidence = _like_for_like_cost_scores(record, passing_pairs)
            record["cost_evidence"] = cost_evidence
            selected = _pick_weakest_passing_above_failure(pairs, failure_pair, hard_pair, record)
            reason = "verified_quality_boundary"
            trial = False
            if selected is None:
                selected = _first_eligible_after_failure(failure_pair, eligible_pairs)
                reason = "quality_failure_boundary_exhausted" if selected is None else "failure_and_success_boundary"
                trial = selected is not None
        return _recommendation(condition, record, pairs, static_pair, hard_pair, selected, reason, trial, record["selection_basis"])
    return _history_locked(args.history, recommend)[1]


def _receipt_pair(receipt, prefix, fallback=None):
    direct_pair = receipt.get(f"{prefix}_pair")
    if direct_pair is not None:
        return parse_pair(direct_pair)
    fallback_model, fallback_effort = fallback or (None, None)
    model = receipt.get(f"{prefix}_model") or fallback_model
    effort = receipt.get(f"{prefix}_effort") or fallback_effort
    return parse_pair(pair_text(model, effort))


def _operational_failure_pairs(receipt):
    route_attempts = receipt.get("route_attempts")
    if not isinstance(route_attempts, list):
        return []
    pairs = []
    for attempt in route_attempts:
        if not isinstance(attempt, dict) or attempt.get("status") not in {"fail", "failed"} or attempt.get("failure_class") not in RUNTIME_FAILURES:
            continue
        try:
            executed = _receipt_pair(attempt, "executed", None)
        except (TypeError, ValueError):
            continue
        if executed is None:
            continue
        text = pair_text(*executed)
        if text not in pairs:
            pairs.append(text)
    return canonical_pair_texts(pairs)


def record_event(args):
    if Path(args.history).expanduser().resolve() == DEFAULT_HISTORY_PATH.resolve():
        raise ValueError("legacy local model_experience.json is read-only")
    condition, summary, pairs, static_pair, hard_pair = _profile(args)
    if args.verify_level != "real":
        raise ValueError("active writes require verify_level=real")
    if args.verify_status not in {"pass", "fail", "unknown"} or args.failure_class not in QUALITY_FAILURES | RUNTIME_FAILURES | {"none"}:
        raise ValueError("verification evidence is invalid")
    receipt = json.loads(Path(args.receipt).expanduser().resolve().read_text(encoding="utf-8"))
    requested = _receipt_pair(receipt, "requested")
    resolved = _receipt_pair(receipt, "resolved", requested)
    effective = _receipt_pair(receipt, "effective", resolved)
    executed = _receipt_pair(receipt, "executed", effective)
    if requested not in pairs or executed not in pairs:
        raise ValueError("receipt pair is not in candidate_ladder")
    run_id = str(args.run_id or f"run_{os.urandom(8).hex()}")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must be sanitized")
    def record(history):
        record = _record_for(history, condition, summary, pairs, static_pair, hard_pair)
        existing = next((task for task in record["tasks"] if task["run_id"] == run_id), None)
        task = existing or {"run_id": run_id, "summary": summary, "requested_pair": pair_text(*requested), "resolved_pair": pair_text(*resolved), "effective_pair": pair_text(*effective), "executed_pair": pair_text(*executed), "operational_failure_pairs": [], "receipt_status": "fail", "real_status": "unknown", "effective_verdict": None, "allowlisted_failure_class": "none", "turn_completed": False, "model_match": False, "effort_match": False, "trial": bool(args.trial), "workload_prompt_sha256": None, "token_totals": {}, "process_ms": None, "recorded_at": datetime.now(timezone.utc).isoformat()}
        task.pop("mini_status", None)
        operational_pairs = _operational_failure_pairs(receipt)
        if args.failure_class in RUNTIME_FAILURES and args.verify_status == "fail":
            operational_pairs.append(pair_text(*executed))
        existing_pairs = _parse_optional_pairs(task.get("operational_failure_pairs", []))
        task["operational_failure_pairs"] = canonical_pair_texts(_dedupe_pairs(existing_pairs + operational_pairs))
        previous_verdict = task_verdict(task) if existing is not None else None
        previous_receipt_status = task.get("receipt_status") if existing is not None else None
        previous_real_pass = existing is not None and task.get("real_status") == "pass"
        task["turn_completed"] = bool(receipt.get("turn_completed") is True)
        task["model_match"] = bool(receipt.get("model_match") is True)
        task["effort_match"] = bool(receipt.get("effort_match") is True)
        task["receipt_status"] = "pass" if receipt.get("status") == "pass" and task["turn_completed"] and task["model_match"] and task["effort_match"] else "fail"
        receipt_is_valid = bool(
            receipt.get("status") == "pass"
            and receipt.get("turn_completed") is True
            and receipt.get("model_match") is True
            and receipt.get("effort_match") is True
        )
        preserve_primary_sample = existing is not None and (
            (args.failure_class in RUNTIME_FAILURES and args.verify_status == "fail")
            or (
                args.failure_class in QUALITY_FAILURES
                and args.verify_status == "fail"
                and not receipt_is_valid
                and (previous_verdict == "fail" or previous_receipt_status == "pass")
            )
        )
        preserve_identity = existing is not None and (
            (args.failure_class in RUNTIME_FAILURES and args.verify_status == "fail")
            or (
                args.failure_class in QUALITY_FAILURES
                and args.verify_status == "fail"
                and not receipt_is_valid
                and (previous_verdict == "pass" or previous_real_pass)
                and previous_receipt_status == "pass"
            )
        )
        update_metrics = not preserve_primary_sample
        if not preserve_identity:
            task["requested_pair"] = pair_text(*requested)
            task["resolved_pair"] = pair_text(*resolved)
            task["effective_pair"] = pair_text(*effective)
            task["executed_pair"] = pair_text(*executed)
        if args.failure_class not in RUNTIME_FAILURES and (task.get("real_status") != "fail" or task.get("allowlisted_failure_class") not in QUALITY_FAILURES):
            task["real_status"] = args.verify_status
        if args.failure_class in QUALITY_FAILURES or task["allowlisted_failure_class"] not in QUALITY_FAILURES:
            task["allowlisted_failure_class"] = args.failure_class
        if update_metrics:
            tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
            task["workload_prompt_sha256"] = _safe_sha256(receipt.get("workload_prompt_sha256"))
            task["token_totals"] = {"input": _safe_int(tokens.get("input_tokens")), "cached_input": _safe_int(tokens.get("cached_input_tokens")), "output": _safe_int(tokens.get("output_tokens")), "reasoning_output": _safe_int(tokens.get("reasoning_output_tokens")), "total": _safe_int(tokens.get("total_tokens"))}
            task["process_ms"] = _safe_int(receipt.get("process_elapsed_ms"))
        if not existing:
            record["tasks"].append(task)
        recompute_bounds(record)
        return {
            "status": "recorded",
            "route_run_id": run_id,
            "receipt_status": task["receipt_status"],
            "verify_level": args.verify_level,
            "verify_status": args.verify_status,
            "calibration_state": record.get("calibration_state"),
            "best_pair": record.get("best_pair"),
            "selection_basis": record.get("selection_basis"),
            "success_model": record.get("success_model"),
            "failed_model": record.get("failed_model"),
            "cost_evidence": record.get("cost_evidence"),
        }
    return _history_locked(args.history, record)[1]


def status(history_path):
    history_path = Path(history_path).expanduser().resolve()
    if not history_path.exists():
        if history_path == DEFAULT_HISTORY_PATH.resolve():
            return {"schema_version": SCHEMA_VERSION, "conditions": 0, "tasks": 0}
        _write_locked(history_path, empty_history())
    history = _history_locked(history_path)[0]
    return {"schema_version": SCHEMA_VERSION, "conditions": len(history["conditions"]), "tasks": sum(len(record["tasks"]) for record in history["conditions"].values())}


def _profile(args):
    condition = validate_condition(vars(args), allow_history_only=False)
    summary = validate_summary(args.task_summary)
    pairs = canonical_pairs(args.candidate_ladder)
    if getattr(args, "enforce_candidate_policy", False):
        expected_pairs = adaptive_pair_texts_for_profile(
            condition["task_family"],
            condition["modality"],
            condition["risk"],
            condition["complexity"],
            condition["ambiguity"],
        )
        if canonical_pair_texts(pairs) != expected_pairs:
            raise ValueError("candidate_ladder does not match the profile's exact adaptive ladder")
    static_pair, hard_pair = parse_pair(args.static_suggestion), parse_pair(args.hard_floor)
    if static_pair not in pairs or hard_pair not in pairs:
        raise ValueError("static_suggestion and hard_floor must be in candidate_ladder")
    return condition, summary, pairs, static_pair, hard_pair


def resolve_profile_arguments(args):
    canonical_owner = canonicalize_installed_skill_id(args.owning_skill, args.skills_root, args.plugins_cache_root) if args.owning_skill is not None else None
    profile = resolve_profile_preset(args.profile_preset, project_family=args.project_family, owning_skill=canonical_owner, execution_domain=args.execution_domain)
    if canonical_owner is None:
        profile["owning_skill"] = canonicalize_installed_skill_id(profile["owning_skill"], args.skills_root, args.plugins_cache_root)
    for field, value in profile.items():
        setattr(args, field, value)
    return args


def add_profile_arguments(parser):
    parser.add_argument("--profile-preset", required=True, choices=profile_preset_names())
    parser.add_argument("--project-family", required=True)
    parser.add_argument("--owning-skill")
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    parser.add_argument("--plugins-cache-root", type=Path, default=DEFAULT_PLUGINS_CACHE_ROOT)
    parser.add_argument(
        "--execution-domain",
        choices=tuple(
            domain
            for domain, metadata in EXECUTION_DOMAINS.items()
            if metadata.get("active") and not metadata.get("history_only")
        ),
    )
    parser.add_argument("--task-summary", required=True)


def execution_domain_rows():
    return {
        "schema_version": 1,
        "registry_version": EXECUTION_DOMAIN_REGISTRY_VERSION,
        "rows": public_execution_domain_rows(),
    }


def profile_preset_rows():
    return {"schema_version": 1, "registry_version": PROFILE_PRESET_VERSION, "rows": public_profile_preset_rows()}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Maintain privacy-safe adaptive-routing experience")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    recommend = commands.add_parser("recommend")
    record = commands.add_parser("record")
    commands.add_parser("domains")
    commands.add_parser("profiles")
    add_profile_arguments(recommend)
    add_profile_arguments(record)
    record.add_argument("--receipt", required=True)
    record.add_argument("--verify-level", required=True)
    record.add_argument("--verify-status", required=True)
    record.add_argument("--failure-class", default="none")
    record.add_argument("--run-id")
    record.add_argument("--trial", action="store_true")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    return resolve_profile_arguments(args) if args.command in {"recommend", "record"} else args


def main():
    args = parse_args()
    if args.command == "recommend":
        args.enforce_candidate_policy = True
        value = recommend_route(args)
    elif args.command == "record":
        args.enforce_candidate_policy = True
        value = record_event(args)
    elif args.command == "domains":
        value = execution_domain_rows()
    elif args.command == "profiles":
        value = profile_preset_rows()
    else:
        value = status(args.history)
    print(json.dumps(value, separators=(",", ":")))


if __name__ == "__main__":
    main()
