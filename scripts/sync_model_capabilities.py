#!/usr/bin/env python3
"""Synchronize the shared model registry and its readable capability snapshot."""

import argparse
import importlib.util
import json
import os
import tempfile
from pathlib import Path


try:
    from model_registry import DEFAULT_MODELS_CACHE_PATH, DEFAULT_REGISTRY_PATH, build_registry, load_catalog, load_registry, refresh_registry, registry_matches_catalog
except ModuleNotFoundError:
    _model_registry_path = Path(__file__).with_name("model_registry.py")
    _model_registry_spec = importlib.util.spec_from_file_location("task_analyze_model_registry", _model_registry_path)
    _model_registry = importlib.util.module_from_spec(_model_registry_spec)
    _model_registry_spec.loader.exec_module(_model_registry)
    DEFAULT_MODELS_CACHE_PATH = _model_registry.DEFAULT_MODELS_CACHE_PATH
    DEFAULT_REGISTRY_PATH = _model_registry.DEFAULT_REGISTRY_PATH
    build_registry = _model_registry.build_registry
    load_catalog = _model_registry.load_catalog
    load_registry = _model_registry.load_registry
    refresh_registry = _model_registry.refresh_registry
    registry_matches_catalog = _model_registry.registry_matches_catalog


DEFAULT_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "references" / "model-capabilities.md"


def effort_markdown_lines(registry):
    lines = []
    for model in registry["catalog_models"]:
        lines.append(f"- `{model['id']}` ({model['catalog_role']}): {', '.join(model['codex_efforts'])}.")
    return lines


def build_snapshot(registry):
    source = registry["source"]
    lines = ["# Cached Model Capabilities", "", "This snapshot and the shared JSON registry come from the local Codex catalog. They change only when the user explicitly runs the manual update command; ordinary routing reads the saved registry without scanning the catalog.", "", "- Source: `~/.codex/models_cache.json`", f"- Codex client version: `{source['client_version']}`", f"- Local catalog snapshot: `{source['fetched_at']}`", f"- Semantic catalog SHA-256: `{source['catalog_sha256']}`", f"- Registry schema: `{registry['schema_version']}`", f"- Active quality family: `{registry['active_family']['id']}` (highest numeric GPT family)", "", "## Quality ladder", "", "Only the highest registered numeric GPT family is active. Within that family, models are weakest to strongest using the provider's current priority order.", "", "| Rank | Display name | Model ID | Role | Inputs | Context | API | Default effort | Supported efforts | Speed tiers |", "|---:|---|---|---|---|---:|---|---|---|---|"]
    for model in registry["models"]:
        roles = ", ".join(model["capability_roles"]) or "quality"
        inputs = ", ".join(model["input_modalities"])
        context = f"{model['context_window']:,}" if isinstance(model["context_window"], int) else "unknown"
        api_support = "yes" if model["supported_in_api"] else "no"
        speed_tiers = ", ".join(model["additional_speed_tiers"]) or "default"
        efforts = ", ".join(model["codex_efforts"])
        lines.append(f"| {model['capability_rank']} | {model['display_name']} | `{model['id']}` | {roles} | {inputs} | {context} | {api_support} | `{model['default_effort']}` | {efforts} | {speed_tiers} |")
    lines.extend(["", "## Catalog-visible models", "", "Only the highest numeric GPT family and the priority producer are retained in this routing snapshot. Older visible models may be present in the machine cache, but they are excluded from the registry, semantic digest, and routing candidates so background catalog refreshes cannot reintroduce them.", "", "| Display name | Model ID | Catalog role | Provider priority | Supported efforts |", "|---|---|---|---:|---|"])
    for model in registry["catalog_models"]:
        lines.append(f"| {model['display_name']} | `{model['id']}` | {model['catalog_role']} | {model['provider_priority']} | {', '.join(model['codex_efforts'])} |")
    lines.extend(["", "## Priority text/code producer", ""])
    priority_producer = registry.get("priority_producer")
    if priority_producer is None:
        lines.append("The current catalog does not advertise a specialized text-only fast coding producer. Eligible work starts on the quality ladder.")
    else:
        lines.extend([f"- Model: `{priority_producer['id']}` ({priority_producer['display_name']})", f"- Positioning: {priority_producer['provider_positioning']}", f"- Inputs: {', '.join(priority_producer['input_modalities'])}; API: {'yes' if priority_producer['supported_in_api'] else 'no'}", f"- Easy / complex effort: `{priority_producer['effort_by_complexity']['easy']}` / `{priority_producer['effort_by_complexity']['complex']}`", "- This producer is attempted before eligible low-risk text/code/write/execute task segments scoring 0-24, regardless of parent score, and is not part of the weakest-to-strongest quality ladder."])
    private_learning = registry["private_learning_contract"]
    lines.extend(["", "## Private learning contract", "", f"- Authority: `{private_learning['authority']}`", f"- Local path: `{private_learning['local_path_template']}`", f"- Obsidian entry: `{private_learning['projection_path_template']}`", f"- Obsidian records: `{private_learning['record_path_template']}`", f"- Native wikilink graph: `{str(private_learning['native_wikilink_graph']).lower()}`; stable categories: {', '.join(private_learning['stable_categories'])}.", f"- Stable event dedupe: `{str(private_learning['event_id_dedupe']).lower()}`", f"- Specificity: {' / '.join(private_learning['specificity_order'])}", f"- Context stays in fields: `{str(private_learning['fields_only']).lower()}`; per-context hierarchy notes: `{str(private_learning['hierarchy_notes']).lower()}`; legacy local JSON: `{private_learning['legacy_local_json']}`.", "", "## Dynamic defaults", "", f"- Floor: `{registry['role_pairs']['floor']}`", f"- Balanced cold start: `{registry['default_cold_start']}`", f"- Balanced complex: `{registry['role_pairs']['balanced_complex']}`", f"- Frontier complex: `{registry['role_pairs']['frontier_complex']}`", "", "| Task type | Easy | Complex |", "|---|---|---|"])
    for task_type, levels in registry["cold_start_defaults"].items():
        lines.append(f"| {task_type} | `{levels['easy']}` | `{levels['complex']}` |")
    lines.extend(["", "## Effort compatibility", "", *effort_markdown_lines(registry), "- Unsupported efforts are normalized within the selected model's advertised effort list.", "", "## Manual update", "", "```bash", "python3 scripts/sync_model_capabilities.py --update", "python3 scripts/sync_model_capabilities.py --check", "```", ""])
    return "\n".join(lines)


