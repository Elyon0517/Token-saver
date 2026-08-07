import importlib.util
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "project_change_memory.py"
SPEC = importlib.util.spec_from_file_location("project_change_memory", SCRIPT_PATH)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


class ProjectChangeMemoryTests(unittest.TestCase):
    def test_root_first_vault_never_creates_legacy_projection_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            vault = Path(temporary_directory) / "vault"
            runtime = vault / "AI Memory" / "ai_memory.py"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("from pathlib import Path\n\ndef record_event(project, module, event_type, summary, reason, result, verification_status, **kwargs):\n    Path(__file__).with_name('called.txt').write_text(project + '|' + module, encoding='utf-8')\n    return {'status': 'written', 'event_id': 'root-event'}\n\ndef render_views():\n    Path(__file__).with_name('rendered.txt').write_text('yes', encoding='utf-8')\n", encoding="utf-8")
            record = {"project": {"root": ".", "name": "Example"}, "module": "runtime", "summary": "Updated runtime", "reason": "Keep one memory", "result": "Passed", "verification_status": "passed", "files": ["src.py"], "verification": [], "decisions": [], "risks": [], "id": "test-id", "recorded_at": "2026-08-01T00:00:00Z", "change_kind": "edit", "scope": "code", "supersedes": ""}
            output = MEMORY._write_obsidian(record, vault)
            self.assertEqual(output["status"], "written")
            self.assertEqual(output["root"], "AI Memory/events.jsonl")
            self.assertEqual(output["event_status"], "written")
            self.assertEqual(output["event_id"], "root-event")
            self.assertEqual((runtime.parent / "called.txt").read_text(encoding="utf-8"), "Example|runtime")
            self.assertEqual((runtime.parent / "rendered.txt").read_text(encoding="utf-8"), "yes")
            self.assertFalse((vault / "Journal").exists())
            self.assertFalse((vault / "Skills" / "Activity Index.md").exists())

    def test_windows_file_lock_uses_msvcrt_byte_lock(self):
        lock_handle = mock.Mock()
        lock_handle.tell.return_value = 0
        windows_lock = mock.Mock()
        windows_lock.LK_LOCK = 1
        with mock.patch.object(MEMORY.os, "name", "nt"), mock.patch.object(MEMORY, "msvcrt", windows_lock, create=True):
            MEMORY._acquire_file_lock(lock_handle)
        lock_handle.write.assert_called_once_with("\0")
        lock_handle.flush.assert_called_once_with()
        windows_lock.locking.assert_called_once_with(lock_handle.fileno(), windows_lock.LK_LOCK, 1)

    def test_journal_pointer_is_idempotent_and_recent_window_stays_bounded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            vault = Path(temporary_directory) / "vault"
            journal = vault / "Journal"
            history = vault / "Skills" / "Global Codex Skills History.md"
            journal.mkdir(parents=True)
            history.parent.mkdir(parents=True)
            history.write_text("# History\n", encoding="utf-8")
            existing = [f"- 2026-07-{day:02d} · [[Skills/Global Codex Skills History#^change-old-{day}|Old {day}]] · PASSED" for day in range(1, 26)]
            (journal / "log.md").write_text("# Journal Log\n\n" + "\n".join(existing) + "\n", encoding="utf-8")
            (journal / "index.md").write_text("# Journal\n\n## Recent\n\n<!-- BEGIN BOUNDED RECENT POINTERS -->\n<!-- END BOUNDED RECENT POINTERS -->\n", encoding="utf-8")
            record = {"id": "20260726T120000Z-new", "recorded_at": "2026-07-26T12:00:00Z", "summary": "New durable change", "verification_status": "passed"}
            MEMORY._write_journal_pointer(history, vault, record)
            MEMORY._write_journal_pointer(history, vault, record)
            log_lines = [line for line in (journal / "log.md").read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
            recent = (journal / "index.md").read_text(encoding="utf-8").split("<!-- BEGIN BOUNDED RECENT POINTERS -->", 1)[1].split("<!-- END BOUNDED RECENT POINTERS -->", 1)[0]
            recent_lines = [line for line in recent.splitlines() if line.startswith("- ")]
        self.assertEqual(len(log_lines), 26)
        self.assertEqual(sum("New durable change" in line for line in log_lines), 1)
        self.assertEqual(len(recent_lines), 20)
        self.assertIn("New durable change", recent_lines[-1])

    def test_real_owner_is_descendant_first_wins_and_block_activity_pointer(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            project = home / "Documents" / "Muse" / "SVGDrawer" / "feature"
            store = temporary / "store"
            vault = temporary / "vault"
            vault.mkdir()
            (project / "src").mkdir(parents=True)
            (project / "src" / "feature.py").write_text("result = 1\n", encoding="utf-8")
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                result = MEMORY.record_change(project, "feature-engine", "code", "edit", "Added editor update", "Preserve project scope", "Feature updates written", "passed", ["src/feature.py"], ["passed test"], ["Keep API stable"], ["none"], store=store, vault=vault, recorded_at=datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc))
                target, _ = MEMORY._canonical_history_target({"project": result["project"]}, Path(vault))
            activity = target.parent / "Activity Index.md"
            journal_log = vault / "Journal" / "log.md"
            journal_index = vault / "Journal" / "index.md"
            self.assertTrue(target.exists())
            self.assertTrue(activity.exists())
            self.assertTrue(journal_log.exists())
            self.assertTrue(journal_index.exists())
            index_text = activity.read_text(encoding="utf-8")
            self.assertIn("#^change-", index_text)
            self.assertIn("[[Projects/SVGDrawer/History#^change-", journal_log.read_text(encoding="utf-8"))
            recent = journal_index.read_text(encoding="utf-8").split("<!-- BEGIN BOUNDED RECENT POINTERS -->", 1)[1].split("<!-- END BOUNDED RECENT POINTERS -->", 1)[0]
            self.assertEqual(len([line for line in recent.splitlines() if line.startswith("- ")]), 1)
            self.assertEqual(target.parent, vault / "Projects" / "SVGDrawer")

    def test_same_basename_clone_is_local_only_and_unknown_date_root_is_unmatched(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            canonical_root = home / "Documents" / "Muse" / "SVGDrawer"
            same_name_clone = temporary / "other" / "SVGDrawer"
            temporary_date_root = home / "Documents" / "Codex" / "2026" / "task"
            canonical_root.mkdir(parents=True)
            same_name_clone.mkdir(parents=True)
            temporary_date_root.mkdir(parents=True)
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                canonical_target, canonical_title = MEMORY._canonical_history_target({"project": {"root": str(canonical_root)}}, Path("/tmp/vault"))
                clone_target, clone_title = MEMORY._canonical_history_target({"project": {"root": str(same_name_clone)}}, Path("/tmp/vault"))
                codex_target, _ = MEMORY._canonical_history_target({"project": {"root": str(temporary_date_root)}}, Path("/tmp/vault"))
            self.assertEqual(canonical_title, "SVGDrawer")
            self.assertEqual(str(canonical_target), "/tmp/vault/Projects/SVGDrawer/History.md")
            self.assertIsNone(clone_target)
            self.assertIsNone(codex_target)

    def test_current_and_historical_registered_roots_share_recall(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            old_root = home / "Documents" / "YofaGames" / "XNews"
            current_root = home / "Documents" / "PythonProject" / "XNews"
            store = temporary / "store"
            (old_root / "src").mkdir(parents=True)
            (current_root / "src").mkdir(parents=True)
            (old_root / "src" / "feed.py").write_text("value = 1\n", encoding="utf-8")
            (current_root / "src" / "feed.py").write_text("value = 2\n", encoding="utf-8")
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                written = MEMORY.record_change(old_root, "feed", "file", "edit", "Updated feed parser", "Preserve parsed stories", "Old-root change recorded", "passed", ["src/feed.py"], ["focused test passed"], store=store, vault=temporary / "missing")
                recalled = MEMORY.search_records(current_root, "feed", ["src/feed.py"], "feed parser", 8, store, include_ambiguous=True)
            self.assertEqual(written["status"], "written")
            self.assertEqual(recalled["matches"][0]["id"], written["record_id"])

    def test_supersedes_accepts_registered_project_move_but_rejects_clone(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            old_root = home / "Documents" / "YofaGames" / "Destiny"
            current_root = home / "Documents" / "FilesManagement" / "Destiny"
            clone = temporary / "other" / "Destiny"
            store = temporary / "store"
            for root in (old_root, current_root, clone):
                root.mkdir(parents=True)
                (root / "config.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                failed = MEMORY.record_change(old_root, "config", "file", "edit", "Changed config", "Apply requested setting", "Verification failed", "failed", ["config.json"], ["wrong value"], store=store, vault=temporary / "missing")
                repaired = MEMORY.record_change(current_root, "config", "file", "edit", "Repaired config", "Fix verified failure", "Verification passed", "passed", ["config.json"], ["focused test passed"], supersedes=failed["record_id"], store=store, vault=temporary / "missing")
                with self.assertRaisesRegex(ValueError, "same project"):
                    MEMORY.record_change(clone, "config", "file", "edit", "Clone repair", "Must remain isolated", "Not accepted", "passed", ["config.json"], ["probe"], supersedes=failed["record_id"], store=store, vault=temporary / "missing")
            self.assertEqual(repaired["status"], "written")

    def test_registry_contains_all_current_project_roots_without_absolute_literals(self):
        expected = {
            "Muse/SVGDrawer", "Muse/MuseAI", "Muse/UserExamples", "YofaGames/ThisIsMyOregon",
            "YofaGames/AIAnimation2D", "YofaGames/AIShaderGraphic2D", "YofaGames/AIVFX2D",
            "FilesManagement/Destiny", "YofaGames/FunctionWebsite",
            "Unity3DPersonalProject/MetaStory", "Unity3DPersonalProject/UnityCodexTest",
            "PythonProject/XNews", "Muse/taggingapilandingpage",
            "PythonProject/Agent-ImageEdtior", "DockerProject/Docker-Mokozoo",
        }
        registered = {relative for relative, _ in MEMORY.DOCUMENT_PROJECT_OWNER_ROOTS}
        self.assertTrue(expected <= registered)
        self.assertFalse(any(str(relative).startswith(("/", "~")) for relative in registered))

    def test_muse_userexamples_is_museai_alias_while_same_name_clone_is_isolated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            museai_root = home / "Documents" / "Muse" / "MuseAI"
            userexamples_root = home / "Documents" / "Muse" / "UserExamples"
            same_name_clone = temporary / "other" / "UserExamples"
            for root in (museai_root, userexamples_root, same_name_clone):
                root.mkdir(parents=True)
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                museai_owner = MEMORY._registered_owner(museai_root)
                userexamples_owner = MEMORY._registered_owner(userexamples_root)
                clone_owner = MEMORY._registered_owner(same_name_clone)
                museai_target, museai_title = MEMORY._canonical_history_target({"project": {"root": str(museai_root)}}, Path("/tmp/vault"))
                userexamples_target, userexamples_title = MEMORY._canonical_history_target({"project": {"root": str(userexamples_root)}}, Path("/tmp/vault"))
                clone_target, clone_title = MEMORY._canonical_history_target({"project": {"root": str(same_name_clone)}}, Path("/tmp/vault"))
            self.assertEqual(museai_owner, "MuseAI")
            self.assertEqual(userexamples_owner, museai_owner)
            self.assertIsNone(clone_owner)
            self.assertEqual(userexamples_target, museai_target)
            self.assertEqual(userexamples_title, museai_title)
            self.assertIsNone(clone_target)
            self.assertEqual(clone_title, "")

    def test_global_codex_root_is_skills_history_with_longest_match(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            canonical_root = home / ".codex"
            descendants = canonical_root / "nested" / "project"
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                canonical_target, canonical_title = MEMORY._canonical_history_target({"project": {"root": str(canonical_root)}}, Path("/tmp/vault"))
                descendant_target, descendant_title = MEMORY._canonical_history_target({"project": {"root": str(descendants)}}, Path("/tmp/vault"))
                clone_target, _ = MEMORY._canonical_history_target({"project": {"root": str(temporary / "other" / ".codex")}}, Path("/tmp/vault"))
            self.assertEqual(canonical_title, "Global Codex Skills")
            self.assertEqual(str(canonical_target), "/tmp/vault/Skills/Global Codex Skills History.md")
            self.assertEqual(descendant_title, "Global Codex Skills")
            self.assertEqual(str(descendant_target), "/tmp/vault/Skills/Global Codex Skills History.md")
            self.assertIsNone(clone_target)

    def test_global_codex_skill_source_checkout_shares_global_owner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            source_root = home / "Documents" / "AIProject" / "qin-codex-skills"
            source_root.mkdir(parents=True)
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                owner = MEMORY._registered_owner(source_root)
            self.assertEqual(owner, "Global Codex Skills")

    def test_cache_descendants_do_not_inherit_registered_project_memory_owner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            home = temporary / "home"
            cache_fixture = home / ".codex" / "Cache" / "tests" / "fixture-project"
            cache_fixture.mkdir(parents=True)
            with mock.patch.object(MEMORY.Path, "home", lambda: home):
                owner = MEMORY._registered_owner(cache_fixture)
                target, title = MEMORY._canonical_history_target(
                    {"project": {"root": str(cache_fixture)}},
                    temporary / "vault",
                )
        self.assertIsNone(owner)
        self.assertIsNone(target)
        self.assertEqual(title, "")

    def test_exact_vault_root_uses_source_ingest_page_but_same_name_clone_is_unmatched(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            vault = temporary / "vault"
            clone = temporary / "clone" / "vault"
            vault.mkdir()
            clone.mkdir(parents=True)
            target, title = MEMORY._canonical_history_target({"project": {"root": str(vault)}}, vault)
            clone_target, clone_title = MEMORY._canonical_history_target({"project": {"root": str(clone)}}, vault)
        self.assertEqual(title, "Source Ingest and Wiki Maintenance")
        self.assertEqual(target, vault / "Knowledge" / "Source Ingest and Wiki Maintenance.md")
        self.assertIsNone(clone_target)
        self.assertEqual(clone_title, "")

    def test_legacy_vault_uses_existing_knowledgeareas_without_creating_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            vault = Path(temporary_directory) / "vault"
            (vault / "KnowledgeAreas").mkdir(parents=True)
            target, title = MEMORY._canonical_history_target({"project": {"root": str(vault)}}, vault)
        self.assertEqual(title, "Source Ingest and Wiki Maintenance")
        self.assertEqual(target, vault / "KnowledgeAreas" / "Source Ingest and Wiki Maintenance.md")
        self.assertFalse((vault / "Knowledge").exists())

    def test_migrated_vault_projects_to_knowledge_and_ignores_legacy_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            vault = root / "vault"
            store = root / "store"
            (vault / "Knowledge").mkdir(parents=True)
            (vault / "KnowledgeAreas").mkdir()
            (vault / "Knowledge" / "index.md").write_text("# Knowledge\n", encoding="utf-8")
            result = MEMORY.record_change(vault, "knowledge-runtime", "project", "edit", "Updated compiled knowledge", "Use the migrated canonical knowledge layer", "Canonical history was projected", "passed", ["Knowledge/index.md"], ["focused projection test passed"], ["Never dual-write legacy folders"], ["none"], store=store, vault=vault, recorded_at=datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc))
            canonical_history = vault / "Knowledge" / "Source Ingest and Wiki Maintenance.md"
            canonical_activity = vault / "Knowledge" / "Activity Index.md"
            journal_log = vault / "Journal" / "log.md"
            legacy_history = vault / "KnowledgeAreas" / "Source Ingest and Wiki Maintenance.md"
            legacy_activity = vault / "KnowledgeAreas" / "Activity Index.md"
            canonical_history_exists = canonical_history.exists()
            canonical_activity_exists = canonical_activity.exists()
            journal_text = journal_log.read_text(encoding="utf-8")
            legacy_history_exists = legacy_history.exists()
            legacy_activity_exists = legacy_activity.exists()
        self.assertEqual(result["obsidian"]["root"], "Knowledge/Source Ingest and Wiki Maintenance.md")
        self.assertTrue(canonical_history_exists)
        self.assertTrue(canonical_activity_exists)
        self.assertIn("[[Knowledge/Source Ingest and Wiki Maintenance#^change-", journal_text)
        self.assertFalse(legacy_history_exists)
        self.assertFalse(legacy_activity_exists)

    def test_real_absolute_nested_root_uses_most_specific_svgdrawer_owner(self):
        record = {"project": {"name": "skill", "root": str(Path.home() / "Documents" / "Muse" / "SVGDrawer" / "skill")}}
        target, title = MEMORY._canonical_history_target(record, Path("/tmp/vault"))
        self.assertEqual(target, Path("/tmp/vault/Projects/SVGDrawer/History.md"))
        self.assertEqual(title, "SVGDrawer")

    def test_record_search_duplicate_and_obsidian_projection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "ExampleProject"
            store = root / "store"
            vault = root / "vault"
            project.mkdir()
            vault.mkdir()
            (project / "src").mkdir()
            (project / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
            recorded_at = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)
            first = MEMORY.record_change(project, "feature-engine", "code", "edit", "Added stable feature behavior", "Preserve the public contract while fixing the implementation", "Focused behavior now passes", "passed", ["src/feature.py"], ["python unit test passed"], ["Keep the public key exact"], ["none"], store=store, vault=vault, recorded_at=recorded_at)
            duplicate = MEMORY.record_change(project, "feature-engine", "code", "edit", "Added stable feature behavior", "Preserve the public contract while fixing the implementation", "Focused behavior now passes", "passed", ["src/feature.py"], ["python unit test passed"], ["Keep the public key exact"], ["none"], store=store, vault=vault, recorded_at=recorded_at)
            search = MEMORY.search_records(project, "feature-engine", ["src/feature.py"], "stable feature", 8, store, include_ambiguous=True)
            self.assertEqual(first["status"], "written")
            self.assertEqual(first["obsidian"]["status"], "no-op")
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(search["matches"][0]["reason"], "Preserve the public contract while fixing the implementation")
            self.assertEqual(len((store / "index.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertFalse((vault / "Projects" / "ExampleProject").exists())

    def test_project_change_recall_isolated_by_session_and_task_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "ExampleProject"
            store = root / "store"
            project.mkdir()
            (project / "step1.py").write_text("value = 1\n", encoding="utf-8")
            first = MEMORY.record_change(project, "routing", "file", "edit", "Step one change", "Keep step one isolated", "Step one passed", "passed", ["step1.py"], ["scope check"], store=store, vault=root / "missing", task_name="step-one", session_id="019fc8e5-87da-7082-90b9-6d505404d229")
            second = MEMORY.record_change(project, "routing", "file", "edit", "Step two change", "Keep step two isolated", "Step two passed", "passed", ["step1.py"], ["scope check"], store=store, vault=root / "missing", task_name="step-two", session_id="019fc8e5-87da-7082-90b9-6d505404d230")
            first_search = MEMORY.search_records(project, "routing", ["step1.py"], "change", 8, store, True, "step-one", "019fc8e5-87da-7082-90b9-6d505404d229")
            second_search = MEMORY.search_records(project, "routing", ["step1.py"], "change", 8, store, True, "step-two", "019fc8e5-87da-7082-90b9-6d505404d230")
        self.assertEqual([match["id"] for match in first_search["matches"]], [first["record_id"]])
        self.assertEqual([match["id"] for match in second_search["matches"]], [second["record_id"]])

    def test_project_change_recall_reads_related_task_group_across_sessions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "ExampleProject"
            store = root / "store"
            project.mkdir()
            (project / "pipeline.py").write_text("value = 1\n", encoding="utf-8")
            first = MEMORY.record_change(project, "routing", "file", "edit", "Step one shared change", "Use the shared pipeline scope", "Step one passed", "passed", ["pipeline.py"], ["scope check"], store=store, vault=root / "missing", task_name="step-one", task_group="shared-pipeline", session_id="019fc8e5-87da-7082-90b9-6d505404d229")
            second = MEMORY.record_change(project, "routing", "file", "edit", "Step two shared change", "Use the shared pipeline scope", "Step two passed", "passed", ["pipeline.py"], ["scope check"], store=store, vault=root / "missing", task_name="step-two", task_group="shared-pipeline", session_id="019fc8e5-87da-7082-90b9-6d505404d230")
            unrelated = MEMORY.record_change(project, "routing", "file", "edit", "Other pipeline change", "Use a different pipeline scope", "Other passed", "passed", ["pipeline.py"], ["scope check"], store=store, vault=root / "missing", task_name="other-step", task_group="other-pipeline", session_id="019fc8e5-87da-7082-90b9-6d505404d231")
            search = MEMORY.search_records(project, "routing", ["pipeline.py"], "change", 8, store, True, "step-two", "019fc8e5-87da-7082-90b9-6d505404d230", "shared-pipeline")
        self.assertEqual({match["id"] for match in search["matches"]}, {first["record_id"], second["record_id"]})
        self.assertNotIn(unrelated["record_id"], {match["id"] for match in search["matches"]})
        self.assertEqual({match["task_group"] for match in search["matches"]}, {"shared-pipeline"})

    def test_record_serialization_and_cli_result_do_not_expose_absolute_machine_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "ExampleProject"
            store = root / "store"
            project.mkdir()
            (project / "src.py").write_text("value = 1\n", encoding="utf-8")
            result = MEMORY.record_change(
                project,
                "portable-paths",
                "file",
                "edit",
                "Recorded portable project identity",
                "Keep local paths out of AI memory",
                "Record uses only relative path labels",
                "passed",
                ["src.py"],
                ["focused serialization check passed"],
                store=store,
                vault=root / "missing-vault",
            )
            record_path = store / result["local"]["record"]
            serialized = record_path.read_text(encoding="utf-8")
            rendered_result = str(result)

        self.assertEqual(result["project"]["root"], ".")
        self.assertEqual(result["local"]["store"], "store")
        self.assertFalse(Path(result["local"]["record"]).is_absolute())
        self.assertNotIn(str(root), serialized)
        self.assertNotIn(str(root), rendered_result)

    def test_search_is_scoped_to_current_working_line(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            project = root / "project-mainline"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
            (project / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
            main_line = {
                "identity_scope": "scoped",
                "canonical_remote": "https://github.com/example/project.git",
                "branch": "main",
                "commit": "aaa",
                "version": "",
            }
            stale_line = {
                "identity_scope": "scoped",
                "canonical_remote": "https://github.com/example/project.git",
                "branch": "main",
                "commit": "bbb",
                "version": "",
            }
            active_line = {"value": main_line}

            def derive_line(project_root):
                return active_line["value"]

            with mock.patch.object(MEMORY, "_derive_working_line", side_effect=derive_line):
                main_record = MEMORY.record_change(project, "runtime", "code", "edit", "Mainline runtime update", "Use current branch line", "Pass", "passed", ["src/feature.py"], ["unit check"], ["Keep branch identity"], ["none"], store=store, vault=root / "missing-vault")
                active_line["value"] = stale_line
                MEMORY.record_change(project, "runtime", "code", "edit", "Stale branch update", "Mature on old commit", "Pass", "passed", ["src/feature.py"], ["unit check"], ["Keep branch identity"], ["none"], store=store, vault=root / "missing-vault")
                active_line["value"] = main_line
                scoped = MEMORY.search_records(project, "runtime", ["src/feature.py"], "runtime", 8, store)
                all_records = MEMORY.search_records(project, "runtime", ["src/feature.py"], "runtime", 8, store, include_ambiguous=True)

            self.assertEqual(len(scoped["matches"]), 1)
            self.assertEqual(scoped["matches"][0]["id"], main_record["record_id"])
            self.assertEqual(len(all_records["matches"]), 2)

    def test_supersede_rejects_working_line_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "store"
            project = root / "mainline"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src" / "config.py").write_text("value = 1\n", encoding="utf-8")
            main_line = {
                "identity_scope": "scoped",
                "canonical_remote": "https://github.com/example/project.git",
                "branch": "main",
                "commit": "1111",
                "version": "",
            }
            stale_line = {
                "identity_scope": "scoped",
                "canonical_remote": "https://github.com/example/project.git",
                "branch": "main",
                "commit": "2222",
                "version": "",
            }
            active_line = {"value": main_line}

            def derive_line(project_root):
                return active_line["value"]

            with mock.patch.object(MEMORY, "_derive_working_line", side_effect=derive_line):
                active_line["value"] = stale_line
                failed = MEMORY.record_change(project, "runtime", "file", "edit", "Mainline failure", "Needs repair", "Still failing", "failed", ["src/config.py"], ["baseline"], ["Must retain line"], ["none"], store=store, vault=root / "missing-vault")
                active_line["value"] = main_line
                MEMORY.record_change(project, "runtime", "file", "edit", "Stale branch change", "Different branch", "Passed", "passed", ["src/config.py"], ["baseline"], ["Keep branch identity"], ["none"], store=store, vault=root / "missing-vault")
                with self.assertRaisesRegex(ValueError, "same project working line"):
                    MEMORY.record_change(project, "runtime", "file", "edit", "Repair attempt", "Corrects failure", "Passed", "passed", ["src/config.py"], ["baseline"], ["Repair needs same line"], ["none"], supersedes=failed["record_id"], store=store, vault=root / "missing-vault")

    def test_rejects_files_outside_project(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            with self.assertRaises(ValueError):
                MEMORY.record_change(project, "project-wide", "project", "edit", "Changed settings", "Match the requested behavior", "Settings updated", "not-run", [root / "outside.txt"], store=root / "store", vault=root / "missing-vault")

    def test_failed_record_is_written_before_repair_supersedes_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            store = root / "store"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding="utf-8")
            failed = MEMORY.record_change(project, "runtime", "file", "edit", "Changed runtime value", "Implement the requested behavior", "Ending Real found an incorrect value", "failed", ["script.py"], ["Expected 2 but observed 1"], ["Repair is a new lifecycle"], ["Incorrect durable edit remains"], store=store, vault=root / "missing-vault")
            repaired = MEMORY.record_change(project, "runtime", "file", "edit", "Repaired runtime value", "Correct the verified failure", "Independent Ending Real passed", "passed", ["script.py"], ["Focused regression passed"], ["Preserve the verified value"], ["none"], supersedes=failed["record_id"], store=store, vault=root / "missing-vault")
            records = MEMORY._read_records(store / "index.jsonl")
            self.assertEqual(failed["status"], "written")
            self.assertEqual(repaired["status"], "written")
            self.assertEqual(records[1]["supersedes"], failed["record_id"])
            self.assertEqual([record["verification_status"] for record in records], ["failed", "passed"])

    def test_supersedes_rejects_unknown_or_unrelated_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            store = root / "store"
            project.mkdir()
            (project / "script.py").write_text("value = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "existing record"):
                MEMORY.record_change(project, "runtime", "file", "edit", "Repair", "Correct failure", "Passed", "passed", ["script.py"], ["test passed"], supersedes="missing-record", store=store, vault=root / "missing-vault")


if __name__ == "__main__":
    unittest.main()
