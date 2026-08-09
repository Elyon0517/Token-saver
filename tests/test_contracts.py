#!/usr/bin/env python3
"""Guard the skill contract.

The engine's own cost is dominated by text it loads and sessions it spawns. Two things keep
that in check, and both are asserted here rather than left to discipline:

1. The skill `description` is the first filter. A small task must never load this skill at
   all -- once it loads, the agent is already reading a contract for work that measured
   65k-97k tokens to route. Repelling small work is therefore a property of the description,
   not of anything downstream.
2. The skill body stays small, and detail lives in references loaded only on demand.
"""

import re
import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1]
SKILL = ENGINE_ROOT / "SKILL.md"
CONTRACTS = ENGINE_ROOT / "contracts"
REFERENCES = CONTRACTS / "references"

# ~4 chars/token. The upstream contract this replaces was ~43,900 bytes.
BYTES_PER_TOKEN = 4
SKILL_TOKEN_BUDGET = 1400

REFERENCE_LINK_PATTERN = re.compile(r"`?contracts/references/([a-z0-9-]+\.md)`?")
FRONTMATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def frontmatter():
    match = FRONTMATTER_PATTERN.match(SKILL.read_text(encoding="utf-8"))
    assert match, "SKILL.md must open with YAML frontmatter"
    return match.group(1)


def description():
    match = re.search(r'description:\s*"(.*)"\s*\Z', frontmatter(), re.DOTALL)
    assert match, "frontmatter must define a quoted description"
    return match.group(1)


class TriggerTests(unittest.TestCase):
    """The description is the gate for small work. Everything else runs too late."""

    def setUp(self):
        self.description = description().lower()

    def test_skill_declares_a_name_and_description(self):
        self.assertIn("name: token-saver", frontmatter())
        self.assertTrue(description().strip())

    def test_description_names_what_must_not_trigger_it(self):
        # Without explicit negatives a description reads as "use me for coding", which is
        # exactly the over-triggering that makes the engine cost more than it saves.
        self.assertIn("do not use", self.description)
        for excluded in ("single-file", "typo", "rename", "question"):
            with self.subTest(excluded=excluded):
                self.assertIn(excluded, self.description)

    def test_description_names_what_should_trigger_it(self):
        for included in ("three or more files", "architecture", "migration"):
            with self.subTest(included=included):
                self.assertIn(included, self.description)

    def test_description_states_the_measured_cost(self):
        # The trigger decision is a cost decision, so the cost travels with the trigger
        # rather than sitting in a reference the agent has not loaded yet.
        self.assertRegex(self.description, r"65,?000")
        self.assertIn("token", self.description)

    def test_description_defaults_to_not_triggering(self):
        self.assertIn("when unsure, do not use", self.description)


class SkillBodyTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_body_stays_within_its_token_budget(self):
        tokens = len(self.text.encode("utf-8")) // BYTES_PER_TOKEN
        self.assertLessEqual(
            tokens,
            SKILL_TOKEN_BUDGET,
            f"SKILL.md grew to ~{tokens} tokens; move detail into contracts/references/ instead",
        )

    def test_body_puts_the_free_confirmation_before_routing(self):
        gate = self.text.index("fast_path.py")
        router = self.text.index("obsidian_adaptive_model_runner.py")
        self.assertLess(gate, router, "the zero-cost check must precede the router")

    def test_body_tells_an_eligible_task_to_stop(self):
        self.assertIn("eligible: true", self.text)
        self.assertIn("Do it here and stop", self.text)

    def test_body_states_the_free_first_order(self):
        for rung in ("local model", "priority producer", "quality ladder"):
            with self.subTest(rung=rung):
                self.assertIn(rung, self.text)

    def test_body_keeps_the_receipt_rule(self):
        self.assertIn("UNVERIFIED (no runtime receipt)", self.text)

    def test_body_states_ladder_movement(self):
        for rule in ("2 receipt-matched Real PASS", "one rung up", "Operational failure"):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.text)

    def test_no_always_resident_bootstrap_remains(self):
        # The skill loads on demand. A resident file would reintroduce the per-session tax
        # that trigger-based loading exists to remove.
        self.assertFalse((ENGINE_ROOT / "AGENTS.md").exists())
        self.assertEqual(sorted(path.name for path in CONTRACTS.glob("*.md")), [])


class ReferenceTests(unittest.TestCase):
    def test_every_reference_the_skill_names_exists(self):
        named = set(REFERENCE_LINK_PATTERN.findall(SKILL.read_text(encoding="utf-8")))
        self.assertTrue(named, "the skill should route to on-demand references")
        for name in sorted(named):
            with self.subTest(reference=name):
                self.assertTrue((REFERENCES / name).is_file(), f"missing reference: {name}")

    def test_measurements_reference_carries_the_observed_costs(self):
        text = (REFERENCES / "measurements.md").read_text(encoding="utf-8")
        self.assertIn("64,980", text)
        self.assertIn("96,584", text)

    def test_no_reference_is_empty(self):
        for path in sorted(REFERENCES.glob("*.md")):
            with self.subTest(reference=path.name):
                self.assertTrue(path.read_text(encoding="utf-8").strip())

    def test_execution_domain_reference_paths_resolve(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("contracts_routing_policy", ENGINE_ROOT / "scripts" / "routing_policy.py")
        routing_policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(routing_policy)
        for domain, metadata in routing_policy.EXECUTION_DOMAINS.items():
            with self.subTest(domain=domain):
                self.assertTrue((ENGINE_ROOT / metadata["reference_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
