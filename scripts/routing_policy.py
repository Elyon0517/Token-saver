#!/usr/bin/env python3
"""Load the shared model ladder and expose deterministic routing helpers."""

import importlib.util
from pathlib import Path

# Repo root. Reference documents and the shared ladder ship with the engine, so they
# resolve from here rather than from any deployed skills directory a caller passes in.
ENGINE_ROOT = Path(__file__).resolve().parents[1]
MODEL_CAPABILITY_CONFIG_PATH = ENGINE_ROOT / "assets" / "model-capability-ladder.json"
MODEL_REGISTRY_SCRIPT_PATH = Path(__file__).resolve().with_name("model_registry.py")
_MODEL_REGISTRY_SPEC = importlib.util.spec_from_file_location("task_analyze_model_registry", MODEL_REGISTRY_SCRIPT_PATH)
_MODEL_REGISTRY = importlib.util.module_from_spec(_MODEL_REGISTRY_SPEC)
_MODEL_REGISTRY_SPEC.loader.exec_module(_MODEL_REGISTRY)

_LOCAL_PRODUCER_SPEC = importlib.util.spec_from_file_location("token_saver_local_producer", Path(__file__).resolve().with_name("local_producer.py"))
_local_producer = importlib.util.module_from_spec(_LOCAL_PRODUCER_SPEC)
_LOCAL_PRODUCER_SPEC.loader.exec_module(_local_producer)


def _load_model_capability_config(path=MODEL_CAPABILITY_CONFIG_PATH):
    try:
        resolved_path = Path(path).expanduser().resolve()
        if resolved_path == MODEL_CAPABILITY_CONFIG_PATH.resolve():
            payload = _MODEL_REGISTRY.load_registry(resolved_path) if resolved_path.exists() else _MODEL_REGISTRY.ensure_registry(registry_path=resolved_path)["registry"]
        else:
            payload = _MODEL_REGISTRY.load_registry(resolved_path)
        return _MODEL_REGISTRY.validate_registry(payload)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError(f"shared model capability registry is unreadable: {error}") from error


MODEL_CAPABILITY_CONFIG = _load_model_capability_config()
ADAPTIVE_POLICY = dict(MODEL_CAPABILITY_CONFIG["policy"])
ACTIVE_MODEL_ROWS = tuple(dict(row) for row in MODEL_CAPABILITY_CONFIG["models"])
ACTIVE_MODEL_ORDER = [row["id"] for row in ACTIVE_MODEL_ROWS]
ACTIVE_MODEL_DEFINITIONS = {row["id"]: {"efforts": list(row["codex_efforts"])} for row in ACTIVE_MODEL_ROWS}
PRIORITY_PRODUCER_CONFIG = dict(MODEL_CAPABILITY_CONFIG["priority_producer"] or {})
PRIORITY_PRODUCER_MODEL = PRIORITY_PRODUCER_CONFIG.get("id")
PRIORITY_PRODUCER_DEFINITIONS = {PRIORITY_PRODUCER_MODEL: {"efforts": list(PRIORITY_PRODUCER_CONFIG["codex_efforts"])}} if PRIORITY_PRODUCER_MODEL else {}
SPARK_FIRST_CONFIG = PRIORITY_PRODUCER_CONFIG
SPARK_MODEL = PRIORITY_PRODUCER_MODEL
SPARK_MODEL_DEFINITIONS = PRIORITY_PRODUCER_DEFINITIONS
MODEL_DEFINITIONS = {**PRIORITY_PRODUCER_DEFINITIONS, **ACTIVE_MODEL_DEFINITIONS}

