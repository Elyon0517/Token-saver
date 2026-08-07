#!/usr/bin/env python3
import importlib.util
import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "routing_policy.py"
MODULE_SPEC = importlib.util.spec_from_file_location("routing_policy", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class RoutingPolicyTests(unittest.TestCase):
    def test_grounded_repository_presets_derive_full_ladder_internally(self):
        easy = module.resolve_profile_preset("grounded-repository-answer-easy", project_family="global", owning_skill="workflow-skill")
        complex_profile = module.resolve_profile_preset("grounded-repository-answer-complex", project_family="museai", owning_skill="muse-ai-plugin:muse-ai-dev-skill")
        self.assertEqual(easy["candidate_ladder"], module.normal_adaptive_pair_texts())
        self.assertEqual(complex_profile["candidate_ladder"], module.normal_adaptive_pair_texts())
        self.assertEqual(complex_profile["static_suggestion"], module.MODEL_ROLE_PAIRS["balanced_complex"])
        self.assertEqual(complex_profile["hard_floor"], module.MODEL_ROLE_PAIRS["floor"])

    def test_tiny_presets_use_shared_dynamic_quality_ladder(self):
        tiny_text = module.resolve_profile_preset("tiny-text", project_family="global")
        tiny_code = module.resolve_profile_preset("tiny-code", project_family="global", execution_domain="python")
        expected = module.normal_adaptive_pair_texts()
        self.assertEqual(tiny_text["candidate_ladder"], expected)
        self.assertEqual(tiny_code["candidate_ladder"], expected)
        self.assertEqual(tiny_text["static_suggestion"], module.MODEL_ROLE_PAIRS["floor"])
        self.assertEqual(tiny_text["hard_floor"], module.MODEL_ROLE_PAIRS["floor"])

    def test_code_presets_support_python_csharp_and_unity_without_duplicate_rows(self):
        domains = ["python", "csharp", "unity_csharp"]
        profiles = [module.resolve_profile_preset("code-complex", project_family="global", execution_domain=domain) for domain in domains]
        self.assertEqual([profile["execution_domain"] for profile in profiles], domains)
        self.assertTrue(all(profile["owning_skill"] == "code-skill" for profile in profiles))
        self.assertTrue(all(profile["candidate_ladder"] == module.normal_adaptive_pair_texts() for profile in profiles))

    def test_code_preset_automatically_accepts_new_active_code_domain(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            module.EXECUTION_DOMAINS["rust"] = {"display_name": "Rust", "kind": "code", "language_aliases": ["rust"], "owner_skill": "code-skill", "owner_enforced": True, "spark_first": True, "reference_path": "contracts/references/python-rules.md", "active": True, "history_only": False}
            profile = module.resolve_profile_preset("code-easy", project_family="global", execution_domain="rust")
            self.assertEqual(profile["execution_domain"], "rust")
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_normal_adaptive_ladder_uses_registry_floor_and_ceiling_without_priority_producer(self):
        ladder = module.normal_adaptive_ladder()
        self.assertEqual(ladder[0], module.parse_pair(module.MODEL_ROLE_PAIRS["floor"]))
        self.assertEqual(ladder[-1], (module.ACTIVE_MODEL_ROWS[-1]["id"], module.ACTIVE_MODEL_ROWS[-1]["codex_efforts"][-1]))
        self.assertTrue(all(model != module.PRIORITY_PRODUCER_MODEL for model, _ in ladder))
        self.assertEqual(module.downgrade_pair(ladder[-1], ladder), ladder[-2])
        self.assertEqual(module.upgrade_pair(ladder[-2], ladder), ladder[-1])

    def test_shared_registry_drives_active_rank_efforts_and_policy(self):
        rows = module.public_model_capability_rows()
        model_ids = [row["id"] for row in rows["models"]]
        self.assertEqual(rows["schema_version"], 2)
        self.assertEqual(model_ids, module.ACTIVE_MODEL_ORDER)
        self.assertEqual(rows["active_family"]["selection"], "highest_numeric_gpt_family")
        self.assertEqual(rows["active_family"]["model_count"], len(model_ids))
        self.assertEqual({row["id"] for row in rows["catalog_models"] if row["catalog_role"] == "active_quality"}, set(model_ids))
        self.assertTrue(set(model_ids).issubset({row["id"] for row in rows["catalog_models"]}))
        self.assertEqual([row["capability_rank"] for row in rows["models"]], list(range(1, len(rows["models"]) + 1)))
        self.assertEqual([row["provider_priority"] for row in rows["models"]], sorted([row["provider_priority"] for row in rows["models"]], reverse=True))
        self.assertEqual(rows["policy"]["minimum_pair"], rows["role_pairs"]["floor"])
        self.assertEqual(rows["role_models"]["weak"], model_ids[0])
        self.assertEqual(rows["role_models"]["frontier"], model_ids[-1])
        self.assertNotIn(module.PRIORITY_PRODUCER_MODEL, model_ids)
        self.assertIn("private_learning_contract", rows)
        self.assertTrue(all(module.ACTIVE_MODEL_EFFORTS[row["id"]] == set(row["codex_efforts"]) for row in rows["models"]))

    def test_custom_schema_v2_registry_accepts_no_priority_producer(self):
        registry = deepcopy(module.MODEL_CAPABILITY_CONFIG)
        priority_id = registry["priority_producer"]["id"]
        registry["priority_producer"] = None
        registry["policy"]["priority_producer_first_text_code"] = False
        registry["policy"]["priority_producer_scheduled_sources_only"] = False
        registry["policy"]["priority_producer_first_small_edits"] = False
        registry["policy"]["priority_producer_task_segments"] = False
        registry["policy"]["priority_producer_scheduled_sources"] = False
        next(row for row in registry["catalog_models"] if row["id"] == priority_id)["catalog_role"] = "catalog_only"
        with tempfile.TemporaryDirectory(prefix="routing-policy-registry-") as temporary:
            registry_path = Path(temporary) / "model-capability-ladder.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            loaded = module._load_model_capability_config(registry_path)
        self.assertIsNone(loaded["priority_producer"])
        self.assertEqual([row["id"] for row in loaded["models"]], module.ACTIVE_MODEL_ORDER)

    def test_default_loader_reads_saved_registry_without_scanning_local_catalog(self):
        with tempfile.TemporaryDirectory(prefix="routing-policy-saved-registry-") as temporary:
            registry_path = Path(temporary) / "model-capability-ladder.json"
            registry_path.write_text(json.dumps(module.MODEL_CAPABILITY_CONFIG), encoding="utf-8")
            original_path = module.MODEL_CAPABILITY_CONFIG_PATH
            try:
                module.MODEL_CAPABILITY_CONFIG_PATH = registry_path
                with mock.patch.object(module._MODEL_REGISTRY, "load_catalog", side_effect=AssertionError("routing scanned the local catalog")):
                    loaded = module._load_model_capability_config(registry_path)
            finally:
                module.MODEL_CAPABILITY_CONFIG_PATH = original_path
        self.assertEqual(loaded["source"]["catalog_sha256"], module.MODEL_CAPABILITY_CONFIG["source"]["catalog_sha256"])

    def test_priority_producer_handles_small_edits_and_bounded_task_segments(self):
        rows = module.public_model_capability_rows()
        priority = rows["priority_producer"]
        self.assertEqual(rows["spark_first"], priority)
        self.assertEqual(module.priority_first_pair("code", "text", "edit", "easy", 18), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertIsNone(module.priority_first_pair("code", "text", "edit", "complex"))
        self.assertEqual(module.spark_first_pair("document", "text", "write", "easy", 12), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertEqual(module.priority_first_pair("question", "text", "answer", "easy", 8), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertEqual(module.priority_first_pair("summary", "text", "work", "easy", 10), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertEqual(module.priority_first_pair("question", "text", "work", "easy", 16, "execute"), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertIsNone(module.priority_first_pair("code", "text", "edit", "easy", 25))
        self.assertEqual(module.scheduled_source_pair("easy"), (priority["id"], priority["effort_by_complexity"]["easy"]))
        self.assertEqual(module.scheduled_source_pair("complex"), (priority["id"], priority["effort_by_complexity"]["complex"]))
        self.assertIsNone(module.priority_first_pair("code", "mixed", "edit", "easy"))
        self.assertIsNone(module.priority_first_pair("code", "text", "review", "easy"))
        self.assertIsNone(module.priority_first_pair("question", "text", "audit", "easy", 8, "execute"))

    def test_documentation_instructions_uses_spark_only_for_small_edit_segments(self):
        self.assertEqual(module.priority_first_pair("documentation-instructions", "text", "edit", "easy", 14), module.SPARK_LOW_PAIR)
        self.assertIsNone(module.priority_first_pair("documentation-instructions", "text", "edit", "complex"))
        self.assertIsNone(module.priority_first_pair("documentation-instructions", "text", "review", "easy"))

    def test_legacy_spark_profile_recognizer_is_history_only_and_bounded(self):
        self.assertTrue(module.is_tiny_spark_profile("tiny_code", "text", "low", "easy", "low"))
        self.assertFalse(module.is_tiny_spark_profile("code", "text", "low"))
        self.assertFalse(module.is_tiny_spark_profile("tiny_code", "image", "low"))
        self.assertFalse(module.is_tiny_spark_profile("tiny_code", "text", "low", "complex", "low"))
        self.assertFalse(module.is_tiny_spark_profile("tiny_code", "text", "low", "easy", "high"))

    def test_non_tiny_profile_uses_exact_full_normal_ladder(self):
        self.assertEqual(
            module.adaptive_pair_texts_for_profile("code", "text", "low", "easy", "low"),
            module.normal_adaptive_pair_texts(),
        )
        self.assertFalse(
            any(pair.startswith(f"{module.PRIORITY_PRODUCER_MODEL}|") for pair in module.adaptive_pair_texts_for_profile("code", "text", "low", "easy", "low"))
        )

    def test_tiny_profile_excludes_history_only_spark(self):
        ladder = module.adaptive_pair_texts_for_profile("tiny_code", "text", "low", "easy", "low")
        self.assertEqual(ladder, module.normal_adaptive_pair_texts())
        self.assertFalse(any(pair.startswith(f"{module.PRIORITY_PRODUCER_MODEL}|") for pair in ladder))

    def test_plugin_frontend_implementation_without_language_is_general(self):
        self.assertEqual(module.resolve_execution_domain(owning_skill="build-web-apps:frontend-app-builder", task_family="integration", purpose="implement"), "general")

    def setUp(self):
        self.pairs = module.canonical_pairs(
            [
                "gpt-5.3-codex-spark|low",
                "gpt-5.3-codex-spark|medium",
                "gpt-5.3-codex-spark|high",
                "gpt-5.3-codex-spark|xhigh",
                "gpt-5.6-luna|low",
                "gpt-5.6-luna|medium",
                "gpt-5.6-luna|high",
                "gpt-5.6-luna|xhigh",
                "gpt-5.6-luna|max",
                "gpt-5.6-terra|low",
                "gpt-5.6-terra|medium",
                "gpt-5.6-terra|high",
                "gpt-5.6-terra|xhigh",
                "gpt-5.6-terra|max",
                "gpt-5.6-terra|ultra",
                "gpt-5.6-sol|low",
                "gpt-5.6-sol|medium",
                "gpt-5.6-sol|high",
                "gpt-5.6-sol|xhigh",
                "gpt-5.6-sol|max",
                "gpt-5.6-sol|ultra",
            ]
        )

    def test_downgrade_boundary_transitions(self):
        self.assertEqual(module.downgrade_pair(("gpt-5.6-sol", "ultra"), self.pairs), ("gpt-5.6-sol", "max"))
        self.assertEqual(module.downgrade_pair(("gpt-5.6-sol", "low"), self.pairs), ("gpt-5.6-terra", "ultra"))
        self.assertEqual(module.downgrade_pair(("gpt-5.6-terra", "low"), self.pairs), ("gpt-5.6-luna", "max"))
        self.assertEqual(module.downgrade_pair(("gpt-5.6-luna", "low"), self.pairs), ("gpt-5.3-codex-spark", "xhigh"))
        self.assertIsNone(module.downgrade_pair(("gpt-5.3-codex-spark", "low"), self.pairs))

    def test_upgrade_boundary_transitions(self):
        self.assertEqual(module.upgrade_pair(("gpt-5.3-codex-spark", "low"), self.pairs), ("gpt-5.3-codex-spark", "medium"))
        self.assertEqual(module.upgrade_pair(("gpt-5.3-codex-spark", "xhigh"), self.pairs), ("gpt-5.6-luna", "low"))
        self.assertEqual(module.upgrade_pair(("gpt-5.6-luna", "medium"), self.pairs), ("gpt-5.6-luna", "high"))
        self.assertEqual(module.upgrade_pair(("gpt-5.6-luna", "max"), self.pairs), ("gpt-5.6-terra", "low"))
        self.assertEqual(module.upgrade_pair(("gpt-5.6-terra", "ultra"), self.pairs), ("gpt-5.6-sol", "low"))
        self.assertIsNone(module.upgrade_pair(("gpt-5.6-sol", "ultra"), self.pairs))

    def test_sparse_eligible_pairs_preserve_model_then_effort_rules(self):
        sparse_upgrade = [("gpt-5.6-luna", "low"), ("gpt-5.6-luna", "max"), ("gpt-5.6-terra", "xhigh"), ("gpt-5.6-sol", "low")]
        sparse_upgrade_pairs = module.canonical_pairs([f"{model}|{effort}" for model, effort in sparse_upgrade])
        self.assertEqual(module.upgrade_pair(("gpt-5.6-luna", "low"), sparse_upgrade_pairs), ("gpt-5.6-luna", "max"))

        sparse_downgrade = [("gpt-5.6-luna", "max"), ("gpt-5.6-luna", "xhigh"), ("gpt-5.6-terra", "ultra"), ("gpt-5.3-codex-spark", "medium")]
        sparse_downgrade_pairs = module.canonical_pairs([f"{model}|{effort}" for model, effort in sparse_downgrade])
        self.assertEqual(module.downgrade_pair(("gpt-5.6-terra", "low"), sparse_downgrade_pairs), ("gpt-5.6-luna", "max"))

    def test_registry_extension_without_rank_code_changes(self):
        original_definitions = deepcopy(module.MODEL_DEFINITIONS)
        original_order = module.MODEL_ORDER[:]
        original_effort_order = module.MODEL_EFFORT_ORDER[:]
        original_efforts = {model: set(efforts) for model, efforts in module.MODEL_EFFORTS.items()}
        original_indexes = {model: dict(indexes) for model, indexes in module.MODEL_EFFORT_INDEX.items()}
        original_position = dict(module.MODEL_POSITION)
        try:
            insertion_index = original_order.index("gpt-5.6-terra")
            module.MODEL_ORDER[:] = original_order[:insertion_index] + ["gpt-future-aurora"] + original_order[insertion_index:]
            module.MODEL_EFFORTS["gpt-future-aurora"] = {"low", "high"}
            module.MODEL_EFFORT_INDEX["gpt-future-aurora"] = {"low": 0, "high": 1}
            module.MODEL_POSITION = {model: index for index, model in enumerate(module.MODEL_ORDER)}

            extended_pairs = module.canonical_pairs(["gpt-5.3-codex-spark|low", "gpt-5.6-luna|high", "gpt-future-aurora|low", "gpt-future-aurora|high", "gpt-5.6-terra|low"])
            self.assertEqual(module.upgrade_pair(("gpt-5.6-luna", "high"), extended_pairs), ("gpt-future-aurora", "low"))
            self.assertEqual(module.downgrade_pair(("gpt-future-aurora", "low"), extended_pairs), ("gpt-5.6-luna", "high"))
        finally:
            module.MODEL_DEFINITIONS.clear()
            module.MODEL_DEFINITIONS.update(original_definitions)
            module.MODEL_ORDER[:] = original_order
            module.MODEL_EFFORT_ORDER[:] = original_effort_order
            module.MODEL_EFFORTS.clear()
            module.MODEL_EFFORTS.update(original_efforts)
            module.MODEL_EFFORT_INDEX.clear()
            module.MODEL_EFFORT_INDEX.update(original_indexes)
            module.MODEL_POSITION.clear()
            module.MODEL_POSITION.update(original_position)

    def test_parse_model_effort_pair_trims_whitespace(self):
        self.assertEqual(module.parse_model_effort_pair(" gpt-5.6-luna | medium "), ("gpt-5.6-luna", "medium"))
        self.assertEqual(module.parse_model_effort_pair("\tgpt-5.6-terra|\txhigh "), ("gpt-5.6-terra", "xhigh"))
        self.assertEqual(module.parse_model_effort_pair("gpt-5.6-sol |xhigh"), ("gpt-5.6-sol", "xhigh"))

    def test_parse_model_effort_pair_requires_exactly_one_separator_after_trimming(self):
        with self.assertRaises(ValueError):
            module.parse_model_effort_pair(" gpt-5.6-luna| medium| high ")
        with self.assertRaises(ValueError):
            module.parse_model_effort_pair("gpt-5.6-luna||low")

    def test_resolve_execution_domain_precedence_and_infer_compatibility(self):
        self.assertEqual(
            module.resolve_execution_domain(
                explicit_domain="unity_csharp",
                language="python",
                owning_skill="code-skill",
                task_family="code",
                purpose="implement",
            ),
            "unity_csharp",
        )
        self.assertEqual(
            module.resolve_execution_domain(owning_skill="code-skill", language="python", task_family="code", purpose="implement"),
            "python",
        )
        self.assertEqual(
            module.resolve_execution_domain(owning_skill="code-skill", language="unity-csharp", task_family="code", purpose="implement"),
            "unity_csharp",
        )
        self.assertEqual(
            module.resolve_execution_domain(owning_skill="code-skill", task_family="code", purpose="implement"),
            "code_unspecified",
        )
        self.assertNotEqual(
            module.resolve_execution_domain(owning_skill="code-skill", task_family="code", language="python"),
            module.resolve_execution_domain(owning_skill="code-skill", task_family="code", language="unity_csharp"),
        )
        self.assertEqual(
            module.resolve_execution_domain(owning_skill="workflow-skill", task_family="direct", language="mystery"),
            "general",
        )
        self.assertEqual(
            module.infer_execution_domain(owning_skill="workflow-skill", task_family="direct", language="python", purpose="implement"),
            module.resolve_execution_domain(owning_skill="workflow-skill", task_family="direct", language="python", purpose="implement"),
        )

    def test_public_execution_domain_rows_are_complete_and_unambiguous(self):
        rows = module.public_execution_domain_rows()
        self.assertEqual(len(rows), len(module.EXECUTION_DOMAINS))
        row_ids = {row["id"] for row in rows}
        self.assertEqual(row_ids, set(module.EXECUTION_DOMAINS))
        for row in rows:
            domain = row["id"]
            metadata = module.EXECUTION_DOMAINS[domain]
            self.assertEqual(row["display_name"], metadata["display_name"])
            self.assertEqual(row["kind"], metadata["kind"])
            self.assertEqual(row["owner_skill"], metadata["owner_skill"])
            self.assertEqual(row["owner_enforced"], metadata["owner_enforced"])
            self.assertEqual(row["spark_first"], metadata["spark_first"])
            self.assertEqual(row["reference_path"], metadata["reference_path"])
            self.assertEqual(row["active"], metadata["active"])
            self.assertEqual(row["history_only"], metadata["history_only"])

        aliases = [alias for row in rows for alias in row["language_aliases"]]
        references = [row["reference_path"] for row in rows]
        self.assertEqual(len(aliases), len(set(aliases)))
        self.assertEqual(len(references), len(set(references)))

    def test_validate_execution_domain_registry_rejects_duplicate_aliases(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            duplicate_alias_domain = {
                "display_name": "Duplicate Alias",
                "kind": "code",
                "language_aliases": ["python"],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": "contracts/references/python-rules.md",
                "active": True,
                "history_only": False,
            }
            module.EXECUTION_DOMAINS["duplicate_alias"] = duplicate_alias_domain
            with self.assertRaises(ValueError):
                module.validate_execution_domain_registry()
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_validate_execution_domain_registry_rejects_duplicate_reference_paths(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            duplicate_ref_domain = {
                "display_name": "Duplicate Path",
                "kind": "code",
                "language_aliases": ["dupref"],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": module.EXECUTION_DOMAINS["python"]["reference_path"],
                "active": True,
                "history_only": False,
            }
            module.EXECUTION_DOMAINS["duplicate_reference_path"] = duplicate_ref_domain
            with self.assertRaises(ValueError):
                module.validate_execution_domain_registry()
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_execution_domain_reference_paths_are_real_files(self):
        expected_paths = {
            "general": "contracts/references/model-selection.md",
            "python": "contracts/references/python-rules.md",
            "csharp": "contracts/references/csharp-rules.md",
            "unity_csharp": "contracts/references/unity-csharp-rules.md",
            "code_unspecified": "contracts/references/spark-small-code.md",
        }
        for domain in expected_paths:
            self.assertEqual(module.EXECUTION_DOMAINS[domain]["reference_path"], expected_paths[domain])
            path = SCRIPT_PATH.parents[1] / expected_paths[domain]
            self.assertTrue(path.is_file(), f"reference path missing: {path}")

    def test_validate_execution_domain_registry_rejects_noncanonical_aliases(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            module.EXECUTION_DOMAINS["noncanonical_alias"] = {
                "display_name": "Noncanonical Alias",
                "kind": "code",
                "language_aliases": [" Python "],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": "contracts/references/python-rules.md",
                "active": True,
                "history_only": False,
            }
            with self.assertRaises(ValueError):
                module.validate_execution_domain_registry()
            self.assertEqual(module.EXECUTION_DOMAINS["noncanonical_alias"]["language_aliases"], [" Python "])
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_validate_execution_domain_registry_rejects_uppercase_aliases_and_preserves_registry(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            module.EXECUTION_DOMAINS["uppercase_alias"] = {
                "display_name": "Uppercase Alias",
                "kind": "code",
                "language_aliases": ["Python"],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": "contracts/references/python-rules.md",
                "active": True,
                "history_only": False,
            }
            with self.assertRaises(ValueError):
                module.validate_execution_domain_registry()
            self.assertEqual(module.EXECUTION_DOMAINS["uppercase_alias"]["language_aliases"], ["Python"])
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_validate_execution_domain_registry_rejects_absolute_reference_path(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            module.EXECUTION_DOMAINS["abs_reference"] = {
                "display_name": "Absolute Reference",
                "kind": "code",
                "language_aliases": ["absref"],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": "/tmp/task-analyze-abs-domain.md",
                "active": True,
                "history_only": False,
            }
            with tempfile.TemporaryDirectory(prefix="task-analyze-policy-abs-") as temporary:
                for metadata in module.EXECUTION_DOMAINS.values():
                    owner_skill = metadata["owner_skill"]
                    skill_dir = Path(temporary) / owner_skill
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(f"{owner_skill} skill\n", encoding="utf-8")
                    reference = Path(temporary) / metadata["reference_path"]
                    reference.parent.mkdir(parents=True, exist_ok=True)
                    reference.write_text(f"reference: {metadata['reference_path']}\n", encoding="utf-8")
                (Path(temporary) / "task-analyze-abs-domain.md").write_text("absolute reference\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    module.validate_execution_domain_registry(reference_root=temporary)
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_validate_execution_domain_registry_rejects_parent_traversal_reference_path(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            module.EXECUTION_DOMAINS["traversal_reference"] = {
                "display_name": "Traversal Reference",
                "kind": "code",
                "language_aliases": ["traverse"],
                "owner_skill": "code-skill",
                "owner_enforced": True,
                "spark_first": True,
                "reference_path": "contracts/references/../outside.md",
                "active": True,
                "history_only": False,
            }
            with tempfile.TemporaryDirectory(prefix="task-analyze-policy-traversal-") as temporary:
                for metadata in module.EXECUTION_DOMAINS.values():
                    owner_skill = metadata["owner_skill"]
                    skill_dir = Path(temporary) / owner_skill
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(f"{owner_skill} skill\n", encoding="utf-8")
                    if metadata["reference_path"] == "contracts/references/../outside.md":
                        continue
                    reference = Path(temporary) / metadata["reference_path"]
                    reference.parent.mkdir(parents=True, exist_ok=True)
                    reference.write_text(f"reference: {metadata['reference_path']}\n", encoding="utf-8")
                (Path(temporary) / "code-skill/references").mkdir(parents=True, exist_ok=True)
                (Path(temporary) / "outside.md").write_text("outside\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    module.validate_execution_domain_registry(reference_root=temporary)
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)

    def test_validate_execution_domain_registry_accepts_relative_reference_path(self):
        original = deepcopy(module.EXECUTION_DOMAINS)
        try:
            with tempfile.TemporaryDirectory(prefix="task-analyze-policy-ok-") as temporary:
                for metadata in original.values():
                    owner_skill = metadata["owner_skill"]
                    skill_dir = Path(temporary) / owner_skill
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(f"{owner_skill} skill\n", encoding="utf-8")
                    reference = Path(temporary) / metadata["reference_path"]
                    reference.parent.mkdir(parents=True, exist_ok=True)
                    reference.write_text(f"reference: {metadata['reference_path']}\n", encoding="utf-8")
                module.validate_execution_domain_registry(reference_root=temporary)
            self.assertEqual(module.EXECUTION_DOMAINS["general"]["reference_path"], "contracts/references/model-selection.md")
        finally:
            module.EXECUTION_DOMAINS.clear()
            module.EXECUTION_DOMAINS.update(original)


if __name__ == "__main__":
    unittest.main()
