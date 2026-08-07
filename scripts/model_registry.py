#!/usr/bin/env python3
"""Build and maintain the shared model registry from the Codex model catalog."""

import hashlib
import json
import os
import re
import string
import tempfile
from pathlib import Path


REGISTRY_SCHEMA_VERSION = 2
REGISTRY_ID = "dynamic-codex-model-capability-ladder"
CANONICAL_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_MODELS_CACHE_PATH = Path.home() / ".codex" / "models_cache.json"
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "assets" / "model-capability-ladder.json"
TASK_TYPES = ("question", "summary", "spreadsheet", "document", "code", "debug", "integration", "prompt", "script", "visual", "normal-script-update", "code-design", "finding-bugs", "documentation-instructions")
PRIORITY_PRODUCER_TASK_TYPES = ("question", "summary", "document", "prompt", "code", "debug", "integration", "script", "normal-script-update", "code-design", "finding-bugs", "documentation-instructions")
SMALL_EDIT_TASK_TYPES = ("document", "prompt", "code", "script", "normal-script-update", "documentation-instructions")
SMALL_EDIT_OPERATIONS = ("edit", "fix", "modify", "rename", "replace", "update", "write")
PRIORITY_PRODUCER_OPERATIONS = ("answer", "command", "edit", "execute", "fix", "modify", "rename", "replace", "respond", "transform", "update", "work", "write")
TASK_SEGMENT_PURPOSES = ("author-probe", "command", "edit", "execute", "implement", "transform", "write")
MODEL_SWITCH_CATEGORIES = ("normal-script-update", "code-design", "finding-bugs", "tests-verification", "documentation-instructions", "general-work")
COMPLEXITY_BANDS = ({"id": "small", "minimum": 0, "maximum": 24}, {"id": "standard", "minimum": 25, "maximum": 49}, {"id": "complex", "minimum": 50, "maximum": 74}, {"id": "advanced", "minimum": 75, "maximum": 100})
NUMERIC_GPT_FAMILY_PATTERN = re.compile(r"^gpt-(\d+(?:\.\d+)*)(?:-|$)", re.IGNORECASE)
SEMANTIC_MODEL_FIELDS = ("slug", "display_name", "description", "default_reasoning_level", "visibility", "supported_in_api", "priority", "additional_speed_tiers", "input_modalities", "context_window")


def _selected_catalog_models(catalog):
    visible_models = [
        model
        for model in catalog["models"]
        if isinstance(model, dict)
        and model.get("visibility") == "list"
        and isinstance(model.get("supported_reasoning_levels"), list)
        and model["supported_reasoning_levels"]
        and isinstance(model.get("slug"), str)
        and model.get("slug")
        and not isinstance(model.get("priority"), bool)
        and isinstance(model.get("priority"), (int, float))
    ]
    priority_model = _select_priority_producer(visible_models)
    quality_candidates = [model for model in visible_models if model is not priority_model]
    family_candidates = [(model, parse_numeric_gpt_family(model["slug"])) for model in quality_candidates]
    family_candidates = [(model, family) for model, family in family_candidates if family is not None]
    if not family_candidates:
        return visible_models
    _, highest_numeric_version = max((family for _, family in family_candidates), key=lambda family: family[1])
    active_ids = {model["slug"] for model, family in family_candidates if family[1] == highest_numeric_version}
    return [model for model in visible_models if model is priority_model or model["slug"] in active_ids]


