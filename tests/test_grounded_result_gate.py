#!/usr/bin/env python3
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "grounded_result_gate.py"
MODULE_SPEC = importlib.util.spec_from_file_location("grounded_result_gate", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class GroundedResultGateTests(unittest.TestCase):
    def write_case(self, directory, message, receipt_changes=None):
        result_path = directory / "result.md"
        result_path.write_text(message + "\n", encoding="utf-8")
        receipt = {"schema_version": 1, "node_type": "locked-route-node", "status": "pass", "failure_class": None, "turn_completed": True, "exit_code": 0, "metrics_complete": True, "model_match": True, "effort_match": True, "pair_match": True, "requested_model": "gpt-5.3-codex-spark", "requested_effort": "low", "requested_pair": "gpt-5.3-codex-spark|low", "resolved_model": "gpt-5.3-codex-spark", "resolved_effort": "low", "effective_model": "gpt-5.3-codex-spark", "effective_pair": "gpt-5.3-codex-spark|low", "output_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()}
        receipt.update(receipt_changes or {})
        receipt_path = directory / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return receipt_path, result_path

    def run_cli(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = module.main(arguments)
        return exit_code, json.loads(output.getvalue())

    def test_plain_json_passes_schema_sorting_and_sources_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_root = directory / "source"
            source_root.mkdir()
            (source_root / "a.py").write_text("TOP SECRET", encoding="utf-8")
            (source_root / "b.py").write_text("ANOTHER SECRET", encoding="utf-8")
            message = json.dumps({"answer": "ok", "files": ["a.py", "b.py"], "groups": [{"names": ["a", "b"]}, {"names": ["c", "d"]}]}, separators=(",", ":"))
            receipt_path, result_path = self.write_case(directory, message)
            exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), "--json-required-keys", "answer,files,groups", "--json-key-order", "answer,files,groups", "--sorted-json-pointer", "/files", "--sorted-json-pointer", "/groups/*/names", "--source-root", str(source_root), "--source-files-pointer", "/files"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(verdict["checks"], {"required_keys": 3, "key_order": True, "sorted_arrays": 3, "source_files": 2})
        self.assertNotIn("TOP SECRET", json.dumps(verdict))
        self.assertNotIn("a.py", json.dumps(verdict))

    def test_final_json_fence_after_prose_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            message = "Bounded result follows.\n```json\n{\"answer\":\"ok\"}\n```"
            receipt_path, result_path = self.write_case(directory, message)
            exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), "--json-required-keys", "answer"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(verdict["result"]["container"], "final_fence")

    def test_extra_fence_or_trailing_text_is_ambiguous(self):
        cases = ["```json\n{}\n```\n```json\n{}\n```", "```json\n{}\n```\ntrailing", "{\"first\":1}\n```json\n{\"second\":2}\n```"]
        for message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                receipt_path, result_path = self.write_case(Path(temp_dir), message)
                exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path)])
            self.assertEqual(exit_code, 1)
            self.assertEqual(verdict["failure"], "result_json_ambiguous")

    def test_receipt_status_completion_model_and_hash_fail_independently(self):
        cases = [({"status": "fail", "failure_class": "execution"}, "receipt_status"), ({"turn_completed": False}, "receipt_incomplete"), ({"pair_match": False}, "receipt_model_mismatch"), ({"effective_pair": "wrong|low"}, "receipt_pair_inconsistent"), ({"output_sha256": "0" * 64}, "result_hash_mismatch")]
        for receipt_changes, expected_failure in cases:
            with self.subTest(expected_failure=expected_failure), tempfile.TemporaryDirectory() as temp_dir:
                receipt_path, result_path = self.write_case(Path(temp_dir), "{}", receipt_changes)
                exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path)])
            self.assertEqual(exit_code, 1)
            self.assertEqual(verdict["failure"], expected_failure)

    def test_bad_json_root_required_keys_and_order_fail(self):
        cases = [("not json", [], "result_json_invalid"), ("[]", [], "result_json_not_object"), ("{\"a\":1}", ["--json-required-keys", "a,b"], "json_required_key_missing"), ("{\"b\":1,\"a\":2}", ["--json-key-order", "a,b"], "json_key_order")]
        for message, options, expected_failure in cases:
            with self.subTest(expected_failure=expected_failure), tempfile.TemporaryDirectory() as temp_dir:
                receipt_path, result_path = self.write_case(Path(temp_dir), message)
                exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), *options])
            self.assertEqual(exit_code, 1)
            self.assertEqual(verdict["failure"], expected_failure)

    def test_unsorted_array_and_wildcard_array_fail(self):
        cases = [({"names": ["b", "a"]}, "/names"), ({"groups": [{"names": ["a", "b"]}, {"names": ["d", "c"]}]}, "/groups/*/names")]
        for document, pointer in cases:
            with self.subTest(pointer=pointer), tempfile.TemporaryDirectory() as temp_dir:
                message = json.dumps(document, separators=(",", ":"))
                receipt_path, result_path = self.write_case(Path(temp_dir), message)
                exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), "--sorted-json-pointer", pointer])
            self.assertEqual(exit_code, 1)
            self.assertEqual(verdict["failure"], "json_array_unsorted")

    def test_source_traversal_absolute_missing_and_symlink_escape_fail_without_path_leakage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source_root = directory / "source"
            source_root.mkdir()
            outside_file = directory / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")
            (source_root / "escape.txt").symlink_to(outside_file)
            cases = [("../outside.txt", "source_path_traversal"), (str(outside_file), "source_path_traversal"), ("missing-private-name.txt", "source_missing_or_outside_root"), ("escape.txt", "source_missing_or_outside_root")]
            for file_name, expected_failure in cases:
                with self.subTest(expected_failure=expected_failure):
                    message = json.dumps({"files": [file_name]}, separators=(",", ":"))
                    receipt_path, result_path = self.write_case(directory, message)
                    exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), "--source-root", str(source_root), "--source-files-pointer", "/files"])
                self.assertEqual(exit_code, 1)
                self.assertEqual(verdict["failure"], expected_failure)
                self.assertNotIn(file_name, json.dumps(verdict))

    def test_pointer_target_must_be_array_and_wildcard_must_match(self):
        cases = [({"names": "a"}, "/names", "sorted_pointer_not_array"), ({"groups": []}, "/groups/*/names", "json_pointer_no_match")]
        for document, pointer, expected_failure in cases:
            with self.subTest(expected_failure=expected_failure), tempfile.TemporaryDirectory() as temp_dir:
                message = json.dumps(document, separators=(",", ":"))
                receipt_path, result_path = self.write_case(Path(temp_dir), message)
                exit_code, verdict = self.run_cli(["--receipt", str(receipt_path), "--result", str(result_path), "--sorted-json-pointer", pointer])
            self.assertEqual(exit_code, 1)
            self.assertEqual(verdict["failure"], expected_failure)


if __name__ == "__main__":
    unittest.main()