MODEL_ORDER = list(MODEL_DEFINITIONS.keys())
MODEL_EFFORT_ORDER = list(MODEL_CAPABILITY_CONFIG["effort_order"])
MODEL_EFFORTS = {model: set(data["efforts"]) for model, data in MODEL_DEFINITIONS.items()}
ACTIVE_MODEL_EFFORTS = {model: set(data["efforts"]) for model, data in ACTIVE_MODEL_DEFINITIONS.items()}
MODEL_EFFORT_INDEX = {model: {effort: index for index, effort in enumerate(data["efforts"])} for model, data in MODEL_DEFINITIONS.items()}
MODEL_POSITION = {model: index for index, model in enumerate(MODEL_ORDER)}
SPARK_BOOTSTRAP_FAMILIES = {"tiny_text", "tiny_code", "command_generation"}
SPARK_LOW_PAIR = (PRIORITY_PRODUCER_MODEL, PRIORITY_PRODUCER_CONFIG["effort_by_complexity"]["easy"]) if PRIORITY_PRODUCER_MODEL else None
NORMAL_ADAPTIVE_MODELS = list(ACTIVE_MODEL_ORDER)
NORMAL_ADAPTIVE_LADDER = [(model, effort) for model in NORMAL_ADAPTIVE_MODELS for effort in MODEL_DEFINITIONS[model]["efforts"]]
MODEL_ROLE_PAIRS = dict(MODEL_CAPABILITY_CONFIG["role_pairs"])
# Retained only so the legacy local-history reader can parse old records.
LEARNING_FIELDS = ("task_family", "artifact", "scope", "ambiguity", "modality", "risk", "complexity", "execution_domain")

EXECUTION_DOMAIN_REGISTRY_VERSION = 1
EXECUTION_DOMAIN_REGISTRY_DEFAULT = "general"
EXECUTION_DOMAIN_REGISTRY_LEGACY = "code_unspecified"

EXECUTION_DOMAINS = {
    "general": {
        "display_name": "General",
        "kind": "general",
        "language_aliases": [],
        "owner_skill": "workflow-skill",
        "owner_enforced": False,
        "spark_first": False,
        "reference_path": "contracts/references/model-selection.md",
        "active": True,
        "history_only": False,
    },
    "python": {
        "display_name": "Python",
        "kind": "code",
        "language_aliases": ["python", "py", "python3"],
        "owner_skill": "code-skill",
        "owner_enforced": True,
        "spark_first": True,
        "reference_path": "contracts/references/python-rules.md",
        "active": True,
        "history_only": False,
    },
    "csharp": {
        "display_name": "C#",
        "kind": "code",
        "language_aliases": ["csharp", "c#"],
        "owner_skill": "code-skill",
        "owner_enforced": True,
        "spark_first": True,
        "reference_path": "contracts/references/csharp-rules.md",
        "active": True,
        "history_only": False,
    },
    "unity_csharp": {
        "display_name": "Unity C#",
        "kind": "code",
        "language_aliases": ["unity_csharp", "unity-csharp", "unitycsharp"],
        "owner_skill": "code-skill",
        "owner_enforced": True,
        "spark_first": True,
        "reference_path": "contracts/references/unity-csharp-rules.md",
        "active": True,
        "history_only": False,
    },
    "code_unspecified": {
        "display_name": "Unspecified Code",
        "kind": "code",
        "language_aliases": [],
        "owner_skill": "code-skill",
        "owner_enforced": False,
        "spark_first": True,
        "reference_path": "contracts/references/spark-small-code.md",
        "active": False,
        "history_only": True,
    },
}

PROFILE_PRESET_VERSION = 2


def _profile_model_fields(role_pair):
    return {"static_suggestion": MODEL_ROLE_PAIRS[role_pair], "hard_floor": MODEL_ROLE_PAIRS["floor"]}