def semantic_catalog_sha256(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("client_version"), str) or not isinstance(catalog.get("models"), list):
        raise ValueError("Codex model catalog is incomplete")
    semantic_models = []
    for model in _selected_catalog_models(catalog):
        if not isinstance(model, dict):
            semantic_models.append(model)
            continue
        supported_levels = model.get("supported_reasoning_levels")
        supported_efforts = [level.get("effort") if isinstance(level, dict) else None for level in supported_levels] if isinstance(supported_levels, list) else supported_levels
        semantic_models.append({**{field: model.get(field) for field in SEMANTIC_MODEL_FIELDS}, "supported_reasoning_efforts": supported_efforts})
    semantic_models.sort(key=lambda model: json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    semantic_catalog = {"client_version": catalog["client_version"], "models": semantic_models}
    canonical_bytes = json.dumps(semantic_catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def load_catalog(models_cache_path=DEFAULT_MODELS_CACHE_PATH):
    models_cache_path = Path(models_cache_path).expanduser().resolve()
    catalog_bytes = models_cache_path.read_bytes()
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("client_version"), str) or not isinstance(catalog.get("fetched_at"), str) or not isinstance(catalog.get("models"), list):
        raise ValueError("Codex model catalog is incomplete")
    return catalog, semantic_catalog_sha256(catalog)


def _supported_efforts(model, effort_order):
    supported_levels = model.get("supported_reasoning_levels")
    if not isinstance(supported_levels, list) or not supported_levels:
        return []
    efforts = []
    for level in supported_levels:
        effort = level.get("effort") if isinstance(level, dict) else None
        if isinstance(effort, str) and effort and effort not in efforts:
            efforts.append(effort)
    return [effort for effort in effort_order if effort in efforts]


def parse_numeric_gpt_family(slug):
    if not isinstance(slug, str):
        return None
    match = NUMERIC_GPT_FAMILY_PATTERN.match(slug.strip())
    if match is None:
        return None
    numeric_version = tuple(int(part) for part in match.group(1).split("."))
    while len(numeric_version) > 1 and numeric_version[-1] == 0:
        numeric_version = numeric_version[:-1]
    return f"gpt-{'.'.join(str(part) for part in numeric_version)}", numeric_version


def _catalog_effort_order(models):
    observed = []
    for model in models:
        for level in model.get("supported_reasoning_levels", []):
            effort = level.get("effort") if isinstance(level, dict) else None
            if isinstance(effort, str) and effort and effort not in observed:
                observed.append(effort)
    canonical = [effort for effort in CANONICAL_EFFORT_ORDER if effort in observed]
    return canonical + sorted(effort for effort in observed if effort not in CANONICAL_EFFORT_ORDER)


def _is_priority_producer(model):
    modalities = model.get("input_modalities")
    if modalities != ["text"]:
        return False
    slug_and_name = f"{model.get('slug', '')} {model.get('display_name', '')}".lower()
    positioning = f"{slug_and_name} {model.get('description', '')}".lower()
    return "spark" in slug_and_name or ("fast" in positioning and ("coding" in positioning or "code" in positioning))


def _select_priority_producer(models):
    candidates = [model for model in models if _is_priority_producer(model)]
    return min(candidates, key=lambda model: (model["priority"], model["slug"])) if candidates else None


def _select_role_models(quality_models):
    balanced_signals = ("balanced", "everyday", "general-purpose", "general purpose")
    balanced_candidates = [model for model in quality_models if any(signal in f"{model.get('slug', '')} {model.get('display_name', '')} {model.get('description', '')}".lower() for signal in balanced_signals)]
    weak = quality_models[0]
    balanced = balanced_candidates[-1] if balanced_candidates else quality_models[len(quality_models) // 2]
    frontier = quality_models[-1]
    return {"weak": weak["slug"], "balanced": balanced["slug"], "frontier": frontier["slug"]}


def _preferred_effort(efforts, preferred):
    return preferred if preferred in efforts else efforts[-1]


def _model_metadata(model, effort_order):
    efforts = _supported_efforts(model, effort_order)
    default_effort = model.get("default_reasoning_level")
    if default_effort not in efforts:
        default_effort = _preferred_effort(efforts, "medium")
    return {"id": model["slug"], "display_name": model.get("display_name", model["slug"]), "provider_priority": model["priority"], "provider_positioning": model.get("description", ""), "supported_in_api": bool(model.get("supported_in_api")), "input_modalities": list(model.get("input_modalities", [])), "context_window": model.get("context_window"), "codex_efforts": efforts, "default_effort": default_effort, "additional_speed_tiers": list(model.get("additional_speed_tiers", []))}


def _model_row(model, rank, effort_order, role_models):
    metadata = _model_metadata(model, effort_order)
    roles = [role for role, slug in role_models.items() if slug == model["slug"]]
    return {**metadata, "capability_rank": rank, "capability_roles": roles}


def _priority_producer_row(model, effort_order):
    if model is None:
        return None
    efforts = _supported_efforts(model, effort_order)
    easy_effort = "low" if "low" in efforts else efforts[0]
    complex_effort = "high" if "high" in efforts else efforts[-1]
    return {"enabled": True, "id": model["slug"], "display_name": model.get("display_name", model["slug"]), "routing_role": "bounded_task_segment_or_small_edit_priority", "provider_priority": model["priority"], "provider_positioning": model.get("description", ""), "supported_in_api": bool(model.get("supported_in_api")), "input_modalities": list(model.get("input_modalities", [])), "context_window": model.get("context_window"), "codex_efforts": efforts, "adaptive_efforts": list(dict.fromkeys((easy_effort, complex_effort))), "effort_by_complexity": {"easy": easy_effort, "complex": complex_effort}, "eligible_task_types": list(PRIORITY_PRODUCER_TASK_TYPES), "eligible_modalities": ["text"], "eligible_operations": list(PRIORITY_PRODUCER_OPERATIONS), "small_edit_task_types": list(SMALL_EDIT_TASK_TYPES), "small_edit_operations": list(SMALL_EDIT_OPERATIONS), "small_edit_maximum_complexity_score": 24, "task_segment_purposes": list(TASK_SEGMENT_PURPOSES), "task_segment_maximum_complexity_score": 24, "excluded_operations": ["audit", "lookup", "read", "review", "status", "verify"], "operational_fallback": "contextual_quality_pair", "quality_failure": "suppress_matching_complexity_band_and_upgrade"}


def build_registry(catalog, catalog_sha256=None):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("client_version"), str) or not isinstance(catalog.get("fetched_at"), str) or not isinstance(catalog.get("models"), list):
        raise ValueError("Codex model catalog is incomplete")
    if catalog_sha256 is None:
        catalog_sha256 = semantic_catalog_sha256(catalog)
    visible_models = []
    for model in catalog["models"]:
        if not isinstance(model, dict) or model.get("visibility") != "list" or not isinstance(model.get("supported_reasoning_levels"), list) or not model["supported_reasoning_levels"]:
            continue
        if not isinstance(model.get("slug"), str) or not model["slug"] or isinstance(model.get("priority"), bool) or not isinstance(model.get("priority"), (int, float)):
            raise ValueError("visible Codex models require a slug and numeric provider priority")
        visible_models.append(model)
    if not visible_models:
        raise ValueError("Codex model catalog has no visible reasoning models")
    effort_order = _catalog_effort_order(visible_models)
    if not effort_order or any(not _supported_efforts(model, effort_order) for model in visible_models):
        raise ValueError("visible Codex models require named supported reasoning levels")
    slugs = [model["slug"] for model in visible_models]
    if len(slugs) != len(set(slugs)):
        raise ValueError("visible Codex model slugs must be unique")
    priority_model = _select_priority_producer(visible_models)
    quality_candidates = [model for model in visible_models if model is not priority_model]
    family_candidates = [(model, parse_numeric_gpt_family(model["slug"])) for model in quality_candidates]
    family_candidates = [(model, family) for model, family in family_candidates if family is not None]
    if not family_candidates:
        raise ValueError("Codex model catalog has no numeric GPT quality family")
    active_family_id, active_numeric_version = max((family for _, family in family_candidates), key=lambda family: family[1])
    quality_models = sorted([model for model, family in family_candidates if family[1] == active_numeric_version], key=lambda model: (-model["priority"], model["slug"]))
    role_models = _select_role_models(quality_models)
    model_rows = [_model_row(model, rank, effort_order, role_models) for rank, model in enumerate(quality_models, start=1)]
    active_model_ids = {model["id"] for model in model_rows}
    catalog_models = []
    # Only the active quality family and the priority producer are retained, so a
    # background catalog refresh that reintroduces an older family cannot leak it into
    # the registry or the semantic digest. validate_registry still accepts a
    # `catalog_only` role for forward compatibility; nothing emits it today.
    selected_catalog_ids = {model["slug"] for model in visible_models if model is priority_model or model["slug"] in active_model_ids}
    for model in sorted(visible_models, key=lambda model: (model["priority"], model["slug"])):
        if model["slug"] not in selected_catalog_ids:
            continue
        catalog_role = "priority_producer" if model is priority_model else "active_quality"
        catalog_models.append({**_model_metadata(model, effort_order), "catalog_role": catalog_role})
    models_by_id = {model["id"]: model for model in model_rows}
    weak = models_by_id[role_models["weak"]]
    balanced = models_by_id[role_models["balanced"]]
    frontier = models_by_id[role_models["frontier"]]
    floor_pair = f"{weak['id']}|{weak['codex_efforts'][0]}"
    weak_default_pair = f"{weak['id']}|{weak['default_effort']}"
    balanced_default_pair = f"{balanced['id']}|{balanced['default_effort']}"
    balanced_complex_pair = f"{balanced['id']}|{_preferred_effort(balanced['codex_efforts'], 'high')}"
    frontier_complex_pair = f"{frontier['id']}|{_preferred_effort(frontier['codex_efforts'], 'high')}"
    cold_starts = {"question": {"easy": floor_pair, "complex": balanced_default_pair}, "summary": {"easy": floor_pair, "complex": balanced_default_pair}, "spreadsheet": {"easy": balanced_default_pair, "complex": balanced_complex_pair}, "document": {"easy": weak_default_pair, "complex": balanced_complex_pair}, "code": {"easy": balanced_default_pair, "complex": balanced_complex_pair}, "debug": {"easy": balanced_default_pair, "complex": frontier_complex_pair}, "integration": {"easy": balanced_complex_pair, "complex": frontier_complex_pair}, "prompt": {"easy": balanced_default_pair, "complex": frontier_complex_pair}, "visual": {"easy": balanced_default_pair, "complex": frontier_complex_pair}}
    cold_starts["script"] = cold_starts["code"]
    cold_starts["normal-script-update"] = cold_starts["code"]
    cold_starts["code-design"] = cold_starts["code"]
    cold_starts["finding-bugs"] = cold_starts["debug"]
    cold_starts["documentation-instructions"] = cold_starts["document"]
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "registry_id": REGISTRY_ID, "scope": "shared_non_personal", "source": {"models_cache": "~/.codex/models_cache.json", "client_version": catalog["client_version"], "fetched_at": catalog["fetched_at"], "catalog_sha256": catalog_sha256}, "active_family": {"id": active_family_id, "numeric_version": list(active_numeric_version), "selection": "highest_numeric_gpt_family", "model_count": len(model_rows)}, "catalog_models": catalog_models, "ladder_direction": "weakest_to_strongest", "effort_order": effort_order, "complexity_scale": {"minimum": 0, "maximum": 100, "bands": list(COMPLEXITY_BANDS), "quality_complex_threshold": 50}, "role_models": role_models, "role_pairs": {"floor": floor_pair, "weak_default": weak_default_pair, "balanced_default": balanced_default_pair, "balanced_complex": balanced_complex_pair, "frontier_complex": frontier_complex_pair}, "policy": {"enabled": True, "quality_first": True, "downgrade_after_repeated_real_passes": True, "minimum_real_passes_before_downgrade": 2, "upgrade_after_quality_failure": True, "operational_failures_are_neutral": True, "freeze_lowest_verified_pair": True, "priority_producer_first_text_code": priority_model is not None, "priority_producer_first_small_edits": priority_model is not None, "priority_producer_task_segments": priority_model is not None, "priority_producer_scheduled_sources": priority_model is not None, "priority_producer_scheduled_sources_only": False, "minimum_pair": floor_pair}, "priority_producer": _priority_producer_row(priority_model, effort_order), "private_learning_contract": {"authority": "dual_local_and_obsidian", "local_path_template": "~/.codex/model-routing-memory/events.jsonl", "projection_path_template": "Model Switch.md", "record_path_template": "<owner-routing>/<Category>.md", "native_wikilink_graph": True, "stable_categories": list(MODEL_SWITCH_CATEGORIES), "event_id_dedupe": True, "specificity_order": ["project_task", "module", "file", "symbol"], "fields_only": True, "hierarchy_notes": False, "legacy_local_json": "read_only_inactive"}, "default_cold_start": balanced_default_pair, "cold_start_defaults": cold_starts, "models": model_rows}


