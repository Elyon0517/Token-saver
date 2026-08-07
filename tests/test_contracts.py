#!/usr/bin/env python3
"""Guard the layering that Opt 2 bought.

The engine's own cost is dominated by prose it loads before doing any work. The upstream
routing contract was ~10,985 tokens and was loaded to make routine decisions. Splitting it
into a small always-resident kernel plus on-demand references is only worth anything if the
kernel stays small, so that bound is asserted here rather than left to discipline.
"""

import re
import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ENGINE_ROOT / "contracts"
KERNEL = CONTRACTS / "routing-kernel.md"
REFERENCES = CONTRACTS / "references"

# ~4 chars/token. The upstream contract this replaces was ~43,900 bytes.
BYTES_PER_TOKEN = 4
KERNEL_TOKEN_BUDGET = 1200

REFERENCE_LINK_PATTERN = re.compile(r"`references/([a-z0-9-]+\.md)`")


class RoutingKernelTests(unittest.TestCase):
    def setUp(self):
        self.text = KERNEL.read_text(encoding="utf-8")

    def test_kernel_exists_and_is_the_only_resident_contract(self):
        resident = sorted(path.name for path in CONTRACTS.glob("*.md"))
        self.assertEqual(resident, ["routing-kernel.md"])

    def test_kernel_stays_within_its_token_budget(self):
        tokens = len(self.text.encode("utf-8")) // BYTES_PER_TOKEN
        self.assertLessEqual(
            tokens,
            KERNEL_TOKEN_BUDGET,
            f"routing kernel grew to ~{tokens} tokens; move detail into contracts/references/ instead",
        )

    def test_kernel_puts_the_free_gate_before_routing(self):
        gate = self.text.index("fast_path.py")
        router = self.text.index("obsidian_adaptive_model_runner.py")
        self.assertLess(gate, router, "the zero-cost gate must be evaluated before the router")

    def test_kernel_states_the_free_first_order(self):
        self.assertIn("local model", self.text)
        self.assertIn("priority producer", self.text)
        self.assertIn("quality ladder", self.text)

    def test_kernel_keeps_the_receipt_rule(self):
        self.assertIn("receipt", self.text.lower())
        self.assertIn("UNVERIFIED (no runtime receipt)", self.text)

    def test_kernel_states_ladder_movement(self):
        for rule in ("2 receipt-matched Real PASS", "one rung up", "Operational failure"):
            self.assertIn(rule, self.text)


class ReferenceTests(unittest.TestCase):
    def test_every_reference_the_kernel_names_exists(self):
        named = set(REFERENCE_LINK_PATTERN.findall(KERNEL.read_text(encoding="utf-8")))
        self.assertTrue(named, "kernel should route to on-demand references")
        for name in sorted(named):
            with self.subTest(reference=name):
                self.assertTrue((REFERENCES / name).is_file(), f"kernel names a missing reference: {name}")

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