def desired_outputs(models_cache_path):
    catalog, catalog_sha256 = load_catalog(models_cache_path)
    registry = build_registry(catalog, catalog_sha256)
    return registry, build_snapshot(registry)


def check_outputs(models_cache_path, registry_path, snapshot_path):
    registry_path = Path(registry_path).expanduser().resolve()
    snapshot_path = Path(snapshot_path).expanduser().resolve()
    actual_registry = None
    if registry_path.exists():
        try:
            actual_registry = load_registry(registry_path)
        except (KeyError, OSError, UnicodeDecodeError, TypeError, json.JSONDecodeError, ValueError):
            actual_registry = None
    try:
        desired_registry, desired_snapshot = desired_outputs(models_cache_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if actual_registry is None:
            raise RuntimeError(f"Codex model catalog is unavailable and no valid model registry exists: {error}") from error
        desired_registry = actual_registry
        desired_snapshot = build_snapshot(actual_registry)
        registry_current = True
        registry_status = "retained"
        catalog_status = "unavailable"
    else:
        registry_current = registry_matches_catalog(actual_registry, desired_registry["source"]["catalog_sha256"], desired_registry)
        desired_snapshot = build_snapshot(actual_registry) if registry_current else desired_snapshot
        registry_status = "current" if registry_current else "stale"
        catalog_status = "available"
    snapshot_current = snapshot_path.exists() and snapshot_path.read_text(encoding="utf-8") == desired_snapshot
    return {"valid": registry_current and snapshot_current, "registry_status": registry_status, "snapshot_status": "current" if snapshot_current else "stale", "catalog_status": catalog_status, "catalog_sha256": desired_registry["source"]["catalog_sha256"]}


def _atomic_write_text(path, content):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def sync_outputs(models_cache_path, registry_path, snapshot_path):
    refresh_outcome = refresh_registry(registry_path, models_cache_path)
    registry = refresh_outcome["registry"]
    snapshot_path = Path(snapshot_path).expanduser().resolve()
    snapshot = build_snapshot(registry)
    if not snapshot_path.exists() or snapshot_path.read_text(encoding="utf-8") != snapshot:
        _atomic_write_text(snapshot_path, snapshot)
    return refresh_outcome


def main():
    parser = argparse.ArgumentParser(description="Explicitly update or check the task-analyze-skill model registry against the local Codex model cache.")
    parser.add_argument("--models-cache", type=Path, default=DEFAULT_MODELS_CACHE_PATH)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--update", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()
    models_cache_path = args.models_cache.expanduser().resolve()
    registry_path = args.registry.expanduser().resolve()
    snapshot_path = args.output.expanduser().resolve()
    if args.check:
        status = check_outputs(models_cache_path, registry_path, snapshot_path)
        print(f"model registry is {status['registry_status']}: {registry_path}")
        print(f"model capability snapshot is {status['snapshot_status']}: {snapshot_path}")
        return 0 if status["valid"] else 1
    refresh_outcome = sync_outputs(models_cache_path, registry_path, snapshot_path)
    registry = refresh_outcome["registry"]
    if refresh_outcome["status"] == "retained":
        print(f"local model catalog unavailable; retained saved model registry: {registry_path}")
    else:
        print(f"model registry {refresh_outcome['status']} from {registry['source']['catalog_sha256']}: {registry_path}")
    print(f"model capability snapshot synchronized: {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
