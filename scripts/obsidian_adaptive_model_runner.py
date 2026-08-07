#!/usr/bin/env python3
"""Run the Obsidian-selected adaptive producer with one stronger operational fallback."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from tempfile import mkstemp
from types import SimpleNamespace


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = Path(__file__).resolve().parents[1]
# Signals live in a dependency-free module so the zero-cost gate can reach them without
# loading this file's transitive stack. Re-exported here so callers keep one definition.
_task_signals = _load_file("obsidian_adaptive_task_signals", SCRIPT_DIR / "task_signals.py")
infer_complexity_score = _task_signals.infer_complexity_score
infer_complexity = _task_signals.infer_complexity
infer_task_type = _task_signals.infer_task_type
infer_operation = _task_signals.infer_operation
model_execution_receipt = _load_file("obsidian_adaptive_receipt", SCRIPT_DIR / "model_execution_receipt.py")
task_route_dispatcher = _load_file("obsidian_adaptive_dispatcher", SCRIPT_DIR / "task_route_dispatcher.py")
resolve_entry_model = _load_file("obsidian_adaptive_entry", SCRIPT_DIR / "resolve_entry_model.py")
model_identity_disclosure = _load_file("obsidian_adaptive_identity_disclosure", SCRIPT_DIR / "model_identity_disclosure.py")
obsidian_model_memory = _load_file(
    "obsidian_adaptive_memory",
    SCRIPT_DIR / "obsidian_model_memory.py",
)

SINGLE_PRODUCER_SOURCE_BYTE_LIMIT = 180_000
ESTIMATED_SESSION_CONTEXT_TOKENS = 36_000
ESTIMATED_CHARS_PER_TOKEN = 4
SMALL_EDIT_MAXIMUM_COMPLEXITY_SCORE = 24


def _emit_result_ready(result_path, ready_monotonic_ns):
    print(json.dumps({"schema_version": 1, "stage": "result-ready", "result_path": str(result_path), "result_ready_monotonic_ns": ready_monotonic_ns}, separators=(",", ":")), flush=True)


def _route_part_label(args, recommendation, node=None):
    session_summary = recommendation.get("session_effort") if isinstance(recommendation.get("session_effort"), dict) else {}
    surface = session_summary.get("solving_surface") or ""
    step_kind = (node.get("step_kind") if isinstance(node, dict) else "") or recommendation.get("step_kind") or getattr(args, "step_kind", "") or getattr(args, "operation", "") or getattr(args, "task_type", "") or "task"
    labels = []
    if isinstance(node, dict) and node.get("id"):
        labels.append(str(node["id"]).replace("-", " "))
    if surface:
        labels.append(str(surface).replace("_", " "))
    if step_kind and str(step_kind).replace("_", " ") not in labels:
        labels.append(str(step_kind).replace("_", " "))
    return " / ".join(labels) + " part"


def _model_route_notice(args, recommendation):
    entry_pair = f"{args.resolved_entry_model}|{args.resolved_entry_effort}"
    attempt_pair = recommendation.get("attempt_pair") or recommendation.get("selected_pair") or "unknown|unknown"
    selected_pair = recommendation.get("selected_pair") or attempt_pair
    session_summary = recommendation.get("session_effort") if isinstance(recommendation.get("session_effort"), dict) else {}
    session_escalation = recommendation.get("session_escalation") if isinstance(recommendation.get("session_escalation"), dict) else {}
    previous_pair = session_escalation.get("from_pair") or session_summary.get("last_model_pair") or entry_pair
    repeated_failure = bool(session_summary.get("failure_recorded"))
    route_changed = bool(previous_pair and attempt_pair != "unknown|unknown" and previous_pair != attempt_pair)
    previous_model = previous_pair.split("|", 1)[0] if "|" in previous_pair else ""
    attempt_model = attempt_pair.split("|", 1)[0] if "|" in attempt_pair else ""
    model_changed = bool(previous_model and attempt_model and previous_model != "unknown" and attempt_model != "unknown" and previous_model != attempt_model)
    effort_changed = bool(route_changed and not model_changed)
    task_part = _route_part_label(args, recommendation)
    if repeated_failure and route_changed and model_changed:
        kind = "session_model_escalation"
        message = f"Model update: increased the model to {attempt_pair} for the {task_part} after repeated same-session failure. Entry model remains {entry_pair}; effort is estimated independently from the steps for this part."
    elif repeated_failure and route_changed and effort_changed:
        kind = "session_effort_escalation"
        message = f"Model update: increased solving effort to {attempt_pair} for the {task_part} after repeated same-session failure. Entry model remains {entry_pair}; the model family did not change."
    elif repeated_failure:
        kind = "session_repeated_failure_route"
        message = f"Model route: repeated same-session failure detected; using {attempt_pair} for the {task_part}. Entry model remains {entry_pair}; this route is not a Real-pass claim."
    else:
        kind = "route_selection"
        message = f"Model route: using {attempt_pair} for the {task_part}; entry model is {entry_pair}. Model family and effort were selected separately for this task part."
    return {"kind": kind, "message": message, "task_part": task_part, "entry_pair": entry_pair, "previous_pair": previous_pair, "selected_pair": selected_pair, "attempt_pair": attempt_pair, "model_changed": model_changed, "effort_changed": effort_changed, "repeated_failure": repeated_failure, "route_changed": route_changed, "session_user_effort": session_summary.get("user_effort"), "estimated_steps": session_summary.get("step_estimate"), "estimated_effort": session_summary.get("estimated_effort"), "model_difficulty": session_summary.get("model_difficulty"), "information_burden": session_summary.get("information_burden")}


def _graph_model_route_notice(args, plan, recommendation, merge_recommendation):
    entry_pair = f"{args.resolved_entry_model}|{args.resolved_entry_effort}"
    session_summary = recommendation.get("session_effort") if isinstance(recommendation.get("session_effort"), dict) else {}
    parts = []
    for node in plan.get("nodes", []):
        if node.get("phase") not in {"result", "ending"}:
            continue
        pair = f"{node.get('model')}|{node.get('effort')}"
        parts.append({"task_part": _route_part_label(args, {"session_effort": session_summary}, node), "node_id": node.get("id"), "phase": node.get("phase"), "pair": pair, "dependencies": list(node.get("dependencies") or []), "user_visible": True})
    escalation = recommendation.get("session_escalation") if isinstance(recommendation.get("session_escalation"), dict) else {}
    message = f"Model route ready: each of {len(parts)} task parts has its own model and effort; entry model is {entry_pair}."
    if escalation.get("applied"):
        message = f"Model update: repeated same-session failure changed the affected solving route to {escalation.get('to_pair') or merge_recommendation.get('selected_pair')}; every other task part remains independently assigned. Entry model remains {entry_pair}."
    return {"kind": "graph_model_route", "message": message, "entry_pair": entry_pair, "parts": parts, "repeated_failure": bool(session_summary.get("failure_recorded")), "session_escalation": escalation}


def _emit_model_route_notice(notice):
    event = {"schema_version": 1, "stage": "model-switch-notice", "user_visible": True, **notice}
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _emit_route_ready(args, recommendation):
    attempt_pair = recommendation.get("attempt_pair") or recommendation.get("selected_pair")
    notice = _model_route_notice(args, recommendation)
    event = {"schema_version": 1, "stage": "route-ready", "complexity_score": args.complexity_score, "complexity_band": obsidian_model_memory.complexity_band(args.complexity_score), "entry_pair": f"{args.resolved_entry_model}|{args.resolved_entry_effort}", "entry_source": args.resolved_entry_source, "selected_pair": recommendation.get("selected_pair"), "attempt_pair": attempt_pair, "active_fallback_pair": recommendation.get("active_fallback_pair"), "switch_direction": recommendation.get("switch_direction", "no_switch"), "switch_change": recommendation.get("switch_change", f"initial->{attempt_pair}"), "receipt_path": str(args.receipt_output), "result_path": str(args.result_output), "result_pending": True, "user_visible_message": notice["message"], "model_route_notice": notice}
    session_summary = recommendation.get("session_effort") if isinstance(recommendation.get("session_effort"), dict) else {}
    if session_summary.get("available"):
        event.update({"session_state": session_summary.get("state"), "session_user_effort": session_summary.get("user_effort"), "session_failure_recorded": session_summary.get("failure_recorded"), "session_escalation": recommendation.get("session_escalation")})
    print(json.dumps(event, separators=(",", ":")), flush=True)
    _emit_model_route_notice(notice)


def _atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _receipt_args(args, selected):
    return SimpleNamespace(
        model=selected[0],
        effort=selected[1],
        workload_id=args.workload_id,
        output=args.receipt_output,
        result_output=args.result_output,
        workdir=args.workdir,
        state_db=args.state_db,
        codex_bin=args.codex_bin,
        sandbox=args.sandbox,
        allow_fallback=[],
        ignore_user_config=args.ignore_user_config,
        entry_task=False,
        node_role="result-producer",
        route_marker="LOCKED_ROUTE_NODE",
        stream_result_ready=True,
        result_ready_callback=_emit_result_ready,
        timeout=args.timeout,
        emit_result=False,
    )


def _recommend(args, prompt=""):
    return obsidian_model_memory.recommend_model(
        args.project_root,
        args.task_type,
        args.module,
        file_value=args.file,
        symbol=args.symbol,
        code_kind=args.code_kind,
        operation=args.operation,
        modality=args.modality,
        complexity=args.complexity,
        complexity_score=args.complexity_score,
        risk=args.risk,
        ambiguity=args.ambiguity,
        task_summary=args.task_summary,
        task_name=getattr(args, "task_name", ""),
        task_group=getattr(args, "task_group", ""),
        step_kind=getattr(args, "step_kind", ""),
        capability_tags=getattr(args, "capability_tag", []),
        entry_model=getattr(args, "resolved_entry_model", None) or getattr(args, "entry_model", None) or "",
        entry_effort=getattr(args, "resolved_entry_effort", None) or getattr(args, "entry_effort", None) or "",
        vault=args.vault,
        ladder=args.ladder,
        session_prompt=prompt,
        session_id=getattr(args, "session_id", ""),
    )


def _zero_token_map():
    return {field: 0 for field in model_execution_receipt.TOKEN_FIELDS}


def scheduled_source_paths(prompt, workdir):
    """Return a safe independent-source graph without reading task sources."""
    text = str(prompt or "")
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    schedule_signal = re.search(r"\b(?:independent|parallel|multi[- ]node|scheduled?|workflow graph)\b", normalized)
    read_only_signal = re.search(r"\b(?:read[- ]only|no edits?)\b", normalized) or "do not edit files" in normalized
    if not schedule_signal or not read_only_signal:
        return []
    if _is_exact_expression_contract(normalized):
        return []
    root = Path(workdir).expanduser().resolve()
    sources = []
    candidates = re.findall(r"(?<![\w./-])([\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml))(?![\w/-])", text)
    for candidate_text in candidates:
        relative = Path(candidate_text)
        if relative.is_absolute() or ".." in relative.parts:
            return []
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return []
        if not candidate.is_file():
            return []
        source = relative.as_posix()
        if source not in sources:
            sources.append(source)
    return sources if 2 <= len(sources) <= 3 else []


def schedule_admission(prompt, workdir, sources):
    """Admit fan-out only for context pressure or an explicit latency contract."""
    root = Path(workdir).expanduser().resolve()
    source_bytes = {source: (root / source).stat().st_size for source in sources}
    total_source_bytes = sum(source_bytes.values())
    source_tokens = (total_source_bytes + ESTIMATED_CHARS_PER_TOKEN - 1) // ESTIMATED_CHARS_PER_TOKEN
    fused = bool(_owned_source_sections(prompt, sources) and len(sources) >= 3)
    scheduled_sessions = len(sources) if fused else len(sources) + 1
    explicit_latency = bool(re.search(r"\b(?:must|required to|latency[- ]critical)\b.{0,48}\bparallel\b|\bparallel\b.{0,48}\b(?:must|required|latency[- ]critical)\b", str(prompt or ""), re.IGNORECASE | re.DOTALL))
    context_pressure = total_source_bytes > SINGLE_PRODUCER_SOURCE_BYTE_LIMIT
    admitted = explicit_latency or context_pressure
    return {
        "candidate": True,
        "admitted": admitted,
        "decision": "scheduled_graph" if admitted else "single_adaptive_producer",
        "reason": "explicit_parallel_latency_contract" if explicit_latency else "single_producer_context_budget_exceeded" if context_pressure else "single_producer_lower_estimated_logical_tokens",
        "source_count": len(sources),
        "source_bytes": source_bytes,
        "total_source_bytes": total_source_bytes,
        "single_producer_source_byte_limit": SINGLE_PRODUCER_SOURCE_BYTE_LIMIT,
        "estimated_source_tokens": source_tokens,
        "estimated_single_input_tokens": ESTIMATED_SESSION_CONTEXT_TOKENS + source_tokens,
        "estimated_scheduled_input_tokens": ESTIMATED_SESSION_CONTEXT_TOKENS * scheduled_sessions + source_tokens,
        "estimated_scheduled_result_sessions": scheduled_sessions,
        "fused_final_available": fused,
    }


def _resolved_entry_pair(args):
    explicit_model = getattr(args, "entry_model", None)
    explicit_effort = getattr(args, "entry_effort", None)
    if explicit_model and explicit_effort:
        return explicit_model, explicit_effort, "explicit"
    sessions_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    resolved = resolve_entry_model.resolve_entry_model(os.environ.get("CODEX_THREAD_ID"), sessions_root)
    identity = model_identity_disclosure.resolve_disclosure_identity(entry_resolution=resolved)
    model, effort = identity["effective_pair"].split("|", 1)
    return model, effort, identity["source"]


def _owned_source_contract(prompt, source):
    """Keep global output rules plus the contract explicitly owned by one source."""
    text = str(prompt or "")
    owner_pattern = re.compile(
        rf"(?m)^(?P<section>[A-Za-z][A-Za-z0-9_]*)\s+is\s+owned\s+only\s+by\s+{re.escape(source)}\b"
    )
    owned = owner_pattern.search(text)
    if not owned:
        return text
    any_owner = re.compile(
        r"(?m)^[A-Za-z][A-Za-z0-9_]*\s+is\s+owned\s+only\s+by\s+[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml)\b"
    )
    first_owner = any_owner.search(text)
    end_match = any_owner.search(text, owned.end())
    source_files_match = re.compile(r"(?m)^source_files\b").search(text, owned.end())
    end_candidates = [match.start() for match in (end_match, source_files_match) if match]
    owned_end = min(end_candidates) if end_candidates else len(text)
    common = text[: first_owner.start() if first_owner else owned.start()].rstrip()
    section = text[owned.start():owned_end].strip()
    return f"{common}\n\nOwned source contract:\n{section}".strip()


def _scheduled_branch_prompt(prompt, source):
    source_contract = _owned_source_contract(prompt, source)
    return f"""Complete only the independent source-audit portion supported by `{source}`.

