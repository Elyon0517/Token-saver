import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ending_evidence_manifest as module


class EndingEvidenceManifestTests(unittest.TestCase):
    def fixture(self, root):
        paths = {
            "main": root / "main.json",
            "producer": root / "producer.json",
            "result": root / "result.json",
            "tests": root / "tests.json",
        }
        paths["main"].write_text(json.dumps({"status": "pass", "requested_pair": "gpt-5.6-sol|ultra", "effective_pair": "gpt-5.6-sol|ultra"}), encoding="utf-8")
        paths["producer"].write_text(json.dumps({"status": "pass", "effective_pair": "gpt-5.6-terra|medium"}), encoding="utf-8")
        paths["result"].write_text('{"status":"done","tests":4}', encoding="utf-8")
        paths["tests"].write_text(json.dumps({"exit_code": 0, "count": 4}), encoding="utf-8")
        return paths

    def test_builds_all_pass_hash_bound_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            manifest = module.build_manifest("run-1", paths["main"], paths["producer"], paths["result"], paths["tests"], "gpt-5.6-sol|ultra", {"status": "done", "tests": 4}, 4)
        self.assertTrue(manifest["all_checks_pass"])
        self.assertEqual(set(manifest["checks"].values()), {True})
        self.assertEqual(len(manifest["sources"]["published_result"]["sha256"]), 64)

    def test_records_mismatch_without_hiding_other_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary))
            manifest = module.build_manifest("run-2", paths["main"], paths["producer"], paths["result"], paths["tests"], "gpt-5.6-sol|ultra", {"status": "done", "tests": 6}, 4)
        self.assertFalse(manifest["all_checks_pass"])
        self.assertFalse(manifest["checks"]["published_result"])
        self.assertTrue(manifest["checks"]["quick_check"])

    def test_validator_passes_exact_manifest_and_detects_changed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.fixture(root)
            manifest = module.build_manifest("run-3", paths["main"], paths["producer"], paths["result"], paths["tests"], "gpt-5.6-sol|ultra", {"status": "done", "tests": 4}, 4)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            passed, reasons = module.validate_manifest(manifest_path, "run-3")
            self.assertTrue(passed)
            self.assertEqual(reasons, [])
            paths["result"].write_text('{"status":"changed"}', encoding="utf-8")
            passed, reasons = module.validate_manifest(manifest_path, "run-3")
        self.assertFalse(passed)
        self.assertIn("published_result_changed", reasons)


if __name__ == "__main__":
    unittest.main()
