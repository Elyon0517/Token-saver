#!/usr/bin/env python3
"""Dual local and Obsidian adaptive model-routing memory.

Supported platforms: Windows, macOS, and Linux.
"""

import argparse
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import mkstemp

if os.name == "nt":
    import msvcrt
else:
    import fcntl

try:
    import project_change_memory
except ModuleNotFoundError:
    import importlib.util

    _memory_path = Path(__file__).with_name("project_change_memory.py")
    _memory_spec = importlib.util.spec_from_file_location("project_change_memory", _memory_path)
    project_change_memory = importlib.util.module_from_spec(_memory_spec)
    _memory_spec.loader.exec_module(project_change_memory)

try:
    import model_registry
except ModuleNotFoundError:
    import importlib.util

    _registry_path = Path(__file__).resolve().parent / "model_registry.py"
    _registry_spec = importlib.util.spec_from_file_location("model_registry", _registry_path)
    model_registry = importlib.util.module_from_spec(_registry_spec)
    _registry_spec.loader.exec_module(model_registry)

try:
    import session_effort
except ModuleNotFoundError:
    import importlib.util

    _session_effort_path = Path(__file__).resolve().parent / "session_effort.py"
    _session_effort_spec = importlib.util.spec_from_file_location("session_effort", _session_effort_path)
    session_effort = importlib.util.module_from_spec(_session_effort_spec)
    _session_effort_spec.loader.exec_module(session_effort)

try:
    import task_signals as _task_signals
except ModuleNotFoundError:
    import importlib.util

    _task_signals_path = Path(__file__).resolve().parent / "task_signals.py"
    _task_signals_spec = importlib.util.spec_from_file_location("task_signals", _task_signals_path)
    _task_signals = importlib.util.module_from_spec(_task_signals_spec)
    _task_signals_spec.loader.exec_module(_task_signals)


SCHEMA_VERSION = 1
LOCAL_MEMORY_SCHEMA_VERSION = 1
MIN_REAL_PASSES_BEFORE_DOWNGRADE = 2
DEFAULT_VAULT = project_change_memory.DEFAULT_VAULT
# Same override as routing_policy: tests resolve a frozen fixture, runtime resolves the
# machine-specific ladder that sync_model_capabilities.py --update maintains.
DEFAULT_LADDER = Path(os.environ.get("TOKEN_SAVER_MODEL_LADDER") or (Path(__file__).resolve().parents[1] / "assets" / "model-capability-ladder.json")).expanduser()
DEFAULT_LOCAL_STORE = Path.home() / ".codex" / "model-routing-memory" / "events.jsonl"
QUALITY_FAILURES = {"quality", "correctness"}
OPERATIONAL_FAILURES = {"availability", "timeout", "protocol", "telemetry", "execution", "receipt"}
FAILURE_CLASSES = {"none"} | QUALITY_FAILURES | OPERATIONAL_FAILURES
LEVEL_VALUES = {"low", "medium", "high"}
COMPLEXITY_VALUES = {"easy", "complex"}
MODALITY_VALUES = {"text", "mixed", "image"}
COMPLEXITY_BANDS = _task_signals.COMPLEXITY_BANDS
STEP_KINDS = {
    "analysis",
    "debugging",
    "documentation",
    "image-generation-control",
    "implementation",
    "integration",
    "local-test",
    "tool-execution",
    "verification",
    "visual-verification",
    "general",
}
STRUCTURAL_CAPABILITY_PREFIXES = ("task-", "code-", "operation-", "modality-")
DISTINCTIVE_CAPABILITY_TAGS = {
    "api-integration",
    "browser-automation",
    "data-analysis",
    "file-transformation",
    "image-generation",
    "local-test",
    "prompt-engineering",
    "tool-control",
    "unity-workflow",
    "visual-verification",
}
MODEL_SWITCH_CATEGORIES = (
    "normal-script-update",
    "code-design",
    "finding-bugs",
    "tests-verification",
    "documentation-instructions",
    "general-work",
)
MODEL_SWITCH_DIRECTIONS = (
    "initial",
    "upgrade",
    "downgrade",
    "freeze",
    "no_switch",
    "operational_fallback",
)
MODEL_SWITCH_CATEGORY_MARKER = "<!-- generated:model-switch-category -->"

MODEL_SWITCH_CATEGORY_TITLES = {
    "normal-script-update": "Normal Script Update",
    "code-design": "Code Design",
    "finding-bugs": "Finding Bugs",
    "documentation-instructions": "Documentation and Instructions",
    "tests-verification": "Tests and Verification",
    "general-work": "General Work",
}

FRONTMATTER_FIELDS = (
    "model_experience_schema",
    "record_id",
    "event_id",
    "recorded_at",
    "project_name",
    "project_key",
    "project_owner",
    "task_type",
    "task_name",
    "task_group",
    "task_scope_key",
    "task_group_key",
    "task_summary",
    "module",
    "file",
    "symbol",
    "code_kind",
    "operation",
    "modality",
    "complexity",
    "complexity_score",
    "complexity_band",
    "risk",
    "ambiguity",
    "step_kind",
    "capability_tags",
    "capability_fingerprint",
    "entry_model",
    "entry_effort",
    "entry_pair",
    "entry_anchor_pair",
    "entry_source",
    "model",
    "effort",
    "pair",
    "selected_pair",
    "prior_pair",
    "attempt_pair",
    "active_fallback_pair",
    "operational_failure_pairs",
    "real_status",
    "failure_class",
    "outcome_reason",
    "verification_count",
    "model_evidence",
    "learning_eligible",
    "observation_id",
    "receipt_status",
    "model_match",
    "effort_match",
    "turn_completed",
    "trial",
    "selection_reason",
    "recommendation_state",
    "specificity",
    "matched_records",
    "success_pair",
    "failed_pair",
    "workload_prompt_sha256",
    "total_tokens",
    "process_ms",
    "receipt_sha256",
    "switch_direction",
    "switch_reason",
    "next_pair",
    "completed_pair",
    "recovery_from_pair",
    "attempt_chain",
    "ending_attempt_number",
    "ending_pass_shape",
    "prior_quality_failure_count",
    "prior_operational_failure_count",
    "matched_pass_count_after",
    "minimum_passes_before_downgrade",
    "next_pair_reason",
    "next_pair_direction",
    "model_suitability",
    "routing_action",
    "session_key",
    "session_task_scope_key",
    "codex_session_key",
    "task_scope_mode",
    "session_state",
    "session_turn_count",
    "session_prior_turn_count",
    "session_same_task_turns",
    "session_user_effort",
    "session_failure_recorded",
    "session_model_pair",
    "session_model_source",
    "session_model_pairs",
    "session_task_fingerprint",
    "session_project_match",
    "session_solving_surface",
    "session_task_length",
    "session_step_estimate",
    "session_step_class",
    "session_estimated_effort",
    "session_information_burden",
    "session_model_family",
    "session_model_difficulty",
    "session_difficulty_class",
    "session_route_class",
    "session_preferred_pair",
    "session_route_reason",
    "session_explicit_route_hint",
    "session_escalation_pair",
    "session_escalation_reason",
    "session_event_id",
)


def _json_safe(value):
    if isinstance(value, Path):
        return value.expanduser().resolve().as_posix()
    if isinstance(value, dict):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(nested) for nested in value]
    return value


def _single_line(value, field, *, required=True, maximum=280):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _slug(value, field):
    text = _single_line(value, field, maximum=80).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", text):
        raise ValueError(f"{field} must be a lowercase slug")
    return text


def _optional_relative_file(project_root, value):
    if not value:
        return ""
    root = Path(project_root).expanduser().resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        relative = candidate.resolve().relative_to(root)
    else:
        relative = PurePosixPath(candidate.as_posix())
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("file must be project-relative and inside project_root")
    return relative.as_posix()


def load_shared_ladder(path=DEFAULT_LADDER):
    try:
        resolved = Path(path).expanduser().resolve()
        if resolved.exists():
            payload = model_registry.load_registry(resolved)
        elif resolved == DEFAULT_LADDER.expanduser().resolve():
            payload = model_registry.ensure_registry(resolved)["registry"]
        else:
            payload = model_registry.load_registry(resolved)
        model_registry.validate_registry(payload)
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"shared model ladder is unreadable: {error}") from error
    pairs = []
    for row in payload["models"]:
        efforts = row["codex_efforts"]
        pairs.extend(f"{row['id']}|{effort}" for effort in efforts)
    return payload, pairs


complexity_band = _task_signals.complexity_band


def _legacy_complexity_score(complexity):
    return 65 if complexity == "complex" else 35


def _record_complexity_band(record):
    stored_band = record.get("complexity_band")
    if stored_band in {name for name, _, _ in COMPLEXITY_BANDS}:
        return stored_band
    stored_score = record.get("complexity_score")
    if isinstance(stored_score, int) and not isinstance(stored_score, bool) and 0 <= stored_score <= 100:
        return complexity_band(stored_score)
    return "complex" if record.get("complexity") == "complex" else "standard"


def _contains_capability_term(text, *terms):
    for term in terms:
        if any("\u4e00" <= character <= "\u9fff" for character in term):
            if term in text:
                return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            return True
    return False


def _normalize_explicit_capability_tags(capability_tags):
    if capability_tags is None:
        return []
    if isinstance(capability_tags, str):
        values = re.split(r"[,\s]+", capability_tags)
    elif isinstance(capability_tags, (list, tuple, set)):
        values = list(capability_tags)
    else:
        raise ValueError("capability_tags must be a string or a list of slugs")
    normalized = []
    for value in values:
        if not str(value or "").strip():
            continue
        tag = _slug(value, "capability_tag")
        if tag not in normalized:
            normalized.append(tag)
    if len(normalized) > 16:
        raise ValueError("capability_tags may contain at most 16 tags")
    return normalized


