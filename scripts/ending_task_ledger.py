#!/usr/bin/env python3
"""Ending lifecycle ledger for Windows, macOS, and Linux."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEFAULT_STORE = Path.home() / ".codex" / "ending-task-memory"
DEFAULT_MAX_REPAIR_ATTEMPTS = 3
SCHEMA_VERSION = 2
TERMINAL_EVENTS = {"pass", "fail", "blocked"}
ALL_EVENTS = TERMINAL_EVENTS | {"note"}
FAILURE_CLASSES = {"none", "availability", "timeout", "protocol", "telemetry", "execution", "receipt", "quality", "correctness"}
QUALITY_FAILURES = {"quality", "correctness"}
OPERATIONAL_FAILURES = FAILURE_CLASSES - QUALITY_FAILURES - {"none"}
MODEL_EVIDENCE_LEVELS = {"runtime_receipt", "verified_entry", "task_assignment", "configured_selection", "unavailable"}
ROUTE_CHANGES = {"upgrade", "downgrade", "freeze", "no_switch", "operational_fallback"}
UNKNOWN_MODEL_PAIR = "unknown|unknown"
REPAIR_DISPATCH_TOOL = "codex_app__send_message_to_thread"
REQUIRED_MODEL_CONTEXT_FIELDS = ("project_root", "task_type", "module", "file", "symbol", "code_kind", "operation", "modality", "complexity", "complexity_score", "complexity_band", "risk", "ambiguity", "task_summary")
OPTIONAL_MODEL_CONTEXT_FIELDS = ("step_kind", "capability_tags", "capability_fingerprint", "entry_model", "entry_effort", "entry_pair", "entry_source")
MODEL_CONTEXT_FIELDS = REQUIRED_MODEL_CONTEXT_FIELDS + OPTIONAL_MODEL_CONTEXT_FIELDS


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _single_line(value, field_name, required=True, max_length=1200):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field_name} is required")
    return text[:max_length]


def _normalize_files(project_root, file_values):
    if not project_root:
        if file_values:
            raise ValueError("--file requires --project-root")
        return []
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_root must be an existing directory")
    normalized = []
    for file_value in file_values:
        candidate = Path(file_value).expanduser()
        relative = candidate.resolve().relative_to(root) if candidate.is_absolute() else PurePosixPath(candidate.as_posix())
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"file must be inside project_root: {file_value}")
        relative_text = relative.as_posix()
        if relative_text not in normalized:
            normalized.append(relative_text)
    return normalized


def _state_path(store, lifecycle_id):
    return store / "lifecycles" / f"{lifecycle_id}.json"


def _read_state(store, lifecycle_id):
    path = _state_path(store, lifecycle_id)
    if not path.is_file():
        raise ValueError(f"unknown lifecycle_id: {lifecycle_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(store, state):
    path = _state_path(store, state["lifecycle_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f".tmp-{uuid.uuid4().hex}")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)
    return path


def _append_event(store, event):
    index_path = store / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


@contextmanager
def _exclusive_file_lock(lock_path):
    path = Path(lock_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _model_pair(value, field_name):
    pair = _single_line(value, field_name, required=False, max_length=160)
    return pair if pair and pair != UNKNOWN_MODEL_PAIR else None


def _origin_session(thread_id="", host_id=""):
    if bool(thread_id) != bool(host_id):
        raise ValueError("origin session requires both origin_thread_id and origin_host_id")
    if not thread_id:
        return None
    return {"thread_id": _single_line(thread_id, "origin_thread_id", max_length=160), "host_id": _single_line(host_id, "origin_host_id", max_length=160)}


def _model_disclosure(selected_pair, producer_binding, requested_pair="", resolved_pair="", effective_pair="", previous_pair="", model_evidence="", route_change="", switch_summary="", reason=""):
    assigned_pair = _model_pair(selected_pair, "selected_pair")
    receipt_requested_pair = producer_binding.get("requested_pair") if producer_binding else None
    receipt_resolved_pair = producer_binding.get("resolved_pair") if producer_binding else None
    receipt_effective_pair = producer_binding.get("effective_pair") if producer_binding else None
    requested_pair = _model_pair(requested_pair, "requested_pair") or receipt_requested_pair or assigned_pair
    resolved_pair = _model_pair(resolved_pair, "resolved_pair") or receipt_resolved_pair or assigned_pair or requested_pair
    effective_pair = receipt_effective_pair or _model_pair(effective_pair, "effective_pair") or resolved_pair or requested_pair or assigned_pair
    known_pair = effective_pair or resolved_pair or requested_pair or assigned_pair
    requested_pair = requested_pair or known_pair or UNKNOWN_MODEL_PAIR
    resolved_pair = resolved_pair or known_pair or UNKNOWN_MODEL_PAIR
    effective_pair = effective_pair or known_pair or UNKNOWN_MODEL_PAIR
    model_evidence = _single_line(model_evidence, "model_evidence", required=False, max_length=40) or ("runtime_receipt" if producer_binding else "task_assignment" if assigned_pair else "configured_selection" if known_pair else "unavailable")
    if model_evidence not in MODEL_EVIDENCE_LEVELS:
        raise ValueError(f"model_evidence must be one of {', '.join(sorted(MODEL_EVIDENCE_LEVELS))}")
    if producer_binding:
        model_evidence = "runtime_receipt"
    elif model_evidence == "runtime_receipt":
        raise ValueError("runtime_receipt model_evidence requires a validated producer receipt")
    elif not known_pair and model_evidence != "unavailable":
        raise ValueError("model_evidence requires a known model identity")
    elif known_pair and model_evidence == "unavailable":
        raise ValueError("unavailable model_evidence requires no model identity")
    current_pair = effective_pair
    previous_pair = _model_pair(previous_pair, "previous_pair") or (resolved_pair if producer_binding and resolved_pair != current_pair else "same as current" if current_pair != UNKNOWN_MODEL_PAIR else "none")
    route_change = _single_line(route_change, "route_change", required=False, max_length=40) or ("operational_fallback" if producer_binding and resolved_pair and resolved_pair != current_pair else "no_switch")
    if route_change not in ROUTE_CHANGES:
        raise ValueError(f"route_change must be one of {', '.join(sorted(ROUTE_CHANGES))}")
    switch_summary = _single_line(switch_summary, "switch_summary", required=False, max_length=600) or ("No model switch" if route_change == "no_switch" else f"Switched from {previous_pair} to {current_pair} per switch rule.")
    if route_change == "no_switch" and current_pair != UNKNOWN_MODEL_PAIR and (previous_pair != "same as current" or any(pair != current_pair for pair in (requested_pair, resolved_pair, effective_pair))):
        raise ValueError("no_switch requires one concrete pair and previous_pair=same as current")
    if route_change == "no_switch" and switch_summary != "No model switch":
        raise ValueError("no_switch requires switch_summary=No model switch")
    reason = _single_line(reason, "reason", required=False, max_length=600) or ("Runtime receipt conflicts with resolved pair." if producer_binding and resolved_pair != current_pair else "Runtime receipt identifies the effective pair." if producer_binding else "Best-known pair used; receipt not available." if current_pair != UNKNOWN_MODEL_PAIR else "Previous-model provenance unavailable: no assignment or receipt.")
    evidence_level = "runtime_receipt" if producer_binding else "UNVERIFIED (no runtime receipt)" if current_pair != UNKNOWN_MODEL_PAIR else "unavailable"
    return {"assigned_pair": assigned_pair, "current_pair": current_pair, "model_evidence": model_evidence, "requested_pair": requested_pair, "resolved_pair": resolved_pair, "effective_pair": effective_pair, "previous_pair": previous_pair, "route_change": route_change, "switch_summary": switch_summary, "reason": reason, "effective_evidence_level": evidence_level}


def _producer_binding(receipt_value, project_root=None):
    if not receipt_value:
        return None
    receipt_path = Path(receipt_value).expanduser().resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    context = receipt.get("model_learning_context") if isinstance(receipt, dict) else None
    context_fields = set(context) if isinstance(context, dict) else set()
    if not isinstance(context, dict) or not set(REQUIRED_MODEL_CONTEXT_FIELDS).issubset(context_fields) or context_fields - set(MODEL_CONTEXT_FIELDS):
        raise ValueError("producer receipt requires the exact sanitized model_learning_context fields")
    sanitized = {}
    for field in (field for field in MODEL_CONTEXT_FIELDS if field in context):
        value = context[field]
        if field == "complexity_score":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError("producer model_learning_context.complexity_score must be an integer from 0 to 100")
            sanitized[field] = value
            continue
        if field == "capability_tags":
            if not isinstance(value, list) or len(value) > 32 or any(not isinstance(tag, str) for tag in value):
                raise ValueError("producer model_learning_context.capability_tags must be a bounded text list")
            cleaned_tags = [_single_line(tag, "model_learning_context.capability_tags", required=True, max_length=120) for tag in value]
            if cleaned_tags != value or len(cleaned_tags) != len(set(cleaned_tags)):
                raise ValueError("producer model_learning_context.capability_tags is not sanitized")
            sanitized[field] = cleaned_tags
            continue
        maximum = 1200 if field == "project_root" else 600 if field in {"file", "symbol", "task_summary"} else 160
        if not isinstance(value, str):
            raise ValueError(f"producer model_learning_context.{field} must be text")
        cleaned = _single_line(value, f"model_learning_context.{field}", required=field in {"project_root", "task_type", "module"}, max_length=maximum)
        if cleaned != value:
            raise ValueError(f"producer model_learning_context.{field} is not sanitized")
        sanitized[field] = cleaned
    if "capability_fingerprint" in sanitized and not re.fullmatch(r"[0-9a-f]{64}", sanitized["capability_fingerprint"]):
        raise ValueError("producer model_learning_context.capability_fingerprint must be lowercase SHA-256")
    if "entry_pair" in sanitized:
        expected_entry_pair = f"{sanitized.get('entry_model', '')}|{sanitized.get('entry_effort', '')}"
        if not sanitized.get("entry_model") or not sanitized.get("entry_effort") or sanitized["entry_pair"] != expected_entry_pair:
            raise ValueError("producer model_learning_context.entry_pair does not match entry_model and entry_effort")
    expected_band = "small" if sanitized["complexity_score"] <= 24 else "standard" if sanitized["complexity_score"] <= 49 else "complex" if sanitized["complexity_score"] <= 74 else "advanced"
    if sanitized["complexity_band"] != expected_band:
        raise ValueError("producer model_learning_context.complexity_band does not match complexity_score")
    context_root = Path(sanitized["project_root"]).expanduser().resolve()
    if not context_root.is_dir():
        raise ValueError("producer model_learning_context.project_root must be an existing directory")
    if project_root and context_root != Path(project_root).expanduser().resolve():
        raise ValueError("producer receipt project_root does not match lifecycle project_root")
    executed_pair = receipt.get("executed_pair") or receipt.get("effective_pair") or receipt.get("requested_pair")
    route_attempts = receipt.get("route_attempts")
    matched_route_attempt = next((attempt for attempt in route_attempts if isinstance(attempt, dict) and attempt.get("status") == "pass" and attempt.get("executed_pair") == executed_pair and attempt.get("model_match") is True and attempt.get("effort_match") is True), None) if isinstance(route_attempts, list) else None
    if receipt.get("status") != "pass" or receipt.get("result_published") is not True or receipt.get("turn_completed") is not True or receipt.get("model_match") is not True or receipt.get("effort_match") is not True or receipt.get("node_type") != "locked-route-node" or receipt.get("node_role") != "result-producer" or not isinstance(executed_pair, str) or not matched_route_attempt:
        raise ValueError("producer receipt must be a matched passing published producer receipt")
    return {"receipt_path": str(receipt_path), "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(), "model_learning_context": sanitized, "executed_pair": executed_pair, "requested_pair": _model_pair(receipt.get("requested_pair"), "producer receipt requested_pair") or executed_pair, "resolved_pair": _model_pair(receipt.get("resolved_pair"), "producer receipt resolved_pair") or _model_pair(receipt.get("requested_pair"), "producer receipt requested_pair") or executed_pair, "effective_pair": executed_pair, "status": "pending"}


def _load_model_memory_module():
    script_path = Path(__file__).resolve().parent / "obsidian_model_memory.py"
    spec = importlib.util.spec_from_file_location("ending_task_obsidian_model_memory", script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_personal_memory_module():
    script_path = Path(__file__).resolve().parent / "personal_memory.py"
    spec = importlib.util.spec_from_file_location("ending_task_personal_memory", script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _memory_candidate_file(state, candidate_file):
    if not candidate_file:
        return None
    path = Path(candidate_file).expanduser()
    root_value = state.get("project_root") or state.get("cwd")
    root = Path(root_value).expanduser().resolve() if root_value else Path.cwd().resolve()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("memory candidates file must stay inside the lifecycle project root") from error
    return resolved


def _record_personal_memory_candidates(state, event_name, candidate_file):
    if event_name not in TERMINAL_EVENTS or not candidate_file:
        return {"status": "no-candidates", "written": False, "candidates": 0}
    path = _memory_candidate_file(state, candidate_file)
    if not path or not path.is_file():
        return {"status": "no-candidates", "written": False, "candidates": 0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    memory = _load_personal_memory_module()
    verification_status = {"pass": "passed", "fail": "partial", "blocked": "partial"}[event_name]
    module = _routing_slug(state.get("module") or state.get("ending_check_id") or "ending-memory", "ending-memory")
    return memory.capture(candidates, project="Global Preferences", module=module, verification_status=verification_status)


def _record_bound_model_result(binding, real_status, failure_class, outcome_reason="", verification_count=0, *, ending_attempt_number=1, prior_quality_failure_count=0, prior_operational_failure_count=0):
    receipt_path = Path(binding["receipt_path"])
    if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != binding["receipt_sha256"]:
        raise ValueError("bound producer receipt changed after lifecycle start")
    context = binding["model_learning_context"]
    memory = _load_model_memory_module()
    return memory.record_model_result(context["project_root"], context["task_type"], context["module"], receipt_path, real_status, failure_class, file_value=context["file"], symbol=context["symbol"], code_kind=context["code_kind"], operation=context["operation"], modality=context["modality"], complexity=context["complexity"], complexity_score=context["complexity_score"], risk=context["risk"], ambiguity=context["ambiguity"], task_summary=context["task_summary"], step_kind=context.get("step_kind", ""), capability_tags=context.get("capability_tags"), entry_model=context.get("entry_model", ""), entry_effort=context.get("entry_effort", ""), bound_receipt=binding, outcome_reason=outcome_reason, verification_count=verification_count, ending_attempt_number=ending_attempt_number, prior_quality_failure_count=prior_quality_failure_count, prior_operational_failure_count=prior_operational_failure_count)


def _routing_slug(value, fallback):
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-._")
    return normalized[:80] if normalized else fallback


def _record_unbound_model_observation(state, real_status, failure_class, outcome_reason="", verification_count=0, *, ending_attempt_number=1, prior_quality_failure_count=0, prior_operational_failure_count=0):
    project_root = state.get("project_root")
    pair = state.get("selected_pair")
    if not project_root or not pair:
        return None
    score = state.get("complexity_score") if isinstance(state.get("complexity_score"), int) else 50
    disclosure = state.get("model_disclosure") if isinstance(state.get("model_disclosure"), dict) else {}
    model_evidence = disclosure.get("model_evidence")
    if model_evidence not in {"verified_entry", "task_assignment", "configured_selection"}:
        model_evidence = "task_assignment"
    memory = _load_model_memory_module()
    return memory.record_model_observation(
        project_root,
        _routing_slug(state.get("task_kind"), "verification"),
        _single_line(state.get("module") or state.get("ending_check_id") or "ending-verification", "module", max_length=160),
        pair,
        real_status,
        failure_class,
        observation_id=state["lifecycle_id"],
        file_value=(state.get("files") or [""])[0],
        code_kind="general",
        operation="verify",
        modality="text",
        complexity="complex" if score >= 50 else "easy",
        complexity_score=score,
        risk="low",
        ambiguity="low",
        task_summary=_single_line(state.get("summary"), "task_summary", required=False, max_length=280),
        step_kind="verification",
        capability_tags=["local-test"],
        model_evidence=model_evidence,
        outcome_reason=outcome_reason,
        verification_count=verification_count,
        ending_attempt_number=ending_attempt_number,
        prior_quality_failure_count=prior_quality_failure_count,
        prior_operational_failure_count=prior_operational_failure_count,
    )


def _successful_model_learning_noop(result):
    return isinstance(result, dict) and result.get("status") == "no-op" and result.get("written") is False and result.get("reason") == "unregistered_or_missing_broad_model_switch"


def _root_lifecycle(store, state):
    current = state
    visited = {current["lifecycle_id"]}
    while current.get("repair_of_lifecycle_id"):
        current = _read_state(store, current["repair_of_lifecycle_id"])
        if current["lifecycle_id"] in visited:
            raise ValueError("repair lifecycle topology contains a cycle")
        visited.add(current["lifecycle_id"])
    return current


def _root_descendants(store, root):
    states = []
    for path in sorted((store / "lifecycles").glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["lifecycle_id"] != root["lifecycle_id"] and _root_lifecycle(store, state)["lifecycle_id"] == root["lifecycle_id"]:
            states.append(state)
    return sorted(states, key=lambda state: (state.get("created_at", ""), state["lifecycle_id"]))


def _has_limit_block(state):
    return any(event.get("event") == "blocked" and event.get("error_fingerprint") == "repair-attempt-limit-exceeded" for event in state.get("events", []))


def _normalize_root_attempts(store, root, descendants, repair_limit):
    for index, descendant in enumerate(descendants, start=1):
        changed = descendant.get("attempt_index") != index or descendant.get("max_repair_attempts") != repair_limit
        if changed:
            descendant["attempt_index"] = index
            descendant["max_repair_attempts"] = repair_limit
            _write_state(store, descendant)


def _terminal_event(state):
    return next((event for event in reversed(state.get("events", [])) if event.get("event") in TERMINAL_EVENTS), None)


def _state_pair(state):
    learning = state.get("model_learning") if isinstance(state.get("model_learning"), dict) else {}
    binding = state.get("producer_binding") if isinstance(state.get("producer_binding"), dict) else {}
    disclosure = state.get("model_disclosure") if isinstance(state.get("model_disclosure"), dict) else {}
    return learning.get("pair") or binding.get("effective_pair") or disclosure.get("current_pair") or state.get("selected_pair") or UNKNOWN_MODEL_PAIR


def _attempt_context(store, state):
    root = _root_lifecycle(store, state)
    chain = [root, *_root_descendants(store, root)]
    chain = [state if item.get("lifecycle_id") == state.get("lifecycle_id") else item for item in chain]
    attempt_number = int(state.get("attempt_index", 0)) + 1
    prior = [item for item in chain if int(item.get("attempt_index", 0)) < int(state.get("attempt_index", 0))]
    prior_events = [(item, _terminal_event(item)) for item in prior]
    prior_events = [(item, event) for item, event in prior_events if event]
    prior_quality = sum(event.get("event") == "fail" and event.get("failure_class") in QUALITY_FAILURES for _, event in prior_events)
    prior_operational = sum(event.get("event") == "fail" and event.get("failure_class") in OPERATIONAL_FAILURES for _, event in prior_events)
    attempts = [
        {
            "attempt": int(item.get("attempt_index", 0)) + 1,
            "lifecycle_id": item.get("lifecycle_id"),
            "status": event.get("event"),
            "failure_class": event.get("failure_class") or "none",
            "pair": _state_pair(item),
        }
        for item, event in prior_events
    ]
    return {
        "attempt_number": attempt_number,
        "prior_quality_failure_count": prior_quality,
        "prior_operational_failure_count": prior_operational,
        "initial_pair": _state_pair(root),
        "attempt_chain": attempts,
    }


def _model_assessment(state, event_name, failure_class, model_learning, attempt_context):
    learning = model_learning if isinstance(model_learning, dict) else {}
    binding = state.get("producer_binding") if isinstance(state.get("producer_binding"), dict) else {}
    attempt_number = attempt_context["attempt_number"]
    producer_pair = binding.get("effective_pair") or UNKNOWN_MODEL_PAIR
    model_record_pair = learning.get("pair") or producer_pair
    ending_pair = state.get("selected_pair") or UNKNOWN_MODEL_PAIR
    next_pair = learning.get("next_pair") or model_record_pair
    prior_quality = attempt_context["prior_quality_failure_count"]
    prior_operational = attempt_context["prior_operational_failure_count"]
    quality_failures = prior_quality + int(event_name == "fail" and failure_class in QUALITY_FAILURES)
    operational_failures = prior_operational + int(event_name == "fail" and failure_class in OPERATIONAL_FAILURES)
    if event_name == "pass" and attempt_number == 1:
        pass_shape = "first_attempt_pass"
        if learning.get("next_pair_direction") == "downgrade" and next_pair != producer_pair:
            suitability = "suitable_downgrade_candidate"
            routing_action = "trial_downgrade_one_rung_next_matching_task"
            reason = "The first Ending attempt passed; accumulated matching Real PASS evidence reached the one-rung downgrade threshold."
        else:
            suitability = "suitable"
            routing_action = "retain_until_second_matching_first_pass"
            reason = "The first Ending attempt passed; retain this pair until the matching Real PASS threshold supports a one-rung trial."
    elif event_name == "pass" and prior_quality:
        pass_shape = "retry_pass"
        suitability = "initial_pair_too_weak_recovered"
        routing_action = "reuse_lowest_successful_recovery_pair"
        reason = "A prior correctness or quality attempt failed and this retry passed; keep the lowest pair that produced the verified recovery."
    elif event_name == "pass":
        pass_shape = "retry_pass"
        suitability = "suitable_after_operational_recovery"
        routing_action = "retain_quality_boundary"
        reason = "The retry passed after an operational interruption; the interruption is quality-neutral."
    elif event_name == "fail" and failure_class in QUALITY_FAILURES:
        pass_shape = "failed_attempt"
        suitability = "too_weak_for_verified_result"
        routing_action = "upgrade_one_rung_for_repair"
        reason = "The real check found a correctness or quality defect; the next repair route moves one rung up."
    elif event_name == "fail":
        pass_shape = "failed_attempt"
        suitability = "quality_unproven_operational_failure"
        routing_action = "retry_without_quality_penalty"
        reason = "The check failed operationally, so model quality is not downgraded or upgraded from this evidence."
    else:
        pass_shape = "blocked"
        suitability = "unproven"
        routing_action = "none_until_blocker_clears"
        reason = "The Ending check was blocked before a quality verdict was available."
    suitability = learning.get("model_suitability") or suitability
    routing_action = learning.get("routing_action") or routing_action
    current_attempt = {
        "attempt": attempt_number,
        "lifecycle_id": state.get("lifecycle_id"),
        "status": event_name,
        "failure_class": failure_class if event_name == "fail" else "none",
        "pair": producer_pair if producer_pair != UNKNOWN_MODEL_PAIR else ending_pair,
    }
    task_context = binding.get("model_learning_context") if isinstance(binding.get("model_learning_context"), dict) else {}
    return {
        "task_complexity_score": task_context.get("complexity_score"),
        "task_complexity_band": task_context.get("complexity_band"),
        "ending_complexity_score": state.get("complexity_score"),
        "ending_complexity_band": state.get("complexity_band"),
        "producer_pair": producer_pair,
        "ending_pair": ending_pair,
        "model_record_pair": model_record_pair,
        "initial_pair": attempt_context["initial_pair"],
        "attempt_count": attempt_number,
        "pass_attempt_number": attempt_number if event_name == "pass" else None,
        "pass_shape": pass_shape,
        "quality_failure_count": quality_failures,
        "operational_failure_count": operational_failures,
        "model_suitability": suitability,
        "routing_action": routing_action,
        "next_pair": next_pair,
        "recovery_from_pair": learning.get("recovery_from_pair"),
        "matched_pass_count_after": learning.get("matched_pass_count_after"),
        "minimum_passes_before_downgrade": learning.get("minimum_passes_before_downgrade"),
        "reason": reason,
        "attempt_chain": [*attempt_context["attempt_chain"], current_attempt],
        "model_record_status": learning.get("obsidian", {}).get("status") if isinstance(learning.get("obsidian"), dict) else learning.get("status") or "unavailable",
        "model_record_document": learning.get("model_record_document"),
        "model_record_link": learning.get("model_record_link"),
        "model_switch_document": learning.get("model_switch_document"),
        "model_switch_link": learning.get("model_switch_link"),
    }


def _repair_prompt(state, event):
    project_root = Path(state["project_root"]).expanduser().resolve() if state.get("project_root") else None
    plan_text = "the saved verification plan"
    if project_root and state.get("verification_plan"):
        try:
            plan_text = Path(state["verification_plan"]).expanduser().resolve().relative_to(project_root).as_posix()
        except ValueError:
            plan_text = "the saved verification plan"
    failure_class = event.get("failure_class") or "correctness"
    error_fingerprint = event.get("error_fingerprint") or "not-supplied"
    if error_fingerprint == "acceptance-mismatch" or failure_class in QUALITY_FAILURES:
        failure_instruction = "The final result is incorrect or differs from the original user request. Compare the delivered result and the original request before changing anything, even if the command exited successfully."
    elif failure_class == "timeout":
        failure_instruction = "The real check timed out. Determine whether the timeout is caused by the changed result or the verification environment, then repair only the result when the evidence supports it."
    else:
        failure_instruction = "The real check did not meet its expected runtime result. Inspect the exact evidence and repair the result only when the evidence identifies a producer defect."
    evidence_text = "\n".join(f"- {value}" for value in event.get("verification", [])) or "- No additional verification text was supplied."
    evidence_text = evidence_text[:2400]
    files_text = ", ".join(state.get("files") or ["the files authorized by the original result task"])
    next_attempt = int(state.get("attempt_index", 0)) + 1
    max_attempts = int(state.get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))
    origin = state.get("origin_session") or {}
    return "\n".join(["ENDING_REPAIR_REQUEST", "Continue in this original source session; do not create a separate repair session or let the Ending verifier edit the target.", f"Original task: {state['summary']}", "Read the original user request and the current delivered result in this session before choosing the repair.", f"Acceptance check: {state.get('ending_check_id') or 'the same saved Ending check'}", f"Verification plan relative to project root: {plan_text}", f"Project binding id: {state.get('project_id') or 'resolve the exact saved project before launching the next Ending'}", f"Origin session thread id: {origin.get('thread_id') or 'current source session'}", f"Origin session host id: {origin.get('host_id') or 'current source session host'}", f"Repair attempt to run: {next_attempt} of {max_attempts}", f"Failure class: {failure_class}", f"Error fingerprint: {error_fingerprint}", f"Failure meaning: {failure_instruction}", "Observed evidence:", evidence_text, f"Authorized result files: {files_text}", "Required actions:", "1. Compare the current result with the original user request and the acceptance contract; if the result still differs, fix the smallest authorized producer path.", "2. Run the producer Quick Check and present the repaired result in this same source session.", f"3. Start a fresh Ending for the same acceptance check as a child of lifecycle {state['lifecycle_id']} using --repair-of-lifecycle-id; never reuse the failed verdict.", "4. Carry the same project binding and origin session metadata into the new launch, create/acknowledge every projectless End Task, and return without waiting for its verdict.", "5. If the repair limit is exhausted or the source session cannot complete the repair, record BLOCKED with the exact evidence; do not claim PASS."])


def _repair_handoff(state, event):
    common = {"repair_of_lifecycle_id": state["lifecycle_id"], "summary": event["summary"], "verification": event["verification"], "error_fingerprint": event["error_fingerprint"], "failure_class": event["failure_class"], "complexity_score": state.get("complexity_score"), "complexity_band": state.get("complexity_band"), "max_repair_attempts": state.get("max_repair_attempts")}
    origin = state.get("origin_session")
    next_attempt = int(state.get("attempt_index", 0)) + 1
    max_attempts = int(state.get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))
    if not origin:
        return {**common, "action": "blocked_origin_session_unavailable", "blocked_reason": "Ending automatic repair requires the exact source session thread_id and host_id captured at launch.", "requires_origin_session": True}
    if next_attempt > max_attempts:
        return {**common, "action": "repair_limit_exhausted", "blocked_reason": f"No automatic repair prompt is allowed after {max_attempts} repair attempts.", "requires_origin_session": True, "origin_session": origin}
    prompt = _repair_prompt(state, event)
    dispatch = {"tool": REPAIR_DISPATCH_TOOL, "arguments": {"threadId": origin["thread_id"], "hostId": origin["host_id"], "prompt": prompt}, "required": True}
    return {**common, "action": "send_repair_prompt_to_origin_session_then_fresh_ending", "requires_origin_session": True, "origin_session": origin, "repair_dispatch": dispatch, "repair_prompt": prompt, "next_step": "origin_session_repairs_then_starts_fresh_ending"}


def start_lifecycle(task_kind, cwd, summary, project_root=None, module="", files=None, repair_of_lifecycle_id="", store=DEFAULT_STORE, max_repair_attempts=DEFAULT_MAX_REPAIR_ATTEMPTS, producer_receipt=None, complexity_score=None, complexity_band="", verification_required=False, verification_plan=None, ending_check_id="", selected_pair="", requested_pair="", resolved_pair="", effective_pair="", previous_pair="", model_evidence="", route_change="", switch_summary="", reason="", project_id="", origin_thread_id="", origin_host_id=""):
    cwd_path = Path(cwd).expanduser().resolve()
    if not cwd_path.is_dir():
        raise ValueError("cwd must be an existing directory")
    producer_binding = _producer_binding(producer_receipt, project_root)
    project_path = Path(project_root).expanduser().resolve() if project_root else None
    if project_path is None and producer_binding:
        project_path = Path(producer_binding["model_learning_context"]["project_root"])
    origin_session = _origin_session(origin_thread_id, origin_host_id)
    project_id_value = _single_line(project_id, "project_id", required=False, max_length=200) or None
    if producer_binding:
        bound_score = producer_binding["model_learning_context"]["complexity_score"]
        bound_band = producer_binding["model_learning_context"]["complexity_band"]
        if complexity_score is None:
            complexity_score = bound_score
            complexity_band = bound_band
    if complexity_score is not None:
        if isinstance(complexity_score, bool) or not 0 <= complexity_score <= 100:
            raise ValueError("complexity_score must be an integer from 0 to 100")
        expected_band = "small" if complexity_score <= 24 else "standard" if complexity_score <= 49 else "complex" if complexity_score <= 74 else "advanced"
        if complexity_band and complexity_band != expected_band:
            raise ValueError("complexity_band does not match complexity_score")
        complexity_band = expected_band
    normalized_files = _normalize_files(project_path, files or [])
    lifecycle_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"
    created_at = _now()
    store_path = Path(store).expanduser().resolve()
    store_path.mkdir(parents=True, exist_ok=True)
    lock_path = store_path / ".lock"
    with _exclusive_file_lock(lock_path):
        parent = None
        root = None
        attempt_index = 0
        repair_limit = int(max_repair_attempts)
        if repair_limit < 0 or repair_limit > 10:
            raise ValueError("max_repair_attempts must be between 0 and 10")
        if repair_of_lifecycle_id:
            parent = _read_state(store_path, repair_of_lifecycle_id)
            if parent["status"] not in {"failed", "blocked"} or _has_limit_block(parent):
                raise ValueError("repair lifecycle requires a failed parent lifecycle or non-limit blocked parent lifecycle")
            root = _root_lifecycle(store_path, parent)
            repair_limit = int(root.get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))
            descendants = _root_descendants(store_path, root)
            _normalize_root_attempts(store_path, root, descendants, repair_limit)
            attempt_index = len(descendants) + 1
            if attempt_index > repair_limit:
                blocked_at = _now()
                blocked_event = {"schema_version": SCHEMA_VERSION, "event": "blocked", "recorded_at": blocked_at, "lifecycle_id": root["lifecycle_id"], "summary": f"Repair attempt limit exhausted at {repair_limit}", "verification": [], "error_fingerprint": "repair-attempt-limit-exceeded"}
                if not _has_limit_block(root):
                    root["events"].append(blocked_event)
                    root["updated_at"] = blocked_at
                    root["status"] = "blocked"
                    _write_state(store_path, root)
                    _append_event(store_path, blocked_event)
                raise ValueError("repair attempt limit exceeded")
            parent_origin = parent.get("origin_session")
            if parent_origin and origin_session and parent_origin != origin_session:
                raise ValueError("repair lifecycle must preserve the origin session")
            origin_session = origin_session or parent_origin
            project_id_value = project_id_value or parent.get("project_id")
        if origin_session and not project_path:
            raise ValueError("origin session requires project_root")
        if origin_session and not project_id_value:
            raise ValueError("origin session requires project_id")
        verification_plan_path = Path(verification_plan).expanduser().resolve() if verification_plan else None
        if verification_required and (not verification_plan_path or not verification_plan_path.is_file()):
            raise ValueError("verification-required lifecycle requires an existing verification plan")
        ending_owns_model_identity = bool(selected_pair and (verification_required or ending_check_id))
        model_disclosure = _model_disclosure(selected_pair, None if ending_owns_model_identity else producer_binding, requested_pair, resolved_pair, effective_pair, previous_pair, model_evidence, route_change, switch_summary, reason)
        event = {"schema_version": SCHEMA_VERSION, "event": "started", "recorded_at": created_at, "lifecycle_id": lifecycle_id, "repair_of_lifecycle_id": repair_of_lifecycle_id or None, "summary": _single_line(summary, "summary"), "complexity_score": complexity_score, "complexity_band": complexity_band or None, "verification_required": bool(verification_required), "verification_plan": str(verification_plan_path) if verification_plan_path else None, "ending_check_id": _single_line(ending_check_id, "ending_check_id", required=False, max_length=80) or None, "selected_pair": _model_pair(selected_pair, "selected_pair"), "model_disclosure": model_disclosure}
        state = {"schema_version": SCHEMA_VERSION, "lifecycle_id": lifecycle_id, "created_at": created_at, "updated_at": created_at, "status": "running", "task_kind": _single_line(task_kind, "task_kind", max_length=80), "cwd": str(cwd_path), "summary": event["summary"], "project_root": str(project_path) if project_path else None, "project_id": project_id_value, "origin_session": origin_session, "module": _single_line(module, "module", required=False, max_length=160), "files": normalized_files, "complexity_score": complexity_score, "complexity_band": complexity_band or None, "verification_required": bool(verification_required), "verification_plan": str(verification_plan_path) if verification_plan_path else None, "ending_check_id": event["ending_check_id"], "selected_pair": event["selected_pair"], "model_disclosure": model_disclosure, "repair_of_lifecycle_id": repair_of_lifecycle_id or None, "attempt_index": attempt_index, "max_repair_attempts": repair_limit, "repair_children": [], "producer_binding": producer_binding, "events": [event]}
        if parent:
            parent_event = {"schema_version": SCHEMA_VERSION, "event": "repair_started", "recorded_at": created_at, "lifecycle_id": parent["lifecycle_id"], "child_lifecycle_id": lifecycle_id, "summary": f"Repair lifecycle {lifecycle_id} started"}
            parent["repair_children"].append(lifecycle_id)
            parent["events"].append(parent_event)
            parent["updated_at"] = created_at
            _write_state(store_path, parent)
            _append_event(store_path, parent_event)
        state_path = _write_state(store_path, state)
        _append_event(store_path, event)
    return {"status": "written", "lifecycle_id": lifecycle_id, "lifecycle_status": "running", "complexity_score": complexity_score, "complexity_band": complexity_band or None, "verification_required": bool(verification_required), "verification_plan": str(verification_plan_path) if verification_plan_path else None, "ending_check_id": event["ending_check_id"], "selected_pair": event["selected_pair"], "model_disclosure": model_disclosure, "project_id": project_id_value, "origin_session": origin_session, "local": {"written": True, "store": str(store_path), "state": str(state_path)}}


def record_event(lifecycle_id, event_name, summary, verification=None, error_fingerprint="", store=DEFAULT_STORE, failure_class="none", memory_candidates_file=None):
    if event_name not in ALL_EVENTS:
        raise ValueError(f"event must be one of {', '.join(sorted(ALL_EVENTS))}")
    if failure_class not in FAILURE_CLASSES:
        raise ValueError(f"failure_class must be one of {', '.join(sorted(FAILURE_CLASSES))}")
    store_path = Path(store).expanduser().resolve()
    lock_path = store_path / ".lock"
    with _exclusive_file_lock(lock_path):
        state = _read_state(store_path, lifecycle_id)
        if state["status"] != "running" and event_name != "note":
            prior_terminal = next((item for item in reversed(state["events"]) if item.get("event") in TERMINAL_EVENTS), None)
            if prior_terminal and prior_terminal.get("event") == event_name and prior_terminal.get("failure_class", "none") == failure_class:
                return {"status": "duplicate", "lifecycle_id": lifecycle_id, "lifecycle_status": state["status"], "model_learning": state.get("model_learning"), "local": {"written": True, "store": str(store_path), "state": str(_state_path(store_path, lifecycle_id))}}
            raise ValueError(f"lifecycle is already terminal: {state['status']}")
        binding = state.get("producer_binding")
        model_learning = None
        attempt_context = _attempt_context(store_path, state)
        if binding and event_name in {"pass", "fail"}:
            if event_name == "pass" and failure_class != "none":
                raise ValueError("a bound Ending pass requires failure_class=none")
            if event_name == "fail" and failure_class == "none":
                raise ValueError("a bound Ending fail requires an explicit failure_class")
            model_learning = _record_bound_model_result(binding, event_name, failure_class, _single_line(summary, "summary", max_length=280), len(verification or []), ending_attempt_number=attempt_context["attempt_number"], prior_quality_failure_count=attempt_context["prior_quality_failure_count"], prior_operational_failure_count=attempt_context["prior_operational_failure_count"])
            state["model_learning"] = model_learning
            state["producer_binding"]["status"] = "no-op" if _successful_model_learning_noop(model_learning) else "recorded" if model_learning.get("written") is True else "unavailable"
        elif event_name in TERMINAL_EVENTS and state.get("project_root") and state.get("selected_pair"):
            model_learning = _record_unbound_model_observation(state, event_name, failure_class, _single_line(summary, "summary", max_length=280), len(verification or []), ending_attempt_number=attempt_context["attempt_number"], prior_quality_failure_count=attempt_context["prior_quality_failure_count"], prior_operational_failure_count=attempt_context["prior_operational_failure_count"])
            if model_learning is not None:
                state["model_learning"] = model_learning
        recorded_at = _now()
        event = {"schema_version": SCHEMA_VERSION, "event": event_name, "recorded_at": recorded_at, "lifecycle_id": lifecycle_id, "summary": _single_line(summary, "summary"), "verification": [_single_line(value, "verification", max_length=600) for value in (verification or [])], "error_fingerprint": _single_line(error_fingerprint, "error_fingerprint", required=False, max_length=160) or None, "failure_class": failure_class if event_name in {"pass", "fail"} else None, "complexity_score": state.get("complexity_score"), "complexity_band": state.get("complexity_band")}
        model_assessment = _model_assessment(state, event_name, failure_class, model_learning, attempt_context) if event_name in TERMINAL_EVENTS else None
        if model_learning is not None:
            event["model_learning"] = model_learning
            event["switch_direction"] = model_learning.get("switch_direction")
            event["switch_reason"] = model_learning.get("switch_reason")
            event["next_pair"] = model_learning.get("next_pair")
        if model_assessment is not None:
            event["model_assessment"] = model_assessment
            state["model_assessment"] = model_assessment
        personal_memory = _record_personal_memory_candidates(state, event_name, memory_candidates_file)
        if event_name in TERMINAL_EVENTS:
            event["personal_memory"] = personal_memory
            state["personal_memory"] = personal_memory
        state["events"].append(event)
        state["updated_at"] = recorded_at
        if event_name in TERMINAL_EVENTS:
            state["status"] = {"pass": "passed", "fail": "failed", "blocked": "blocked"}[event_name]
        repair_handoff = _repair_handoff(state, event) if event_name == "fail" else None
        limit_blocked_event = None
        if repair_handoff is not None:
            event["repair_handoff"] = repair_handoff
            state["repair_handoff"] = repair_handoff
            if repair_handoff.get("action") == "repair_limit_exhausted":
                limit_blocked_event = {"schema_version": SCHEMA_VERSION, "event": "blocked", "recorded_at": recorded_at, "lifecycle_id": lifecycle_id, "summary": f"Repair attempt limit exhausted at {state.get('max_repair_attempts', DEFAULT_MAX_REPAIR_ATTEMPTS)}", "verification": event["verification"], "error_fingerprint": "repair-attempt-limit-exceeded", "failure_class": None, "repair_handoff": repair_handoff}
                state["events"].append(limit_blocked_event)
                state["status"] = "blocked"
        state_path = _write_state(store_path, state)
        _append_event(store_path, event)
        if limit_blocked_event is not None:
            _append_event(store_path, limit_blocked_event)
    output = {"status": "written", "lifecycle_id": lifecycle_id, "lifecycle_status": state["status"], "final_gate_passed": event_name == "pass", "local": {"written": True, "store": str(store_path), "state": str(state_path)}}
    if model_learning is not None:
        output["model_learning"] = model_learning
    if model_assessment is not None:
        output["model_assessment"] = model_assessment
    if event_name in TERMINAL_EVENTS:
        output["personal_memory"] = event.get("personal_memory")
    if event_name == "fail":
        output["repair_required"] = True
        output["repair_handoff"] = repair_handoff
    return output


def audit_lifecycle(lifecycle_id, store=DEFAULT_STORE):
    store_path = Path(store).expanduser().resolve()
    root = _root_lifecycle(store_path, _read_state(store_path, lifecycle_id))
    descendants = _root_descendants(store_path, root)
    active = descendants[-1] if descendants else root
    if _has_limit_block(root):
        terminal_status = "blocked"
    else:
        terminal_status = active["status"] if active["status"] in {"passed", "blocked"} else "pending"
    chain = [root["lifecycle_id"], *(state["lifecycle_id"] for state in descendants)]
    return {"status": "pass" if terminal_status == "passed" else terminal_status, "root_lifecycle_id": root["lifecycle_id"], "active_lifecycle_id": active["lifecycle_id"], "terminal_status": terminal_status, "complexity_score": active.get("complexity_score"), "complexity_band": active.get("complexity_band"), "attempt_count": int(active.get("attempt_index", 0)) + 1, "model_assessment": active.get("model_assessment"), "chain": chain, "descendants": [state["lifecycle_id"] for state in descendants], "final_gate_passed": terminal_status == "passed"}


def main():
    parser = argparse.ArgumentParser(description="Record eligible post-result Ending Task lifecycles")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--task-kind", required=True)
    start_parser.add_argument("--cwd", type=Path, required=True)
    start_parser.add_argument("--summary", required=True)
    start_parser.add_argument("--project-root", type=Path)
    start_parser.add_argument("--module", default="")
    start_parser.add_argument("--file", action="append", default=[])
    start_parser.add_argument("--repair-of-lifecycle-id", default="")
    start_parser.add_argument("--max-repair-attempts", type=int, default=DEFAULT_MAX_REPAIR_ATTEMPTS)
    start_parser.add_argument("--producer-receipt", type=Path)
    start_parser.add_argument("--complexity-score", type=int)
    start_parser.add_argument("--complexity-band", choices=("small", "standard", "complex", "advanced"), default="")
    start_parser.add_argument("--verification-required", action="store_true")
    start_parser.add_argument("--verification-plan", type=Path)
    start_parser.add_argument("--ending-check-id", default="")
    start_parser.add_argument("--selected-pair", default="")
    start_parser.add_argument("--requested-pair", default="")
    start_parser.add_argument("--resolved-pair", default="")
    start_parser.add_argument("--effective-pair", default="")
    start_parser.add_argument("--previous-pair", default="")
    start_parser.add_argument("--model-evidence", choices=sorted(MODEL_EVIDENCE_LEVELS), default="")
    start_parser.add_argument("--route-change", choices=sorted(ROUTE_CHANGES), default="")
    start_parser.add_argument("--switch-summary", default="")
    start_parser.add_argument("--reason", default="")
    start_parser.add_argument("--project-id", default="")
    start_parser.add_argument("--origin-thread-id", default="")
    start_parser.add_argument("--origin-host-id", default="")
    event_parser = subparsers.add_parser("event")
    event_parser.add_argument("--lifecycle-id", required=True)
    event_parser.add_argument("--event", choices=sorted(ALL_EVENTS), required=True)
    event_parser.add_argument("--summary", required=True)
    event_parser.add_argument("--verification", action="append", default=[])
    event_parser.add_argument("--error-fingerprint", default="")
    event_parser.add_argument("--failure-class", choices=sorted(FAILURE_CLASSES), default="none")
    event_parser.add_argument("--memory-candidates-file", type=Path)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--lifecycle-id", required=True)
    args = parser.parse_args()
    if args.command == "start":
        output = start_lifecycle(args.task_kind, args.cwd, args.summary, args.project_root, args.module, args.file, args.repair_of_lifecycle_id, args.store, args.max_repair_attempts, args.producer_receipt, args.complexity_score, args.complexity_band, args.verification_required, args.verification_plan, args.ending_check_id, args.selected_pair, args.requested_pair, args.resolved_pair, args.effective_pair, args.previous_pair, args.model_evidence, args.route_change, args.switch_summary, args.reason, args.project_id, args.origin_thread_id, args.origin_host_id)
    elif args.command == "event":
        output = record_event(args.lifecycle_id, args.event, args.summary, args.verification, args.error_fingerprint, args.store, args.failure_class, args.memory_candidates_file)
    else:
        output = audit_lifecycle(args.lifecycle_id, args.store)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0 if output.get("final_gate_passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