def validate_registry(registry):
    if not isinstance(registry, dict) or registry.get("schema_version") != REGISTRY_SCHEMA_VERSION or registry.get("registry_id") != REGISTRY_ID or registry.get("scope") != "shared_non_personal" or registry.get("ladder_direction") != "weakest_to_strongest":
        raise ValueError("model registry header is invalid")
    source = registry.get("source")
    if not isinstance(source, dict) or source.get("models_cache") != "~/.codex/models_cache.json" or not isinstance(source.get("client_version"), str) or not isinstance(source.get("fetched_at"), str) or not isinstance(source.get("catalog_sha256"), str) or len(source["catalog_sha256"]) != 64 or any(character not in string.hexdigits for character in source["catalog_sha256"]):
        raise ValueError("model registry source metadata is invalid")
    effort_order = registry.get("effort_order")
    models = registry.get("models")
    if not isinstance(effort_order, list) or not effort_order or len(effort_order) != len(set(effort_order)) or not isinstance(models, list) or not models:
        raise ValueError("model registry ladder is incomplete")
    model_ids = []
    valid_pairs = []
    provider_priorities = []
    for rank, model in enumerate(models, start=1):
        if not isinstance(model, dict) or model.get("capability_rank") != rank or not isinstance(model.get("id"), str) or model["id"] in model_ids or isinstance(model.get("provider_priority"), bool) or not isinstance(model.get("provider_priority"), (int, float)):
            raise ValueError("model registry ranks and model ids must be unique and contiguous")
        efforts = model.get("codex_efforts")
        if not isinstance(efforts, list) or not efforts or efforts != [effort for effort in effort_order if effort in efforts]:
            raise ValueError(f"model registry efforts are invalid for {model.get('id')}")
        model_ids.append(model["id"])
        provider_priorities.append(model["provider_priority"])
        valid_pairs.extend(f"{model['id']}|{effort}" for effort in efforts)
    if provider_priorities != sorted(provider_priorities, reverse=True):
        raise ValueError("model registry provider priorities must be weakest-to-strongest")
    active_family = registry.get("active_family")
    if not isinstance(active_family, dict) or active_family.get("selection") != "highest_numeric_gpt_family" or active_family.get("model_count") != len(models) or not isinstance(active_family.get("numeric_version"), list) or not active_family["numeric_version"] or any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in active_family["numeric_version"]):
        raise ValueError("model registry active family metadata is invalid")
    active_numeric_version = tuple(active_family["numeric_version"])
    expected_family_id = f"gpt-{'.'.join(str(part) for part in active_numeric_version)}"
    if active_family.get("id") != expected_family_id or any(parse_numeric_gpt_family(model_id) != (expected_family_id, active_numeric_version) for model_id in model_ids):
        raise ValueError("model registry active models do not match the active family")
    catalog_models = registry.get("catalog_models")
    if not isinstance(catalog_models, list) or not catalog_models:
        raise ValueError("model registry catalog metadata is missing")
    catalog_ids = []
    catalog_priorities = []
    for model in catalog_models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or model["id"] in catalog_ids or model.get("catalog_role") not in {"active_quality", "priority_producer", "catalog_only"} or isinstance(model.get("provider_priority"), bool) or not isinstance(model.get("provider_priority"), (int, float)):
            raise ValueError("model registry catalog metadata is invalid")
        efforts = model.get("codex_efforts")
        if not isinstance(efforts, list) or not efforts or efforts != [effort for effort in effort_order if effort in efforts]:
            raise ValueError(f"model registry catalog efforts are invalid for {model.get('id')}")
        catalog_ids.append(model["id"])
        catalog_priorities.append(model["provider_priority"])
    if catalog_priorities != sorted(catalog_priorities):
        raise ValueError("model registry catalog metadata must retain provider order")
    role_models = registry.get("role_models")
    if not isinstance(role_models, dict) or set(role_models) != {"weak", "balanced", "frontier"} or any(model not in model_ids for model in role_models.values()) or role_models["weak"] != model_ids[0] or role_models["frontier"] != model_ids[-1]:
        raise ValueError("model registry role assignments are invalid")
    complexity_scale = registry.get("complexity_scale")
    expected_bands = list(COMPLEXITY_BANDS)
    if not isinstance(complexity_scale, dict) or complexity_scale.get("minimum") != 0 or complexity_scale.get("maximum") != 100 or complexity_scale.get("quality_complex_threshold") != 50 or complexity_scale.get("bands") != expected_bands:
        raise ValueError("model registry complexity scale is invalid")
    role_pairs = registry.get("role_pairs")
    if not isinstance(role_pairs, dict) or set(role_pairs) != {"floor", "weak_default", "balanced_default", "balanced_complex", "frontier_complex"} or any(pair not in valid_pairs for pair in role_pairs.values()) or role_pairs["floor"] != valid_pairs[0]:
        raise ValueError("model registry role pairs are invalid")
    policy = registry.get("policy")
    if not isinstance(policy, dict) or policy.get("enabled") is not True or policy.get("minimum_pair") != valid_pairs[0]:
        raise ValueError("model registry adaptive policy is invalid")
    if registry.get("default_cold_start") not in valid_pairs:
        raise ValueError("model registry default cold start is invalid")
    cold_starts = registry.get("cold_start_defaults")
    if not isinstance(cold_starts, dict) or set(cold_starts) != set(TASK_TYPES) or any(not isinstance(levels, dict) or set(levels) != {"easy", "complex"} or any(pair not in valid_pairs for pair in levels.values()) for levels in cold_starts.values()):
        raise ValueError("model registry cold starts are invalid")
    priority_producer = registry.get("priority_producer")
    if priority_producer is not None:
        if not isinstance(priority_producer, dict) or priority_producer.get("id") in model_ids or priority_producer.get("input_modalities") != ["text"] or priority_producer.get("routing_role") != "bounded_task_segment_or_small_edit_priority":
            raise ValueError("model registry priority producer is invalid")
        efforts = priority_producer.get("codex_efforts")
        if not isinstance(efforts, list) or not efforts or efforts != [effort for effort in effort_order if effort in efforts]:
            raise ValueError("model registry priority producer efforts are invalid")
        effort_by_complexity = priority_producer.get("effort_by_complexity")
        if priority_producer.get("enabled") is not True or not isinstance(effort_by_complexity, dict) or set(effort_by_complexity) != {"easy", "complex"} or any(effort not in efforts for effort in effort_by_complexity.values()):
            raise ValueError("model registry priority producer policy is invalid")
        if priority_producer.get("eligible_task_types") != list(PRIORITY_PRODUCER_TASK_TYPES) or priority_producer.get("eligible_operations") != list(PRIORITY_PRODUCER_OPERATIONS):
            raise ValueError("model registry bounded text/code producer policy is invalid")
        if priority_producer.get("small_edit_task_types") != list(SMALL_EDIT_TASK_TYPES) or priority_producer.get("small_edit_operations") != list(SMALL_EDIT_OPERATIONS) or priority_producer.get("small_edit_maximum_complexity_score") != 24:
            raise ValueError("model registry small-edit producer policy is invalid")
        if priority_producer.get("task_segment_purposes") != list(TASK_SEGMENT_PURPOSES) or priority_producer.get("task_segment_maximum_complexity_score") != 24:
            raise ValueError("model registry task-segment producer policy is invalid")
    if bool(priority_producer) != bool(policy.get("priority_producer_first_text_code")) or policy.get("priority_producer_scheduled_sources_only") is not False or bool(priority_producer) != bool(policy.get("priority_producer_first_small_edits")) or bool(priority_producer) != bool(policy.get("priority_producer_task_segments")) or bool(priority_producer) != bool(policy.get("priority_producer_scheduled_sources")):
        raise ValueError("model registry priority producer admission is inconsistent")
    if policy.get("downgrade_after_repeated_real_passes") is not True or policy.get("minimum_real_passes_before_downgrade") != 2 or policy.get("upgrade_after_quality_failure") is not True:
        raise ValueError("model registry adaptive learning policy is invalid")
    priority_producer_id = priority_producer.get("id") if isinstance(priority_producer, dict) else None
    if priority_producer_id is not None and priority_producer_id not in catalog_ids:
        raise ValueError("model registry priority producer is missing from catalog metadata")
    quality_catalog_models = [model for model in catalog_models if model["id"] != priority_producer_id]
    numeric_catalog_families = [(model, parse_numeric_gpt_family(model["id"])) for model in quality_catalog_models]
    numeric_catalog_families = [(model, family) for model, family in numeric_catalog_families if family is not None]
    if not numeric_catalog_families:
        raise ValueError("model registry catalog has no numeric GPT quality family")
    highest_family_id, highest_numeric_version = max((family for _, family in numeric_catalog_families), key=lambda family: family[1])
    expected_active_ids = {model["id"] for model, family in numeric_catalog_families if family[1] == highest_numeric_version}
    expected_roles = {model["id"]: "priority_producer" if model["id"] == priority_producer_id else "active_quality" if model["id"] in expected_active_ids else "catalog_only" for model in catalog_models}
    if (highest_family_id, highest_numeric_version) != (expected_family_id, active_numeric_version) or set(model_ids) != expected_active_ids or any(model["catalog_role"] != expected_roles[model["id"]] for model in catalog_models):
        raise ValueError("model registry active family does not match the highest catalog family")
    private_contract = registry.get("private_learning_contract")
    if not isinstance(private_contract, dict) or private_contract.get("authority") != "dual_local_and_obsidian" or private_contract.get("local_path_template") != "~/.codex/model-routing-memory/events.jsonl" or private_contract.get("projection_path_template") != "Model Switch.md" or private_contract.get("record_path_template") != "<owner-routing>/<Category>.md" or private_contract.get("native_wikilink_graph") is not True or private_contract.get("stable_categories") != list(MODEL_SWITCH_CATEGORIES) or private_contract.get("event_id_dedupe") is not True or private_contract.get("specificity_order") != ["project_task", "module", "file", "symbol"] or private_contract.get("fields_only") is not True or private_contract.get("hierarchy_notes") is not False or private_contract.get("legacy_local_json") != "read_only_inactive":
        raise ValueError("model registry private learning contract is invalid")
    return registry