def task_capability_profile(task_type, code_kind="general", operation="work", modality="text", complexity_score=35, risk="low", ambiguity="low", task_summary="", step_kind="", capability_tags=None):
    """Build a sanitized, deterministic step-capability identity.

    Explicit tags are authoritative. Without them, a bounded bilingual keyword
    vocabulary derives only durable capability categories; raw task text is
    never persisted in the fingerprint.
    """
    normalized_task_type = _slug(task_type, "task_type")
    normalized_code_kind = _slug(code_kind, "code_kind")
    normalized_operation = _slug(operation, "operation")
    if modality not in MODALITY_VALUES or risk not in LEVEL_VALUES or ambiguity not in LEVEL_VALUES:
        raise ValueError("modality, risk, or ambiguity is invalid")
    band = complexity_band(complexity_score)
    summary = _single_line(task_summary, "task_summary", required=False).lower()
    explicit_tags = _normalize_explicit_capability_tags(capability_tags)
    tags = {
        f"task-{normalized_task_type}",
        f"code-{normalized_code_kind}",
        f"operation-{normalized_operation}",
        f"modality-{modality}",
    }
    if normalized_task_type in {"code", "script", "normal-script-update"} and normalized_operation in {"edit", "fix", "implement", "modify", "replace", "update", "work", "write"}:
        tags.add("code-authoring")
    if normalized_task_type in {"debug", "finding-bugs"} or normalized_operation in {"debug", "diagnose", "fix", "repair"}:
        tags.add("debugging")
    if normalized_operation == "test":
        tags.add("local-test")
    elif normalized_operation in {"audit", "review", "verify", "validation"}:
        tags.add("verification")
    if normalized_operation in {"execute", "command"}:
        tags.add("command-execution")
    if explicit_tags:
        tags.update(explicit_tags)
    else:
        if normalized_task_type in {"debug", "finding-bugs"} or normalized_operation in {"debug", "diagnose", "fix", "repair"} or _contains_capability_term(summary, "debug", "diagnose", "bug", "调试", "排错", "修复"):
            tags.add("debugging")
        if normalized_operation == "test" or _contains_capability_term(summary, "local test", "unit test", "pytest", "unittest", "regression test", "本地测试", "单元测试", "回归测试"):
            tags.add("local-test")
        elif normalized_operation in {"audit", "review", "verify", "validation"} or _contains_capability_term(summary, "verify", "validation", "audit", "review", "验证", "复核", "审计"):
            tags.add("verification")
        if _contains_capability_term(summary, "image generation", "image generator", "generate image", "generate images", "generates image", "generates images", "generated image", "generated images", "imagegen", "text to image", "图片生成", "生成图片", "生成图像", "生图"):
            tags.add("image-generation")
        if _contains_capability_term(summary, "control", "controller", "automate", "automation", "invoke", "drive", "控制", "调用", "驱动", "自动化"):
            tags.add("tool-control")
        if _contains_capability_term(summary, "visual inspection", "visual compare", "rendered output", "pixel", "视觉检查", "视觉对比", "渲染结果"):
            tags.add("visual-verification")
        if _contains_capability_term(summary, "browser", "playwright", "chrome", "浏览器"):
            tags.add("browser-automation")
        if _contains_capability_term(summary, "api", "endpoint", "webhook", "接口"):
            tags.add("api-integration")
        if _contains_capability_term(summary, "unity", "game engine", "游戏引擎"):
            tags.add("unity-workflow")
        if normalized_task_type in {"prompt", "documentation-instructions"} or _contains_capability_term(summary, "prompt", "提示词"):
            tags.add("prompt-engineering")
        if _contains_capability_term(summary, "data analysis", "analytics", "dataset", "数据分析", "数据集"):
            tags.add("data-analysis")
        if _contains_capability_term(summary, "convert file", "transform file", "export file", "文件转换", "导出文件"):
            tags.add("file-transformation")
        if normalized_operation in {"execute", "command"} or _contains_capability_term(summary, "run command", "execute command", "运行命令", "执行命令"):
            tags.add("command-execution")
    if step_kind:
        normalized_step_kind = _slug(step_kind, "step_kind")
        if normalized_step_kind not in STEP_KINDS:
            raise ValueError(f"step_kind must be one of {', '.join(sorted(STEP_KINDS))}")
    elif {"image-generation", "tool-control"}.issubset(tags):
        normalized_step_kind = "image-generation-control"
    elif "local-test" in tags:
        normalized_step_kind = "local-test"
    elif "visual-verification" in tags:
        normalized_step_kind = "visual-verification"
    elif "debugging" in tags:
        normalized_step_kind = "debugging"
    elif normalized_task_type == "integration" or normalized_operation == "integration":
        normalized_step_kind = "integration"
    elif "code-authoring" in tags:
        normalized_step_kind = "implementation"
    elif "command-execution" in tags or "tool-control" in tags:
        normalized_step_kind = "tool-execution"
    elif "verification" in tags:
        normalized_step_kind = "verification"
    elif normalized_task_type in {"document", "documentation-instructions", "prompt"}:
        normalized_step_kind = "documentation"
    elif normalized_task_type in {"question", "summary", "analysis"}:
        normalized_step_kind = "analysis"
    else:
        normalized_step_kind = "general"
    ordered_tags = sorted(tags)
    payload = {
        "task_type": normalized_task_type,
        "code_kind": normalized_code_kind,
        "operation": normalized_operation,
        "modality": modality,
        "complexity_band": band,
        "risk": risk,
        "ambiguity": ambiguity,
        "step_kind": normalized_step_kind,
        "capability_tags": ordered_tags,
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    semantic_tags = [tag for tag in ordered_tags if not tag.startswith(STRUCTURAL_CAPABILITY_PREFIXES)]
    return {"step_kind": normalized_step_kind, "capability_tags": ordered_tags, "semantic_capability_tags": semantic_tags, "capability_fingerprint": fingerprint}


def _record_capability_profile(record):
    stored_tags = record.get("capability_tags")
    return task_capability_profile(
        record.get("task_type") or "question",
        record.get("code_kind") or "general",
        record.get("operation") or "work",
        record.get("modality") or "text",
        _legacy_complexity_score(record.get("complexity") or "easy") if not isinstance(record.get("complexity_score"), int) else record["complexity_score"],
        record.get("risk") or "low",
        record.get("ambiguity") or "low",
        record.get("task_summary") or "",
        record.get("step_kind") or "",
        stored_tags if isinstance(stored_tags, list) else None,
    )


def _query(project_root, task_type, module, file_value="", symbol="", code_kind="general", operation="work", modality="text", complexity="easy", complexity_score=None, risk="low", ambiguity="low", task_summary="", step_kind="", capability_tags=None, task_name="", task_group=""):
    project = project_change_memory._project_identity(project_root)
    if modality not in MODALITY_VALUES or complexity not in COMPLEXITY_VALUES or risk not in LEVEL_VALUES or ambiguity not in LEVEL_VALUES:
        raise ValueError("modality, complexity, risk, or ambiguity is invalid")
    score = _legacy_complexity_score(complexity) if complexity_score is None else complexity_score
    band = complexity_band(score)
    derived_complexity = "complex" if score >= 50 else "easy"
    capability = task_capability_profile(task_type, code_kind, operation, modality, score, risk, ambiguity, task_summary, step_kind, capability_tags)
    normalized_task_name = session_effort.normalize_task_name(task_name) if str(task_name or "").strip() else ""
    normalized_task_group = session_effort.normalize_task_name(task_group, "group") if str(task_group or "").strip() else ""
    group_key = session_effort.task_group_key(project["key"], normalized_task_group, normalized_task_name)
    return {
        "project": project,
        "task_type": _slug(task_type, "task_type"),
        "task_name": normalized_task_name,
        "task_group": normalized_task_group,
        "task_scope_key": session_effort.task_scope_key(project["key"], _slug(task_type, "task_type"), _single_line(module, "module", maximum=160), normalized_task_name) if normalized_task_name else "",
        "task_group_key": group_key,
        "task_scope_enforced": bool(str(task_name or "").strip() or str(task_group or "").strip()),
        "task_summary": _single_line(task_summary, "task_summary", required=False),
        "module": _single_line(module, "module", maximum=160),
        "file": _optional_relative_file(project["root"], file_value),
        "symbol": _single_line(symbol, "symbol", required=False, maximum=180),
        "code_kind": _slug(code_kind, "code_kind"),
        "operation": _slug(operation, "operation"),
        "modality": modality,
        "complexity": derived_complexity,
        "complexity_score": score,
        "complexity_band": band,
        "risk": risk,
        "ambiguity": ambiguity,
        **capability,
    }


def _project_switch_directory(vault_path, owner):
    if owner == "Global Codex Skills":
        return vault_path / "Skills"
    return vault_path / "Projects" / owner


def _memory_root_owner(vault_path, owner):
    if owner is None:
        return None
    return _project_switch_directory(vault_path, owner) / "Model Switch.md"


def _is_configured_owner(vault_path, owner):
    if vault_path is None or owner is None:
        return False
    return _project_switch_directory(vault_path, owner).is_dir()


def _memory_root(query, vault):
    vault_path = project_change_memory._resolve_vault(vault)
    if vault_path is None:
        return None, None
    owner = query["project"].get("owner") or project_change_memory._registered_owner(query["project"]["root"])
    if owner is None:
        return vault_path, None
    return vault_path, _memory_root_owner(vault_path, owner)


def model_switch_reference(project_root, *, vault=None):
    project = project_change_memory._project_identity(project_root)
    vault_path = project_change_memory._resolve_vault(vault)
    if vault_path is None:
        return {"status": "unavailable", "owner": project.get("owner") or "", "document": "", "link": ""}
    owner = project.get("owner") or project_change_memory._registered_owner(project["root"])
    if owner is None:
        return {"status": "unregistered", "owner": "", "document": "", "link": ""}
    memory_root = _memory_root_owner(vault_path, owner)
    if memory_root is None:
        return {"status": "unavailable", "owner": owner, "document": "", "link": ""}
    document = _vault_relative_path(vault_path, memory_root).as_posix()
    link_target = document[:-3] if document.endswith(".md") else document
    return {"status": "ready" if memory_root.exists() else "pending", "owner": owner, "document": document, "link": f"[[{link_target}]]"}


def _project_switch_index(vault_path, owner):
    return _project_switch_directory(vault_path, owner) / "index.md"


def _ensure_model_switch_index_link(vault_path, owner, memory_root):
    index_path = _project_switch_index(vault_path, owner)
    if not index_path.exists():
        return
    target = _vault_relative_path(vault_path, memory_root).as_posix().removesuffix(".md")
    canonical = f"- [[{target}|Model Switch]] — Compact entry to native linked model-routing categories."
    text = index_path.read_text(encoding="utf-8")
    output = []
    found = False
    for line in text.splitlines():
        targets = [match.split("|", 1)[0].strip().removesuffix(".md") for match in re.findall(r"\[\[([^\]]+)\]\]", line)]
        if target in targets:
            if not found:
                output.append(canonical)
                found = True
            continue
        output.append(line)
    if not found:
        output.append(canonical)
    _atomic_write(index_path, "\n".join(output).rstrip() + "\n")


def _parse_frontmatter(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return None
    block = text.split("\n---\n", 1)[0][4:]
    record = {}
    for line in block.splitlines():
        if ": " not in line:
            return None
        key, raw = line.split(": ", 1)
        try:
            record[key] = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if record.get("model_experience_schema") != SCHEMA_VERSION:
        return None
    return record


def _read_project_records(memory_root):
    if memory_root is None or not memory_root.exists():
        return []
    if memory_root.is_file():
        records = []
        for raw in re.findall(r"<!-- model-experience: (.*?) -->", memory_root.read_text(encoding="utf-8"), flags=re.DOTALL):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("model_experience_schema") == SCHEMA_VERSION:
                records.append(_json_safe(record))
        routing_directory = memory_root.parent / ("Model Routing Records" if memory_root.parent.name == "Skills" else "Model Routing")
        if routing_directory.exists():
            for page in routing_directory.glob("*.md"):
                records.extend(_read_embedded_records(page))
        return _merge_model_records(records, [])
    records = []
    for path in sorted((memory_root / "records").glob("*/*/*.md")):
        record = _parse_frontmatter(path)
        if record is not None:
            records.append(_json_safe(record))
    return records


def _record_scope_relation(record, query):
    if not query.get("scope_enforced"):
        return {"matched": True, "reason": "unscoped_query", "same_session": False}
    return session_effort.scope_relation(record, session_key_value=query.get("codex_session_key", ""), task_scope=query.get("task_scope_key", ""), task_group_key_value=query.get("task_group_key", ""))


def _record_in_query_scope(record, query):
    return bool(_record_scope_relation(record, query)["matched"])


def _scope_score(record, query):
    if record.get("task_type") != query["task_type"]:
        return None
    if not _record_in_query_scope(record, query):
        return None
    try:
        record_capability = _record_capability_profile(record)
    except ValueError:
        return None
    if record_capability["capability_fingerprint"] != query["capability_fingerprint"]:
        return None
    module_match = record.get("module") == query["module"]
    distinctive = bool(set(query["semantic_capability_tags"]) & DISTINCTIVE_CAPABILITY_TAGS)
    if not module_match and not distinctive:
        return None
    file_match = bool(query["file"] and module_match and record.get("file") == query["file"])
    symbol_match = bool(query["symbol"] and file_match and record.get("symbol") == query["symbol"])
    if symbol_match:
        scope_level = 4
    elif file_match:
        scope_level = 3
    elif module_match:
        scope_level = 2
    else:
        scope_level = 1
    score = scope_level * 1_000
    context_weights = {
        "code_kind": 16,
        "operation": 32,
        "modality": 64,
        "risk": 4,
        "ambiguity": 2,
    }
    for field, weight in context_weights.items():
        if query[field] and record.get(field) == query[field]:
            score += weight
    if _record_complexity_band(record) == query["complexity_band"]:
        score += 128
    return scope_level, score


def _best_scope_records(records, query):
    scored = [(*scope_score, record) for record in records if (scope_score := _scope_score(record, query)) is not None]
    if not scored:
        return [], "project_task", 0
    best_scope = max(scope for scope, _, _ in scored)
    scoped = [(score, record) for scope, score, record in scored if scope == best_scope]
    best_score = max(score for score, _ in scoped)
    selected = [record for score, record in scoped if score == best_score]
    level = {1: "project_task", 2: "module", 3: "file", 4: "symbol"}[best_scope]
    return selected, level, best_score


def _transferable_local_records(records, query):
    """Reuse only an exact step-capability identity across project roots."""
    required_fields = ("task_type", "code_kind", "operation", "modality", "risk", "ambiguity")
    distinctive = bool(set(query["semantic_capability_tags"]) & DISTINCTIVE_CAPABILITY_TAGS)
    transferable = []
    for record in records:
        if project_change_memory._record_matches_project(record, query["project"]):
            continue
        if not _record_in_query_scope(record, query):
            continue
        if any(record.get(field) != query[field] for field in required_fields):
            continue
        try:
            record_capability = _record_capability_profile(record)
        except ValueError:
            continue
        if record_capability["capability_fingerprint"] != query["capability_fingerprint"]:
            continue
        if record.get("module") != query["module"] and not distinctive:
            continue
        if _record_complexity_band(record) != query["complexity_band"]:
            continue
        if _quality_verdict(record) is None:
            continue
        transferable.append(record)
    return transferable


def _entry_context(shared, pairs, entry_model="", entry_effort=""):
    model = _single_line(entry_model, "entry_model", required=False, maximum=120)
    effort = _single_line(entry_effort, "entry_effort", required=False, maximum=40)
    if bool(model) != bool(effort):
        raise ValueError("entry_model and entry_effort must be provided together")
    if not model:
        return {"entry_model": None, "entry_effort": None, "entry_pair": None, "entry_anchor_pair": None, "entry_source": "unavailable"}
    entry_pair = f"{model}|{effort}"
    if entry_pair in pairs:
        return {"entry_model": model, "entry_effort": effort, "entry_pair": entry_pair, "entry_anchor_pair": entry_pair, "entry_source": "active_quality_pair"}
    model_rows = {row["id"]: row for row in shared.get("models", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
    if model in model_rows:
        supported = [candidate for candidate in pairs if candidate.startswith(f"{model}|")]
        effort_order = shared.get("effort_order", [])
        requested_index = effort_order.index(effort) if effort in effort_order else -1
        eligible = [candidate for candidate in supported if candidate.split("|", 1)[1] in effort_order and effort_order.index(candidate.split("|", 1)[1]) <= requested_index]
        anchor_pair = eligible[-1] if eligible else supported[0] if supported else None
        return {"entry_model": model, "entry_effort": effort, "entry_pair": entry_pair, "entry_anchor_pair": anchor_pair, "entry_source": "normalized_active_quality_pair" if anchor_pair else "unranked"}
    catalog_row = next((row for row in shared.get("catalog_models", []) if isinstance(row, dict) and row.get("id") == model), None)
    priority = shared.get("priority_producer")
    if catalog_row is not None or isinstance(priority, dict) and priority.get("id") == model:
        return {"entry_model": model, "entry_effort": effort, "entry_pair": entry_pair, "entry_anchor_pair": pairs[0], "entry_source": "catalog_lower_anchor"}
    return {"entry_model": model, "entry_effort": effort, "entry_pair": entry_pair, "entry_anchor_pair": None, "entry_source": "unranked"}


def _static_cold_start(shared, query, pairs):
    levels = shared.get("cold_start_defaults", {}).get(query["task_type"], {})
    pair = levels.get(query["complexity"], shared["default_cold_start"])
    return pair if pair in pairs else shared["default_cold_start"]


def _cold_start(shared, query, pairs, entry_anchor_pair=None):
    static_pair = _static_cold_start(shared, query, pairs)
    if entry_anchor_pair not in pairs:
        return static_pair, static_pair
    return pairs[min(pairs.index(static_pair), pairs.index(entry_anchor_pair))], static_pair


def _quality_verdict(record):
    valid_receipt = record.get("receipt_status") == "pass" and record.get("turn_completed") is True and record.get("model_match") is True and record.get("effort_match") is True
    if not valid_receipt:
        return None
    if record.get("real_status") == "fail" and record.get("failure_class") in QUALITY_FAILURES:
        return "fail"
    if record.get("real_status") == "pass" and record.get("failure_class") == "none":
        return "pass"
    return None


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _cost_evidence(status, pairs, *, shared_hashes=None, scores=None):
    hashes = sorted(shared_hashes or [])
    digest = hashlib.sha256("\n".join(hashes).encode()).hexdigest() if hashes else None
    return {
        "status": status,
        "compared_pairs": list(pairs),
        "shared_cohort_count": len(hashes),
        "shared_cohort_digest": digest,
        "scores": {
            pair: {"median_total_tokens": score[0], "median_process_ms": score[1]}
            for pair, score in (scores or {}).items()
        },
    }


def _like_for_like_cost_scores(records, passing_pairs):
    candidates = list(passing_pairs)
    if len(candidates) < 2:
        return None, _cost_evidence("insufficient_pairs", candidates)
    grouped = {pair: {} for pair in candidates}
    for record in records:
        pair = record.get("pair")
        workload_hash = record.get("workload_prompt_sha256")
        if pair not in grouped or _quality_verdict(record) != "pass" or not isinstance(workload_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", workload_hash):
            continue
        total_tokens = record.get("total_tokens")
        process_ms = record.get("process_ms")
        grouped[pair].setdefault(workload_hash, []).append((total_tokens, process_ms))
    common_hashes = set(grouped[candidates[0]])
    for pair in candidates[1:]:
        common_hashes.intersection_update(grouped[pair])
    if not common_hashes:
        return None, _cost_evidence("no_common_workload", candidates)
    cohort_scores = {pair: [] for pair in candidates}
    for workload_hash in sorted(common_hashes):
        for pair in candidates:
            samples = grouped[pair][workload_hash]
            if any(
                isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
                or isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0
                for tokens, elapsed in samples
            ):
                return None, _cost_evidence("incomplete_metrics", candidates, shared_hashes=common_hashes)
            cohort_scores[pair].append(
                (
                    _median([tokens for tokens, _ in samples]),
                    _median([elapsed for _, elapsed in samples]),
                )
            )
    scores = {
        pair: (
            _median([sample[0] for sample in samples]),
            _median([sample[1] for sample in samples]),
        )
        for pair, samples in cohort_scores.items()
    }
    return scores, _cost_evidence("like_for_like", candidates, shared_hashes=common_hashes, scores=scores)


def _active_recommendation(shared, pairs, query, records, entry_anchor_pair=None):
    verdicts = {}
    pass_counts = {pair: 0 for pair in pairs}
    quality_samples = 0
    for record in records:
        pair = record.get("pair")
        if pair not in pairs:
            continue
        verdict = _quality_verdict(record)
        if verdict is None:
            continue
        quality_samples += 1
        if verdict == "fail":
            verdicts[pair] = "fail"
        elif verdicts.get(pair) != "fail":
            verdicts[pair] = "pass"
            pass_counts[pair] += 1
    failed_pairs = [pair for pair, verdict in verdicts.items() if verdict == "fail"]
    failed_pair = max(failed_pairs, key=pairs.index) if failed_pairs else None
    passing_pairs = [pair for pair, verdict in verdicts.items() if verdict == "pass" and (failed_pair is None or pairs.index(pair) > pairs.index(failed_pair))]
    success_pair = min(passing_pairs, key=pairs.index) if passing_pairs else None
    _, cost_evidence = _like_for_like_cost_scores(records, passing_pairs)
    selected_pair = None
    trial = False
    state = "cold_start"
    reason = "shared_cold_start"
    static_cold_start = _static_cold_start(shared, query, pairs)
    if failed_pair is None and success_pair is None:
        selected_pair, static_cold_start = _cold_start(shared, query, pairs, entry_anchor_pair)
        if entry_anchor_pair in pairs and selected_pair == entry_anchor_pair and entry_anchor_pair != static_cold_start:
            reason = "entry_anchored_cold_start"
    elif failed_pair is None:
        success_index = pairs.index(success_pair)
        if success_index == 0:
            selected_pair = success_pair
            state = "frozen"
            reason = "verified_floor_retained"
        elif pass_counts[success_pair] < MIN_REAL_PASSES_BEFORE_DOWNGRADE:
            selected_pair = success_pair
            state = "collecting_evidence"
            reason = "real_pass_collecting_evidence"
        else:
            selected_pair = pairs[success_index - 1]
            trial = True
            state = "provisional"
            reason = "repeated_real_pass_one_rung_down"
    elif success_pair is None:
        failed_index = pairs.index(failed_pair)
        if failed_index + 1 < len(pairs):
            selected_pair = pairs[failed_index + 1]
            trial = True
            state = "quality_boundary"
            reason = "quality_failure_one_rung_up"
        else:
            state = "blocked"
            reason = "quality_boundary_exhausted"
    else:
        # A verified recovery pair is the durable boundary for the next highly
        # similar step. Do not immediately retry an untested gap and recreate a
        # failure the user already paid to recover from.
        selected_pair = success_pair
        state = "frozen"
        reason = "verified_quality_boundary"
    return {
        "selected_pair": selected_pair,
        "trial": trial,
        "reason": reason,
        "calibration_state": state,
        "success_model": success_pair,
        "failed_model": failed_pair,
        "quality_samples": quality_samples,
        "pass_counts": {pair: count for pair, count in pass_counts.items() if count},
        "minimum_passes_before_downgrade": MIN_REAL_PASSES_BEFORE_DOWNGRADE,
        "cost_evidence": cost_evidence,
        "static_cold_start": static_cold_start,
        "entry_route_reason": "history_verified_selection" if quality_samples else "entry_anchor_selected" if entry_anchor_pair in pairs and selected_pair == entry_anchor_pair and entry_anchor_pair != static_cold_start else "contextual_static_below_entry" if entry_anchor_pair in pairs and selected_pair == static_cold_start and entry_anchor_pair != static_cold_start else "history_or_shared_selection",
    }


def _priority_producer_pair(shared, query):
    producer = shared.get("priority_producer")
    if not isinstance(producer, dict) or producer.get("enabled") is not True:
        return None
    if (
        query["task_type"] not in set(producer["eligible_task_types"])
        or query["modality"] not in set(producer["eligible_modalities"])
        or query["operation"] not in set(producer["eligible_operations"])
        or query["operation"] in set(producer["excluded_operations"])
        or query["complexity_score"] > producer["small_edit_maximum_complexity_score"]
        or query["risk"] != "low"
        or query["ambiguity"] != "low"
    ):
        return None
    effort = producer["effort_by_complexity"]["easy"]
    if effort not in set(producer["adaptive_efforts"]):
        return None
    return f"{producer['id']}|{effort}"


def _priority_history(records, query, priority_pair):
    relevant = []
    for record in records:
        if not _record_in_query_scope(record, query):
            continue
        try:
            capability_match = _record_capability_profile(record)["capability_fingerprint"] == query["capability_fingerprint"]
        except ValueError:
            capability_match = False
        if capability_match and record.get("task_type") == query["task_type"] and record.get("operation") == query["operation"] and record.get("code_kind") == query["code_kind"] and _record_complexity_band(record) == query["complexity_band"] and record.get("pair") == priority_pair:
            relevant.append(record)
    verdicts = [_quality_verdict(record) for record in relevant]
    if "fail" in verdicts:
        return {"verdict": "fail", "pass_count": verdicts.count("pass"), "matched_records": len(relevant)}
    if "pass" in verdicts:
        return {"verdict": "pass", "pass_count": verdicts.count("pass"), "matched_records": len(relevant)}
    return {"verdict": None, "pass_count": 0, "matched_records": len(relevant)}


def _operational_fallback_pair(selected_pair, pairs):
    if selected_pair not in pairs:
        return None
    selected_index = pairs.index(selected_pair)
    return pairs[selected_index + 1] if selected_index + 1 < len(pairs) else None


def recommend_model(project_root, task_type, module, *, file_value="", symbol="", code_kind="general", operation="work", modality="text", complexity="easy", complexity_score=None, risk="low", ambiguity="low", task_summary="", task_name="", task_group="", step_kind="", capability_tags=None, entry_model="", entry_effort="", vault=None, ladder=DEFAULT_LADDER, local_store=None, session_prompt="", session_id="", session_context=None):
    started_ns = time.perf_counter_ns()
    shared, pairs = load_shared_ladder(ladder)
    query = _query(project_root, task_type, module, file_value, symbol, code_kind, operation, modality, complexity, complexity_score, risk, ambiguity, task_summary, step_kind, capability_tags, task_name, task_group)
    if isinstance(session_context, dict):
        session_summary = session_effort.sanitize_summary(session_context)
    else:
        session_summary = session_effort.assess_session(session_prompt or task_summary, Path(project_root).expanduser().resolve(), project_key=query["project"]["key"], task_type=query["task_type"], module=query["module"], capability_fingerprint=query["capability_fingerprint"], operation=query["operation"], modality=query["modality"], complexity_score=query["complexity_score"], task_summary=task_summary, task_name=task_name, task_group=task_group, session_id=session_id, local_store=local_store)
    requested_scope = bool(str(session_id or "").strip() or str(task_name or "").strip() or str(task_group or "").strip() or session_summary.get("codex_session_key"))
    context_scope = bool(isinstance(session_context, dict) and (session_summary.get("codex_session_key") or session_summary.get("task_scope_key") or session_summary.get("task_group_key")))
    active_scope = bool(session_summary.get("codex_session_key") or query.get("task_scope_key") or query.get("task_group_key")) if requested_scope else context_scope
    query.update({"task_name": query.get("task_name", ""), "task_group": query.get("task_group", ""), "task_scope_key": query.get("task_scope_key", "") if str(task_name or "").strip() else "", "task_group_key": query.get("task_group_key", ""), "codex_session_key": session_summary.get("codex_session_key") if active_scope else "", "scope_enforced": active_scope})
    entry = _entry_context(shared, pairs, entry_model, entry_effort)
    vault_path, memory_root = _memory_root(query, vault)
    model_switch = model_switch_reference(project_root, vault=vault)
    owner = query["project"].get("owner") or project_change_memory._registered_owner(query["project"]["root"])
    obsidian_configured = _is_configured_owner(vault_path, owner)
    all_local_records = _read_local_records(local_store)
    local_records = [record for record in all_local_records if project_change_memory._record_matches_project(record, query["project"]) and _record_in_query_scope(record, query)]
    category = _task_category(query)
    native_read = _native_model_records(vault_path, owner, category, query) if vault_path is not None and owner else {"current_records": [], "transfer_records": [], "page_count": 0, "read_bytes": 0, "candidate_records": 0, "project_index_document": "", "model_switch_document": "", "current_document": "", "current_source_document": "", "shared_index_document": "", "shared_document": "", "linked_documents": []}
    obsidian_records = [record for record in native_read["current_records"] if project_change_memory._record_matches_project(record, query["project"]) and _record_in_query_scope(record, query)]
    local_record_count = len(local_records)
    obsidian_record_count = len(obsidian_records)
    merged_record_count = len(_merge_model_records(local_records, obsidian_records))
    scope_relation_counts = {}
    related_session_keys = set()
    related_session_evidence = []
    for scoped_record in _merge_model_records(local_records, obsidian_records):
        relation = _record_scope_relation(scoped_record, query)
        scope_relation_counts[relation["reason"]] = scope_relation_counts.get(relation["reason"], 0) + 1
        record_session_key = str(scoped_record.get("codex_session_key") or scoped_record.get("session_key") or "")
        if relation["matched"] and relation["reason"].startswith("related_") and record_session_key:
            related_session_keys.add(record_session_key)
            related_session_evidence.append({"record_id": str(scoped_record.get("record_id") or scoped_record.get("event_id") or ""), "source_session_key": record_session_key, "relation_reason": relation["reason"]})
    pending_projection_count = _pending_projection_count(local_store, query["project"])
    project_records = _merge_model_records(local_records, obsidian_records)
    records, specificity, score = _best_scope_records(project_records, query)
    transfer_records = []
    transfer_selection_basis = None
    if not records or specificity == "project_task":
        local_transfer_candidates = _transferable_local_records(all_local_records, query)
        obsidian_transfer_candidates = _transferable_local_records(native_read["transfer_records"], query)
        transfer_candidates = _merge_model_records(local_transfer_candidates, obsidian_transfer_candidates)
        transfer_records, transfer_specificity, transfer_score = _best_scope_records(transfer_candidates, query)
        if transfer_records and (not records or transfer_score > score):
            records = transfer_records
            specificity = f"cross_project_{transfer_specificity}"
            score = transfer_score
            selected_event_ids = {_model_event_key(record) for record in transfer_records}
            local_transfer_ids = {_model_event_key(record) for record in local_transfer_candidates}
            obsidian_transfer_ids = {_model_event_key(record) for record in obsidian_transfer_candidates}
            uses_local = bool(selected_event_ids & local_transfer_ids)
            uses_obsidian = bool(selected_event_ids & obsidian_transfer_ids)
            transfer_selection_basis = "local_and_obsidian_linked_transfer_history" if uses_local and uses_obsidian else "obsidian_linked_transfer_history" if uses_obsidian else "local_transfer_history"
    active = _active_recommendation(shared, pairs, query, records, entry["entry_anchor_pair"])
    selected_pair = active["selected_pair"]
    priority_pair = _priority_producer_pair(shared, query)
    priority_history = _priority_history(project_records, query, priority_pair) if priority_pair else {"verdict": None, "pass_count": 0, "matched_records": 0}
    if priority_pair and priority_history["verdict"] != "fail":
        attempt_pair = priority_pair
        attempt_reason = "bounded_text_code_spark_verified" if priority_history["verdict"] == "pass" else "bounded_text_code_spark_priority"
        attempt_state = "priority_verified" if priority_history["verdict"] == "pass" else "priority_trial"
        attempt_trial = priority_history["verdict"] != "pass"
        operational_fallback_pair = selected_pair
        switch_direction = "freeze" if priority_history["verdict"] == "pass" else "downgrade"
        switch_change = f"{entry['entry_pair'] or selected_pair}->{priority_pair}"
    elif priority_pair and priority_history["verdict"] == "fail":
        attempt_pair = selected_pair
        attempt_reason = "spark_verify_failure_upgrade"
        attempt_state = "quality_boundary"
        attempt_trial = True
        operational_fallback_pair = _operational_fallback_pair(selected_pair, pairs)
        switch_direction = "upgrade"
        switch_change = f"{entry['entry_pair'] or priority_pair}->{selected_pair}"
    else:
        attempt_pair = selected_pair
        attempt_reason = active["reason"]
        attempt_state = active["calibration_state"]
        attempt_trial = active["trial"]
        operational_fallback_pair = _operational_fallback_pair(selected_pair, pairs)
        if entry["entry_anchor_pair"] in pairs and attempt_pair in pairs:
            entry_index = pairs.index(entry["entry_anchor_pair"])
            attempt_index = pairs.index(attempt_pair)
            switch_direction = "upgrade" if attempt_index > entry_index else "downgrade" if attempt_index < entry_index else "freeze" if attempt_state == "frozen" else "no_switch"
        else:
            switch_direction = "downgrade" if "one_rung_down" in attempt_reason else "upgrade" if "one_rung_up" in attempt_reason or "quality_failure" in attempt_reason else "freeze" if attempt_state == "frozen" else "no_switch"
        switch_change = f"{entry['entry_pair']}->{attempt_pair}" if entry["entry_pair"] else f"{active['success_model'] or active['failed_model']}->{attempt_pair}" if active["success_model"] or active["failed_model"] else f"initial->{attempt_pair}"
    base_selected_pair = selected_pair
    base_attempt_pair = attempt_pair
    session_escalation = {"applied": False, "reason": "no_repeated_session_failure", "from_pair": session_summary.get("last_model_pair") or "", "to_pair": "", "frontier_reached": False}
    if session_summary.get("failure_recorded"):
        previous_pair = session_summary.get("last_model_pair") or base_attempt_pair or base_selected_pair
        quality_base_pair = base_selected_pair if base_selected_pair in pairs else base_attempt_pair if base_attempt_pair in pairs else None
        route_choice = session_effort.solve_route_pair(session_summary, previous_pair, pairs, quality_base_pair)
        forced_pair = route_choice["pair"]
        escalation_reason = route_choice["reason"]
        if forced_pair:
            selected_pair = forced_pair
            attempt_pair = forced_pair
            operational_fallback_pair = _operational_fallback_pair(forced_pair, pairs)
            attempt_reason = escalation_reason
            attempt_state = "session_quality_frontier" if route_choice["frontier_reached"] else "session_quality_boundary"
            attempt_trial = False
            switch_direction = "freeze" if forced_pair == base_attempt_pair else "upgrade"
            switch_change = f"{previous_pair}->{forced_pair}"
            session_escalation = {"applied": forced_pair != base_attempt_pair, "reason": escalation_reason, "from_pair": previous_pair, "to_pair": forced_pair, "frontier_reached": route_choice["frontier_reached"]}
    if attempt_pair is None:
        attempt_state = "blocked"
    attempt_model, attempt_effort = attempt_pair.split("|", 1) if attempt_pair else (None, None)
    selected_model, selected_effort = selected_pair.split("|", 1) if selected_pair else (None, None)
    session_event = {"status": "not_recorded", "written": False, "reason": "session_not_recordable"}
    if session_summary.get("available") and session_summary.get("project_match"):
        try:
            session_event = session_effort.record_session_effort(session_summary, project_key=query["project"]["key"], task_type=query["task_type"], module=query["module"], capability_fingerprint=query["capability_fingerprint"], complexity_score=query["complexity_score"], complexity_band=query["complexity_band"], selected_pair=attempt_pair or selected_pair, requested_pair=base_attempt_pair or base_selected_pair, local_store=local_store)
        except (OSError, ValueError):
            session_event = {"status": "failed", "written": False, "reason": "session_record_write_failed"}
    session_summary = dict(session_summary)
    session_summary["session_event_id"] = session_event.get("event_id", "")
    session_summary["session_event_status"] = session_event.get("status", "not_recorded")
    route_chain = [document for document in (native_read["project_index_document"], native_read["model_switch_document"], native_read["current_document"], native_read["shared_index_document"], native_read["shared_document"], *native_read["linked_documents"]) if document]
    selection_basis = "session_effort_escalation" if session_escalation["applied"] else transfer_selection_basis if transfer_selection_basis else "local_and_obsidian" if records and local_records and obsidian_records else "local_history" if records and local_records else "obsidian_history" if records and obsidian_records else "entry_aware_cold_start" if entry["entry_anchor_pair"] else "shared_cold_start"
    recommendation_reason = attempt_reason if session_summary.get("failure_recorded") else active["reason"]
    recommendation_state = attempt_state if session_summary.get("failure_recorded") else active["calibration_state"]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "local_and_obsidian_model_history",
        "memory_available": True,
        "local_memory_available": True,
        "obsidian_memory_available": obsidian_configured,
        "model_switch_status": model_switch["status"],
        "model_switch_document": model_switch["document"],
        "model_switch_link": model_switch["link"],
        "selection_basis": selection_basis,
        "shared_model_registry": shared["registry_id"],
        "project_key": query["project"]["key"],
        "task_type": query["task_type"],
        "task_name": query["task_name"],
        "task_group": query.get("task_group", ""),
        "task_scope_key": query["task_scope_key"],
        "task_group_key": query.get("task_group_key", ""),
        "task_scope_enforced": query["task_scope_enforced"],
        "codex_session_key": query.get("codex_session_key", ""),
        "scope_enforced": query.get("scope_enforced", False),
        "task_scope_mode": session_summary.get("task_scope_mode", "unscoped"),
        "module": query["module"],
        "file": query["file"],
        "symbol": query["symbol"],
        "code_kind": query["code_kind"],
        "operation": query["operation"],
        "modality": query["modality"],
        "complexity": query["complexity"],
        "complexity_score": query["complexity_score"],
        "complexity_band": query["complexity_band"],
        "step_kind": query["step_kind"],
        "capability_tags": query["capability_tags"],
        "capability_fingerprint": query["capability_fingerprint"],
        "specificity": specificity,
        "specificity_score": score,
        "matched_records": len(records),
        "local_record_count": local_record_count,
        "obsidian_record_count": obsidian_record_count,
        "merged_record_count": merged_record_count,
        "scope_relation_counts": scope_relation_counts,
        "related_session_count": len(related_session_keys),
        "related_session_keys": sorted(related_session_keys),
        "related_session_evidence": related_session_evidence[:50],
        "transfer_record_count": len(transfer_records),
        "pending_projection_count": pending_projection_count,
        "quality_samples": active["quality_samples"],
        "entry_model": entry["entry_model"],
        "entry_effort": entry["entry_effort"],
        "entry_pair": entry["entry_pair"],
        "entry_anchor_pair": entry["entry_anchor_pair"],
        "entry_source": entry["entry_source"],
        "static_cold_start": active["static_cold_start"],
        "entry_route_reason": active["entry_route_reason"],
        "selected_pair": selected_pair,
        "selected_model": selected_model,
        "selected_effort": selected_effort,
        "attempt_pair": attempt_pair,
        "attempt_model": attempt_model,
        "attempt_effort": attempt_effort,
        "active_fallback_pair": operational_fallback_pair,
        "priority_verdict": priority_history["verdict"],
        "priority_pass_count": priority_history["pass_count"],
        "priority_matched_records": priority_history["matched_records"],
        "priority_producer_scope": "bounded_text_code_and_scheduled_independent_sources",
        "trial": active["trial"],
        "reason": recommendation_reason,
        "calibration_state": recommendation_state,
        "attempt_trial": attempt_trial,
        "attempt_reason": attempt_reason,
        "attempt_calibration_state": attempt_state,
        "switch_direction": switch_direction,
        "switch_change": switch_change,
        "success_model": active["success_model"],
        "failed_model": active["failed_model"],
        "pass_counts": active["pass_counts"],
        "minimum_passes_before_downgrade": active["minimum_passes_before_downgrade"],
        "cost_evidence": active["cost_evidence"],
        "session_effort": session_summary,
        "session_escalation": session_escalation,
        "route_capsule": {"mode": "obsidian_native_wikilinks", "chain": route_chain, "current_document": native_read["current_document"], "current_source_document": native_read["current_source_document"], "shared_document": native_read["shared_document"], "linked_documents": native_read["linked_documents"], "pages_read": native_read["page_count"], "read_bytes": native_read["read_bytes"], "candidate_records": native_read["candidate_records"], "elapsed_ms": round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)},
    }


def _receipt_pair(receipt):
    for key in ("executed_pair", "effective_pair", "resolved_pair", "requested_pair"):
        if isinstance(receipt.get(key), str):
            return receipt[key]
    model = receipt.get("effective_model") or receipt.get("resolved_model") or receipt.get("requested_model")
    effort = receipt.get("effective_effort") or receipt.get("resolved_effort") or receipt.get("requested_effort")
    return f"{model}|{effort}" if model and effort else None


def _receipt_entry(receipt):
    context = receipt.get("model_learning_context") if isinstance(receipt.get("model_learning_context"), dict) else {}
    entry_pair = receipt.get("entry_pair") or context.get("entry_pair")
    entry_model = receipt.get("entry_model") or context.get("entry_model")
    entry_effort = receipt.get("entry_effort") or context.get("entry_effort")
    if isinstance(entry_pair, str) and "|" in entry_pair:
        pair_model, pair_effort = entry_pair.split("|", 1)
        if entry_model not in {None, pair_model} or entry_effort not in {None, pair_effort}:
            raise ValueError("receipt entry identity is inconsistent")
        entry_model, entry_effort = pair_model, pair_effort
    if bool(entry_model) != bool(entry_effort):
        raise ValueError("receipt entry identity is incomplete")
    return entry_model or "", entry_effort or "", receipt.get("entry_source") or context.get("entry_source") or "receipt"


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _resolve_local_store(local_store=None):
    configured = local_store or os.getenv("CODEX_MODEL_ROUTING_MEMORY") or DEFAULT_LOCAL_STORE
    return Path(configured).expanduser().resolve()


def _projection_state_path(local_store=None):
    return _resolve_local_store(local_store).with_name("projection-state.json")


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


def _model_event_key(record):
    event_id = record.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return "|".join(str(record.get(field) or "") for field in ("project_key", "receipt_sha256", "real_status", "failure_class"))


def _read_local_records(local_store=None):
    path = _resolve_local_store(local_store)
    if not path.exists():
        return []
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        if envelope.get("local_model_memory_schema") != LOCAL_MEMORY_SCHEMA_VERSION or envelope.get("event") != "model-result":
            continue
        record = envelope.get("record")
        if isinstance(record, dict) and record.get("model_experience_schema") == SCHEMA_VERSION:
            normalized = dict(record)
            if isinstance(envelope.get("event_id"), str) and envelope["event_id"]:
                normalized.setdefault("event_id", envelope["event_id"])
            records.append(_json_safe(normalized))
    return records


def _model_routing_directory(vault_path, owner):
    return vault_path / "Skills" / "Model Routing Records" if owner == "Global Codex Skills" else _project_switch_directory(vault_path, owner) / "Model Routing"


def _category_page(vault_path, owner, category):
    return _model_routing_directory(vault_path, owner) / f"{MODEL_SWITCH_CATEGORY_TITLES[category]}.md"


def _category_reference(vault_path, owner, category):
    page = _category_page(vault_path, owner, category)
    document = _vault_relative_path(vault_path, page).as_posix()
    return {"document": document, "link": _wikilink(vault_path, page)}


def _shared_routing_directory(vault_path):
    return vault_path / "Skills" / "Model Routing"


def _shared_category_page(vault_path, category):
    return _shared_routing_directory(vault_path) / f"{MODEL_SWITCH_CATEGORY_TITLES[category]}.md"


def _owner_index(vault_path, owner):
    return _project_switch_index(vault_path, owner)


def _wikilink(vault_path, path):
    relative = _vault_relative_path(vault_path, path).as_posix()
    return f"[[{relative[:-3] if relative.endswith('.md') else relative}]]"


def _read_embedded_records(path):
    if path is None or not Path(path).exists():
        return []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records = []
    for raw in re.findall(r"<!-- model-experience: (.*?) -->", text, flags=re.DOTALL):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("model_experience_schema") == SCHEMA_VERSION:
            records.append(_json_safe(record))
    return records


def _record_capability_or_default(record):
    try:
        return _record_capability_profile(record)
    except ValueError:
        return {"step_kind": str(record.get("step_kind") or "general"), "semantic_capability_tags": [], "capability_fingerprint": str(record.get("capability_fingerprint") or "")}


def _native_model_records(vault_path, owner, category, query):
    current_page = _category_page(vault_path, owner, category)
    model_switch = _memory_root_owner(vault_path, owner)
    current_source = current_page if current_page.exists() else model_switch
    current_records = _read_embedded_records(current_source)
    current_bytes = current_source.stat().st_size if current_source.exists() else 0
    page_count = int(current_source.exists())
    shared_page = _shared_category_page(vault_path, category)
    try:
        shared_text = shared_page.read_text(encoding="utf-8")
    except OSError:
        shared_text = ""
    page_count += int(bool(shared_text))
    candidate_pages = []
    fingerprint = query["capability_fingerprint"]
    for line in shared_text.splitlines():
        match = re.search(r"\[\[([^\]]+)\]\].*?fingerprints:\s*([^\s]+)", line)
        if match and fingerprint in set(match.group(2).split(",")):
            target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            candidate = (vault_path / (target if target.endswith(".md") else target + ".md")).resolve()
            try:
                candidate.relative_to(vault_path.resolve())
            except ValueError:
                continue
            if candidate != current_page.resolve() and candidate.is_file():
                candidate_pages.append(candidate)
    transfer_records = []
    candidate_bytes = 0
    for candidate in sorted(set(candidate_pages)):
        candidate_bytes += candidate.stat().st_size
        transfer_records.extend(_read_embedded_records(candidate))
    transfer_records = _merge_model_records([], transfer_records)
    linked_documents = [_vault_relative_path(vault_path, candidate).as_posix() for candidate in sorted(set(candidate_pages))]
    return {"current_records": current_records, "transfer_records": transfer_records, "page_count": page_count + len(linked_documents), "read_bytes": current_bytes + len(shared_text.encode("utf-8")) + candidate_bytes, "candidate_records": len(current_records) + len(transfer_records), "project_index_document": _vault_relative_path(vault_path, _owner_index(vault_path, owner)).as_posix(), "model_switch_document": _vault_relative_path(vault_path, model_switch).as_posix(), "current_document": _vault_relative_path(vault_path, current_page).as_posix(), "current_source_document": _vault_relative_path(vault_path, current_source).as_posix(), "shared_index_document": _vault_relative_path(vault_path, _shared_routing_directory(vault_path) / "index.md").as_posix(), "shared_document": _vault_relative_path(vault_path, shared_page).as_posix(), "linked_documents": linked_documents}


def _render_category_page(vault_path, owner, category, records):
    title = MODEL_SWITCH_CATEGORY_TITLES[category]
    project_index = _owner_index(vault_path, owner)
    model_switch = _memory_root_owner(vault_path, owner)
    fingerprints = sorted({capability["capability_fingerprint"] for capability in (_record_capability_or_default(record) for record in records) if capability["capability_fingerprint"]})
    lines = [MODEL_SWITCH_CATEGORY_MARKER, f"# {owner} · {title}", "", f"- Project index: {_wikilink(vault_path, project_index)}", f"- Model Switch: {_wikilink(vault_path, model_switch)}", f"- Shared task type: {_wikilink(vault_path, _shared_category_page(vault_path, category))}", "", f"Records: {len(records)}", f"Fingerprints: {', '.join(f'`{fingerprint}`' for fingerprint in fingerprints)}", "", "| Task | Step / capability | Score | Module / file / symbol | Entry / selected / effective / next | Direction / reason | Outcome / recovery | Tokens / time | Evidence / Ending attempt | Next decision |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for record in records:
        detail = _switch_details(record)
        capability = _record_capability_or_default(record)
        capability_text = ", ".join(capability["semantic_capability_tags"]) or "structural"
        location = " / ".join(value for value in (record.get("module"), record.get("file"), record.get("symbol")) if value) or "—"
        outcome_reason = str(record.get("outcome_reason") or record.get("failure_class") or "—").replace("|", "/")
        recovery = record.get("recovery_from_pair") or "—"
        evidence = record.get("model_evidence") or ("runtime_receipt" if record.get("receipt_status") == "pass" else record.get("receipt_status") or "unavailable")
        next_decision = f"{record.get('routing_action','—')} / {record.get('next_pair_direction','—')} / {record.get('next_pair_reason','—')}"
        if record.get("session_key"):
            next_decision = f"{next_decision}; session {record.get('session_state') or 'unknown'} / {record.get('session_route_class') or 'unclassified'} / steps {record.get('session_step_estimate') or '—'} / effort-class {record.get('session_estimated_effort') or '—'} / info {record.get('session_information_burden') or '—'} / difficulty {record.get('session_model_difficulty') or '—'} / effort {record.get('session_user_effort') or '—'} / failed {record.get('session_failure_recorded')} / model {record.get('session_model_pair') or '—'} / preferred {record.get('session_preferred_pair') or '—'} / escalate {record.get('session_escalation_pair') or '—'}"
        lines.append(f"| {record.get('task_type','')} | {capability['step_kind']} / {capability_text} / {capability['capability_fingerprint'][:12]} | {record.get('complexity_score','—')}/100 {record.get('complexity_band') or _record_complexity_band(record)} | {location} | {record.get('entry_pair') or '—'} / {detail['selected_pair'] or '—'} / {detail['effective_pair'] or '—'} / {detail['next_pair'] or '—'} | {detail['switch_direction']} / {detail['switch_reason']} | {outcome_reason} / {recovery} | {record.get('total_tokens','—')} / {record.get('process_ms','—')} | {evidence} / {record.get('real_status','')} / {record.get('ending_attempt_number','—')} / {record.get('ending_pass_shape','—')} / {record.get('model_suitability','—')} | {next_decision} |")
        lines.append("<!-- model-experience: " + json.dumps(_json_safe(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + " -->")
    return "\n".join(lines).rstrip() + "\n"


def _render_model_switch(vault_path, owner, records_by_category, foreign_records):
    lines = ["# Model Switch", "", f"- Project index: {_wikilink(vault_path, _owner_index(vault_path, owner))}", f"- Shared routing graph: {_wikilink(vault_path, _shared_routing_directory(vault_path) / 'index.md')}", "", "Model routing records by stable task category.", ""]
    for category in MODEL_SWITCH_CATEGORIES:
        records = records_by_category.get(category, [])
        if records:
            lines.append(f"- {_wikilink(vault_path, _category_page(vault_path, owner, category))} ({len(records)})")
    if foreign_records:
        lines.extend(["", "<!-- preserved foreign model-experience records -->"])
        lines.extend("<!-- model-experience: " + json.dumps(_json_safe(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + " -->" for record in foreign_records)
    return "\n".join(lines) + "\n"


def _refresh_shared_category(vault_path, category):
    shared_page = _shared_category_page(vault_path, category)
    project_pattern = f"*/Model Routing/{MODEL_SWITCH_CATEGORY_TITLES[category]}.md"
    pages = sorted((vault_path / "Projects").glob(project_pattern)) if (vault_path / "Projects").exists() else []
    global_page = vault_path / "Skills" / "Model Routing Records" / f"{MODEL_SWITCH_CATEGORY_TITLES[category]}.md"
    if global_page.exists():
        pages.append(global_page)
    lines = [f"# {MODEL_SWITCH_CATEGORY_TITLES[category]}", "", f"- Routing index: {_wikilink(vault_path, _shared_routing_directory(vault_path) / 'index.md')}", "", "Project routing records with native Obsidian links.", ""]
    for page in pages:
        fingerprints = sorted({capability["capability_fingerprint"] for capability in (_record_capability_or_default(record) for record in _read_embedded_records(page)) if capability["capability_fingerprint"]})
        lines.append(f"- {_wikilink(vault_path, page)} — fingerprints: {','.join(fingerprints)}")
    _atomic_write(shared_page, "\n".join(lines) + "\n")


def _refresh_shared_routing_index(vault_path):
    routing_index = _shared_routing_directory(vault_path) / "index.md"
    lines = ["# Model Routing", "", "Native Obsidian task-type links between project model memories.", ""]
    lines.extend(f"- {_wikilink(vault_path, _shared_category_page(vault_path, category))}" for category in MODEL_SWITCH_CATEGORIES)
    _atomic_write(routing_index, "\n".join(lines) + "\n")
    skills_index = vault_path / "Skills" / "index.md"
    if skills_index.exists():
        link_line = f"- {_wikilink(vault_path, routing_index)} — Native project/task-type model-memory graph."
        skills_text = skills_index.read_text(encoding="utf-8")
        if link_line not in skills_text:
            _atomic_write(skills_index, skills_text.rstrip() + "\n" + link_line + "\n")



def _merge_model_records(local_records, obsidian_records):
    merged = {}
    for record in [*obsidian_records, *local_records]:
        key = _model_event_key(record)
        if key:
            merged[key] = _json_safe(record)
    return sorted(merged.values(), key=lambda record: (record.get("recorded_at") or "", _model_event_key(record)))


def _read_projection_state(local_store=None):
    path = _projection_state_path(local_store)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("events", {}) if payload.get("schema_version") == LOCAL_MEMORY_SCHEMA_VERSION and isinstance(payload.get("events"), dict) else {}


def _write_projection_state(states, local_store=None):
    payload = {"schema_version": LOCAL_MEMORY_SCHEMA_VERSION, "events": states}
    _atomic_write(_projection_state_path(local_store), json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _pending_projection_count(local_store, project):
    states = _read_projection_state(local_store)
    event_ids = {_model_event_key(record) for record in _read_local_records(local_store) if project_change_memory._record_matches_project(record, project)}
    return sum(1 for event_id in event_ids if states.get(event_id, {}).get("status") != "written")


def _append_local_record(record, local_store=None):
    path = _resolve_local_store(local_store)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_file_lock(lock_path):
        duplicate = next((item for item in _read_local_records(path) if _model_event_key(item) == _model_event_key(record)), None)
        if duplicate is not None:
            return {"status": "duplicate", "written": True, "path": str(path), "record": duplicate}
        envelope = {"local_model_memory_schema": LOCAL_MEMORY_SCHEMA_VERSION, "event": "model-result", "event_id": record["event_id"], "record": _json_safe(record)}
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        _atomic_write(path, existing + json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        try:
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
        except OSError:
            pass
        states = _read_projection_state(path)
        states.setdefault(record["event_id"], {"status": "pending", "reason": "not_projected"})
        _write_projection_state(states, path)
    return {"status": "written", "written": True, "path": str(path), "record": record}


def _vault_relative_path(vault_path, path):
    canonical_vault = Path(vault_path).expanduser().resolve()
    canonical_path = Path(path).expanduser().resolve()
    try:
        return canonical_path.relative_to(canonical_vault)
    except ValueError as error:
        raise ValueError(f"model experience path must stay inside the Obsidian vault: {canonical_path}") from error
def _task_category(record):
    task_type = str(record.get("task_type") or "").strip().lower()
    code_kind = str(record.get("code_kind") or "").strip().lower()
    operation = str(record.get("operation") or "").strip().lower()
    values = " ".join((task_type, code_kind, operation))
    task_type_tokens = set(re.split(r"[._-]+", task_type))
    if task_type_tokens & {"doc", "docs", "documentation", "instruction", "instructions", "instructional", "prompt", "prompts", "prompting"}:
        return "documentation-instructions"
    if any(term in values for term in ("test", "verify", "validation")):
        return "tests-verification"
    if any(term in values for term in ("document", "instruction", "prompt", "readme", "guide")):
        return "documentation-instructions"
    if any(term in values for term in ("bug", "debug", "find", "diagnos")):
        return "finding-bugs"
    if any(term in values for term in ("design", "architect", "plan")):
        return "code-design"
    if task_type == "script" or code_kind == "script" or operation in {"script_update", "script_edit"} or (operation in {"edit", "update", "implement", "fix"} and code_kind in {"python", "csharp", "code"}):
        return "normal-script-update"
    return "general-work"


def _switch_details(record):
    attempt_pair = record.get("attempt_pair") or None
    selected_pair = record.get("selected_pair") or record.get("active_fallback_pair") or record.get("pair") or None
    effective_pair = record.get("pair") or None
    prior_pair = record.get("prior_pair") or record.get("success_pair") or record.get("failed_pair") or None
    reason = record.get("selection_reason") or "unknown_selection_reason"
    state = record.get("recommendation_state") or "unknown_state"
    operational_failures = record.get("operational_failure_pairs") if isinstance(record.get("operational_failure_pairs"), list) else []
    if record.get("switch_direction") in MODEL_SWITCH_DIRECTIONS and record.get("switch_reason"):
        switch_direction = record["switch_direction"]
        switch_reason = record["switch_reason"]
    elif operational_failures and effective_pair and attempt_pair and effective_pair != attempt_pair:
        switch_direction = "operational_fallback"
        switch_reason = f"operational fallback after {', '.join(operational_failures)}; selection {reason}"
    elif prior_pair is None:
        switch_direction = "initial"
        switch_reason = reason
    elif "one_rung_down" in reason:
        switch_direction = "downgrade"
        switch_reason = reason
    elif "one_rung_up" in reason or "quality_failure" in reason:
        switch_direction = "upgrade"
        switch_reason = reason
    elif state == "frozen" or "retained" in reason or reason in {"verified_floor_retained", "verified_quality_boundary"}:
        switch_direction = "freeze"
        switch_reason = reason
    else:
        switch_direction = "no_switch"
        switch_reason = reason
    return {"prior_pair": prior_pair, "selected_pair": selected_pair, "effective_pair": effective_pair, "attempt_pair": attempt_pair, "switch_direction": switch_direction, "switch_reason": switch_reason, "next_pair": record.get("next_pair")}


def rebuild_model_switches(project_root, *, vault=None):
    project = project_change_memory._project_identity(project_root)
    vault_path = project_change_memory._resolve_vault(vault)
    if vault_path is None:
        return {"status": "unavailable", "written": False, "reason": "obsidian_vault_unavailable"}
    owner = project.get("owner") or project_change_memory._registered_owner(project["root"])
    if owner is None:
        return {"status": "no-op", "written": False, "reason": "unregistered_project"}
    broad_page = _memory_root_owner(vault_path, owner)
    legacy_records = _read_embedded_records(broad_page)
    category_records = []
    for category in MODEL_SWITCH_CATEGORIES:
        category_records.extend(_read_embedded_records(_category_page(vault_path, owner, category)))
    all_records = _merge_model_records(legacy_records, category_records)
    own_records = [record for record in all_records if project_change_memory._record_matches_project(record, project)]
    foreign_records = [record for record in all_records if not project_change_memory._record_matches_project(record, project)]
    records_by_category = {category: [] for category in MODEL_SWITCH_CATEGORIES}
    for record in own_records:
        records_by_category[_task_category(record)].append(record)
    broad_page.parent.mkdir(parents=True, exist_ok=True)
    for category, records in records_by_category.items():
        category_page = _category_page(vault_path, owner, category)
        if records:
            category_page.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(category_page, _render_category_page(vault_path, owner, category, records))
    _atomic_write(broad_page, _render_model_switch(vault_path, owner, records_by_category, foreign_records))
    _ensure_model_switch_index_link(vault_path, owner, broad_page)
    _refresh_shared_routing_index(vault_path)
    for category in MODEL_SWITCH_CATEGORIES:
        _refresh_shared_category(vault_path, category)
    return {"status": "rebuilt", "written": True, "project_key": project["key"], "records": len(own_records), "page_records": len(all_records), "summary": _vault_relative_path(vault_path, broad_page).as_posix(), "category_pages": [_vault_relative_path(vault_path, _category_page(vault_path, owner, category)).as_posix() for category, records in records_by_category.items() if records]}



def relink_project(project_root, *, vault=None):
    project = project_change_memory._project_identity(project_root)
    return {"status": "disabled", "written": False, "project_key": project["key"], "reason": "legacy_hierarchy_relink_disabled"}


def _project_record_to_obsidian(project_root, record, vault=None, local_store=None):
    project = project_change_memory._project_identity(project_root)
    vault_path = project_change_memory._resolve_vault(vault)
    if vault_path is None:
        return {"status": "unavailable", "written": False, "reason": "obsidian_vault_unavailable"}
    owner = project.get("owner") or project_change_memory._registered_owner(project["root"])
    memory_root = _memory_root_owner(vault_path, owner)
    if memory_root is None or not _is_configured_owner(vault_path, owner):
        return {"status": "pending", "written": False, "reason": "unregistered_or_unknown_project_root"}
    lock_path = _resolve_local_store(local_store).with_name("obsidian-projection.lock")
    with _exclusive_file_lock(lock_path):
        records = _read_project_records(memory_root)
        duplicate = next((item for item in records if project_change_memory._record_matches_project(item, project) and _model_event_key(item) == _model_event_key(record)), None)
        if duplicate is None:
            records.append(_json_safe(record))
            _atomic_write(memory_root, "# Model Switch\n\n" + "\n".join("<!-- model-experience: " + json.dumps(_json_safe(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + " -->" for item in records) + "\n")
            _ensure_model_switch_index_link(vault_path, owner, memory_root)
        model_switch = rebuild_model_switches(project_root, vault=vault)
    model_record = _category_reference(vault_path, owner, _task_category(duplicate or record))
    return {"status": "duplicate" if duplicate is not None else "written", "written": True, "record_id": (duplicate or record).get("record_id"), "obsidian_note": _vault_relative_path(vault_path, memory_root).as_posix(), "model_record_document": model_record["document"], "model_record_link": model_record["link"], "model_switch": model_switch}


def reconcile_local_model_history(project_root, *, vault=None, local_store=None):
    project = project_change_memory._project_identity(project_root)
    local_path = _resolve_local_store(local_store)
    with _exclusive_file_lock(local_path.with_suffix(local_path.suffix + ".lock")):
        records = [record for record in _read_local_records(local_path) if project_change_memory._record_matches_project(record, project)]
        states = _read_projection_state(local_path)
        projected = 0
        latest = None
        for record in records:
            event_id = _model_event_key(record)
            if states.get(event_id, {}).get("status") == "written":
                continue
            result = _project_record_to_obsidian(project_root, record, vault, local_path)
            latest = result
            if result.get("written") is True:
                states[event_id] = {"status": "written", "reason": result["status"], "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                projected += 1
            else:
                states[event_id] = {"status": "pending", "reason": result.get("reason") or result.get("status") or "projection_failed"}
        _write_projection_state(states, local_path)
    pending = _pending_projection_count(local_store, project)
    return {"status": "written" if projected else "pending" if pending else "up-to-date", "written": projected > 0, "projected": projected, "pending": pending, "latest": latest}


def record_model_observation(project_root, task_type, module, pair, real_status, failure_class, *, observation_id, file_value="", symbol="", code_kind="general", operation="verify", modality="text", complexity="easy", complexity_score=None, risk="low", ambiguity="low", task_summary="", task_name="", task_group="", session_id="", step_kind="verification", capability_tags=None, entry_model="", entry_effort="", model_evidence="task_assignment", vault=None, ladder=DEFAULT_LADDER, recorded_at=None, local_store=None, outcome_reason="", verification_count=0, ending_attempt_number=1, prior_quality_failure_count=0, prior_operational_failure_count=0):
    """Persist a visible, non-learning model observation when no runtime receipt exists."""
    shared, pairs = load_shared_ladder(ladder)
    priority = shared.get("priority_producer")
    priority_pairs = {f"{priority['id']}|{effort}" for effort in priority["adaptive_efforts"]} if isinstance(priority, dict) and priority.get("enabled") is True else set()
    pair = _single_line(pair, "pair", maximum=160)
    if pair not in set(pairs) | priority_pairs:
        raise ValueError("observed pair is outside the shared active producer contract")
    if real_status not in {"pass", "fail", "blocked"} or failure_class not in FAILURE_CLASSES:
        raise ValueError("observation status or failure class is invalid")
    if real_status == "pass" and failure_class != "none":
        raise ValueError("a passing observation requires failure_class=none")
    if real_status == "fail" and failure_class == "none":
        raise ValueError("a failed observation requires an explicit failure class")
    if model_evidence not in {"verified_entry", "task_assignment", "configured_selection"}:
        raise ValueError("an unreceipted observation requires a known non-runtime evidence level")
    observation_id = _single_line(observation_id, "observation_id", maximum=160)
    if isinstance(verification_count, bool) or not isinstance(verification_count, int) or verification_count < 0:
        raise ValueError("verification_count must be a non-negative integer")
    for field, value in (
        ("ending_attempt_number", ending_attempt_number),
        ("prior_quality_failure_count", prior_quality_failure_count),
        ("prior_operational_failure_count", prior_operational_failure_count),
    ):
        minimum = 1 if field == "ending_attempt_number" else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{field} must be an integer >= {minimum}")
    query = _query(project_root, task_type, module, file_value, symbol, code_kind, operation, modality, complexity, complexity_score, risk, ambiguity, task_summary, step_kind, capability_tags, task_name, task_group)
    recommendation = recommend_model(
        project_root,
        task_type,
        module,
        file_value=file_value,
        symbol=symbol,
        code_kind=code_kind,
        operation=operation,
        modality=modality,
        complexity=complexity,
        complexity_score=query["complexity_score"],
        risk=risk,
        ambiguity=ambiguity,
        task_summary=task_summary,
        task_name=task_name,
        task_group=task_group,
        session_id=session_id,
        step_kind=query["step_kind"],
        capability_tags=query["capability_tags"],
        entry_model=entry_model,
        entry_effort=entry_effort,
        vault=vault,
        ladder=ladder,
        local_store=local_store,
        session_prompt=task_summary,
    )
    timestamp = recorded_at or datetime.now(timezone.utc)
    owner = query["project"].get("owner") or project_change_memory._registered_owner(query["project"]["root"])
    outcome_reason = _single_line(outcome_reason or ("Ending verification passed without a producer runtime receipt." if real_status == "pass" else "Ending verification recorded without a producer runtime receipt."), "outcome_reason", maximum=280)
    ending_pass_shape = "first_attempt_pass" if real_status == "pass" and ending_attempt_number == 1 else "retry_pass" if real_status == "pass" else "failed_attempt" if real_status == "fail" else "blocked"
    if real_status == "pass":
        model_suitability = "suitable_observed_no_runtime_receipt"
    elif real_status == "fail" and failure_class in QUALITY_FAILURES:
        model_suitability = "quality_failure_observed_no_runtime_receipt"
    elif real_status == "fail":
        model_suitability = "operational_failure_observed_no_runtime_receipt"
    else:
        model_suitability = "unproven_observed_no_runtime_receipt"
    routing_action = "record_only_require_receipted_evidence_before_model_movement"
    session_summary = session_effort.sanitize_summary(recommendation.get("session_effort"))
    session_escalation = recommendation.get("session_escalation") if isinstance(recommendation.get("session_escalation"), dict) else {}
    event_payload = f"{query['project']['key']}|{observation_id}|{real_status}|{failure_class}|{pair}"
    event_id = hashlib.sha256(event_payload.encode()).hexdigest()
    base = {
        "model_experience_schema": SCHEMA_VERSION,
        "record_id": "",
        "event_id": event_id,
        "recorded_at": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "project_name": query["project"]["name"],
        "project_key": query["project"]["key"],
        "project_owner": owner,
        "task_type": query["task_type"],
        "task_name": query.get("task_name", ""),
        "task_group": query.get("task_group", ""),
        "task_scope_key": query.get("task_scope_key", ""),
        "task_group_key": query.get("task_group_key", ""),
        "task_summary": query["task_summary"],
        "module": query["module"],
        "file": query["file"],
        "symbol": query["symbol"],
        "code_kind": query["code_kind"],
        "operation": query["operation"],
        "modality": query["modality"],
        "complexity": query["complexity"],
        "complexity_score": query["complexity_score"],
        "complexity_band": query["complexity_band"],
        "risk": query["risk"],
        "ambiguity": query["ambiguity"],
        "step_kind": query["step_kind"],
        "capability_tags": query["capability_tags"],
        "capability_fingerprint": query["capability_fingerprint"],
        "entry_model": recommendation["entry_model"],
        "entry_effort": recommendation["entry_effort"],
        "entry_pair": recommendation["entry_pair"],
        "entry_anchor_pair": recommendation["entry_anchor_pair"],
        "entry_source": recommendation["entry_source"],
        "model": pair.split("|", 1)[0],
        "effort": pair.split("|", 1)[1],
        "pair": pair,
        "selected_pair": pair,
        "prior_pair": recommendation.get("success_model") or recommendation.get("failed_model"),
        "attempt_pair": pair,
        "active_fallback_pair": recommendation.get("active_fallback_pair"),
        "operational_failure_pairs": [],
        "real_status": real_status,
        "failure_class": failure_class,
        "outcome_reason": outcome_reason,
        "verification_count": verification_count,
        "model_evidence": model_evidence,
        "learning_eligible": False,
        "observation_id": observation_id,
        "receipt_status": "unavailable",
        "model_match": False,
        "effort_match": False,
        "turn_completed": True,
        "trial": False,
        "selection_reason": f"{model_evidence}_without_runtime_receipt",
        "recommendation_state": "observed_not_learning_eligible",
        "specificity": recommendation["specificity"],
        "matched_records": recommendation["matched_records"],
        "success_pair": recommendation["success_model"],
        "failed_pair": recommendation["failed_model"],
        "workload_prompt_sha256": None,
        "total_tokens": None,
        "process_ms": None,
        "receipt_sha256": None,
        "switch_direction": "no_switch",
        "switch_reason": "observation_only_no_runtime_receipt",
        "next_pair": pair,
        "completed_pair": pair,
        "recovery_from_pair": None,
        "attempt_chain": [{"pair": pair, "status": real_status, "failure_class": failure_class}],
        "ending_attempt_number": ending_attempt_number,
        "ending_pass_shape": ending_pass_shape,
        "prior_quality_failure_count": prior_quality_failure_count,
        "prior_operational_failure_count": prior_operational_failure_count,
        "matched_pass_count_after": recommendation.get("pass_counts", {}).get(pair, 0),
        "minimum_passes_before_downgrade": MIN_REAL_PASSES_BEFORE_DOWNGRADE,
        "next_pair_reason": "observation_only_no_runtime_receipt",
        "next_pair_direction": "no_switch",
        "model_suitability": model_suitability,
        "routing_action": routing_action,
        "session_key": session_summary.get("session_key", ""),
        "session_task_scope_key": session_summary.get("session_task_scope_key", ""),
        "codex_session_key": session_summary.get("codex_session_key", ""),
        "task_scope_mode": session_summary.get("task_scope_mode", "unscoped"),
        "session_state": session_summary.get("state", ""),
        "session_turn_count": session_summary.get("turn_count", 0),
        "session_prior_turn_count": session_summary.get("prior_turn_count", 0),
        "session_same_task_turns": session_summary.get("same_task_turns", 0),
        "session_user_effort": session_summary.get("user_effort", 0),
        "session_failure_recorded": session_summary.get("failure_recorded", False),
        "session_model_pair": session_summary.get("last_model_pair", ""),
        "session_model_source": session_summary.get("last_model_source", ""),
        "session_model_pairs": session_summary.get("model_pairs", []),
        "session_task_fingerprint": session_summary.get("task_topic_fingerprint", ""),
        "session_project_match": session_summary.get("project_match", False),
        "session_solving_surface": session_summary.get("solving_surface", ""),
        "session_task_length": session_summary.get("task_length", ""),
        "session_step_estimate": session_summary.get("step_estimate", 0),
        "session_step_class": session_summary.get("step_class", ""),
        "session_estimated_effort": session_summary.get("estimated_effort", ""),
        "session_information_burden": session_summary.get("information_burden", ""),
        "session_model_family": session_summary.get("model_family", ""),
        "session_model_difficulty": session_summary.get("model_difficulty", ""),
        "session_difficulty_class": session_summary.get("difficulty_class", ""),
        "session_route_class": session_summary.get("route_class", ""),
        "session_preferred_pair": session_summary.get("preferred_solving_pair", ""),
        "session_route_reason": session_summary.get("route_reason", ""),
        "session_explicit_route_hint": session_summary.get("explicit_route_hint", ""),
        "session_escalation_pair": session_escalation.get("to_pair", ""),
        "session_escalation_reason": session_escalation.get("reason", ""),
        "session_event_id": session_summary.get("session_event_id", ""),
    }
    base = _json_safe(base)
    fingerprint_payload = _json_safe({key: base[key] for key in FRONTMATTER_FIELDS if key not in {"record_id", "recorded_at"}})
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    base["record_id"] = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{fingerprint[:12]}"
    local_result = _append_local_record(base, local_store)
    stored_record = local_result["record"]
    reconcile_result = reconcile_local_model_history(project_root, vault=vault, local_store=local_store)
    states = _read_projection_state(local_store)
    projection_state = states.get(event_id, {"status": "pending", "reason": "projection_state_missing"})
    latest_projection = reconcile_result.get("latest") if isinstance(reconcile_result, dict) else None
    model_switch = latest_projection.get("model_switch") if isinstance(latest_projection, dict) else None
    vault_path, memory_root = _memory_root(query, vault)
    model_switch_reference_result = model_switch_reference(project_root, vault=vault)
    obsidian_note = _vault_relative_path(vault_path, memory_root).as_posix() if projection_state.get("status") == "written" and vault_path is not None and memory_root is not None else None
    model_record = _category_reference(vault_path, owner, _task_category(stored_record)) if obsidian_note and owner else {"document": None, "link": None}
    if model_switch is None and obsidian_note:
        model_switch = rebuild_model_switches(project_root, vault=vault)
    return {
        "status": "duplicate" if local_result["status"] == "duplicate" else "written",
        "written": True,
        "learning_eligible": False,
        "record_id": stored_record["record_id"],
        "event_id": stored_record["event_id"],
        "project_key": stored_record["project_key"],
        "pair": stored_record["pair"],
        "real_status": stored_record["real_status"],
        "failure_class": stored_record["failure_class"],
        "complexity_score": stored_record["complexity_score"],
        "complexity_band": stored_record["complexity_band"],
        "ending_attempt_number": stored_record["ending_attempt_number"],
        "ending_pass_shape": stored_record["ending_pass_shape"],
        "model_suitability": stored_record["model_suitability"],
        "routing_action": stored_record["routing_action"],
        "switch_direction": stored_record["switch_direction"],
        "switch_reason": stored_record["switch_reason"],
        "next_pair": stored_record["next_pair"],
        "matched_pass_count_after": stored_record["matched_pass_count_after"],
        "minimum_passes_before_downgrade": stored_record["minimum_passes_before_downgrade"],
        "next_pair_reason": stored_record["next_pair_reason"],
        "next_pair_direction": stored_record["next_pair_direction"],
        "local": {"status": local_result["status"], "written": True, "path": local_result["path"]},
        "obsidian": projection_state,
        "obsidian_note": obsidian_note,
        "model_record_document": model_record["document"],
        "model_record_link": model_record["link"],
        "model_switch_status": model_switch_reference_result["status"],
        "model_switch_document": model_switch_reference_result["document"],
        "model_switch_link": model_switch_reference_result["link"],
        "pending_projection_count": reconcile_result["pending"],
        "model_switch": model_switch or {"status": projection_state.get("status"), "written": projection_state.get("status") == "written", "reason": projection_state.get("reason")},
    }


def record_model_result(project_root, task_type, module, receipt_path, real_status, failure_class, *, file_value="", symbol="", code_kind="general", operation="work", modality="text", complexity="easy", complexity_score=None, risk="low", ambiguity="low", task_summary="", task_name="", task_group="", session_id="", step_kind="", capability_tags=None, entry_model="", entry_effort="", trial=False, vault=None, ladder=DEFAULT_LADDER, recorded_at=None, bound_receipt=None, local_store=None, outcome_reason="", verification_count=0, ending_attempt_number=1, prior_quality_failure_count=0, prior_operational_failure_count=0):
    shared, pairs = load_shared_ladder(ladder)
    query = _query(project_root, task_type, module, file_value, symbol, code_kind, operation, modality, complexity, complexity_score, risk, ambiguity, task_summary, step_kind, capability_tags, task_name, task_group)
    if real_status not in {"pass", "fail"} or failure_class not in FAILURE_CLASSES:
        raise ValueError("Real status or failure class is invalid")
    receipt_path = Path(receipt_path).expanduser().resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    pair = _receipt_pair(receipt)
    receipt_entry_model, receipt_entry_effort, receipt_entry_source = _receipt_entry(receipt)
    if entry_model and receipt_entry_model and (entry_model, entry_effort) != (receipt_entry_model, receipt_entry_effort):
        raise ValueError("receipt entry identity does not match the recording scope")
    resolved_entry_model = receipt_entry_model or entry_model
    resolved_entry_effort = receipt_entry_effort or entry_effort
    priority_producer = shared.get("priority_producer")
    priority_pairs = {f"{priority_producer['id']}|{effort}" for effort in priority_producer["adaptive_efforts"]} if isinstance(priority_producer, dict) and priority_producer.get("enabled") is True else set()
    if pair not in set(pairs) | priority_pairs:
        raise ValueError("receipt pair is outside the shared active producer contract")
    valid_receipt = receipt.get("status") == "pass" and receipt.get("turn_completed") is True and receipt.get("model_match") is True and receipt.get("effort_match") is True
    historical_binding = isinstance(bound_receipt, dict)
    if historical_binding:
        if bound_receipt.get("receipt_sha256") != receipt_sha256 or bound_receipt.get("model_learning_context") != receipt.get("model_learning_context") or bound_receipt.get("executed_pair") not in {None, pair}:
            raise ValueError("bound producer receipt does not match its immutable lifecycle binding")
        matched_route_attempt = next((attempt for attempt in receipt.get("route_attempts", []) if isinstance(attempt, dict) and attempt.get("status") == "pass" and attempt.get("executed_pair") == pair and attempt.get("model_match") is True and attempt.get("effort_match") is True), None)
        if receipt.get("node_type") != "locked-route-node" or receipt.get("node_role") != "result-producer" or receipt.get("result_published") is not True or not matched_route_attempt:
            raise ValueError("bound producer receipt is missing locked-route execution evidence")
    if real_status == "pass" and (failure_class != "none" or not valid_receipt):
        raise ValueError("a Real pass requires a matched passing producer receipt and failure_class=none")
    if failure_class in QUALITY_FAILURES and (real_status != "fail" or not valid_receipt):
        raise ValueError("a quality failure requires Real=fail and a matched passing producer receipt")
    if failure_class in OPERATIONAL_FAILURES and real_status != "fail":
        raise ValueError("an operational failure requires Real=fail")
    owner = query["project"].get("owner") or project_change_memory._registered_owner(query["project"]["root"])
    outcome_reason = _single_line(outcome_reason or ("Real verification passed." if real_status == "pass" else f"Real verification failed: {failure_class}."), "outcome_reason", maximum=280)
    if isinstance(verification_count, bool) or not isinstance(verification_count, int) or verification_count < 0:
        raise ValueError("verification_count must be a non-negative integer")
    for field, value in (
        ("ending_attempt_number", ending_attempt_number),
        ("prior_quality_failure_count", prior_quality_failure_count),
        ("prior_operational_failure_count", prior_operational_failure_count),
    ):
        minimum = 1 if field == "ending_attempt_number" else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{field} must be an integer >= {minimum}")
    recommendation = recommend_model(
        project_root,
        task_type,
        module,
        file_value=file_value,
        symbol=symbol,
        code_kind=code_kind,
        operation=operation,
        modality=modality,
        complexity=complexity,
        complexity_score=query["complexity_score"],
        risk=risk,
        ambiguity=ambiguity,
        task_summary=task_summary,
        task_name=task_name,
        task_group=task_group,
        session_id=session_id,
        step_kind=query["step_kind"],
        capability_tags=query["capability_tags"],
        entry_model=resolved_entry_model,
        entry_effort=resolved_entry_effort,
        vault=vault,
        ladder=ladder,
        local_store=local_store,
        session_prompt=task_summary,
        session_context=receipt.get("session_effort") if isinstance(receipt.get("session_effort"), dict) else None,
    )
    priority_attempt_pair = receipt.get("priority_attempt_pair") or receipt.get("requested_pair")
    operational_failure_pairs = receipt.get("operational_failure_pairs") if isinstance(receipt.get("operational_failure_pairs"), list) else []
    operational_failure_pairs = [value for value in operational_failure_pairs if value in set(pairs) | priority_pairs]
    if valid_receipt and not historical_binding and recommendation.get("attempt_pair") != priority_attempt_pair:
        raise ValueError("receipt attempt does not match the current dual-history recommendation")
    if valid_receipt and not historical_binding and pair not in {recommendation.get("attempt_pair"), recommendation.get("active_fallback_pair")}:
        raise ValueError("receipt result is outside the authorized priority/quality route")
    if valid_receipt and not historical_binding and pair != recommendation.get("attempt_pair") and recommendation.get("attempt_pair") not in operational_failure_pairs:
        raise ValueError("fallback receipt lacks the failed priority attempt")
    timestamp = recorded_at or datetime.now(timezone.utc)
    tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
    workload_hash = receipt.get("workload_prompt_sha256")
    if not isinstance(workload_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", workload_hash):
        workload_hash = None
    priority_failure = pair in priority_pairs and real_status == "fail" and failure_class in QUALITY_FAILURES
    quality_failure = real_status == "fail" and failure_class in QUALITY_FAILURES
    operational_failure = real_status == "fail" and failure_class in OPERATIONAL_FAILURES
    recorded_switch_direction = "upgrade" if quality_failure else "operational_fallback" if operational_failure else recommendation["switch_direction"]
    recorded_switch_reason = f"spark_verify_failure_suppresses_{query['complexity_band']}_band" if priority_failure else f"{failure_class}_failure_one_rung_up" if quality_failure else f"{failure_class}_before_result_use_contextual_pair" if operational_failure else recommendation["attempt_reason"]
    prior_pass_count = recommendation.get("pass_counts", {}).get(pair, 0)
    matched_pass_count_after = prior_pass_count + (1 if real_status == "pass" and pair in pairs else 0)
    if priority_failure:
        next_pair = recommendation["active_fallback_pair"]
        next_pair_reason = "spark_quality_failure_uses_contextual_quality_pair"
        next_pair_direction = "upgrade"
    elif quality_failure:
        pair_index = pairs.index(pair) if pair in pairs else -1
        next_pair = pairs[pair_index + 1] if 0 <= pair_index < len(pairs) - 1 else pair
        next_pair_reason = "quality_failure_one_rung_up" if next_pair != pair else "quality_boundary_exhausted"
        next_pair_direction = "upgrade" if next_pair != pair else "freeze"
    elif operational_failure:
        next_pair = recommendation["attempt_pair"] or pair
        next_pair_reason = "operational_failure_is_quality_neutral"
        next_pair_direction = "no_switch" if next_pair == pair else "operational_fallback"
    elif pair in priority_pairs:
        next_pair = pair
        next_pair_reason = "verified_priority_pair_retained"
        next_pair_direction = "freeze"
    elif recommendation.get("failed_model") and real_status == "pass":
        next_pair = pair
        next_pair_reason = "verified_recovery_pair_retained"
        next_pair_direction = "freeze"
    elif real_status == "pass" and pair in pairs and matched_pass_count_after >= MIN_REAL_PASSES_BEFORE_DOWNGRADE and pairs.index(pair) > 0:
        next_pair = pairs[pairs.index(pair) - 1]
        next_pair_reason = "two_matching_real_passes_trial_one_rung_down"
        next_pair_direction = "downgrade"
    else:
        next_pair = pair or recommendation["attempt_pair"]
        next_pair_reason = "first_real_pass_retain_collecting_evidence" if real_status == "pass" else "retain_current_boundary"
        next_pair_direction = "freeze" if next_pair == pair else "no_switch"
    ending_pass_shape = "first_attempt_pass" if real_status == "pass" and ending_attempt_number == 1 else "retry_pass" if real_status == "pass" else "failed_attempt"
    if real_status == "pass" and ending_attempt_number == 1 and next_pair_direction == "downgrade":
        model_suitability = "suitable_downgrade_candidate"
        routing_action = "trial_downgrade_one_rung_next_matching_task"
    elif real_status == "pass" and ending_attempt_number == 1:
        model_suitability = "suitable"
        routing_action = "retain_until_second_matching_first_pass"
    elif real_status == "pass" and prior_quality_failure_count:
        model_suitability = "initial_pair_too_weak_recovered"
        routing_action = "reuse_lowest_successful_recovery_pair"
    elif real_status == "pass":
        model_suitability = "suitable_after_operational_recovery"
        routing_action = "retain_quality_boundary"
    elif quality_failure:
        model_suitability = "too_weak_for_verified_result"
        routing_action = "upgrade_one_rung_for_repair"
    else:
        model_suitability = "quality_unproven_operational_failure"
        routing_action = "retry_without_quality_penalty"
    attempt_chain = []
    for attempt in receipt.get("route_attempts", []) if isinstance(receipt.get("route_attempts"), list) else []:
        if not isinstance(attempt, dict):
            continue
        attempt_pair = attempt.get("executed_pair") or attempt.get("effective_pair") or attempt.get("requested_pair")
        if attempt_pair not in set(pairs) | priority_pairs:
            continue
        attempt_chain.append({"pair": attempt_pair, "status": str(attempt.get("status") or "unknown")[:32], "failure_class": str(attempt.get("failure_class") or "none")[:32]})
        if len(attempt_chain) == 8:
            break
    session_summary = session_effort.sanitize_summary(receipt.get("session_effort"))
    session_escalation = receipt.get("session_escalation") if isinstance(receipt.get("session_escalation"), dict) else {}
    event_payload = f"{query['project']['key']}|{receipt_sha256}|{real_status}|{failure_class}"
    event_id = hashlib.sha256(event_payload.encode()).hexdigest()
    base = {
        "model_experience_schema": SCHEMA_VERSION,
        "record_id": "",
        "event_id": event_id,
        "recorded_at": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "project_name": query["project"]["name"],
        "project_key": query["project"]["key"],
        "project_owner": owner,
        "task_type": query["task_type"],
        "task_name": query.get("task_name", ""),
        "task_group": query.get("task_group", ""),
        "task_scope_key": query.get("task_scope_key", ""),
        "task_group_key": query.get("task_group_key", ""),
        "task_summary": query["task_summary"],
        "module": query["module"],
        "file": query["file"],
        "symbol": query["symbol"],
        "code_kind": query["code_kind"],
        "operation": query["operation"],
        "modality": query["modality"],
        "complexity": query["complexity"],
        "complexity_score": query["complexity_score"],
        "complexity_band": query["complexity_band"],
        "risk": query["risk"],
        "ambiguity": query["ambiguity"],
        "step_kind": query["step_kind"],
        "capability_tags": query["capability_tags"],
        "capability_fingerprint": query["capability_fingerprint"],
        "entry_model": recommendation["entry_model"],
        "entry_effort": recommendation["entry_effort"],
        "entry_pair": recommendation["entry_pair"],
        "entry_anchor_pair": recommendation["entry_anchor_pair"],
        "entry_source": receipt_entry_source if recommendation["entry_pair"] else recommendation["entry_source"],
        "model": pair.split("|", 1)[0],
        "effort": pair.split("|", 1)[1],
        "pair": pair,
        "selected_pair": receipt.get("selected_pair") or receipt.get("active_fallback_pair") or (pair if historical_binding else recommendation.get("selected_pair")),
        "prior_pair": receipt.get("prior_pair") or (recommendation.get("selected_pair") if pair in priority_pairs else recommendation.get("success_model") or recommendation.get("failed_model")),
        "attempt_pair": priority_attempt_pair,
        "active_fallback_pair": recommendation.get("active_fallback_pair"),
        "operational_failure_pairs": operational_failure_pairs,
        "real_status": real_status,
        "failure_class": failure_class,
        "outcome_reason": outcome_reason,
        "verification_count": verification_count,
        "model_evidence": "runtime_receipt",
        "learning_eligible": True,
        "observation_id": None,
        "receipt_status": "pass" if valid_receipt else "fail",
        "model_match": receipt.get("model_match") is True,
        "effort_match": receipt.get("effort_match") is True,
        "turn_completed": receipt.get("turn_completed") is True,
        "trial": receipt.get("trial") if historical_binding and isinstance(receipt.get("trial"), bool) else recommendation["attempt_trial"],
        "selection_reason": receipt.get("selection_reason") or ("bound_historical_receipt" if historical_binding else recommendation["attempt_reason"]),
        "recommendation_state": receipt.get("recommendation_state") or ("bound_historical_receipt" if historical_binding else recommendation["attempt_calibration_state"]),
        "specificity": recommendation["specificity"],
        "matched_records": recommendation["matched_records"],
        "success_pair": recommendation["success_model"],
        "failed_pair": recommendation["failed_model"],
        "workload_prompt_sha256": workload_hash,
        "total_tokens": tokens.get("total_tokens") if isinstance(tokens.get("total_tokens"), int) and tokens.get("total_tokens") >= 0 else None,
        "process_ms": receipt.get("process_elapsed_ms") if isinstance(receipt.get("process_elapsed_ms"), int) and receipt.get("process_elapsed_ms") >= 0 else None,
        "receipt_sha256": receipt_sha256,
        "switch_direction": recorded_switch_direction,
        "switch_reason": recorded_switch_reason,
        "next_pair": next_pair,
        "completed_pair": pair,
        "recovery_from_pair": recommendation.get("failed_model") if real_status == "pass" else None,
        "attempt_chain": attempt_chain,
        "ending_attempt_number": ending_attempt_number,
        "ending_pass_shape": ending_pass_shape,
        "prior_quality_failure_count": prior_quality_failure_count,
        "prior_operational_failure_count": prior_operational_failure_count,
        "matched_pass_count_after": matched_pass_count_after,
        "minimum_passes_before_downgrade": MIN_REAL_PASSES_BEFORE_DOWNGRADE,
        "next_pair_reason": next_pair_reason,
        "next_pair_direction": next_pair_direction,
        "model_suitability": model_suitability,
        "routing_action": routing_action,
        "session_key": session_summary.get("session_key", ""),
        "session_task_scope_key": session_summary.get("session_task_scope_key", ""),
        "codex_session_key": session_summary.get("codex_session_key", ""),
        "task_scope_mode": session_summary.get("task_scope_mode", "unscoped"),
        "session_state": session_summary.get("state", ""),
        "session_turn_count": session_summary.get("turn_count", 0),
        "session_prior_turn_count": session_summary.get("prior_turn_count", 0),
        "session_same_task_turns": session_summary.get("same_task_turns", 0),
        "session_user_effort": session_summary.get("user_effort", 0),
        "session_failure_recorded": session_summary.get("failure_recorded", False),
        "session_model_pair": session_summary.get("last_model_pair", ""),
        "session_model_pairs": session_summary.get("model_pairs", []),
        "session_task_fingerprint": session_summary.get("task_topic_fingerprint", ""),
        "session_project_match": session_summary.get("project_match", False),
        "session_model_source": session_summary.get("last_model_source", ""),
        "session_solving_surface": session_summary.get("solving_surface", ""),
        "session_task_length": session_summary.get("task_length", ""),
        "session_step_estimate": session_summary.get("step_estimate", 0),
        "session_step_class": session_summary.get("step_class", ""),
        "session_estimated_effort": session_summary.get("estimated_effort", ""),
        "session_information_burden": session_summary.get("information_burden", ""),
        "session_model_family": session_summary.get("model_family", ""),
        "session_model_difficulty": session_summary.get("model_difficulty", ""),
        "session_difficulty_class": session_summary.get("difficulty_class", ""),
        "session_route_class": session_summary.get("route_class", ""),
        "session_preferred_pair": session_summary.get("preferred_solving_pair", ""),
        "session_route_reason": session_summary.get("route_reason", ""),
        "session_explicit_route_hint": session_summary.get("explicit_route_hint", ""),
        "session_escalation_pair": session_escalation.get("to_pair", ""),
        "session_escalation_reason": session_escalation.get("reason", ""),
        "session_event_id": session_summary.get("session_event_id", ""),
    }
    base = _json_safe(base)
    fingerprint_payload = _json_safe({key: base[key] for key in FRONTMATTER_FIELDS if key not in {"record_id", "recorded_at"}})
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    base["record_id"] = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{fingerprint[:12]}"
    local_result = _append_local_record(base, local_store)
    stored_record = local_result["record"]
    reconcile_result = reconcile_local_model_history(project_root, vault=vault, local_store=local_store)
    states = _read_projection_state(local_store)
    projection_state = states.get(event_id, {"status": "pending", "reason": "projection_state_missing"})
    latest_projection = reconcile_result.get("latest") if isinstance(reconcile_result, dict) else None
    model_switch = latest_projection.get("model_switch") if isinstance(latest_projection, dict) else None
    vault_path, memory_root = _memory_root(query, vault)
    model_switch_reference_result = model_switch_reference(project_root, vault=vault)
    obsidian_note = _vault_relative_path(vault_path, memory_root).as_posix() if projection_state.get("status") == "written" and vault_path is not None and memory_root is not None else None
    owner = query["project"].get("owner") or project_change_memory._registered_owner(query["project"]["root"])
    model_record = _category_reference(vault_path, owner, _task_category(stored_record)) if obsidian_note and owner else {"document": None, "link": None}
    if model_switch is None and obsidian_note:
        model_switch = rebuild_model_switches(project_root, vault=vault)
    return {
        "status": "duplicate" if local_result["status"] == "duplicate" else "written",
        "written": True,
        "record_id": stored_record["record_id"],
        "event_id": stored_record["event_id"],
        "project_key": query["project"]["key"],
        "pair": stored_record["pair"],
        "real_status": stored_record["real_status"],
        "failure_class": stored_record["failure_class"],
        "outcome_reason": stored_record["outcome_reason"],
        "complexity_score": stored_record["complexity_score"],
        "complexity_band": stored_record["complexity_band"],
        "step_kind": stored_record["step_kind"],
        "capability_tags": stored_record["capability_tags"],
        "capability_fingerprint": stored_record["capability_fingerprint"],
        "entry_pair": stored_record["entry_pair"],
        "entry_anchor_pair": stored_record["entry_anchor_pair"],
        "switch_direction": stored_record["switch_direction"],
        "switch_reason": stored_record["switch_reason"],
        "next_pair": stored_record["next_pair"],
        "recovery_from_pair": stored_record["recovery_from_pair"],
        "ending_attempt_number": stored_record.get("ending_attempt_number"),
        "ending_pass_shape": stored_record.get("ending_pass_shape"),
        "prior_quality_failure_count": stored_record.get("prior_quality_failure_count"),
        "prior_operational_failure_count": stored_record.get("prior_operational_failure_count"),
        "matched_pass_count_after": stored_record.get("matched_pass_count_after"),
        "minimum_passes_before_downgrade": stored_record.get("minimum_passes_before_downgrade"),
        "next_pair_reason": stored_record.get("next_pair_reason"),
        "next_pair_direction": stored_record.get("next_pair_direction"),
        "model_suitability": stored_record.get("model_suitability"),
        "routing_action": stored_record.get("routing_action"),
        "session_state": stored_record.get("session_state"),
        "session_user_effort": stored_record.get("session_user_effort"),
        "session_failure_recorded": stored_record.get("session_failure_recorded"),
        "session_model_pair": stored_record.get("session_model_pair"),
        "session_solving_surface": stored_record.get("session_solving_surface"),
        "session_task_length": stored_record.get("session_task_length"),
        "session_step_estimate": stored_record.get("session_step_estimate"),
        "session_step_class": stored_record.get("session_step_class"),
        "session_estimated_effort": stored_record.get("session_estimated_effort"),
        "session_information_burden": stored_record.get("session_information_burden"),
        "session_model_family": stored_record.get("session_model_family"),
        "session_model_difficulty": stored_record.get("session_model_difficulty"),
        "session_difficulty_class": stored_record.get("session_difficulty_class"),
        "session_route_class": stored_record.get("session_route_class"),
        "session_preferred_pair": stored_record.get("session_preferred_pair"),
        "session_route_reason": stored_record.get("session_route_reason"),
        "session_escalation_pair": stored_record.get("session_escalation_pair"),
        "session_event_id": stored_record.get("session_event_id"),
        "shared_model_registry": shared["registry_id"],
        "local": {"status": local_result["status"], "written": True, "path": local_result["path"]},
        "obsidian": projection_state,
        "obsidian_note": obsidian_note,
        "model_record_document": model_record["document"],
        "model_record_link": model_record["link"],
        "model_switch_status": model_switch_reference_result["status"],
        "model_switch_document": model_switch_reference_result["document"],
        "model_switch_link": model_switch_reference_result["link"],
        "pending_projection_count": reconcile_result["pending"],
        "model_switch": model_switch or {"status": projection_state.get("status"), "written": projection_state.get("status") == "written", "reason": projection_state.get("reason")},
    }


def memory_status(project_root=None, *, vault=None, ladder=DEFAULT_LADDER, local_store=None):
    shared, pairs = load_shared_ladder(ladder)
    vault_path = project_change_memory._resolve_vault(vault)
    local_path = _resolve_local_store(local_store)
    local_records = _read_local_records(local_path)
    priority_producer = shared.get("priority_producer")
    priority_pair_count = len(priority_producer["adaptive_efforts"]) if isinstance(priority_producer, dict) and priority_producer.get("enabled") is True else 0
    output = {"status": "ready", "authority": "dual_local_and_obsidian", "shared_model_registry": shared["registry_id"], "active_pairs": len(pairs) + priority_pair_count, "active_quality_pairs": len(pairs), "priority_attempt_pairs": priority_pair_count, "priority_producer": priority_producer.get("id") if isinstance(priority_producer, dict) else None, "local_store": str(local_path), "local_records": len(local_records), "vault": str(vault_path) if vault_path else "", "obsidian_available": vault_path is not None}
    if project_root:
        project = project_change_memory._project_identity(project_root)
        model_switch = model_switch_reference(project_root, vault=vault)
        owner = project.get("owner") or project_change_memory._registered_owner(project["root"])
        _, page = _memory_root(_query(project_root, "script", "general"), vault)
        memory_available = _is_configured_owner(vault_path, owner)
        if owner is None:
            reason = "unregistered_or_unknown_project_root"
        elif page is None:
            reason = "unconfigured_project_root"
        elif not page.exists():
            reason = "configured_model_switch_missing"
        else:
            reason = None
        output.update(
            {
                "project_key": project["key"],
                "memory_available": memory_available,
                "model_switch_owner": owner,
                "records": 0 if page is None else len(_read_project_records(page)),
                "project_local_records": len([record for record in local_records if project_change_memory._record_matches_project(record, project)]),
                "pending_projection_count": _pending_projection_count(local_path, project),
                "page": "" if page is None else _vault_relative_path(vault_path, page).as_posix(),
                "model_switch_status": model_switch["status"],
                "model_switch_document": model_switch["document"],
                "model_switch_link": model_switch["link"],
                "reason": reason,
            }
        )
    return output


def _add_scope_arguments(parser, *, summary_required=False):
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--file", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--code-kind", default="general")
    parser.add_argument("--operation", default="work")
    parser.add_argument("--modality", choices=sorted(MODALITY_VALUES), default="text")
    parser.add_argument("--complexity", choices=sorted(COMPLEXITY_VALUES), default="easy")
    parser.add_argument("--complexity-score", type=int)
    parser.add_argument("--risk", choices=sorted(LEVEL_VALUES), default="low")
    parser.add_argument("--ambiguity", choices=sorted(LEVEL_VALUES), default="low")
    parser.add_argument("--task-summary", required=summary_required, default="")
    parser.add_argument("--task-name", default=os.environ.get("CODEX_TASK_NAME", ""))
    parser.add_argument("--task-group", default=os.environ.get("CODEX_TASK_GROUP", ""))
    parser.add_argument("--session-id", default=os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID", ""))
    parser.add_argument("--step-kind", choices=sorted(STEP_KINDS), default="")
    parser.add_argument("--capability-tag", action="append", default=[])
    parser.add_argument("--entry-model", default="")
    parser.add_argument("--entry-effort", default="")


def _compact_recommendation(recommendation):
    keys = ("selection_basis", "project_key", "complexity_score", "complexity_band", "step_kind", "capability_fingerprint", "specificity", "matched_records", "entry_pair", "selected_pair", "attempt_pair", "active_fallback_pair", "attempt_trial", "attempt_reason", "attempt_calibration_state", "switch_direction", "model_switch_document", "model_switch_link", "route_capsule")
    return {"status": "blocked" if recommendation.get("attempt_calibration_state") == "blocked" else "ready", **{key: recommendation.get(key) for key in keys}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Read and write dual local and Obsidian adaptive model memory")
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--ladder", type=Path, default=DEFAULT_LADDER)
    parser.add_argument("--local-store", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    recommend = commands.add_parser("recommend")
    _add_scope_arguments(recommend)
    recommend.add_argument("--compact", action="store_true")
    record = commands.add_parser("record")
    _add_scope_arguments(record, summary_required=True)
    record.add_argument("--receipt", type=Path, required=True)
    record.add_argument("--real-status", choices=("pass", "fail"), required=True)
    record.add_argument("--failure-class", choices=sorted(FAILURE_CLASSES), default="none")
    record.add_argument("--trial", action="store_true")
    record.add_argument("--outcome-reason", default="")
    record.add_argument("--verification-count", type=int, default=0)
    status = commands.add_parser("status")
    status.add_argument("--project-root", type=Path)
    relink = commands.add_parser("relink")
    relink.add_argument("--project-root", type=Path, required=True)
    rebuild = commands.add_parser("rebuild-model-switches")
    rebuild.add_argument("--project-root", type=Path, required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--project-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    common = {"vault": args.vault, "ladder": args.ladder, "local_store": args.local_store}
    if args.command == "status":
        output = memory_status(args.project_root, **common)
    elif args.command == "relink":
        output = relink_project(args.project_root, vault=args.vault)
    elif args.command == "rebuild-model-switches":
        output = rebuild_model_switches(args.project_root, vault=args.vault)
    elif args.command == "reconcile":
        output = reconcile_local_model_history(args.project_root, vault=args.vault, local_store=args.local_store)
    else:
        scope = {"file_value": args.file, "symbol": args.symbol, "code_kind": args.code_kind, "operation": args.operation, "modality": args.modality, "complexity": args.complexity, "complexity_score": args.complexity_score, "risk": args.risk, "ambiguity": args.ambiguity, "task_summary": args.task_summary, "task_name": args.task_name, "task_group": args.task_group, "session_id": args.session_id, "step_kind": args.step_kind, "capability_tags": args.capability_tag, "entry_model": args.entry_model, "entry_effort": args.entry_effort, **common}
        if args.command == "recommend":
            output = recommend_model(args.project_root, args.task_type, args.module, **scope)
            if args.compact:
                output = _compact_recommendation(output)
        else:
            output = record_model_result(args.project_root, args.task_type, args.module, args.receipt, args.real_status, args.failure_class, trial=args.trial, outcome_reason=args.outcome_reason, verification_count=args.verification_count, **scope)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0 if output.get("status") not in {"unavailable"} and output.get("calibration_state") != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