PROFILE_PRESETS = {
    "general-answer-easy": {"task_family": "direct", "artifact": "answer", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("weak_default")},
    "summary-easy": {"task_family": "document", "artifact": "answer", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("floor")},
    "analysis-complex": {"task_family": "grounded", "artifact": "report", "scope": "multi", "ambiguity": "medium", "modality": "mixed", "risk": "low", "complexity": "complex", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_complex")},
    "spreadsheet-easy": {"task_family": "data", "artifact": "document", "scope": "single", "ambiguity": "low", "modality": "mixed", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_default")},
    "spreadsheet-complex": {"task_family": "data", "artifact": "document", "scope": "multi", "ambiguity": "medium", "modality": "mixed", "risk": "low", "complexity": "complex", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_complex")},
    "document-easy": {"task_family": "document", "artifact": "document", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("weak_default")},
    "document-complex": {"task_family": "document", "artifact": "document", "scope": "multi", "ambiguity": "medium", "modality": "mixed", "risk": "low", "complexity": "complex", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_complex")},
    "integration-complex": {"task_family": "integration", "artifact": "patch", "scope": "project", "ambiguity": "high", "modality": "mixed", "risk": "medium", "complexity": "complex", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("frontier_complex")},
    "grounded-repository-answer-easy": {"task_family": "grounded", "artifact": "answer", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_default")},
    "grounded-repository-answer-complex": {"task_family": "grounded", "artifact": "answer", "scope": "multi", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "complex", "verification_shape": "real", "owning_skill": None, "execution_domain": "general", **_profile_model_fields("balanced_complex")},
    "tiny-text": {"task_family": "tiny_text", "artifact": "answer", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": "code-skill", "execution_domain": "general", **_profile_model_fields("floor")},
    "command-generation": {"task_family": "command_generation", "artifact": "script", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": "code-skill", "execution_domain": "general", **_profile_model_fields("floor")},
    "tiny-code": {"task_family": "tiny_code", "artifact": "patch", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": "code-skill", "execution_domain": None, **_profile_model_fields("floor")},
    "code-easy": {"task_family": "code", "artifact": "patch", "scope": "single", "ambiguity": "low", "modality": "text", "risk": "low", "complexity": "easy", "verification_shape": "real", "owning_skill": "code-skill", "execution_domain": None, **_profile_model_fields("balanced_default")},
    "code-complex": {"task_family": "code", "artifact": "patch", "scope": "multi", "ambiguity": "medium", "modality": "text", "risk": "low", "complexity": "complex", "verification_shape": "real", "owning_skill": "code-skill", "execution_domain": None, **_profile_model_fields("balanced_complex")},
}


def execution_domain_names():
    return list(EXECUTION_DOMAINS.keys())


def execution_domain_metadata(execution_domain):
    metadata = EXECUTION_DOMAINS.get(execution_domain)
    if metadata is None:
        raise ValueError(f"unknown execution domain: {execution_domain}")
    payload = dict(metadata)
    payload["id"] = execution_domain
    payload["language_aliases"] = list(metadata.get("language_aliases", []))
    return payload


def execution_domain_names_set():
    return set(execution_domain_names())


def is_code_execution_domain(execution_domain):
    return execution_domain_metadata(execution_domain).get("kind") == "code"


def expected_owner_skill(execution_domain):
    metadata = execution_domain_metadata(execution_domain)
    if not metadata.get("owner_enforced", False):
        return None
    return metadata.get("owner_skill")


def requires_spark_first(execution_domain):
    return bool(execution_domain_metadata(execution_domain).get("spark_first"))


def reference_path_for(execution_domain):
    return execution_domain_metadata(execution_domain).get("reference_path")


def execution_domain_is_active(execution_domain):
    return bool(execution_domain_metadata(execution_domain).get("active"))


def resolve_execution_domain(owning_skill=None, task_family=None, explicit_domain=None, language=None, purpose=None):
    if explicit_domain is not None:
        explicit_domain = str(explicit_domain).strip()
        if explicit_domain not in EXECUTION_DOMAINS:
            raise ValueError(f"unknown execution domain {explicit_domain}")
        return explicit_domain

    if language is not None:
        normalized = str(language).strip().lower()
        for domain in execution_domain_names():
            aliases = execution_domain_metadata(domain).get("language_aliases", [])
            if normalized in aliases:
                return domain

    if owning_skill == "code-skill":
        return EXECUTION_DOMAIN_REGISTRY_LEGACY

    if task_family in {"code", "tiny_code"}:
        return EXECUTION_DOMAIN_REGISTRY_LEGACY

    return EXECUTION_DOMAIN_REGISTRY_DEFAULT


def infer_execution_domain(owning_skill=None, task_family=None, explicit_domain=None, purpose=None, language=None):
    return resolve_execution_domain(
        owning_skill=owning_skill,
        task_family=task_family,
        explicit_domain=explicit_domain,
        language=language,
        purpose=purpose,
    )


def validate_execution_domain_registry(reference_root=None):
    """Validate the execution-domain registry.

    reference_root is where relative reference_path values resolve. It defaults to
    the engine root because the reference documents ship with this repo; it is
    deliberately NOT the deployed skills directory, which upstream conflated.
    """
    reference_root = Path(reference_root) if reference_root is not None else ENGINE_ROOT
    if not execution_domain_names():
        raise ValueError("execution domain registry must contain at least one domain")
    if EXECUTION_DOMAIN_REGISTRY_DEFAULT not in execution_domain_names():
        raise ValueError(f"execution domain default is unknown: {EXECUTION_DOMAIN_REGISTRY_DEFAULT}")
    if EXECUTION_DOMAIN_REGISTRY_LEGACY not in execution_domain_names():
        raise ValueError(f"execution domain legacy id is unknown: {EXECUTION_DOMAIN_REGISTRY_LEGACY}")

    seen_aliases = set()
    seen_reference_paths = set()

    def _normalize_reference_path(value, *, domain):
        if not isinstance(value, str) or not value:
            raise ValueError(f"execution domain {domain} has invalid reference_path")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"execution domain {domain} has invalid reference_path")
        reference = Path(normalized)
        if reference.is_absolute():
            raise ValueError(f"execution domain {domain} reference_path must be relative to the skills root: {normalized}")
        if any(part == ".." for part in reference.parts):
            raise ValueError(f"execution domain {domain} reference_path must not use parent traversal: {normalized}")
        if not reference.as_posix():
            raise ValueError(f"execution domain {domain} has invalid reference_path")
        return normalized

    for domain in execution_domain_names():
        metadata = execution_domain_metadata(domain)
        required = {
            "display_name",
            "kind",
            "language_aliases",
            "owner_skill",
            "owner_enforced",
            "spark_first",
            "reference_path",
            "active",
            "history_only",
        }
        missing = required - set(metadata.keys())
        if missing:
            raise ValueError(f"execution domain {domain} missing metadata: {', '.join(sorted(missing))}")
        if not isinstance(metadata["display_name"], str) or not metadata["display_name"]:
            raise ValueError(f"execution domain {domain} has invalid display_name")
        if metadata["kind"] not in {"general", "code"}:
            raise ValueError(f"execution domain {domain} has invalid kind: {metadata['kind']}")
        if not isinstance(metadata["language_aliases"], list):
            raise ValueError(f"execution domain {domain} language_aliases must be a list")
        aliases = []
        for alias in metadata["language_aliases"]:
            raw_alias = str(alias)
            if raw_alias != raw_alias.strip().lower():
                raise ValueError(f"execution domain {domain} has non-canonical language_alias: {raw_alias}")
            alias = raw_alias
            if not alias:
                raise ValueError(f"execution domain {domain} has empty language_alias")
            if alias in seen_aliases:
                raise ValueError(f"language_alias is reused by registry: {alias}")
            aliases.append(alias)
            seen_aliases.add(alias)
        # owner_skill is a route-contract *label*: the dispatcher checks that a plan node
        # declares the owner this domain expects. It is deliberately not resolved against the
        # filesystem -- that assumed the upstream Codex skill-bundle deployment shape, which
        # this engine no longer requires. reference_path below is the real, checked artifact.
        if not isinstance(metadata["owner_skill"], str) or not metadata["owner_skill"]:
            raise ValueError(f"execution domain {domain} has invalid owner_skill")
        if not isinstance(metadata["owner_enforced"], bool):
            raise ValueError(f"execution domain {domain} owner_enforced must be bool")
        if not isinstance(metadata["spark_first"], bool):
            raise ValueError(f"execution domain {domain} spark_first must be bool")
        if not isinstance(metadata["reference_path"], str) or not metadata["reference_path"]:
            raise ValueError(f"execution domain {domain} must include a reference_path")
        normalized_reference = _normalize_reference_path(metadata["reference_path"], domain=domain)
        if normalized_reference in seen_reference_paths:
            raise ValueError(f"execution domain {domain} reuses reference_path")
        seen_reference_paths.add(normalized_reference)
        reference = reference_root / normalized_reference
        if not reference.is_file():
            raise ValueError(f"execution domain {domain} reference file is missing: {reference}")
        if not isinstance(metadata["active"], bool):
            raise ValueError(f"execution domain {domain} active must be bool")
        if not isinstance(metadata["history_only"], bool):
            raise ValueError(f"execution domain {domain} history_only must be bool")
        if metadata["active"] and metadata["history_only"]:
            raise ValueError(f"execution domain {domain} cannot be both active and history-only")
    return True


def public_execution_domain_rows():
    return [
        {
            "id": domain,
            "display_name": metadata.get("display_name"),
            "kind": metadata.get("kind"),
            "language_aliases": list(metadata.get("language_aliases", [])),
            "owner_skill": metadata.get("owner_skill"),
            "owner_enforced": metadata.get("owner_enforced"),
            "spark_first": metadata.get("spark_first"),
            "reference_path": metadata.get("reference_path"),
            "active": metadata.get("active"),
            "history_only": metadata.get("history_only"),
        }
        for domain, metadata in (
            (domain, execution_domain_metadata(domain))
            for domain in execution_domain_names()
        )
    ]


def pair_text(model, effort):
    _ensure_supported_pair(model, effort)
    return f"{model}|{effort}"


def parse_model_effort_pair(value):
    if not isinstance(value, str):
        raise ValueError("model|effort pair must be a string")
    value = value.strip()
    if value.count("|") != 1:
        raise ValueError("model|effort pair must be separated by one |")
    model, effort = (part.strip() for part in value.split("|", 1))
    _ensure_supported_pair(model, effort)
    return model, effort


def parse_pair(value):
    return parse_model_effort_pair(value)


def _ensure_model(model):
    if model not in MODEL_EFFORTS or model not in MODEL_ORDER:
        raise ValueError("unsupported model")


def _ensure_supported_pair(model, effort):
    _ensure_model(model)
    if effort not in MODEL_EFFORTS[model]:
        raise ValueError("unsupported effort")


def compare_pair(left, right):
    if not isinstance(left, tuple) or not isinstance(right, tuple):
        raise ValueError("pairs must be tuples")
    left_model, left_effort = left
    right_model, right_effort = right
    _ensure_supported_pair(left_model, left_effort)
    _ensure_supported_pair(right_model, right_effort)
    left_model_position = MODEL_ORDER.index(left_model)
    right_model_position = MODEL_ORDER.index(right_model)
    if left_model != right_model:
        return -1 if left_model_position < right_model_position else 1
    left_index = MODEL_EFFORT_ORDER.index(left_effort)
    right_index = MODEL_EFFORT_ORDER.index(right_effort)
    if left_index == right_index:
        return 0
    return -1 if left_index < right_index else 1


def _sorted_pairs(values):
    return sorted(values, key=lambda pair: [MODEL_ORDER.index(pair[0]), MODEL_EFFORT_ORDER.index(pair[1])])


def canonical_pairs(values):
    pairs = []
    seen = set()
    for value in values:
        pair = parse_pair(value) if isinstance(value, str) else value
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("pair values must be (model, effort)")
        _ensure_supported_pair(*pair)
        if pair in seen:
            raise ValueError("candidate_ladder must contain unique pairs")
        pairs.append(pair)
        seen.add(pair)
    return _sorted_pairs(pairs)


def normal_adaptive_ladder():
    return list(NORMAL_ADAPTIVE_LADDER)


def normal_adaptive_pair_texts():
    return [pair_text(*pair) for pair in NORMAL_ADAPTIVE_LADDER]


def is_tiny_spark_profile(task_family, modality, risk, complexity="easy", ambiguity="low"):
    """Recognize the former narrow Spark profile for legacy history parsing."""
    return (
        task_family in SPARK_BOOTSTRAP_FAMILIES
        and modality == "text"
        and risk == "low"
        and complexity == "easy"
        and ambiguity == "low"
    )


def adaptive_ladder_for_profile(task_family, modality, risk, complexity="easy", ambiguity="low"):
    return normal_adaptive_ladder()


def priority_first_pair(task_type, modality="text", operation="work", complexity="easy", complexity_score=None, purpose=None):
    """Return the priority producer for a bounded eligible task or task segment."""
    if not PRIORITY_PRODUCER_CONFIG.get("enabled") or PRIORITY_PRODUCER_MODEL is None or modality != "text" or complexity != "easy":
        return None
    maximum_score = PRIORITY_PRODUCER_CONFIG.get("task_segment_maximum_complexity_score", PRIORITY_PRODUCER_CONFIG.get("small_edit_maximum_complexity_score", 24))
    if complexity_score is None or isinstance(complexity_score, bool) or not isinstance(complexity_score, int) or not 0 <= complexity_score <= maximum_score:
        return None
    eligible_task = task_type in PRIORITY_PRODUCER_CONFIG.get("eligible_task_types", [])
    eligible_operation = operation in PRIORITY_PRODUCER_CONFIG.get("eligible_operations", [])
    excluded_operation = operation in PRIORITY_PRODUCER_CONFIG.get("excluded_operations", [])
    eligible_segment = purpose in PRIORITY_PRODUCER_CONFIG.get("task_segment_purposes", [])
    if excluded_operation or not ((eligible_task and eligible_operation) or eligible_segment):
        return None
    effort = PRIORITY_PRODUCER_CONFIG["effort_by_complexity"]["easy"]
    return (PRIORITY_PRODUCER_MODEL, effort)


def spark_first_pair(task_type, modality="text", operation="work", complexity="easy", complexity_score=None, purpose=None):
    return priority_first_pair(task_type, modality, operation, complexity, complexity_score, purpose)


def local_first_pair(task_type, modality="text", operation="work", complexity="easy", complexity_score=None, purpose=None):
    """Return the local-model pair for a node the priority producer would also accept.

    The local rung sits one below the priority producer and shares its admission rules
    exactly -- anything cheap enough for the priority producer is cheap enough to try for
    free first. It returns None when no local endpoint is configured, which is the
    default, so the ladder behaves as before unless the rung is switched on.
    """
    if not _local_producer.rung_enabled():
        return None
    if priority_first_pair(task_type, modality, operation, complexity, complexity_score, purpose) is None:
        return None
    return _local_producer.local_pair()


def free_first_ladder(task_type, modality="text", operation="work", complexity="easy", complexity_score=None, purpose=None):
    """Ordered pre-ladder attempts, cheapest first, for one eligible bounded node.

    Both rungs sit outside the quality ladder: a zero-result operational failure at either
    falls through to the next entry and is not learned as a quality failure.
    """
    attempts = []
    local = local_first_pair(task_type, modality, operation, complexity, complexity_score, purpose)
    if local is not None:
        attempts.append(local)
    priority = priority_first_pair(task_type, modality, operation, complexity, complexity_score, purpose)
    if priority is not None:
        attempts.append(priority)
    return attempts


def scheduled_source_pair(complexity="easy"):
    if not PRIORITY_PRODUCER_CONFIG.get("enabled") or PRIORITY_PRODUCER_MODEL is None:
        return None
    effort = PRIORITY_PRODUCER_CONFIG["effort_by_complexity"].get(complexity)
    return (PRIORITY_PRODUCER_MODEL, effort) if effort in PRIORITY_PRODUCER_CONFIG["adaptive_efforts"] else None


def adaptive_pair_texts_for_profile(task_family, modality, risk, complexity="easy", ambiguity="low"):
    return [pair_text(*pair) for pair in adaptive_ladder_for_profile(task_family, modality, risk, complexity, ambiguity)]


def profile_preset_names():
    return tuple(PROFILE_PRESETS.keys())


def resolve_profile_preset(profile_preset, *, project_family, owning_skill=None, execution_domain=None):
    if profile_preset not in PROFILE_PRESETS:
        raise ValueError(f"unknown profile preset: {profile_preset}")
    if not isinstance(project_family, str) or not project_family.strip():
        raise ValueError("project_family is required")
    preset = PROFILE_PRESETS[profile_preset]
    fixed_owner = preset["owning_skill"]
    if fixed_owner is None:
        if not isinstance(owning_skill, str) or not owning_skill.strip():
            raise ValueError("owning_skill is required for this profile preset")
        resolved_owner = owning_skill
    else:
        if owning_skill is not None and owning_skill != fixed_owner:
            raise ValueError(f"profile preset requires owning_skill={fixed_owner}")
        resolved_owner = fixed_owner
    fixed_domain = preset["execution_domain"]
    if fixed_domain is None:
        if execution_domain is None:
            raise ValueError("an active code execution_domain is required for this profile preset")
        domain_metadata = execution_domain_metadata(execution_domain)
        if not domain_metadata["active"] or domain_metadata["history_only"] or domain_metadata["kind"] != "code":
            raise ValueError("profile preset requires an active code execution_domain")
        resolved_domain = execution_domain
    else:
        if execution_domain is not None and execution_domain != fixed_domain:
            raise ValueError(f"profile preset requires execution_domain={fixed_domain}")
        resolved_domain = fixed_domain
    profile = {field: preset[field] for field in ("task_family", "artifact", "scope", "ambiguity", "modality", "risk", "complexity", "verification_shape")}
    profile.update({"owning_skill": resolved_owner, "project_family": project_family, "execution_domain": resolved_domain})
    profile["candidate_ladder"] = adaptive_pair_texts_for_profile(profile["task_family"], profile["modality"], profile["risk"], profile["complexity"], profile["ambiguity"])
    profile["static_suggestion"] = preset["static_suggestion"]
    profile["hard_floor"] = preset["hard_floor"]
    return profile


def public_profile_preset_rows():
    rows = []
    for profile_preset, preset in PROFILE_PRESETS.items():
        condition = {field: preset[field] for field in ("task_family", "artifact", "scope", "ambiguity", "modality", "risk", "complexity", "verification_shape")}
        rows.append({"id": profile_preset, "condition": condition, "owning_skill": preset["owning_skill"] or "caller_required", "execution_domain": preset["execution_domain"] or "active_code_domain_required", "static_suggestion": preset["static_suggestion"], "hard_floor": preset["hard_floor"]})
    return rows


def public_model_capability_rows():
    return {
        "schema_version": MODEL_CAPABILITY_CONFIG["schema_version"],
        "registry_id": MODEL_CAPABILITY_CONFIG["registry_id"],
        "scope": MODEL_CAPABILITY_CONFIG["scope"],
        "source": dict(MODEL_CAPABILITY_CONFIG["source"]),
        "active_family": dict(MODEL_CAPABILITY_CONFIG["active_family"]),
        "catalog_models": [dict(row) for row in MODEL_CAPABILITY_CONFIG["catalog_models"]],
        "ladder_direction": MODEL_CAPABILITY_CONFIG["ladder_direction"],
        "role_models": dict(MODEL_CAPABILITY_CONFIG["role_models"]),
        "role_pairs": dict(MODEL_ROLE_PAIRS),
        "policy": dict(ADAPTIVE_POLICY),
        "priority_producer": dict(PRIORITY_PRODUCER_CONFIG) if PRIORITY_PRODUCER_CONFIG else None,
        "spark_first": dict(SPARK_FIRST_CONFIG),
        "private_learning_contract": dict(MODEL_CAPABILITY_CONFIG["private_learning_contract"]),
        "default_cold_start": MODEL_CAPABILITY_CONFIG["default_cold_start"],
        "cold_start_defaults": {key: dict(value) for key, value in MODEL_CAPABILITY_CONFIG["cold_start_defaults"].items()},
        "effort_order": list(MODEL_EFFORT_ORDER),
        "effort_guidance": dict(MODEL_CAPABILITY_CONFIG.get("effort_guidance", {})),
        "models": [dict(row) for row in ACTIVE_MODEL_ROWS],
    }


def validate_profile_preset_registry():
    required = {"task_family", "artifact", "scope", "ambiguity", "modality", "risk", "complexity", "verification_shape", "owning_skill", "execution_domain", "static_suggestion", "hard_floor"}
    for profile_preset, preset in PROFILE_PRESETS.items():
        if set(preset) != required:
            raise ValueError(f"profile preset has invalid fields: {profile_preset}")
        sample_owner = preset["owning_skill"] or "workflow-skill"
        sample_domain = preset["execution_domain"] or next(domain for domain in EXECUTION_DOMAINS if execution_domain_metadata(domain)["active"] and execution_domain_metadata(domain)["kind"] == "code")
        resolved = resolve_profile_preset(profile_preset, project_family="global", owning_skill=sample_owner, execution_domain=sample_domain)
        pairs = canonical_pairs(resolved["candidate_ladder"])
        if parse_pair(resolved["static_suggestion"]) not in pairs or parse_pair(resolved["hard_floor"]) not in pairs:
            raise ValueError(f"profile preset pair is outside its canonical ladder: {profile_preset}")
    return True


def validate_model_capability_registry():
    _MODEL_REGISTRY.validate_registry(MODEL_CAPABILITY_CONFIG)
    if set(ACTIVE_MODEL_ORDER) != set(ACTIVE_MODEL_EFFORTS):
        raise ValueError("shared active model registry is inconsistent")
    minimum_pair = parse_pair(ADAPTIVE_POLICY["minimum_pair"])
    if minimum_pair != NORMAL_ADAPTIVE_LADDER[0]:
        raise ValueError("shared minimum pair must be the weakest active ladder pair")
    if PRIORITY_PRODUCER_MODEL and any(model == PRIORITY_PRODUCER_MODEL for model, _ in NORMAL_ADAPTIVE_LADDER):
        raise ValueError("priority producer must stay outside the quality ladder")
    if ACTIVE_MODEL_ORDER != [row["id"] for row in sorted(ACTIVE_MODEL_ROWS, key=lambda row: row["capability_rank"])]:
        raise ValueError("quality models must remain weakest-to-strongest")
    return True


def _eligible_pairs_by_model(pairs, target):
    return [pair for pair in pairs if pair[0] == target]


def downgrade_pair(current, eligible):
    current_pair = current
    if not isinstance(current_pair, tuple):
        raise ValueError("current pair must be a tuple")
    _ensure_supported_pair(*current_pair)
    ordered = canonical_pairs(eligible)
    if not ordered:
        return None
    current_model, current_effort = current_pair
    same_model = _eligible_pairs_by_model(ordered, current_model)
    for pair in reversed(same_model):
        if compare_pair(pair, current_pair) < 0:
            return pair
    current_model_rank = MODEL_POSITION[current_model]
    for model_rank in range(current_model_rank - 1, -1, -1):
        candidate_model = MODEL_ORDER[model_rank]
        candidates = _eligible_pairs_by_model(ordered, candidate_model)
        if candidates:
            return candidates[-1]
    return None


def upgrade_pair(current, eligible):
    current_pair = current
    if not isinstance(current_pair, tuple):
        raise ValueError("current pair must be a tuple")
    _ensure_supported_pair(*current_pair)
    ordered = canonical_pairs(eligible)
    if not ordered:
        return None
    current_model, current_effort = current_pair
    same_model = _eligible_pairs_by_model(ordered, current_model)
    for pair in same_model:
        if compare_pair(pair, current_pair) > 0:
            return pair
    current_model_rank = MODEL_POSITION[current_model]
    for model_rank in range(current_model_rank + 1, len(MODEL_ORDER)):
        candidate_model = MODEL_ORDER[model_rank]
        candidates = _eligible_pairs_by_model(ordered, candidate_model)
        if candidates:
            return candidates[0]
    return None


def canonical_pair_texts(pairs):
    return [pair_text(*pair) for pair in canonical_pairs(pairs)]


validate_execution_domain_registry()
validate_model_capability_registry()
validate_profile_preset_registry()
