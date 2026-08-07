#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _support import QuietCliMixin

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fast_path = load_module("fast_path")


class FastPathEvaluateTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.ledger = Path(self._temp.name) / "fast-path-events.jsonl"

    def evaluate(self, prompt, **kwargs):
        return fast_path.evaluate(prompt, ledger=self.ledger, **kwargs)

    def test_small_single_file_edit_is_eligible(self):
        decision = self.evaluate("fix the typo in the docstring of parse_config in config.py")
        self.assertTrue(decision["eligible"], decision["reasons"])
        self.assertEqual(decision["reasons"], [])
        self.assertLessEqual(decision["complexity_score"], fast_path.MAX_FAST_PATH_SCORE)
        self.assertEqual(decision["complexity_band"], "small")

    def test_decision_is_free(self):
        self.assertEqual(self.evaluate("rename the flag in app.py")["decision_cost_tokens"], 0)

    def test_multi_file_scope_is_rejected(self):
        decision = self.evaluate("update the handler in api.py and models.py to match")
        self.assertFalse(decision["eligible"])
        self.assertIn("multiple_files_referenced", decision["reasons"])

    def test_structural_change_is_rejected_even_when_score_is_low(self):
        decision = self.evaluate("small refactor in a.py")
        self.assertFalse(decision["eligible"])
        self.assertIn("structural_change", decision["reasons"])

    def test_read_only_work_is_rejected_because_it_never_routed(self):
        # infer_operation() cannot return "review", so this must be matched on the
        # prompt; a regression here would silently re-admit read-only work.
        decision = self.evaluate("review app.py")
        self.assertFalse(decision["eligible"])
        self.assertIn("read_only_operation_already_inline", decision["reasons"])

    def test_elevated_risk_or_ambiguity_is_rejected(self):
        self.assertIn("risk_not_low", self.evaluate("fix a.py", risk="high")["reasons"])
        self.assertIn("ambiguity_not_low", self.evaluate("fix a.py", ambiguity="high")["reasons"])

    def test_multi_stage_request_is_rejected(self):
        decision = self.evaluate("fix a.py and then update the changelog")
        self.assertFalse(decision["eligible"])
        self.assertIn("multi_stage", decision["reasons"])

    def test_irreversible_operation_is_rejected(self):
        self.assertIn("irreversible_operation", self.evaluate("delete the stale flag in a.py")["reasons"])

    def test_empty_prompt_is_rejected(self):
        self.assertIn("empty_prompt", self.evaluate("   ")["reasons"])


class FastPathLearningTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.ledger = Path(self._temp.name) / "fast-path-events.jsonl"
        self.prompt = "fix the typo in the docstring of parse_config in config.py"

    def test_passing_run_records_without_suppressing(self):
        fast_path.record(self.prompt, outcome="pass", ledger=self.ledger)
        self.assertEqual(fast_path.suppressed_fingerprints(self.ledger), frozenset())
        self.assertTrue(fast_path.evaluate(self.prompt, ledger=self.ledger)["eligible"])

    def test_correction_suppresses_the_matching_fingerprint(self):
        before = fast_path.evaluate(self.prompt, ledger=self.ledger)
        self.assertTrue(before["eligible"])
        # evaluate() resolves the fingerprint lazily -- with an empty ledger there is
        # nothing to match, so it skips the heavy profiler import entirely. Ask for it
        # directly when the fingerprint itself is what is under test.
        fingerprint = fast_path.capability_fingerprint(self.prompt)

        fast_path.record(self.prompt, outcome="corrected", ledger=self.ledger)

        self.assertIn(fingerprint, fast_path.suppressed_fingerprints(self.ledger))
        after = fast_path.evaluate(self.prompt, ledger=self.ledger)
        self.assertFalse(after["eligible"])
        self.assertIn("fingerprint_suppressed_by_prior_correction", after["reasons"])

    def test_ledger_lines_are_json_and_carry_no_raw_prompt(self):
        fast_path.record(self.prompt, outcome="pass", ledger=self.ledger, note="inline edit")
        lines = [line for line in self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["event"], "record")
        self.assertEqual(event["outcome"], "pass")
        # The fingerprint is a bounded-category hash; the prompt itself never lands here.
        self.assertNotIn(self.prompt, lines[0])

    def test_malformed_ledger_lines_are_ignored(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("not json\n\n", encoding="utf-8")
        self.assertEqual(fast_path.suppressed_fingerprints(self.ledger), frozenset())

    def test_missing_ledger_is_not_an_error(self):
        self.assertEqual(fast_path.suppressed_fingerprints(self.ledger / "absent"), frozenset())


class FastPathCliTests(QuietCliMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.ledger = str(Path(self._temp.name) / "events.jsonl")

    def test_evaluate_command_exits_zero(self):
        self.assertEqual(fast_path.main(["--ledger", self.ledger, "evaluate", "fix a.py"]), 0)

    def test_empty_prompt_exits_nonzero(self):
        self.assertEqual(fast_path.main(["--ledger", self.ledger, "evaluate", "  "]), 1)

    def test_record_then_suppressed_roundtrip(self):
        self.assertEqual(fast_path.main(["--ledger", self.ledger, "record", "fix a.py", "--outcome", "corrected"]), 0)
        self.assertEqual(fast_path.main(["--ledger", self.ledger, "suppressed"]), 0)


if __name__ == "__main__":
    unittest.main()
