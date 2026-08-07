#!/usr/bin/env python3
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "task_complexity_score.py"
SPEC = importlib.util.spec_from_file_location("task_complexity_score", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class TaskComplexityScoreTests(unittest.TestCase):
    def test_positional_prompt_is_passed_exactly_and_takes_precedence_over_stdin(self):
        prompt = "  Preserve this exact prompt.\nDo not normalize it.  "
        captured = []
        output = io.StringIO()

        def capture_score(value):
            captured.append(value)
            return 12

        with patch.object(module.RUNNER, "infer_complexity_score", side_effect=capture_score), patch.object(module.RUNNER, "infer_operation", return_value="work"), patch.object(module.sys, "stdin", io.StringIO("stdin prompt")), patch.object(module.sys, "stdout", output):
            status = module.main([prompt])
        self.assertEqual(status, 0)
        self.assertEqual(captured, [prompt])
        self.assertEqual(json.loads(output.getvalue())["complexity_score"], 12)

    def test_stdin_prompt_is_scored_when_positional_prompt_is_absent(self):
        prompt = "Read the prompt from stdin."
        captured = []
        output = io.StringIO()

        def capture_score(value):
            captured.append(value)
            return 35

        with patch.object(module.RUNNER, "infer_complexity_score", side_effect=capture_score), patch.object(module.RUNNER, "infer_operation", return_value="work"), patch.object(module.sys, "stdin", io.StringIO(prompt)), patch.object(module.sys, "stdout", output):
            status = module.main([])
        self.assertEqual(status, 0)
        self.assertEqual(captured, [prompt])
        self.assertEqual(json.loads(output.getvalue())["complexity_score"], 35)

    def test_empty_positional_or_stdin_prompt_retains_prompt_required_validation(self):
        output = io.StringIO()
        with patch.object(module.sys, "stdout", output):
            status = module.main([""])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "fail", "reason": "prompt_required"})


if __name__ == "__main__":
    unittest.main()