Read only `{source}`. Do not read another source, edit files, run tests, call APIs, or add Markdown commentary. Return one compact valid JSON object containing every final-contract fact explicitly owned by this source. Omit unsupported fields; never guess, substitute, expand identifiers, reorder source events, or emit empty placeholders. Preserve exact identifiers, expressions, leading syntax keywords, contract literals, key order, JSON scalar types, booleans, and source-order semantics. Strip a keyword only when the contract explicitly says to strip it. Before returning, compare every emitted value against the owned contract one final time.

Source-specific output contract:
{source_contract}"""


def _scheduled_merge_prompt(prompt):
    return f"""Use only the completed dependency results below. Do not read source files, edit files, run tests, call APIs, or add Markdown commentary.

Assemble and return exactly the final artifact required by the parent output contract. Reconcile overlapping dependency facts across sources; treat omitted fields as unknown, never as empty values. Prefer direct defining-source facts over a dependent source's unresolved reference. Before release, check every required key, key order, count, expression, leading syntax keyword, explicit contract literal, and source path against the parent contract. Return the exact requested one-line minified JSON and nothing else.

Parent output contract:
{prompt}"""


def _owned_source_sections(prompt, sources):
    """Return exact one-to-one section ownership, or an empty mapping."""
    owner_pattern = re.compile(
        r"(?m)^(?P<section>[A-Za-z][A-Za-z0-9_]*)\s+is\s+owned\s+only\s+by\s+"
        r"(?P<source>[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml))\b"
    )
    matches = list(owner_pattern.finditer(str(prompt or "")))
    source_set = set(sources)
    if len(matches) != len(sources) or not re.search(r"(?m)^source_files\b", str(prompt or "")):
        return {}
    ownership = {}
    sections = set()
    for match in matches:
        source = match.group("source")
        section = match.group("section")
        if source not in source_set or source in ownership or section in sections:
            return {}
        ownership[source] = section
        sections.add(section)
    return ownership if set(ownership) == source_set else {}


def _scheduled_fused_final_prompt(prompt, source):
    source_contract = _owned_source_contract(prompt, source)
    return f"""Complete the final owned source audit supported by `{source}`, then assemble the final artifact from that audit and the completed dependency results below.

