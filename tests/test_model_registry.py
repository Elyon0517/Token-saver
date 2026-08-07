#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import model_registry


def catalog_model(slug, priority, description, efforts=("low", "medium", "high"), modalities=("text", "image"), visibility="list", default_effort="medium", supported_in_api=True):
    levels = [{"effort": effort, "description": effort} for effort in efforts]
    return {"slug": slug, "display_name": slug.upper(), "description": description, "default_reasoning_level": default_effort, "supported_reasoning_levels": levels, "visibility": visibility, "supported_in_api": supported_in_api, "priority": priority, "additional_speed_tiers": ["fast"], "input_modalities": list(modalities), "context_window": 100000}


def catalog(models, version="1.2.3", fetched_at="2026-07-15T00:00:00Z"):
    return {"client_version": version, "fetched_at": fetched_at, "models": models}


class ModelRegistryTests(unittest.TestCase):
    def current_catalog(self):
        models = [catalog_model("gpt-5.6-sol", 1, "Latest frontier coding model", efforts=("ultra", "low", "high", "medium")), catalog_model("gpt-5.6-terra", 2, "Balanced model for everyday work"), catalog_model("gpt-5.6-luna", 3, "Fast and affordable model"), catalog_model("gpt-5.3-codex-spark", 26, "Ultra-fast coding model", efforts=("xhigh", "low", "high", "medium"), modalities=("text",), supported_in_api=False), catalog_model("hidden-review", 40, "Automatic review model", visibility="hide")]
        return catalog(models)

    def test_builds_generic_registry_from_visible_catalog(self):
        registry = model_registry.build_registry(self.current_catalog(), "a" * 64)
        self.assertEqual(registry["schema_version"], 2)
        self.assertEqual(registry["active_family"], {"id": "gpt-5.6", "numeric_version": [5, 6], "selection": "highest_numeric_gpt_family", "model_count": 3})
        self.assertEqual([model["id"] for model in registry["models"]], ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"])
        self.assertEqual(registry["priority_producer"]["id"], "gpt-5.3-codex-spark")
        self.assertEqual(registry["priority_producer"]["routing_role"], "bounded_task_segment_or_small_edit_priority")
        self.assertTrue(registry["policy"]["priority_producer_task_segments"])
        self.assertIn("execute", registry["priority_producer"]["task_segment_purposes"])
        self.assertTrue(registry["policy"]["priority_producer_first_text_code"])
        self.assertTrue(registry["policy"]["priority_producer_first_small_edits"])
        self.assertTrue(registry["policy"]["priority_producer_scheduled_sources"])
        self.assertFalse(registry["policy"]["priority_producer_scheduled_sources_only"])
        self.assertIn("answer", registry["priority_producer"]["eligible_operations"])
        self.assertIn("work", registry["priority_producer"]["eligible_operations"])
        self.assertEqual(registry["complexity_scale"]["bands"][0], {"id": "small", "minimum": 0, "maximum": 24})
        self.assertEqual(registry["policy"]["minimum_real_passes_before_downgrade"], 2)
        self.assertNotIn("gpt-5.3-codex-spark", [model["id"] for model in registry["models"]])
        self.assertNotIn("hidden-review", [model["id"] for model in registry["models"]])
        self.assertEqual(registry["models"][-1]["codex_efforts"], ["low", "medium", "high", "ultra"])
        self.assertEqual([model["id"] for model in registry["catalog_models"]], ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex-spark"])
        self.assertEqual([model["catalog_role"] for model in registry["catalog_models"]], ["active_quality", "active_quality", "active_quality", "priority_producer"])
        self.assertEqual(registry["source"], {"models_cache": "~/.codex/models_cache.json", "client_version": "1.2.3", "fetched_at": "2026-07-15T00:00:00Z", "catalog_sha256": "a" * 64})
        self.assertIs(model_registry.validate_registry(registry), registry)

    def test_derives_floor_and_cold_starts_from_roles(self):
        registry = model_registry.build_registry(self.current_catalog(), "b" * 64)
        self.assertEqual(registry["role_models"], {"weak": "gpt-5.6-luna", "balanced": "gpt-5.6-terra", "frontier": "gpt-5.6-sol"})
        self.assertEqual(registry["policy"]["minimum_pair"], "gpt-5.6-luna|low")
        self.assertEqual(registry["default_cold_start"], "gpt-5.6-terra|medium")
        self.assertEqual(registry["cold_start_defaults"]["debug"]["complex"], "gpt-5.6-sol|high")
        self.assertEqual(registry["cold_start_defaults"]["code"]["complex"], "gpt-5.6-terra|high")
        self.assertEqual(registry["cold_start_defaults"]["script"]["easy"], registry["cold_start_defaults"]["code"]["easy"])
        self.assertEqual(registry["cold_start_defaults"]["script"]["complex"], registry["cold_start_defaults"]["code"]["complex"])
        self.assertEqual(registry["cold_start_defaults"]["normal-script-update"], registry["cold_start_defaults"]["code"])
        self.assertEqual(registry["cold_start_defaults"]["code-design"], registry["cold_start_defaults"]["code"])
        self.assertEqual(registry["cold_start_defaults"]["finding-bugs"], registry["cold_start_defaults"]["debug"])
        self.assertEqual(registry["cold_start_defaults"]["documentation-instructions"], registry["cold_start_defaults"]["document"])

    def test_higher_numeric_family_completely_takes_over_quality_ladder(self):
        models = [catalog_model("gpt-5.6-luna", 30, "Older economy model"), catalog_model("gpt-5.6-sol", 10, "Older expert model"), catalog_model("gpt-8-economy", 25, "Higher-family economy model"), catalog_model("gpt-8-balanced", 15, "Higher-family balanced model"), catalog_model("gpt-8-frontier", 5, "Higher-family frontier model")]
        registry = model_registry.build_registry(catalog(models), "c" * 64)
        self.assertIsNone(registry["priority_producer"])
        self.assertEqual(registry["active_family"]["id"], "gpt-8")
        self.assertEqual([model["id"] for model in registry["models"]], ["gpt-8-economy", "gpt-8-balanced", "gpt-8-frontier"])
        self.assertEqual([model["id"] for model in registry["catalog_models"]], ["gpt-8-frontier", "gpt-8-balanced", "gpt-8-economy"])
        self.assertFalse(registry["policy"]["priority_producer_first_text_code"])
        self.assertFalse(registry["policy"]["priority_producer_first_small_edits"])
        self.assertFalse(registry["policy"]["priority_producer_scheduled_sources"])
        self.assertFalse(registry["policy"]["priority_producer_scheduled_sources_only"])
        model_registry.validate_registry(registry)

    def test_older_visible_catalog_models_do_not_change_selected_digest_or_registry(self):
        current = self.current_catalog()
        legacy = catalog_model("gpt-4.9", 7, "Prior frontier model")
        legacy_midsize = catalog_model("gpt-4.8", 16, "Prior everyday model")
        legacy_small = catalog_model("gpt-4.8-mini", 23, "Prior small model")
        noisy = deepcopy(current)
        noisy["models"].extend([legacy, legacy_midsize, legacy_small])

        current_digest = model_registry.semantic_catalog_sha256(current)
        noisy_digest = model_registry.semantic_catalog_sha256(noisy)
        self.assertEqual(noisy_digest, current_digest)
        registry = model_registry.build_registry(noisy, noisy_digest)
        self.assertEqual([model["id"] for model in registry["catalog_models"]], ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex-spark"])

    def test_numeric_family_parser_supports_future_major_and_dotted_versions(self):
        self.assertEqual(model_registry.parse_numeric_gpt_family("gpt-6-codex"), ("gpt-6", (6,)))
        self.assertEqual(model_registry.parse_numeric_gpt_family("gpt-5.10-pro"), ("gpt-5.10", (5, 10)))
        self.assertEqual(model_registry.parse_numeric_gpt_family("GPT-7.2.1-Luna"), ("gpt-7.2.1", (7, 2, 1)))
        self.assertIsNone(model_registry.parse_numeric_gpt_family("codex-next"))

    def test_future_major_family_wins_over_all_older_minor_families(self):
        models = [catalog_model("gpt-5.10-pro", 1, "Older model"), catalog_model("gpt-6-mini", 20, "New small model"), catalog_model("gpt-6-pro", 2, "New frontier model")]
        registry = model_registry.build_registry(catalog(models), "f" * 64)
        self.assertEqual(registry["active_family"], {"id": "gpt-6", "numeric_version": [6], "selection": "highest_numeric_gpt_family", "model_count": 2})
        self.assertEqual([model["id"] for model in registry["models"]], ["gpt-6-mini", "gpt-6-pro"])

    def test_detects_generic_text_only_fast_coding_producer(self):
        models = [catalog_model("gpt-7-economy", 30, "Economy model"), catalog_model("flash-code", 40, "Extremely fast coding model", modalities=("text",), supported_in_api=False)]
        registry = model_registry.build_registry(catalog(models), "d" * 64)
        self.assertEqual(registry["priority_producer"]["id"], "flash-code")
        self.assertEqual(registry["priority_producer"]["effort_by_complexity"], {"easy": "low", "complex": "high"})

    def test_script_task_uses_priority_producer_fast_ladder_eligibility(self):
        registry = model_registry.build_registry(self.current_catalog(), "g" * 64)
        self.assertEqual(registry["priority_producer"]["effort_by_complexity"], {"easy": "low", "complex": "high"})
        self.assertIn("script", registry["priority_producer"]["eligible_task_types"])
        self.assertIn("script", registry["cold_start_defaults"])

    def test_model_switch_task_aliases_use_requested_priority_and_quality_profiles(self):
        registry = model_registry.build_registry(self.current_catalog(), "i" * 64)
        for task_type in ("normal-script-update", "code-design", "finding-bugs", "documentation-instructions", "script"):
            self.assertIn(task_type, registry["priority_producer"]["eligible_task_types"])
        self.assertEqual(registry["cold_start_defaults"]["normal-script-update"], registry["cold_start_defaults"]["code"])
        self.assertEqual(registry["cold_start_defaults"]["code-design"], registry["cold_start_defaults"]["code"])
        self.assertEqual(registry["cold_start_defaults"]["finding-bugs"], registry["cold_start_defaults"]["debug"])
        self.assertEqual(registry["cold_start_defaults"]["documentation-instructions"], registry["cold_start_defaults"]["document"])

    def test_private_learning_contract_is_dual_local_and_obsidian(self):
        registry = model_registry.build_registry(self.current_catalog(), "h" * 64)
        contract = registry["private_learning_contract"]
        self.assertEqual(contract["authority"], "dual_local_and_obsidian")
        self.assertEqual(contract["local_path_template"], "~/.codex/model-routing-memory/events.jsonl")
        self.assertEqual(contract["projection_path_template"], "Model Switch.md")
        self.assertEqual(contract["record_path_template"], "<owner-routing>/<Category>.md")
        self.assertTrue(contract["native_wikilink_graph"])
        self.assertEqual(contract["stable_categories"], list(model_registry.MODEL_SWITCH_CATEGORIES))
        self.assertTrue(contract["event_id_dedupe"])
        self.assertEqual(contract["specificity_order"], ["project_task", "module", "file", "symbol"])
        self.assertTrue(contract["fields_only"])
        self.assertFalse(contract["hierarchy_notes"])

    def test_ensure_bootstraps_once_and_explicit_refresh_applies_catalog_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            cache_path.write_text(json.dumps(self.current_catalog()), encoding="utf-8")
            generated = model_registry.ensure_registry(registry_path, cache_path)
            self.assertEqual(generated["status"], "generated")
            updated_catalog = self.current_catalog()
            updated_catalog["client_version"] = "2.0.0"
            cache_path.write_text(json.dumps(updated_catalog), encoding="utf-8")
            loaded = model_registry.ensure_registry(registry_path, cache_path)
            self.assertEqual(loaded["status"], "loaded")
            self.assertEqual(loaded["registry"]["source"]["client_version"], "1.2.3")
            refreshed = model_registry.refresh_registry(registry_path, cache_path)
            self.assertEqual(refreshed["status"], "refreshed")
            self.assertEqual(refreshed["registry"]["source"]["client_version"], "2.0.0")

    def test_semantic_digest_and_explicit_refresh_ignore_only_volatile_wrapper_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            initial_catalog = self.current_catalog()
            initial_catalog["etag"] = "first"
            cache_path.write_text(json.dumps(initial_catalog), encoding="utf-8")
            _, initial_digest = model_registry.load_catalog(cache_path)
            generated = model_registry.ensure_registry(registry_path, cache_path)
            initial_content = registry_path.read_bytes()
            initial_mtime = registry_path.stat().st_mtime_ns
            refreshed_wrapper = deepcopy(initial_catalog)
            refreshed_wrapper["fetched_at"] = "2026-07-16T00:01:00Z"
            refreshed_wrapper["etag"] = "second"
            refreshed_wrapper["last_checked_at"] = "volatile"
            cache_path.write_text(json.dumps(refreshed_wrapper), encoding="utf-8")
            _, refreshed_digest = model_registry.load_catalog(cache_path)
            current = model_registry.refresh_registry(registry_path, cache_path)
            self.assertEqual(refreshed_digest, initial_digest)
            self.assertEqual(current["status"], "current")
            self.assertEqual(current["registry"]["source"]["fetched_at"], generated["registry"]["source"]["fetched_at"])
            self.assertEqual(registry_path.read_bytes(), initial_content)
            self.assertEqual(registry_path.stat().st_mtime_ns, initial_mtime)

    def test_explicit_refresh_applies_semantic_model_effort_visibility_and_client_version_changes(self):
        base_catalog = self.current_catalog()
        model_changed = deepcopy(base_catalog)
        model_changed["models"][0]["description"] = "Changed frontier positioning"
        effort_changed = deepcopy(base_catalog)
        effort_changed["models"][1]["supported_reasoning_levels"].append({"effort": "xhigh", "description": "xhigh"})
        visibility_changed = deepcopy(base_catalog)
        visibility_changed["models"][3]["visibility"] = "hide"
        version_changed = deepcopy(base_catalog)
        version_changed["client_version"] = "2.0.0"
        mutations = [("model", model_changed), ("effort", effort_changed), ("visibility", visibility_changed), ("version", version_changed)]
        for label, changed_catalog in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                cache_path = root / "models_cache.json"
                registry_path = root / "registry.json"
                cache_path.write_text(json.dumps(base_catalog), encoding="utf-8")
                generated = model_registry.ensure_registry(registry_path, cache_path)
                cache_path.write_text(json.dumps(changed_catalog), encoding="utf-8")
                refreshed = model_registry.refresh_registry(registry_path, cache_path)
                self.assertEqual(generated["status"], "generated")
                self.assertEqual(refreshed["status"], "refreshed")
                self.assertNotEqual(refreshed["registry"]["source"]["catalog_sha256"], generated["registry"]["source"]["catalog_sha256"])

    def test_explicit_refresh_rebuilds_generated_policy_when_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            cache_path.write_text(json.dumps(self.current_catalog()), encoding="utf-8")
            generated = model_registry.ensure_registry(registry_path, cache_path)
            stale_registry = deepcopy(generated["registry"])
            stale_registry["policy"]["quality_first"] = False
            model_registry.atomic_write_registry(registry_path, stale_registry)
            refreshed = model_registry.refresh_registry(registry_path, cache_path)
            self.assertEqual(refreshed["status"], "refreshed")
            self.assertTrue(refreshed["registry"]["policy"]["quality_first"])
            self.assertEqual(refreshed["registry"]["source"]["catalog_sha256"], generated["registry"]["source"]["catalog_sha256"])

    def test_ensure_loads_valid_registry_without_reading_local_catalog(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            registry = model_registry.build_registry(self.current_catalog(), "e" * 64)
            model_registry.atomic_write_registry(registry_path, registry)
            with mock.patch.object(model_registry, "load_catalog", side_effect=AssertionError("normal load scanned the catalog")):
                loaded = model_registry.ensure_registry(registry_path, cache_path)
            self.assertEqual(loaded, {"status": "loaded", "registry": registry})

    def test_explicit_refresh_retains_valid_registry_when_cache_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            registry = model_registry.build_registry(self.current_catalog(), "e" * 64)
            model_registry.atomic_write_registry(registry_path, registry)
            retained = model_registry.refresh_registry(registry_path, cache_path)
            self.assertEqual(retained["status"], "retained")
            self.assertEqual(retained["registry"], registry)
            self.assertIn("catalog_error", retained)

    def test_ensure_fails_clearly_without_catalog_or_saved_registry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(RuntimeError, "saved model registry is missing"):
                model_registry.ensure_registry(root / "registry.json", root / "models_cache.json")

    def test_explicit_refresh_fails_without_catalog_or_valid_registry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(RuntimeError, "no valid model registry exists"):
                model_registry.refresh_registry(root / "registry.json", root / "models_cache.json")


if __name__ == "__main__":
    unittest.main()
