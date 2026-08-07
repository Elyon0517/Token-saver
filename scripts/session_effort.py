#!/usr/bin/env python3
"""Detect repeated same-task user effort inside one Codex session.

This module keeps session evidence local and prompt-free. It reads the native
Codex rollout only to derive a hashed task fingerprint, continuity count,
failure signal, and the model pair used before the current submission.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SESSION_SCHEMA_VERSION = 2
LOCAL_MEMORY_SCHEMA_VERSION = 2
TASK_NAME_MAX_LENGTH = 96
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
UUID_SEARCH_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
PAIR_PATTERN = re.compile(r"^gpt-[^|]+\|(?:low|medium|high|xhigh|max|ultra)$")
MODEL_ORDER = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_ALIASES = {"light": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "ultra"}
QUALITY_EFFORTS = {"gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"), "gpt-5.6-terra": EFFORT_ORDER, "gpt-5.6-sol": EFFORT_ORDER}
KNOWN_QUALITY_PAIRS = tuple(f"{model}|{effort}" for model in MODEL_ORDER for effort in QUALITY_EFFORTS[model])
REPAIR_SIGNALS = frozenset({"again", "already", "broken", "didnt", "failed", "fix", "incorrect", "issue", "problem", "redo", "remove", "repair", "retry", "same", "still", "wrong", "not", "nothing"})
TASK_SIGNALS = frozenset({"check", "change", "create", "debug", "edit", "fix", "implement", "inspect", "make", "modify", "repair", "solve", "test", "update", "write"})
STOPWORDS = frozenset({"a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be", "because", "before", "but", "by", "can", "could", "did", "do", "does", "for", "from", "get", "give", "has", "have", "here", "how", "i", "if", "in", "is", "it", "just", "me", "my", "no", "of", "on", "one", "or", "should", "show", "so", "that", "the", "then", "these", "this", "to", "two", "use", "was", "we", "what", "when", "where", "which", "with", "why", "will", "you", "your"})
GENERIC_TOPIC = frozenset({"change", "check", "code", "edit", "fix", "issue", "make", "problem", "repair", "solve", "task", "test", "update", "work"})
TOKEN_ALIASES = {"conner": "corner", "conners": "corner", "corners": "corner", "dots": "dot", "lines": "line", "intersections": "intersection", "intersecting": "intersection", "sleeves": "sleeve", "skills": "skill", "models": "model", "svgdrawer": "svg"}
CORE_SOLVING_TERMS = frozenset({"algorithm", "analyze", "architecture", "build", "code", "compare", "corner", "debug", "decide", "design", "diagnose", "edit", "fix", "geometry", "implement", "integration", "intersection", "line", "migrate", "outline", "path", "plan", "problem", "research", "repair", "review", "shape", "solve", "svg", "synthesize", "test", "trace", "transform", "understand", "update"})
IMAGE_INSPECTION_TERMS = frozenset({"compare", "image", "inspect", "look", "read", "screenshot", "view", "visual"})
LONG_TASK_TERMS = frozenset({"all", "across", "every", "first", "integrate", "many", "multiple", "pipeline", "second", "step", "then", "workflow"})
DIFFICULTY_TERMS = frozenset({"architecture", "difficult", "geometry", "hard", "integration", "multiple", "pipeline", "complex", "workflow"})
STEP_ACTION_TERMS = frozenset({"analyze", "check", "choose", "compare", "create", "decide", "deploy", "edit", "export", "fix", "generate", "implement", "inspect", "integrate", "interpret", "make", "migrate", "migration", "modify", "publish", "read", "repair", "research", "review", "run", "solve", "synthesize", "test", "tests", "trace", "transform", "understand", "update", "verify", "write"})
INFORMATION_BURDEN_TERMS = frozenset({"analyze", "comprehensive", "context", "deep", "every", "information", "massive", "read", "research", "source", "synthesize", "understand", "understanding"})
FRONTIER_DIFFICULTY_TERMS = frozenset({"ambiguous", "architecture", "comprehensive", "deep", "difficult", "massive", "research", "source", "synthesize", "tradeoff", "understand", "understanding"})
ROUTE_HINT_PATTERN = re.compile(r"\b(?:gpt[- ]?5\.6[- ]?)?(luna|terra|sol)\s*(?:\||/|-|to)?\s*(light|low|medium|high|xhigh|max|ultra)\b", re.IGNORECASE)


def _valid_session_id(value):
    return isinstance(value, str) and bool(UUID_PATTERN.fullmatch(value.strip().lower()))


def _codex_home():
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def session_key(session_id):
    return hashlib.sha256(str(session_id or "").strip().lower().encode("utf-8")).hexdigest()[:24]


def _session_key(session_id):
    return session_key(session_id)


def normalize_task_name(value, fallback="task"):
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    normalized = re.sub(r"(?:sk-[a-z0-9_-]{8,}|/users/[^ ]+|/home/[^ ]+|[a-z]:\\[^ ]+)", "private", normalized, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:TASK_NAME_MAX_LENGTH]
    if slug:
        return slug
    digest = hashlib.sha256(str(value or fallback).encode("utf-8")).hexdigest()[:12]
    return f"{fallback}-{digest}"


def task_scope_key(project_key="", task_type="", module="", task_name=""):
    payload = "|".join(normalize_task_name(value, "scope") if index == 3 else str(value or "").strip().lower() for index, value in enumerate((project_key, task_type, module, task_name)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def task_group_key(project_key="", task_group="", task_name=""):
    relation_name = task_group or task_name
    if not str(relation_name or "").strip():
        return ""
    normalized = normalize_task_name(relation_name, "group")
    return hashlib.sha256(f"{str(project_key or '').strip().lower()}|task-group|{normalized}".encode("utf-8")).hexdigest()[:24]


def task_context_key(project_key="", task_type="", module="", file_value="", symbol="", capability_fingerprint="", operation="", modality="", complexity_band="", risk="", ambiguity=""):
    values = (project_key, task_type, module, file_value, symbol, capability_fingerprint, operation, modality, complexity_band, risk, ambiguity)
    if not any(str(value or "").strip() for value in values):
        return ""
    payload = "|".join(re.sub(r"\s+", " ", str(value or "")).strip().lower() for value in values)
    return hashlib.sha256(f"task-context|{payload}".encode("utf-8")).hexdigest()[:24]


def session_task_scope_key(session_key_value, task_scope):
    return hashlib.sha256(f"{str(session_key_value or '')}|{str(task_scope or '')}".encode("utf-8")).hexdigest()[:24]


def scope_relation(record, *, session_key_value="", task_scope="", task_group_key_value="", task_context_key_value=""):
    """Return the evidence-backed relation between a record and the active scope.

    A session key is an identity boundary, not an unconditional cross-session
    sharing ban. Same-task, same-group, and matching bounded task-context keys
    may relate different sessions; otherwise a different session is excluded.
    Scoped reads also exclude legacy rows that do not carry the requested
    relation metadata.
    """
    record_session_key = str(record.get("codex_session_key") or record.get("session_key") or "")
    record_task_scope = str(record.get("task_scope_key") or "")
    record_project = record.get("project") if isinstance(record.get("project"), dict) else {}
    record_project_key = str(record.get("project_key") or record_project.get("key") or "")
    record_group_key = str(record.get("task_group_key") or "") or task_group_key(record_project_key, record.get("task_group", ""), record.get("task_name", ""))
    record_context_key = str(record.get("task_context_key") or "") or task_context_key(record_project_key, record.get("task_type", ""), record.get("module", ""), record.get("file", ""), record.get("symbol", ""), record.get("capability_fingerprint", ""), record.get("operation", ""), record.get("modality", ""), record.get("complexity_band") or record.get("complexity", ""), record.get("risk", ""), record.get("ambiguity", ""))
    active_session_key = str(session_key_value or "")
    active_task_scope = str(task_scope or "")
    active_group_key = str(task_group_key_value or "")
    active_context_key = str(task_context_key_value or "")
    if not (active_session_key or active_task_scope or active_group_key):
        return {"matched": True, "reason": "unscoped_query", "same_session": False}
    same_session = bool(active_session_key and record_session_key == active_session_key)
    same_task = bool(active_task_scope and record_task_scope == active_task_scope)
    same_group = bool(active_group_key and record_group_key == active_group_key)
    same_context = bool(active_context_key and record_context_key and record_context_key == active_context_key)
    if same_task:
        return {"matched": True, "reason": "same_session_task" if same_session else "related_task_scope", "same_session": same_session}
    if same_group:
        return {"matched": True, "reason": "same_session_task_group" if same_session else "related_task_group", "same_session": same_session}
    if same_context and not same_session:
        return {"matched": True, "reason": "related_task_context", "same_session": False}
    if same_session and not (active_task_scope or active_group_key):
        return {"matched": True, "reason": "same_session", "same_session": True}
    return {"matched": False, "reason": "unrelated_session", "same_session": same_session}


def scope_matches(record, *, session_key_value="", task_scope="", task_group_key_value="", task_context_key_value=""):
    return bool(scope_relation(record, session_key_value=session_key_value, task_scope=task_scope, task_group_key_value=task_group_key_value, task_context_key_value=task_context_key_value)["matched"])


def _prompt_hash(prompt):
    normalized = re.sub(r"\s+", " ", str(prompt or "")).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _task_tokens(text):
    tokens = []
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        token = TOKEN_ALIASES.get(token, token)
        if token in STOPWORDS or token in TASK_SIGNALS or len(token) < 2:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 3:
            token = token[:-1]
        tokens.append(token)
    return set(tokens)


def _failure_signals(text):
    tokens = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    signals = tokens & REPAIR_SIGNALS
    if re.search(r"\b(?:not|never|didn['’]?t|doesn['’]?t|isn['’]?t)\s+(?:solve|solved|work|working|change|changed|fix|fixed|remove|removed)\b", normalized):
        signals.add("explicit_failure")
    if re.search(r"\b(?:keep|keeps|kept)\s+(?:using|giving|making|showing)\b", normalized):
        signals.add("repeated_behavior")
    return sorted(signals)


def _topic_similarity(left, right):
    left_tokens = _task_tokens(left)
    right_tokens = _task_tokens(right)
    overlap = left_tokens & right_tokens
    if len(overlap) >= 2:
        return 1.0, overlap
    distinctive = overlap - GENERIC_TOPIC
    if distinctive:
        return 0.5, overlap
    if not left_tokens and not right_tokens:
        return 0.25, overlap
    return 0.0, overlap


def _looks_like_continuation(text):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return bool(re.search(r"\b(?:again|already|same|still|this|that|these|it|retry|continue|problem|issue)\b", normalized))


def _explicit_route_hint(text):
    match = ROUTE_HINT_PATTERN.search(str(text or ""))
    if not match:
        return ""
    model, effort = match.groups()
    return f"gpt-5.6-{model.lower()}|{EFFORT_ALIASES[effort.lower()]}"


def _classification_text(prompt, task_summary):
    values = []
    for value in (prompt, task_summary):
        normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
        if normalized and normalized not in values:
            values.append(normalized)
    return " ".join(values)


def _estimate_steps(text):
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    action_count = sum(word in STEP_ACTION_TERMS for word in words)
    action_count = max(1, action_count)
    explicit_counts = [int(value) for value in re.findall(r"\b(\d{1,2})\s*(?:steps?|stages?|phases?)\b", text.lower())]
    ordinal_count = len(re.findall(r"\b(?:first|second|third|fourth|finally|next)\b", text.lower()))
    connector_count = len(re.findall(r"\b(?:then|after that|also|plus|and|before|while|until)\b", text.lower()))
    information_hits = len(set(words) & INFORMATION_BURDEN_TERMS)
    estimate = action_count + max(0, ordinal_count - 1) + max(0, connector_count - 2) // 2
    estimate += min(3, max(0, information_hits - 2) // 2)
    if len(set(words) & LONG_TASK_TERMS) >= 2 or re.search(r"\b(?:multi[- ]file|many steps|step by step|first .* later|all .* files)\b", text.lower()):
        estimate += 2
    if re.search(r"\b(?:massive information|comprehensive|deep research|many sources|multiple sources)\b", text.lower()):
        estimate += 2
    if len(text) >= 500:
        estimate += min(3, len(text) // 350)
    return max(explicit_counts + [ordinal_count, estimate, 1])


def _effort_for_steps(step_estimate):
    if step_estimate <= 2:
        return "low"
    if step_estimate <= 4:
        return "medium"
    if step_estimate <= 6:
        return "high"
    if step_estimate <= 10:
        return "max"
    return "ultra"


def _step_class(step_estimate):
    if step_estimate <= 2:
        return "one_or_two_steps"
    if step_estimate <= 4:
        return "few_steps"
    if step_estimate <= 6:
        return "multi_step"
    if step_estimate <= 10:
        return "many_steps"
    return "extended_steps"


def classify_task(prompt, *, task_type="", operation="", modality="", complexity_score=None, task_summary=""):
    text = _classification_text(prompt, task_summary)
    tokens = set(re.findall(r"[a-z0-9]+", text))
    explicit_pair = _explicit_route_hint(text)
    operation_name = str(operation or "").lower()
    task_type_name = str(task_type or "").lower()
    core_task_types = {"analysis", "architecture", "code", "debug", "design", "development", "implementation", "integration", "migration", "planning", "research", "testing"}
    core_operations = {"edit", "fix", "implement", "modify", "repair", "transform", "update"}
    has_core_terms = bool(tokens & CORE_SOLVING_TERMS) or operation_name in core_operations or task_type_name in core_task_types
    has_image_terms = bool(tokens & IMAGE_INSPECTION_TERMS) or str(modality or "").lower() in {"image", "mixed"}
    explicit_core_operation = operation_name in core_operations
    image_only = has_image_terms and not explicit_core_operation and operation_name in {"", "check", "inspect", "read", "review", "verify", "view", "write"}
    solving_surface = "image_inspection" if image_only else "core_solving" if has_core_terms else "supporting"
    step_estimate = _estimate_steps(text)
    effort_class = _effort_for_steps(step_estimate)
    step_class = _step_class(step_estimate)
    task_length = "short" if step_estimate <= 3 else "medium" if step_estimate <= 6 else "long"
    information_hits = len(tokens & INFORMATION_BURDEN_TERMS)
    massive_information = bool(re.search(r"\b(?:massive information|comprehensive|all context|many sources|multiple sources|deep research)\b", text))
    information_burden = "massive" if massive_information or information_hits >= 5 else "high" if information_hits >= 3 else "medium" if information_hits else "low"
    score_difficult = isinstance(complexity_score, int) and complexity_score >= 50
    frontier_markers = len(tokens & FRONTIER_DIFFICULTY_TERMS)
    frontier = isinstance(complexity_score, int) and complexity_score >= 75 or information_burden == "massive" or frontier_markers >= 3
    difficult = frontier or score_difficult or bool(tokens & DIFFICULTY_TERMS)
    difficulty_class = "frontier" if frontier else "difficult" if difficult else "bounded"
    if solving_surface == "image_inspection":
        model_family = "gpt-5.6-luna"
        preferred_pair = f"{model_family}|{effort_class}"
        route_reason = "cheaper_image_inspection_route" if effort_class == "low" else f"cheaper_image_inspection_{effort_class}_route"
    elif frontier:
        model_family = "gpt-5.6-sol"
        preferred_pair = f"{model_family}|{effort_class}"
        route_reason = f"frontier_information_solving_{effort_class}_route"
    elif difficult:
        model_family = "gpt-5.6-terra"
        preferred_pair = f"{model_family}|{effort_class}"
        if solving_surface == "core_solving" and task_length == "short" and effort_class == "low":
            route_reason = "short_difficult_core_solving_light_route"
        elif solving_surface == "core_solving" and task_length == "long" and effort_class == "max":
            route_reason = "long_core_solving_max_route"
        else:
            route_reason = f"difficult_solving_{effort_class}_route"
    elif solving_surface == "core_solving":
        model_family = "gpt-5.6-terra"
        preferred_pair = f"{model_family}|{effort_class}"
        route_reason = f"bounded_core_solving_{effort_class}_route"
    else:
        model_family = "gpt-5.6-luna"
        preferred_pair = f"{model_family}|{effort_class}"
        route_reason = f"supporting_task_{effort_class}_route"
    route_class = f"{solving_surface}_{task_length}_{difficulty_class}"
    return {"solving_surface": solving_surface, "task_length": task_length, "step_estimate": step_estimate, "step_class": step_class, "estimated_effort": effort_class, "information_burden": information_burden, "model_family": model_family, "model_difficulty": difficulty_class, "difficulty_class": difficulty_class, "route_class": route_class, "preferred_solving_pair": preferred_pair, "route_reason": route_reason, "explicit_route_hint": explicit_pair}


def _extract_user_text(message):
    text = str(message or "")
    marker = "My request for Codex:"
    if marker in text:
        text = text.split(marker, 1)[1]
    text = re.sub(r"(?s)<image>.*?</image>", " ", text)
    text = re.sub(r"(?s)```.*?```", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _candidate_paths(session_id, sessions_root=None):
    normalized_id = session_id.lower()
    root = Path(sessions_root or (_codex_home() / "sessions")).expanduser().resolve()
    candidates = []
    if root.is_dir():
        for date_dir in root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
            candidates.extend(path for path in date_dir.glob(f"*{normalized_id}.jsonl") if path.is_file() and path.stem.lower().endswith(normalized_id))
    archive_root = root.parent / "archived_sessions" if root.name == "sessions" else root / "archived_sessions"
    if archive_root.is_dir():
        candidates.extend(path for path in archive_root.glob(f"*{normalized_id}.jsonl") if path.is_file() and path.stem.lower().endswith(normalized_id))
    return sorted(set(candidates))


def _read_rollout(path, session_id):
    current_session = None
    session_cwd = ""
    matched = False
    active_turn_key = ""
    users = []
    contexts = []
    try:
        handle = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    with handle:
        for raw_line in handle:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("type") == "session_meta":
                current_session = str(payload.get("session_id") or payload.get("id") or "").lower()
                matched = current_session == session_id.lower()
                session_cwd = str(payload.get("cwd") or "") if matched else session_cwd
            if not matched:
                continue
            if event.get("type") == "turn_context":
                model = payload.get("model")
                effort = payload.get("effort")
                turn_id = str(payload.get("turn_id") or payload.get("id") or "")
                active_turn_key = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24] if turn_id else ""
                if isinstance(model, str) and isinstance(effort, str):
                    contexts.append({"turn_key": active_turn_key, "pair": f"{model}|{effort}"})
            if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                text = _extract_user_text(payload.get("message"))
                if text:
                    users.append({"text": text, "turn_key": active_turn_key})
    if not matched:
        return None
    return {"cwd": session_cwd, "users": users, "contexts": contexts}


def _read_local_effort_records(local_store, session_key_value, project_key, task_type, module, session_task_scope=""):
    path = Path(local_store).expanduser().resolve()
    if not path.is_file():
        return []
    records = []
    try:
        handle = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    with handle:
        for raw_line in handle:
            try:
                envelope = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if envelope.get("local_model_memory_schema") != LOCAL_MEMORY_SCHEMA_VERSION or envelope.get("event") != "session-effort":
                continue
            record = envelope.get("record")
            if not isinstance(record, dict) or record.get("session_memory_schema") != SESSION_SCHEMA_VERSION:
                continue
            if record.get("session_key") != session_key_value or record.get("project_key") != project_key or record.get("task_type") != task_type or record.get("module") != module or record.get("session_task_scope_key") != session_task_scope:
                continue
            records.append(record)
    return records


def resolve_session_id(prompt="", explicit=None):
    if _valid_session_id(explicit):
        return explicit.strip().lower()
    matches = UUID_SEARCH_PATTERN.findall(str(prompt or "").lower())
    if matches:
        return matches[-1]
    for candidate in (os.environ.get("CODEX_THREAD_ID"), os.environ.get("CODEX_SESSION_ID")):
        if _valid_session_id(candidate):
            return candidate.strip().lower()
    return ""


def _quality_pairs(pairs):
    return [pair for pair in pairs if isinstance(pair, str) and PAIR_PATTERN.fullmatch(pair) and pair.split("|", 1)[0] in MODEL_ORDER]


def next_escalation_pair(current_pair, pairs):
    quality_pairs = _quality_pairs(pairs)
    if current_pair not in quality_pairs:
        return quality_pairs[0] if quality_pairs else None
    model, effort = current_pair.split("|", 1)
    model_index = MODEL_ORDER.index(model) if model in MODEL_ORDER else -1
    for candidate_model in MODEL_ORDER[model_index + 1:]:
        candidate = f"{candidate_model}|{effort}"
        if candidate in quality_pairs:
            return candidate
    effort_index = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else -1
    for candidate_effort in EFFORT_ORDER[effort_index + 1:]:
        candidate = f"{model}|{candidate_effort}"
        if candidate in quality_pairs:
            return candidate
    return None


def solve_route_pair(summary, previous_pair, pairs, quality_base_pair=None):
    observed_pairs = _quality_pairs(pairs)
    quality_pairs = list(dict.fromkeys([*observed_pairs, *KNOWN_QUALITY_PAIRS]))
    preferred_pair = summary.get("preferred_solving_pair") if isinstance(summary, dict) else ""
    if preferred_pair not in quality_pairs:
        preferred_pair = quality_base_pair if quality_base_pair in quality_pairs else quality_pairs[0] if quality_pairs else None
    if not preferred_pair:
        return {"pair": None, "reason": "no_quality_solving_pair", "frontier_reached": True}
    if not isinstance(previous_pair, str) or previous_pair not in quality_pairs:
        return {"pair": preferred_pair, "reason": summary.get("route_reason") or "session_task_class_route", "frontier_reached": False}
    previous_model, previous_effort = previous_pair.split("|", 1)
    preferred_model, preferred_effort = preferred_pair.split("|", 1)
    previous_model_index = MODEL_ORDER.index(previous_model) if previous_model in MODEL_ORDER else -1
    preferred_model_index = MODEL_ORDER.index(preferred_model) if preferred_model in MODEL_ORDER else -1
    if previous_pair == preferred_pair:
        for candidate_model in MODEL_ORDER[previous_model_index + 1:]:
            candidate = f"{candidate_model}|{preferred_effort}"
            if candidate in quality_pairs:
                return {"pair": candidate, "reason": "repeated_core_route_model_upgrade_same_task_class", "frontier_reached": False}
        return {"pair": previous_pair, "reason": "solving_route_frontier_reached", "frontier_reached": True}
    if previous_model_index < preferred_model_index or previous_model == preferred_model and EFFORT_ORDER.index(previous_effort) < EFFORT_ORDER.index(preferred_effort):
        return {"pair": preferred_pair, "reason": summary.get("route_reason") or "session_task_class_route", "frontier_reached": False}
    if previous_model == preferred_model and EFFORT_ORDER.index(previous_effort) > EFFORT_ORDER.index(preferred_effort) and summary.get("last_model_source") == "context":
        return {"pair": preferred_pair, "reason": "entry_to_task_class_solving_route", "frontier_reached": False}
    if previous_model_index > preferred_model_index or previous_model == preferred_model and EFFORT_ORDER.index(previous_effort) > EFFORT_ORDER.index(preferred_effort):
        return {"pair": previous_pair, "reason": "solving_route_frontier_reached", "frontier_reached": True}
    return {"pair": preferred_pair, "reason": summary.get("route_reason") or "session_task_class_route", "frontier_reached": False}


def _summary_defaults():
    return {"available": False, "session_key": "", "codex_session_key": "", "project_match": False, "state": "unavailable", "turn_count": 0, "prior_turn_count": 0, "same_task_turns": 0, "user_effort": 0, "failure_recorded": False, "last_model_pair": "", "last_model_source": "", "model_pairs": [], "task_name": "", "task_group": "", "task_scope_key": "", "task_group_key": "", "task_scope_mode": "unscoped", "session_task_scope_key": "", "task_topic_fingerprint": "", "current_turn_key": "", "current_prompt_sha256": "", "solving_surface": "", "task_length": "", "step_estimate": 0, "step_class": "", "estimated_effort": "", "information_burden": "", "model_family": "", "model_difficulty": "", "difficulty_class": "", "route_class": "", "preferred_solving_pair": "", "route_reason": "", "explicit_route_hint": "", "session_event_id": "", "session_event_status": "not_recorded"}


def assess_session(prompt, project_root, *, project_key="", task_type="", module="", capability_fingerprint="", operation="", modality="", complexity_score=None, task_summary="", task_name="", task_group="", session_id="", sessions_root=None, local_store=None):
    resolved_session_id = resolve_session_id(prompt, session_id)
    summary = _summary_defaults()
    explicit_task_name = str(task_name or os.environ.get("CODEX_TASK_NAME") or "").strip()
    explicit_task_group = str(task_group or os.environ.get("CODEX_TASK_GROUP") or "").strip()
    summary["task_name"] = normalize_task_name(explicit_task_name) if explicit_task_name else ""
    summary["task_group"] = normalize_task_name(explicit_task_group, "group") if explicit_task_group else ""
    stable_task_scope = task_scope_key(project_key, task_type, module, summary["task_name"] or module or task_type)
    summary["task_scope_key"] = stable_task_scope if explicit_task_name else ""
    summary["task_group_key"] = task_group_key(project_key, summary["task_group"], summary["task_name"])
    if not resolved_session_id:
        summary["task_scope_mode"] = "task+group" if summary["task_scope_key"] and summary["task_group_key"] else "task" if summary["task_scope_key"] else "group" if summary["task_group_key"] else "unscoped"
        return summary
    summary["session_key"] = session_key(resolved_session_id)
    summary["codex_session_key"] = summary["session_key"]
    summary["session_task_scope_key"] = session_task_scope_key(summary["session_key"], stable_task_scope)
    summary["task_scope_mode"] = "session+task+group" if summary["task_scope_key"] and summary["task_group_key"] else "session+task" if summary["task_scope_key"] else "session+group" if summary["task_group_key"] else "session"
    summary["current_prompt_sha256"] = _prompt_hash(prompt)
    candidates = _candidate_paths(resolved_session_id, sessions_root)
    rollout = _read_rollout(candidates[-1], resolved_session_id) if candidates else None
    if rollout is None:
        summary["state"] = "session_file_unavailable"
        return summary
    summary["available"] = True
    classification = classify_task(prompt, task_type=task_type, operation=operation, modality=modality, complexity_score=complexity_score, task_summary=task_summary)
    summary.update(classification)
    session_cwd = Path(rollout.get("cwd") or "").expanduser().resolve() if rollout.get("cwd") else None
    project_path = Path(project_root).expanduser().resolve()
    summary["project_match"] = bool(session_cwd and session_cwd == project_path)
    users = rollout.get("users", [])
    contexts = rollout.get("contexts", [])
    prompt_text = _extract_user_text(prompt)
    current_index = None
    current_turn_key = ""
    normalized_prompt = re.sub(r"\s+", " ", prompt_text).strip().lower()
    for index in range(len(users) - 1, -1, -1):
        if users[index]["text"].strip().lower() == normalized_prompt:
            current_index = index
            current_turn_key = users[index].get("turn_key", "")
            break
    prior_users = users[:current_index] if current_index is not None else users
    current_failure = _failure_signals(prompt_text)
    current_tokens = _task_tokens(prompt_text)
    explicit_task_scope = bool(summary.get("task_scope_key"))
    local_path = Path(local_store).expanduser().resolve() if local_store else Path(os.environ.get("CODEX_MODEL_ROUTING_MEMORY") or (Path.home() / ".codex" / "model-routing-memory" / "events.jsonl")).expanduser().resolve()
    local_records = _read_local_effort_records(local_path, summary["session_key"], project_key, task_type, module, summary["session_task_scope_key"])
    task_turn_keys = {str(record.get("turn_key") or "") for record in local_records if record.get("turn_key")}
    candidate_users = [user for user in prior_users if user.get("turn_key") in task_turn_keys] if explicit_task_scope or task_turn_keys else prior_users
    same_task = []
    overlaps = []
    for user in candidate_users:
        similarity, overlap = _topic_similarity(prompt_text, user.get("text", ""))
        if similarity > 0:
            same_task.append(user)
            overlaps.extend(overlap)
    if not same_task and _looks_like_continuation(prompt_text) and candidate_users and not current_tokens:
        same_task.append(candidate_users[-1])
    topic_tokens = sorted(current_tokens or _task_tokens(module) or _task_tokens(capability_fingerprint))
    topic_fingerprint = hashlib.sha256("|".join(topic_tokens).encode("utf-8")).hexdigest()[:24]
    context_pairs = []
    seen_turns = set()
    for context in contexts:
        if context.get("turn_key") and context.get("turn_key") == current_turn_key:
            continue
        if (explicit_task_scope or task_turn_keys) and context.get("turn_key") not in task_turn_keys:
            continue
        turn_key = context.get("turn_key") or f"pair:{context.get('pair')}"
        if turn_key in seen_turns:
            continue
        pair = context.get("pair")
        if isinstance(pair, str) and pair not in context_pairs:
            context_pairs.append(pair)
        seen_turns.add(turn_key)
    if current_turn_key:
        local_records = [record for record in local_records if record.get("turn_key") != current_turn_key]
    local_pairs = [record.get("selected_pair") for record in local_records if isinstance(record.get("selected_pair"), str) and record.get("selected_pair")]
    model_pairs = []
    for pair in context_pairs + local_pairs:
        if pair not in model_pairs:
            model_pairs.append(pair)
    last_model_pair = local_pairs[-1] if local_pairs else context_pairs[-1] if context_pairs else ""
    summary.update({"turn_count": len(users), "prior_turn_count": len(prior_users), "same_task_turns": len(same_task), "user_effort": len(same_task) + 1 if same_task else 1, "failure_recorded": bool(summary["project_match"] and same_task and current_failure), "last_model_pair": last_model_pair, "last_model_source": "local" if local_pairs else "context" if context_pairs else "", "model_pairs": model_pairs, "task_topic_fingerprint": topic_fingerprint, "current_turn_key": current_turn_key})
    if not summary["project_match"]:
        summary["state"] = "foreign_project"
    elif summary["failure_recorded"]:
        summary["state"] = "repeated_failure"
    elif same_task:
        summary["state"] = "same_task_continuation"
    else:
        summary["state"] = "solved_or_new_topic"
    return summary


def record_session_effort(summary, *, project_key, task_type, module, capability_fingerprint="", complexity_score=None, complexity_band="", selected_pair="", requested_pair="", local_store=None):
    if not isinstance(summary, dict) or not summary.get("available") or not summary.get("project_match") or not summary.get("session_key"):
        return {"status": "skipped", "written": False, "reason": "session_not_recordable"}
    current_prompt_sha256 = summary.get("current_prompt_sha256") or ""
    event_payload = "|".join(str(value or "") for value in (summary["session_key"], summary.get("session_task_scope_key"), project_key, task_type, module, current_prompt_sha256, summary.get("current_turn_key")))
    event_id = hashlib.sha256(event_payload.encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    record = {"session_memory_schema": SESSION_SCHEMA_VERSION, "event_id": event_id, "recorded_at": timestamp, "session_key": str(summary["session_key"]), "codex_session_key": str(summary.get("codex_session_key") or summary.get("session_key") or ""), "session_task_scope_key": str(summary.get("session_task_scope_key") or ""), "task_name": str(summary.get("task_name") or ""), "task_group": str(summary.get("task_group") or ""), "task_scope_key": str(summary.get("task_scope_key") or ""), "task_group_key": str(summary.get("task_group_key") or ""), "task_scope_mode": str(summary.get("task_scope_mode") or "session"), "turn_key": str(summary.get("current_turn_key") or ""), "project_key": str(project_key or ""), "task_type": str(task_type or ""), "module": str(module or ""), "capability_fingerprint": str(capability_fingerprint or ""), "complexity_score": complexity_score, "complexity_band": str(complexity_band or ""), "state": str(summary.get("state") or ""), "failure_recorded": bool(summary.get("failure_recorded")), "same_task_turns": int(summary.get("same_task_turns") or 0), "user_effort": int(summary.get("user_effort") or 0), "selected_pair": str(selected_pair or ""), "requested_pair": str(requested_pair or ""), "prior_model_pair": str(summary.get("last_model_pair") or ""), "model_pairs": [str(pair) for pair in summary.get("model_pairs", []) if isinstance(pair, str)], "task_topic_fingerprint": str(summary.get("task_topic_fingerprint") or ""), "solving_surface": str(summary.get("solving_surface") or ""), "task_length": str(summary.get("task_length") or ""), "step_estimate": int(summary.get("step_estimate") or 0), "step_class": str(summary.get("step_class") or ""), "estimated_effort": str(summary.get("estimated_effort") or ""), "information_burden": str(summary.get("information_burden") or ""), "model_family": str(summary.get("model_family") or ""), "model_difficulty": str(summary.get("model_difficulty") or ""), "difficulty_class": str(summary.get("difficulty_class") or ""), "route_class": str(summary.get("route_class") or ""), "preferred_solving_pair": str(summary.get("preferred_solving_pair") or ""), "route_reason": str(summary.get("route_reason") or ""), "explicit_route_hint": str(summary.get("explicit_route_hint") or "")}
    path = Path(local_store).expanduser().resolve() if local_store else Path(os.environ.get("CODEX_MODEL_ROUTING_MEMORY") or (Path.home() / ".codex" / "model-routing-memory" / "events.jsonl")).expanduser().resolve()
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        if os.name == "nt":
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if f'"event_id":"{event_id}"' in existing:
            status = "duplicate"
        else:
            envelope = {"local_model_memory_schema": LOCAL_MEMORY_SCHEMA_VERSION, "event": "session-effort", "event_id": event_id, "record": record}
            path.write_text(existing + json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            status = "written"
        if os.name == "nt":
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {"status": status, "written": status in {"written", "duplicate"}, "event_id": event_id, "path": str(path), "record": record}


def sanitize_summary(summary):
    if not isinstance(summary, dict):
        return _summary_defaults()
    clean = _summary_defaults()
    for key in clean:
        if key in summary:
            clean[key] = summary[key]
    clean["model_pairs"] = [str(pair) for pair in clean.get("model_pairs", []) if isinstance(pair, str)][:16]
    clean["failure_recorded"] = bool(clean.get("failure_recorded"))
    return clean