def is_valid_registry(registry):
    try:
        validate_registry(registry)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _registry_without_fetched_at(registry):
    source = {key: value for key, value in registry["source"].items() if key != "fetched_at"}
    return {**registry, "source": source}


def registry_matches_catalog(registry, catalog_sha256, desired_registry=None):
    if not is_valid_registry(registry) or registry["source"]["catalog_sha256"] != catalog_sha256:
        return False
    if desired_registry is None:
        return True
    return is_valid_registry(desired_registry) and desired_registry["source"]["catalog_sha256"] == catalog_sha256 and _registry_without_fetched_at(registry) == _registry_without_fetched_at(desired_registry)


def load_registry(registry_path=DEFAULT_REGISTRY_PATH):
    registry_path = Path(registry_path).expanduser().resolve()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return validate_registry(registry)


def atomic_write_registry(registry_path, registry):
    registry_path = Path(registry_path).expanduser().resolve()
    validate_registry(registry)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{registry_path.name}.", suffix=".tmp", dir=registry_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, registry_path.stat().st_mode & 0o777 if registry_path.exists() else 0o644)
        os.replace(temporary_path, registry_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def ensure_registry(registry_path=DEFAULT_REGISTRY_PATH, models_cache_path=DEFAULT_MODELS_CACHE_PATH):
    registry_path = Path(registry_path).expanduser().resolve()
    if registry_path.exists():
        return {"status": "loaded", "registry": load_registry(registry_path)}
    try:
        catalog, catalog_sha256 = load_catalog(models_cache_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"Codex model catalog is unavailable and the saved model registry is missing: {error}") from error
    registry = build_registry(catalog, catalog_sha256)
    atomic_write_registry(registry_path, registry)
    return {"status": "generated", "registry": registry}


def refresh_registry(registry_path=DEFAULT_REGISTRY_PATH, models_cache_path=DEFAULT_MODELS_CACHE_PATH):
    registry_path = Path(registry_path).expanduser().resolve()
    existing_registry = None
    if registry_path.exists():
        try:
            existing_registry = load_registry(registry_path)
        except (KeyError, OSError, TypeError, json.JSONDecodeError, ValueError):
            existing_registry = None
    try:
        catalog, catalog_sha256 = load_catalog(models_cache_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if existing_registry is not None:
            return {"status": "retained", "registry": existing_registry, "catalog_error": str(error)}
        raise RuntimeError(f"Codex model catalog is unavailable and no valid model registry exists: {error}") from error
    desired_registry = build_registry(catalog, catalog_sha256)
    if registry_matches_catalog(existing_registry, catalog_sha256, desired_registry):
        return {"status": "current", "registry": existing_registry}
    status = "refreshed" if registry_path.exists() else "generated"
    atomic_write_registry(registry_path, desired_registry)
    return {"status": status, "registry": desired_registry}
