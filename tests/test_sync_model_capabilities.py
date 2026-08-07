#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_model_capabilities


def catalog_model(slug, priority, description, modalities=("text", "image"), supported_in_api=True):
    levels = [{"effort": effort, "description": effort} for effort in ("low", "medium", "high")]
    return {"slug": slug, "display_name": slug.upper(), "description": description, "default_reasoning_level": "medium", "supported_reasoning_levels": levels, "visibility": "list", "supported_in_api": supported_in_api, "priority": priority, "additional_speed_tiers": [], "input_modalities": list(modalities), "context_window": 200000}


def catalog():
    models = [catalog_model("gpt-9.9-expert", 1, "Frontier model"), catalog_model("gpt-9.9-balanced", 10, "Balanced everyday model"), catalog_model("gpt-9.9-economy", 20, "Small model"), catalog_model("gpt-9.8-legacy", 25, "Older model"), catalog_model("quick-code", 30, "Ultra-fast coding model", modalities=("text",), supported_in_api=False)]
    return {"client_version": "9.9.9", "fetched_at": "2026-07-15T01:00:00Z", "models": models}


class SyncModelCapabilitiesTests(unittest.TestCase):
    def test_snapshot_uses_same_catalog_digest_and_every_registry_model(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "models_cache.json"
            cache_path.write_text(json.dumps(catalog()), encoding="utf-8")
            registry, snapshot = sync_model_capabilities.desired_outputs(cache_path)
            self.assertIn(registry["source"]["catalog_sha256"], snapshot)
            self.assertIn("`quick-code`", snapshot)
            self.assertIn("Active quality family: `gpt-9.9`", snapshot)
            # An older visible family is deliberately excluded from the registry and its
            # rendered snapshot -- see test_model_registry's
            # test_older_visible_catalog_models_do_not_change_selected_digest_or_registry.
            self.assertNotIn("`gpt-9.8-legacy`", snapshot)
            for model in registry["models"]:
                self.assertIn(f"`{model['id']}`", snapshot)
            self.assertIn("Only the highest registered numeric GPT family is active", snapshot)
            self.assertIn("change only when the user explicitly runs the manual update command", snapshot)
            self.assertIn("sync_model_capabilities.py --update", snapshot)

    def test_sync_and_check_cover_registry_and_markdown_together(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            snapshot_path = root / "model-capabilities.md"
            cache_path.write_text(json.dumps(catalog()), encoding="utf-8")
            synchronized = sync_model_capabilities.sync_outputs(cache_path, registry_path, snapshot_path)
            self.assertEqual(synchronized["status"], "generated")
            current = sync_model_capabilities.check_outputs(cache_path, registry_path, snapshot_path)
            self.assertEqual(current["registry_status"], "current")
            self.assertEqual(current["snapshot_status"], "current")
            self.assertTrue(current["valid"])
            snapshot_path.write_text("stale", encoding="utf-8")
            stale = sync_model_capabilities.check_outputs(cache_path, registry_path, snapshot_path)
            self.assertEqual(stale["registry_status"], "current")
            self.assertEqual(stale["snapshot_status"], "stale")
            self.assertFalse(stale["valid"])

    def test_fetched_at_only_change_keeps_synced_outputs_current(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            snapshot_path = root / "model-capabilities.md"
            initial_catalog = catalog()
            cache_path.write_text(json.dumps(initial_catalog), encoding="utf-8")
            sync_model_capabilities.sync_outputs(cache_path, registry_path, snapshot_path)
            initial_registry = registry_path.read_bytes()
            initial_mtime = registry_path.stat().st_mtime_ns
            initial_snapshot = snapshot_path.read_bytes()
            initial_snapshot_mtime = snapshot_path.stat().st_mtime_ns
            initial_catalog["fetched_at"] = "2026-07-15T01:30:00Z"
            initial_catalog["etag"] = "volatile"
            cache_path.write_text(json.dumps(initial_catalog), encoding="utf-8")
            current = sync_model_capabilities.check_outputs(cache_path, registry_path, snapshot_path)
            sync_model_capabilities.sync_outputs(cache_path, registry_path, snapshot_path)
            self.assertTrue(current["valid"])
            self.assertEqual(current["registry_status"], "current")
            self.assertEqual(current["snapshot_status"], "current")
            self.assertEqual(registry_path.read_bytes(), initial_registry)
            self.assertEqual(registry_path.stat().st_mtime_ns, initial_mtime)
            self.assertEqual(snapshot_path.read_bytes(), initial_snapshot)
            self.assertEqual(snapshot_path.stat().st_mtime_ns, initial_snapshot_mtime)

    def test_explicit_update_retains_saved_registry_when_local_catalog_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            snapshot_path = root / "model-capabilities.md"
            cache_path.write_text(json.dumps(catalog()), encoding="utf-8")
            sync_model_capabilities.sync_outputs(cache_path, registry_path, snapshot_path)
            registry_bytes = registry_path.read_bytes()
            cache_path.unlink()
            retained = sync_model_capabilities.sync_outputs(cache_path, registry_path, snapshot_path)
            self.assertEqual(retained["status"], "retained")
            self.assertEqual(registry_path.read_bytes(), registry_bytes)
            checked = sync_model_capabilities.check_outputs(cache_path, registry_path, snapshot_path)
            self.assertEqual(checked["registry_status"], "retained")
            self.assertEqual(checked["catalog_status"], "unavailable")
            self.assertTrue(checked["valid"])

    def test_cli_updates_then_checks_both_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            snapshot_path = root / "model-capabilities.md"
            cache_path.write_text(json.dumps(catalog()), encoding="utf-8")
            command = [sys.executable, str(SCRIPTS_DIR / "sync_model_capabilities.py"), "--models-cache", str(cache_path), "--registry", str(registry_path), "--output", str(snapshot_path)]
            no_action = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(no_action.returncode, 0)
            update = subprocess.run(command + ["--update"], capture_output=True, text=True, check=False)
            self.assertEqual(update.returncode, 0, update.stderr)
            check = subprocess.run(command + ["--check"], capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertIn("model registry is current", check.stdout)
            self.assertIn("model capability snapshot is current", check.stdout)

    def test_cli_update_reports_retained_registry_when_local_catalog_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "models_cache.json"
            registry_path = root / "registry.json"
            snapshot_path = root / "model-capabilities.md"
            cache_path.write_text(json.dumps(catalog()), encoding="utf-8")
            command = [sys.executable, str(SCRIPTS_DIR / "sync_model_capabilities.py"), "--models-cache", str(cache_path), "--registry", str(registry_path), "--output", str(snapshot_path), "--update"]
            generated = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            cache_path.unlink()
            retained = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(retained.returncode, 0, retained.stderr)
            self.assertIn("retained saved model registry", retained.stdout)


if __name__ == "__main__":
    unittest.main()