Read only `{source}`. Do not read another source, edit files, run tests, call APIs, or add Markdown commentary. Dependency results own every other section; do not redo or reinterpret their source audits. Preserve each dependency section exactly unless the parent contract requires only deterministic placement into the final object. Audit every fact owned by this final source, then return exactly the final one-line minified JSON required by the parent contract. Before release, check every required key, key order, JSON scalar type, source path, and source-order rule.

Final-source contract:
{source_contract}

Parent output contract:
{prompt}"""


def _is_exact_expression_contract(prompt):
    normalized = re.sub(r"\s+", " ", str(prompt or "")).strip().lower()
    return "return exactly" in normalized and sum(
        marker in normalized
        for marker in ("copy", "preserve", "exact literal", "exact expression", "key order")
    ) >= 2


def _scheduled_branch_pair(prompt, floor_pair):
    """Protect exact-expression contracts from weak/low-effort source drift."""
    if _is_exact_expression_contract(prompt):
        return tuple(task_route_dispatcher.MODEL_ROLE_PAIRS["balanced_default"].split("|", 1))
    return tuple(floor_pair.split("|", 1))


def _scheduled_plan(args, prompt, sources, entry_model, entry_effort, entry_recommendation=None):
    schedule_digest = hashlib.sha256((str(args.workdir) + "\0" + prompt).encode("utf-8")).hexdigest()[:16]
    configured_cache_root = getattr(args, "cache_root", None)
    cache_root = Path(configured_cache_root).expanduser().resolve() if configured_cache_root is not None else Path(args.project_root).expanduser().resolve() / "Cache" / "task-analyze"
    cache_dir = cache_root / f"adaptive-schedule-{schedule_digest}"
    floor_pair = task_route_dispatcher.MODEL_ROLE_PAIRS["floor"]
    floor_model, floor_effort = floor_pair.split("|", 1)
    schedule_producer = task_route_dispatcher.PRIORITY_PRODUCER_CONFIG
    schedule_pair = f"{schedule_producer['id']}|{schedule_producer['effort_by_complexity']['easy']}" if schedule_producer.get("enabled") else floor_pair
    branch_model, branch_effort = _scheduled_branch_pair(prompt, schedule_pair)
    owned_sections = _owned_source_sections(prompt, sources)
    fused_source = sources[-1] if owned_sections and len(sources) >= 3 else None
    independent_sources = sources[:-1] if fused_source else sources
    branch_ids = []
    nodes = []
    for index, source in enumerate(independent_sources, start=1):
        node_id = f"source-{index}"
        branch_ids.append(node_id)
        nodes.append({"id": node_id, "phase": "result", "skill": "workflow-skill", "model": branch_model, "effort": branch_effort, "priority_producer": True, "dependencies": [], "prompt": _scheduled_branch_prompt(prompt, source), "sandbox": "read-only", "source_allowlist": [source], "execution_domain": "general", "timeout": min(args.timeout, 300)})
    condition = {"task_family": "grounded", "artifact": "answer", "scope": "multi", "ambiguity": args.ambiguity, "modality": "text", "risk": args.risk, "complexity": "complex", "owning_skill": "workflow-skill", "project_family": "global", "verification_shape": "real", "execution_domain": "general"}
    candidate_ladder = task_route_dispatcher.adaptive_pair_texts_for_profile("grounded", "text", args.risk, "complex", args.ambiguity)
    main_node = {"id": "merge-result", "phase": "result", "skill": "workflow-skill", "model": floor_model, "effort": floor_effort, "dependencies": branch_ids, "prompt": _scheduled_fused_final_prompt(prompt, fused_source) if fused_source else _scheduled_merge_prompt(prompt), "sandbox": "read-only", "execution_domain": "general", "routing_condition": condition, "task_summary": "Audit the final owned source and merge independent source results." if fused_source else "Merge independent source audits into one exact JSON manifest.", "candidate_ladder": candidate_ladder, "static_suggestion": floor_pair, "hard_floor": floor_pair, "trial": False, "timeout": min(args.timeout, 300), "model_memory_scope": {"task_type": "question", "module": args.module, "code_kind": "general", "operation": "work", "step_kind": "integration", "capability_tags": ["dependency-merge", "grounded-source-audit"]}}
    if fused_source:
        main_node["source_allowlist"] = [fused_source]
        main_node["fuses_owned_source_with_dependencies"] = True
    else:
        main_node["reads_dependency_results_only"] = True
    recommendation, proof = task_route_dispatcher._obsidian_recommendation_and_proof(main_node, args.workdir, entry_model, entry_effort)
    selected_pair = recommendation.get("selected_pair")
    if not selected_pair:
        raise ValueError("scheduled merge recommendation is exhausted")
    main_node["model"], main_node["effort"] = selected_pair.split("|", 1)
    main_node["trial"] = recommendation.get("trial") is True
    main_node["routing_recommendation"] = proof
    nodes.append(main_node)
    nodes.append({"id": "ending-verify", "phase": "ending", "skill": "verify-skill", "model": floor_model, "effort": floor_effort, "dependencies": ["merge-result"], "prompt": "Audit only the released scheduled-route receipts, dependency coverage, and exact published result. Do not rerun sources, tests, APIs, edits, or repairs.", "sandbox": "read-only", "timeout": 60})
    return {"schema_version": 2, "complexity": "complex", "topology": "mixed" if fused_source else "parallel", "schedule_mode": "parallel_sources_fused_final" if fused_source else "parallel_independent_sources", "fused_source": fused_source, "parallel_branch_count": len(independent_sources), "cache_dir": str(cache_dir), "entry": {"model": entry_model, "effort": entry_effort}, "nodes": nodes, "main_result_node": "merge-result", "first_result_timeout_seconds": min(max(args.timeout, 60), 900)}, recommendation


def _run_scheduled_graph(args, prompt, sources, recommendation, started_ns, admission=None):
    entry_model = args.resolved_entry_model
    entry_effort = args.resolved_entry_effort
    entry_source = args.resolved_entry_source
    plan, merge_recommendation = _scheduled_plan(args, prompt, sources, entry_model, entry_effort, recommendation)
    graph_notice = _graph_model_route_notice(args, plan, recommendation, merge_recommendation)
    _emit_model_route_notice(graph_notice)
    ready = {}

    def publish_result(result_path, ready_monotonic_ns):
        text = Path(result_path).read_text(encoding="utf-8")
        _atomic_write_text(args.result_output, text)
        ready["monotonic_ns"] = ready_monotonic_ns
        _emit_result_ready(args.result_output, ready_monotonic_ns)

    manifest = task_route_dispatcher.run_plan(plan, entry_model, entry_effort, args.workdir, state_db=args.state_db, codex_bin=args.codex_bin, skills_root=SKILLS_ROOT, result_ready_callback=publish_result)
    if manifest.get("status") != "pass" or not args.result_output.is_file():
        return {"status": "fail", "reason": "scheduled_graph_failed", "execution_mode": "scheduled_adaptive_graph", "entry_pair": f"{entry_model}|{entry_effort}", "entry_source": entry_source, "sources": sources, "manifest_path": manifest.get("manifest_path"), "failures": manifest.get("failures", []), "ending_real_status": "not_started"}
    result_nodes = [node for node in manifest.get("nodes", []) if node.get("phase") == "result"]
    main_node = next(node for node in result_nodes if node.get("id") == plan["main_result_node"])
    main_receipt = json.loads(Path(main_node["receipt_path"]).read_text(encoding="utf-8"))
    node_receipts = [json.loads(Path(node["receipt_path"]).read_text(encoding="utf-8")) for node in result_nodes]
    tokens = model_execution_receipt.aggregate_token_maps([receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {} for receipt in node_receipts])
    route_attempts = [attempt for receipt in node_receipts for attempt in receipt.get("route_attempts", []) if isinstance(attempt, dict)]
    receipt = dict(main_receipt)
    receipt["route_attempts"] = route_attempts
    receipt["strategy_tokens"] = tokens
    receipt["tokens"] = tokens
    receipt["process_elapsed_ms"] = manifest.get("first_result_elapsed_ms")
    receipt["scheduled_graph"] = True
    receipt["schedule_mode"] = plan.get("schedule_mode", "parallel_independent_sources")
    receipt["scheduled_sources"] = sources
    receipt["scheduled_nodes"] = [{"id": node.get("id"), "requested_pair": f"{node.get('requested_model')}|{node.get('requested_effort')}", "effective_pair": f"{node.get('model')}|{node.get('effort')}", "tokens": (node.get("tokens") or {}).get("total_tokens"), "process_elapsed_ms": node.get("process_elapsed_ms"), "step_kind": (node.get("model_memory_scope") or {}).get("step_kind", "grounded-source-audit"), "capability_tags": (node.get("model_memory_scope") or {}).get("capability_tags", ["grounded-source-audit"])} for node in result_nodes]
    receipt["scheduled_result_node_count"] = len(result_nodes)
    receipt["parallel_branch_count"] = plan.get("parallel_branch_count", len(sources))
    receipt["fused_source"] = plan.get("fused_source")
    receipt["schedule_admission"] = admission
    receipt["result_published"] = True
    receipt["result_ready_monotonic_ns"] = ready.get("monotonic_ns", main_receipt.get("result_ready_monotonic_ns"))
    receipt["entry_model"] = entry_model
    receipt["entry_effort"] = entry_effort
    receipt["entry_pair"] = f"{entry_model}|{entry_effort}"
    receipt["entry_source"] = entry_source
    receipt["model_learning_context"] = _model_learning_context(args)
    if isinstance(recommendation.get("session_effort"), dict) and recommendation["session_effort"].get("available"):
        receipt["session_effort"] = recommendation["session_effort"]
        receipt["session_escalation"] = recommendation.get("session_escalation")
    receipt["recommendation_state"] = merge_recommendation.get("attempt_calibration_state", merge_recommendation.get("calibration_state"))
    receipt["trial"] = merge_recommendation.get("attempt_trial", merge_recommendation.get("trial"))
    receipt["selection_provenance"] = merge_recommendation.get("selection_basis")
    receipt["capability_assignment"] = [{"node_id": node["id"], "step_kind": node["step_kind"], "capability_tags": node["capability_tags"], "effective_pair": node["effective_pair"]} for node in receipt["scheduled_nodes"]]
    receipt["complexity_score"] = args.complexity_score
    receipt["complexity_band"] = obsidian_model_memory.complexity_band(args.complexity_score)
    receipt["switch_direction"] = "no_switch"
    receipt["switch_change"] = "scheduled_graph"
    receipt["model_route_notice"] = graph_notice
    _atomic_write_json(args.receipt_output, receipt)
    effective_pairs = [node["effective_pair"] for node in receipt["scheduled_nodes"]]
    ready_ns = receipt.get("result_ready_monotonic_ns")
    summary = {"status": "pass", "reason": "independent_graph_scheduled", "execution_mode": "scheduled_adaptive_graph", "schedule_mode": receipt["schedule_mode"], "schedule_admission": admission, "entry_pair": f"{entry_model}|{entry_effort}", "entry_source": entry_source, "memory_source": recommendation["source"], "memory_available": recommendation["memory_available"], "selected_pair": merge_recommendation.get("selected_pair"), "executed_pair": receipt.get("effective_pair") or receipt.get("requested_pair"), "executed_pairs": effective_pairs, "complexity_score": args.complexity_score, "complexity_band": receipt["complexity_band"], "switch_direction": "no_switch", "switch_change": "scheduled_graph", "scheduled_sources": sources, "parallel_branch_count": receipt["parallel_branch_count"], "fused_source": receipt["fused_source"], "scheduled_result_node_count": len(result_nodes), "receipt_path": str(args.receipt_output), "result_path": str(args.result_output), "result_published": True, "manifest_path": manifest.get("manifest_path"), "ending_handoff_path": manifest.get("ending_handoff_path"), "total_tokens": tokens.get("total_tokens"), "elapsed_ms": manifest.get("first_result_elapsed_ms"), "first_result_elapsed_ms": round((ready_ns - started_ns) / 1_000_000) if isinstance(ready_ns, int) and ready_ns >= started_ns else manifest.get("first_result_elapsed_ms"), "ending_real_status": "pending", "model_learning_context": receipt["model_learning_context"], "model_route_notice": graph_notice}
    if args.emit_result:
        summary["result"] = args.result_output.read_text(encoding="utf-8").rstrip("\n")
    return summary


def _model_learning_context(args):
    def clean(value, limit=600):
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]
    capability = obsidian_model_memory.task_capability_profile(args.task_type, args.code_kind, args.operation, args.modality, args.complexity_score, args.risk, args.ambiguity, args.task_summary, getattr(args, "step_kind", ""), getattr(args, "capability_tag", []))
    normalized_task_name = obsidian_model_memory.session_effort.normalize_task_name(getattr(args, "task_name", "")) if getattr(args, "task_name", "") else ""
    normalized_task_group = obsidian_model_memory.session_effort.normalize_task_name(getattr(args, "task_group", ""), "group") if getattr(args, "task_group", "") else ""
    project_key = obsidian_model_memory.project_change_memory._project_identity(args.project_root)["key"]
    task_scope = obsidian_model_memory.session_effort.task_scope_key(project_key, args.task_type, args.module, normalized_task_name) if normalized_task_name else ""
    task_group_key = obsidian_model_memory.session_effort.task_group_key(project_key, normalized_task_group, normalized_task_name)
    codex_session_key = obsidian_model_memory.session_effort.session_key(getattr(args, "session_id", "")) if getattr(args, "session_id", "") else ""
    return {"project_root": clean(Path(args.project_root).expanduser().resolve(), 1200), "task_type": clean(args.task_type, 160), "module": clean(args.module, 160), "file": clean(args.file), "symbol": clean(args.symbol), "code_kind": clean(args.code_kind, 80), "operation": clean(args.operation, 80), "modality": clean(args.modality, 40), "complexity": clean(args.complexity, 40), "complexity_score": args.complexity_score, "complexity_band": obsidian_model_memory.complexity_band(args.complexity_score), "risk": clean(args.risk, 40), "ambiguity": clean(args.ambiguity, 40), "task_name": normalized_task_name, "task_group": normalized_task_group, "task_scope_key": task_scope, "task_group_key": task_group_key, "codex_session_key": codex_session_key, "task_summary": clean(args.task_summary), "step_kind": capability["step_kind"], "capability_tags": capability["capability_tags"], "capability_fingerprint": capability["capability_fingerprint"], "entry_model": clean(getattr(args, "resolved_entry_model", ""), 120), "entry_effort": clean(getattr(args, "resolved_entry_effort", ""), 40), "entry_pair": clean(f"{getattr(args, 'resolved_entry_model', '')}|{getattr(args, 'resolved_entry_effort', '')}", 180), "entry_source": clean(getattr(args, "resolved_entry_source", ""), 80)}


def _pre_execution_failure(receipt_args):
    receipt = model_execution_receipt.failed_run_receipt(receipt_args, "execution")
    receipt["tokens"] = _zero_token_map()
    receipt["process_elapsed_ms"] = 0
    receipt["pre_execution_failure"] = True
    if receipt.get("route_attempts"):
        receipt["route_attempts"][0]["tokens"] = _zero_token_map()
        receipt["route_attempts"][0]["process_elapsed_ms"] = 0
        receipt["route_attempts"][0]["pre_execution_failure"] = True
    return model_execution_receipt.annotate_operational_fallback(receipt)


def _attempt_pairs(args, recommendation):
    attempt_pair = recommendation.get("attempt_pair") or recommendation["selected_pair"]
    active_pair = recommendation.get("active_fallback_pair")
    _, active_pairs = obsidian_model_memory.load_shared_ladder(args.ladder)
    pairs = [attempt_pair]
    if active_pair and active_pair != attempt_pair:
        pairs.append(active_pair)
    for pair in model_execution_receipt.normalize_fallback_pairs(args.allow_fallback):
        if pair in active_pairs and pair not in pairs:
            pairs.append(pair)
    return pairs


def _exact_contract_recommendation(prompt, recommendation):
    if not _is_exact_expression_contract(prompt):
        return recommendation
    guarded = dict(recommendation)
    pair = task_route_dispatcher.MODEL_ROLE_PAIRS["frontier_complex"]
    model, effort = pair.split("|", 1)
    prior_attempt = recommendation.get("attempt_pair") or recommendation.get("selected_pair")
    guarded.update({
        "selected_pair": pair,
        "selected_model": model,
        "selected_effort": effort,
        "attempt_pair": pair,
        "active_fallback_pair": None,
        "attempt_trial": False,
        "attempt_reason": "exact_expression_quality_guard",
        "attempt_calibration_state": "quality_boundary",
        "trial": False,
        "switch_direction": "upgrade",
        "switch_change": f"{prior_attempt}->{pair}",
        "reason": "exact_expression_quality_guard",
        "calibration_state": "quality_boundary",
    })
    return guarded


def _merge_attempt_receipts(receipts, planned_pairs, attempt_pair, active_pair, result_output):
    receipt = dict(receipts[-1])
    route_attempts = []
    operational_failures = []
    for attempted_pair, attempted_receipt in zip(planned_pairs, receipts):
        attempts = attempted_receipt.get("route_attempts")
        if isinstance(attempts, list):
            route_attempts.extend(dict(attempt) for attempt in attempts if isinstance(attempt, dict))
        if model_execution_receipt.immediate_operational_fallback(attempted_receipt):
            operational_failures.append(attempted_pair)
    metrics = model_execution_receipt.aggregate_token_maps([
        attempted.get("tokens") if isinstance(attempted.get("tokens"), dict) else {}
        for attempted in receipts
    ])
    elapsed_values = [attempted.get("process_elapsed_ms") for attempted in receipts]
    elapsed = sum(elapsed_values) if elapsed_values and all(isinstance(value, int) and value >= 0 for value in elapsed_values) else None
    receipt["priority_attempt_pair"] = attempt_pair
    receipt["initial_attempt_pair"] = attempt_pair
    receipt["selected_pair"] = attempt_pair
    if isinstance(attempt_pair, str) and "|" in attempt_pair:
        requested_model, requested_effort = attempt_pair.split("|", 1)
        receipt["requested_pair"] = attempt_pair
        receipt["requested_model"] = requested_model
        receipt["requested_effort"] = requested_effort
    receipt["active_fallback_pair"] = active_pair
    receipt["allowed_fallback_pairs"] = planned_pairs[1:]
    receipt["operational_failure_pairs"] = operational_failures
    receipt["route_attempts"] = route_attempts
    receipt["last_attempt_tokens"] = dict(receipt.get("tokens") or {})
    receipt["last_attempt_process_elapsed_ms"] = receipt.get("process_elapsed_ms")
    receipt["strategy_tokens"] = metrics
    receipt["strategy_elapsed_ms"] = elapsed
    receipt["tokens"] = metrics
    receipt["process_elapsed_ms"] = elapsed
    receipt["result_published"] = bool(result_output.is_file() and result_output.stat().st_size > 0)
    return receipt


def run(args, prompt):
    started_ns = time.monotonic_ns()
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt_required")
    if not hasattr(args, "complexity_score") or args.complexity_score is None:
        args.complexity_score = 65 if args.complexity == "complex" else 35
    args.resolved_entry_model, args.resolved_entry_effort, args.resolved_entry_source = _resolved_entry_pair(args)
    recommendation = _exact_contract_recommendation(prompt, _recommend(args, prompt))
    _emit_route_ready(args, recommendation)
    sources = scheduled_source_paths(prompt, args.workdir)
    admission = schedule_admission(prompt, args.workdir, sources) if sources else None
    if admission and admission["admitted"]:
        return _run_scheduled_graph(args, prompt, sources, recommendation, started_ns, admission)
    pair = recommendation.get("attempt_pair") or recommendation.get("selected_pair")
    if not pair:
        return {"status": "blocked", "reason": recommendation.get("reason"), "recommendation": recommendation}
    if args.result_output.exists():
        args.result_output.unlink()
    active_pair = recommendation.get("active_fallback_pair")
    planned_pairs = _attempt_pairs(args, recommendation)
    receipts = []
    attempted_pairs = []
    for planned_pair in planned_pairs:
        if args.result_output.exists() and args.result_output.stat().st_size == 0:
            args.result_output.unlink()
        selected = tuple(planned_pair.split("|", 1))
        receipt_args = _receipt_args(args, selected)
        try:
            with model_execution_receipt.adaptive_producer_authorization():
                attempt_receipt = model_execution_receipt.run_receipt(receipt_args, prompt)
        except (OSError, ValueError):
            attempt_receipt = _pre_execution_failure(receipt_args)
        visible_result = bool(args.result_output.is_file() and args.result_output.stat().st_size > 0)
        attempt_receipt["result_published"] = visible_result
        attempt_receipt = model_execution_receipt.annotate_operational_fallback(attempt_receipt)
        receipts.append(attempt_receipt)
        attempted_pairs.append(planned_pair)
        if attempt_receipt.get("status") == "pass" and visible_result:
            break
        if not model_execution_receipt.immediate_operational_fallback(attempt_receipt):
            break
    receipt = _merge_attempt_receipts(receipts, attempted_pairs, pair, active_pair, args.result_output)
    receipt["schedule_admission"] = admission
    receipt["quality_pair"] = recommendation.get("selected_pair")
    receipt["selection_reason"] = recommendation.get("attempt_reason", recommendation.get("reason"))
    receipt["recommendation_state"] = recommendation.get("attempt_calibration_state", recommendation.get("calibration_state"))
    receipt["trial"] = recommendation.get("attempt_trial", recommendation.get("trial"))
    receipt["complexity_score"] = args.complexity_score
    receipt["complexity_band"] = obsidian_model_memory.complexity_band(args.complexity_score)
    receipt["switch_direction"] = recommendation.get("switch_direction", "no_switch")
    receipt["switch_change"] = recommendation.get("switch_change", f"initial->{pair}")
    receipt["entry_model"] = args.resolved_entry_model
    receipt["entry_effort"] = args.resolved_entry_effort
    receipt["entry_pair"] = f"{args.resolved_entry_model}|{args.resolved_entry_effort}"
    receipt["entry_source"] = args.resolved_entry_source
    receipt["model_route_notice"] = _model_route_notice(args, recommendation)
    learning_context = _model_learning_context(args)
    receipt["model_learning_context"] = learning_context
    if isinstance(recommendation.get("session_effort"), dict) and recommendation["session_effort"].get("available"):
        receipt["session_effort"] = recommendation["session_effort"]
        receipt["session_escalation"] = recommendation.get("session_escalation")
    receipt["selection_provenance"] = recommendation.get("selection_basis")
    receipt["capability_assignment"] = [{"node_id": "result", "step_kind": learning_context["step_kind"], "capability_tags": learning_context["capability_tags"], "effective_pair": receipt.get("effective_pair") or receipt.get("requested_pair")}]
    result_published = bool(receipt.get("result_published") is True and args.result_output.is_file() and args.result_output.stat().st_size > 0)
    receipt["result_published"] = result_published
    if result_published:
        result_text = args.result_output.read_text(encoding="utf-8", errors="replace")
        try:
            normalized_result = model_identity_disclosure.normalize_result_disclosure(
                result_text,
                args.complexity_score,
                runtime_receipt=receipt,
            )
        except ValueError:
            pass
        else:
            _atomic_write_text(args.result_output, normalized_result.rstrip("\n") + "\n")
    _atomic_write_json(args.receipt_output, receipt)
    tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
    ready_ns = receipt.get("result_ready_monotonic_ns")
    summary = {
        "status": "pass" if receipt.get("status") == "pass" and result_published else "fail",
        "reason": recommendation.get("attempt_reason", recommendation["reason"]) if receipt.get("status") == "pass" and result_published else "producer_operational_failure",
        "execution_mode": "obsidian_adaptive_producer",
        "schedule_admission": admission,
        "memory_source": recommendation["source"],
        "memory_available": recommendation["memory_available"],
        "entry_pair": receipt["entry_pair"],
        "entry_source": receipt["entry_source"],
        "entry_anchor_pair": recommendation.get("entry_anchor_pair"),
        "selected_pair": pair,
        "active_fallback_pair": active_pair,
        "executed_pair": receipt.get("effective_pair") or receipt.get("requested_pair"),
        "quality_pair": recommendation.get("selected_pair"),
        "complexity_score": args.complexity_score,
        "complexity_band": receipt["complexity_band"],
        "switch_direction": receipt["switch_direction"],
        "switch_change": receipt["switch_change"],
        "operational_failure_pairs": receipt.get("operational_failure_pairs", []),
        "trial": recommendation.get("attempt_trial", recommendation["trial"]),
        "calibration_state": recommendation.get("attempt_calibration_state", recommendation["calibration_state"]),
        "specificity": recommendation["specificity"],
        "matched_records": recommendation["matched_records"],
        "project_key": recommendation["project_key"],
        "receipt_path": str(args.receipt_output),
        "result_path": str(args.result_output),
        "result_published": result_published,
        "total_tokens": tokens.get("total_tokens"),
        "elapsed_ms": receipt.get("process_elapsed_ms"),
        "first_result_elapsed_ms": round((ready_ns - started_ns) / 1_000_000) if isinstance(ready_ns, int) and ready_ns >= started_ns else None,
        "ending_real_status": "pending" if receipt.get("status") == "pass" and result_published else "not_started",
        "model_learning_context": learning_context,
        "model_route_notice": receipt["model_route_notice"],
    }
    if isinstance(recommendation.get("session_effort"), dict) and recommendation["session_effort"].get("available"):
        summary["session_effort"] = recommendation["session_effort"]
        summary["session_escalation"] = recommendation.get("session_escalation")
    if args.emit_result and summary["status"] == "pass":
        summary["result"] = args.result_output.read_text(encoding="utf-8").rstrip("\n")
    return summary


def resolve_fast_path_args(args, prompt):
    explicit_fields = ("project_root", "task_type", "module", "workload_id", "receipt_output", "result_output")
    fast_path = not all(getattr(args, field) is not None for field in explicit_fields)
    workdir = Path(args.workdir).expanduser().resolve()
    project_root = Path(args.project_root or os.environ.get("CODEX_PROJECT_ROOT") or workdir).expanduser().resolve()
    prompt_text = str(prompt or "")
    read_only_answer = bool(re.search(r"\b(?:read[- ]only|no edits?)\b", prompt_text, re.IGNORECASE) and re.search(r"[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml)\b", prompt_text))
    task_type = args.task_type or ("question" if read_only_answer else infer_task_type(prompt))
    module_name = args.module or project_root.name or "workspace"
    if args.complexity_score is None:
        args.complexity_score = 65 if args.complexity == "complex" else 35 if args.complexity == "easy" else infer_complexity_score(prompt) if fast_path else 35
    if isinstance(args.complexity_score, bool) or not 0 <= args.complexity_score <= 100:
        raise ValueError("complexity_score must be an integer from 0 to 100")
    args.complexity = "complex" if args.complexity_score >= 50 else "easy"
    if fast_path and args.operation == "work":
        args.operation = infer_operation(prompt)
    complexity_band = obsidian_model_memory.complexity_band(args.complexity_score)
    args.complexity_band = complexity_band
    identity = "\0".join((str(project_root), task_type, module_name, args.file, args.symbol, args.code_kind, args.operation, args.modality, str(args.complexity_score), complexity_band, args.risk, args.ambiguity, getattr(args, "step_kind", ""), ",".join(getattr(args, "capability_tag", [])), prompt))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    default_output_root = project_root / "Cache" / "task-analyze" / "adaptive-producer" / f"fast-{digest}"
    args.workdir = workdir
    args.project_root = project_root
    args.task_type = task_type
    args.module = module_name
    args.task_name = args.task_name or os.environ.get("CODEX_TASK_NAME", "")
    args.task_group = args.task_group or os.environ.get("CODEX_TASK_GROUP", "")
    args.session_id = obsidian_model_memory.session_effort.resolve_session_id(prompt, args.session_id)
    args.task_summary = args.task_summary or re.sub(r"\s+", " ", prompt).strip()[:280]
    args.workload_id = args.workload_id or f"fast-{digest}"
    args.receipt_output = Path(args.receipt_output) if args.receipt_output is not None else default_output_root / "receipt.json"
    args.result_output = Path(args.result_output) if args.result_output is not None else default_output_root / "result.txt"
    args.cache_root = Path(args.cache_root).expanduser().resolve() if args.cache_root is not None else project_root / "Cache" / "task-analyze"
    args.sandbox = args.sandbox or ("workspace-write" if fast_path else "read-only")
    args.emit_result = bool(args.emit_result or fast_path)
    if args.timeout <= 0 or args.receipt_output == args.result_output:
        raise ValueError("invalid runner output or timeout")
    return args


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run one Obsidian-memory-selected catalog priority/quality producer")
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--ladder", type=Path, default=obsidian_model_memory.DEFAULT_LADDER)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--task-type")
    parser.add_argument("--module")
    parser.add_argument("--file", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--code-kind", default="general")
    parser.add_argument("--operation", default="work")
    parser.add_argument("--modality", choices=sorted(obsidian_model_memory.MODALITY_VALUES), default="text")
    parser.add_argument("--complexity", choices=sorted(obsidian_model_memory.COMPLEXITY_VALUES))
    parser.add_argument("--complexity-score", type=int)
    parser.add_argument("--risk", choices=sorted(obsidian_model_memory.LEVEL_VALUES), default="low")
    parser.add_argument("--ambiguity", choices=sorted(obsidian_model_memory.LEVEL_VALUES), default="low")
    parser.add_argument("--task-summary", default="")
    parser.add_argument("--task-name", default="")
    parser.add_argument("--task-group", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--step-kind", choices=sorted(obsidian_model_memory.STEP_KINDS), default="")
    parser.add_argument("--capability-tag", action="append", default=[])
    parser.add_argument("--workload-id")
    parser.add_argument("--receipt-output", type=Path)
    parser.add_argument("--result-output", type=Path)
    parser.add_argument("--cache-root", type=Path, help="Runtime-derived root for scheduled graph support artifacts; defaults to project Cache/task-analyze.")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--state-db", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state_5.sqlite")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--ignore-user-config", action="store_true")
    parser.add_argument("--allow-fallback", action="append", default=[])
    parser.add_argument("--entry-model")
    parser.add_argument("--entry-effort")
    parser.add_argument("--emit-result", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    prompt = sys.stdin.read()
    try:
        args = resolve_fast_path_args(args, prompt)
        summary = run(args, prompt)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        summary = {"status": "fail", "reason": str(error)[:120] or "runner_validation_failed"}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
