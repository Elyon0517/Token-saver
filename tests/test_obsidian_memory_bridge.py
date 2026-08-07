#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "obsidian_memory_bridge.py"
MODULE_SPEC = importlib.util.spec_from_file_location("obsidian_memory_bridge", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class ObsidianMemoryBridgeTests(unittest.TestCase):
    def test_missing_vault_is_non_blocking(self):
        missing = Path(tempfile.gettempdir()) / "missing-obsidian-memory-bridge-vault"
        self.assertEqual(module.search_memory("routing failure", missing)["status"], "unavailable")

    def test_search_prefers_related_project_knowledge_and_bounds_digest(self):
        with tempfile.TemporaryDirectory(prefix="obsidian-memory-search-") as temporary:
            vault = Path(temporary)
            (vault / "Skills").mkdir()
            (vault / "Projects" / "MuseAI").mkdir(parents=True)
            (vault / "Skills" / "Knowledge.md").write_text("General Python routing guidance.\n", encoding="utf-8")
            (vault / "Projects" / "MuseAI" / "Knowledge.md").write_text("MuseAI coordinate failure and exact correction.\n", encoding="utf-8")
            result = module.search_memory("MuseAI coordinate failure", vault, max_results=2, max_chars=120)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["matches"][0]["path"], "Projects/MuseAI/Knowledge.md")
            self.assertLessEqual(len(result["digest"]), 120)

    def test_empty_query_has_no_matches(self):
        with tempfile.TemporaryDirectory(prefix="obsidian-memory-empty-") as temporary:
            self.assertEqual(module.search_memory("the and this", Path(temporary))["status"], "no_matches")

    def test_migrated_vault_searches_knowledge_preferences_and_ignores_legacy_roots(self):
        with tempfile.TemporaryDirectory(prefix="obsidian-memory-canonical-") as temporary:
            vault = Path(temporary)
            (vault / "Knowledge" / "Preferences").mkdir(parents=True)
            (vault / "KnowledgeAreas").mkdir()
            (vault / "AestheticTaste").mkdir()
            (vault / "Knowledge" / "Preferences" / "Visual.md").write_text("Compact interface rows are preferred for visual controls.\n", encoding="utf-8")
            (vault / "KnowledgeAreas" / "Legacy.md").write_text("legacy-only-marker visual controls\n", encoding="utf-8")
            (vault / "AestheticTaste" / "Legacy.md").write_text("legacy-only-marker visual controls\n", encoding="utf-8")
            canonical = module.search_memory("compact interface rows", vault)
            ignored_legacy = module.search_memory("legacy-only-marker", vault)
        self.assertEqual(canonical["matches"][0]["path"], "Knowledge/Preferences/Visual.md")
        self.assertEqual(ignored_legacy["status"], "no_matches")

    def test_legacy_vault_searches_old_knowledge_and_taste_roots(self):
        with tempfile.TemporaryDirectory(prefix="obsidian-memory-legacy-") as temporary:
            vault = Path(temporary)
            (vault / "KnowledgeAreas").mkdir()
            (vault / "AestheticTaste").mkdir()
            (vault / "KnowledgeAreas" / "Verification.md").write_text("Receipt verification boundary.\n", encoding="utf-8")
            (vault / "AestheticTaste" / "Visual.md").write_text("Horizontal control layout preference.\n", encoding="utf-8")
            knowledge = module.search_memory("receipt verification", vault)
            preference = module.search_memory("horizontal control", vault)
        self.assertEqual(knowledge["matches"][0]["path"], "KnowledgeAreas/Verification.md")
        self.assertEqual(preference["matches"][0]["path"], "AestheticTaste/Visual.md")


if __name__ == "__main__":
    unittest.main()
