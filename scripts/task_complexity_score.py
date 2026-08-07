#!/usr/bin/env python3
"""Return the universal 0-100 task complexity score without executing a model."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("obsidian_adaptive_model_runner.py")
SPEC = importlib.util.spec_from_file_location("task_complexity_runner", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score one task prompt without executing a model.")
    parser.add_argument("prompt", nargs="?")
    arguments = parser.parse_args(argv)
    prompt = arguments.prompt if arguments.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        print(json.dumps({"status": "fail", "reason": "prompt_required"}, separators=(",", ":")))
        return 1
    score = RUNNER.infer_complexity_score(prompt)
    band = RUNNER.obsidian_model_memory.complexity_band(score)
    print(json.dumps({"status": "pass", "complexity_score": score, "complexity_band": band, "operation": RUNNER.infer_operation(prompt)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
