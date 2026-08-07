#!/usr/bin/env python3
"""Deterministically bridge a frozen benchmark workload into the adaptive runner."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_PATH_ENV = "CODEX_AUTO_BENCHMARK_PROMPT_PATH"
PROMPT_SHA256_ENV = "CODEX_AUTO_BENCHMARK_WORKLOAD_SHA256"
CODEX_BIN_ENV = "CODEX_AUTO_BENCHMARK_CODEX_BIN"
CHILD_TIMEOUT_ENV = "CODEX_AUTO_BENCHMARK_CHILD_TIMEOUT"
CACHE_ROOT_ENV = "CODEX_AUTO_BENCHMARK_CACHE_ROOT"
PYTHON_ENV = "CODEX_AUTO_BENCHMARK_PYTHON"
ENTRY_MODEL_ENV = "CODEX_AUTO_BENCHMARK_ENTRY_MODEL"
ENTRY_EFFORT_ENV = "CODEX_AUTO_BENCHMARK_ENTRY_EFFORT"
ENGINE_ROOT_ENV = "TOKEN_SAVER_ENGINE_ROOT"
LAUNCH_CLAIM_NAME = "adaptive-entry-launch.json"
AUTO_ENTRY_PAIRS = (("gpt-5.6-luna", "max"), ("gpt-5.6-sol", "ultra"))
STABLE_RECOMMENDATION_STATES = frozenset({"frozen", "priority_verified", "verified_recovery"})
STABLE_SELECTION_PROVENANCE = frozenset({"dual_model_history", "local_transfer_history", "local_and_obsidian", "local_history", "obsidian_history"})


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def strict_json_object(payload):
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("benchmark result must be one JSON object")
    return document


def source_tree_sha256(root):
    resolved_root = root.expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    for source_path in sorted(path for path in resolved_root.rglob("*") if path.is_file()):
        if source_path.is_symlink():
            raise ValueError("benchmark source tree cannot contain symlinks")
        relative_path = source_path.relative_to(resolved_root).as_posix().encode("utf-8")
        source_bytes = source_path.read_bytes()
        digest.update(len(relative_path).to_bytes(8, "big"))
        digest.update(relative_path)
        digest.update(len(source_bytes).to_bytes(8, "big"))
        digest.update(source_bytes)
    return digest.hexdigest()


def benchmark_complexity_score(prompt_text):
    matches = re.findall(r"(?im)\bcomplexity score:\s*(\d{1,3})\b", prompt_text)
    if len(matches) != 1:
        raise ValueError("benchmark prompt must bind one complexity score")
    score = int(matches[0])
    if not 0 <= score <= 100:
        raise ValueError("benchmark complexity score is invalid")
    return score


def entry_pair_from_environment():
    pair = (os.environ.get(ENTRY_MODEL_ENV), os.environ.get(ENTRY_EFFORT_ENV))
    if pair not in AUTO_ENTRY_PAIRS:
        raise ValueError("benchmark adaptive entry pair binding is invalid")
    return pair


def claim_adaptive_launch(cache_root, workload_sha256, entry_pair):
    cache_root = cache_root.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    claim_path = cache_root / LAUNCH_CLAIM_NAME
    payload = json.dumps({"schema_version": 3, "workload_sha256": workload_sha256, "entry_pair": "|".join(entry_pair)}, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("benchmark adaptive producer was already launched")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return claim_path


def prepare_read_only_workspace(source_root, cache_root):
    source_root = source_root.expanduser().resolve(strict=True)
    cache_root = cache_root.expanduser().resolve()
    try:
        cache_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("benchmark cache root must be outside the frozen source tree")
    cache_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="source-copy-", dir=cache_root))
    shutil.copytree(source_root, workspace, dirs_exist_ok=True, copy_function=shutil.copy2)
    if source_tree_sha256(source_root) != source_tree_sha256(workspace):
        raise ValueError("benchmark source copy does not match the frozen source tree")
    return workspace


def extract_result_document(result_text):
    text = str(result_text or "").lstrip("\ufeff")
    if text.startswith("Complexity:"):
        paragraph_end = text.find("\n\n")
        if paragraph_end < 0:
            raise ValueError("adaptive result disclosure has no result payload")
        text = text[paragraph_end + 2:]
    return strict_json_object(text)


def validated_prompt(prompt_file):
    registered_path = os.environ.get(PROMPT_PATH_ENV)
    expected_sha256 = os.environ.get(PROMPT_SHA256_ENV)
    if not registered_path or not expected_sha256:
        raise ValueError("benchmark prompt binding is unavailable")
    resolved_prompt = prompt_file.expanduser().resolve(strict=True)
    if resolved_prompt != Path(registered_path).expanduser().resolve(strict=True) or not resolved_prompt.is_file():
        raise ValueError("benchmark prompt path does not match its binding")
    prompt_bytes = resolved_prompt.read_bytes()
    if sha256_bytes(prompt_bytes) != expected_sha256:
        raise ValueError("benchmark prompt hash does not match its binding")
    prompt_text = prompt_bytes.decode("utf-8")
    if not prompt_text.strip():
        raise ValueError("benchmark prompt is empty")
    return prompt_text


def receipt_projection(receipt):
    if not isinstance(receipt, dict):
        raise ValueError("adaptive child receipt is invalid")
    tokens = receipt.get("strategy_tokens") if isinstance(receipt.get("strategy_tokens"), dict) else receipt.get("tokens")
    total_tokens = tokens.get("total_tokens") if isinstance(tokens, dict) else None
    selected_pair = receipt.get("selected_pair") or receipt.get("effective_pair") or receipt.get("requested_pair")
    scheduled_nodes = receipt.get("scheduled_nodes") if isinstance(receipt.get("scheduled_nodes"), list) else []
    route_attempts = receipt.get("route_attempts")
    assigned_pairs = [node.get("effective_pair") for node in scheduled_nodes if isinstance(node, dict) and isinstance(node.get("effective_pair"), str)]
    if not assigned_pairs:
        assigned_pairs = [receipt.get("effective_pair")]
    recommendation_state = receipt.get("recommendation_state") or receipt.get("calibration_state")
    selection_provenance = receipt.get("selection_provenance")
    capability_assignment = receipt.get("capability_assignment")
    failed_attempts = [attempt for attempt in route_attempts if isinstance(attempt, dict) and attempt.get("status") != "pass"] if isinstance(route_attempts, list) else []
    calibration_attempt_count = len(failed_attempts) + int(receipt.get("trial") is True) + len(receipt.get("reroutes") if isinstance(receipt.get("reroutes"), list) else [])
    calibration_failure_elapsed_ms = sum(attempt.get("process_elapsed_ms", 0) for attempt in failed_attempts if isinstance(attempt.get("process_elapsed_ms"), int) and attempt["process_elapsed_ms"] >= 0)
    calibration_failure_logical_tokens = sum((attempt.get("tokens") or {}).get("total_tokens", 0) for attempt in failed_attempts if isinstance((attempt.get("tokens") or {}).get("total_tokens", 0), int) and (attempt.get("tokens") or {}).get("total_tokens", 0) >= 0)
    route_signature = {"selected_pair": selected_pair, "effective_pair": receipt.get("effective_pair"), "scheduled_graph": receipt.get("scheduled_graph") is True, "assigned_pairs": assigned_pairs, "trial": receipt.get("trial"), "recommendation_state": recommendation_state, "selection_provenance": selection_provenance, "capability_assignment": capability_assignment}
    clean = receipt.get("status") == "pass" and receipt.get("metrics_complete") is True and receipt.get("result_published") is True and receipt.get("trial") is False and recommendation_state in STABLE_RECOMMENDATION_STATES and selection_provenance in STABLE_SELECTION_PROVENANCE and isinstance(capability_assignment, list) and capability_assignment and isinstance(total_tokens, int) and total_tokens >= 0 and isinstance(receipt.get("process_elapsed_ms"), int) and receipt["process_elapsed_ms"] >= 0 and isinstance(route_attempts, list) and route_attempts and not failed_attempts and receipt.get("reroutes") == [] and receipt.get("node_role") != "repair"
    if not clean or not isinstance(selected_pair, str) or any(not isinstance(pair, str) for pair in route_signature["assigned_pairs"]):
        raise ValueError("adaptive child receipt is not a clean frozen selected execution")
    return {"schema_version": 2, "receipt_sha256": sha256_bytes(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")), "selected_pair": selected_pair, "effective_pair": receipt.get("effective_pair"), "steady_state_logical_tokens": total_tokens, "steady_state_execution_elapsed_ms": receipt["process_elapsed_ms"], "calibration_attempt_count": calibration_attempt_count, "calibration_failure_elapsed_ms": calibration_failure_elapsed_ms, "calibration_failure_logical_tokens": calibration_failure_logical_tokens, "route_signature": route_signature}


def run_adaptive_entry(adaptive_runner, entry_pair, source_root, workspace, runtime_cache_root, output_root, complexity_score, codex_bin, timeout, prompt_text):
    output_root.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(adaptive_runner), "--entry-model", entry_pair[0], "--entry-effort", entry_pair[1], "--sandbox", "read-only", "--project-root", str(source_root), "--task-type", "code", "--module", source_root.name, "--complexity-score", str(complexity_score), "--workload-id", f"benchmark-{sha256_bytes(prompt_text.encode('utf-8'))[:16]}", "--receipt-output", str(output_root / "receipt.json"), "--result-output", str(output_root / "result.json"), "--workdir", str(workspace), "--cache-root", str(runtime_cache_root), "--codex-bin", codex_bin, "--timeout", str(timeout), "--emit-result"]
    process = subprocess.run(command, input=prompt_text, text=True, capture_output=True, cwd=workspace, env=os.environ.copy(), shell=False, timeout=timeout + 30, check=False)
    if process.returncode != 0:
        raise RuntimeError("adaptive runner failed")
    summaries = []
    for raw_line in process.stdout.splitlines():
        try:
            summary = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(summary, dict) and "status" in summary:
            summaries.append(summary)
    if len(summaries) != 1 or summaries[0].get("status") != "pass" or not isinstance(summaries[0].get("result"), str):
        raise RuntimeError("adaptive runner did not return one passing result")
    receipt = strict_json_object((output_root / "receipt.json").read_text(encoding="utf-8"))
    return extract_result_document(summaries[0]["result"]), receipt_projection(receipt)


def resolve_adaptive_runner(codex_home):
    """Locate the adaptive runner across the engine's supported install shapes.

    Order matters: an explicit ENGINE_ROOT_ENV override wins, then the legacy
    Codex skill-bundle layout under CODEX_HOME, then this repo's own flat
    scripts/ directory. The repo-local sibling is last so a deployed bundle
    keeps driving the deployed runner rather than the checkout it was run from.
    """
    override = os.environ.get(ENGINE_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve() / "scripts" / "obsidian_adaptive_model_runner.py"
    bundled = codex_home / "skills" / "task-analyze-skill" / "scripts" / "obsidian_adaptive_model_runner.py"
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().parent / "obsidian_adaptive_model_runner.py"


def run_bridge(args):
    prompt_text = validated_prompt(args.prompt_file)
    complexity_score = benchmark_complexity_score(prompt_text)
    configured_python = Path(os.environ[PYTHON_ENV]).expanduser().resolve(strict=True)
    if configured_python != Path(sys.executable).resolve(strict=True):
        raise ValueError("benchmark bridge interpreter does not match its binding")
    codex_home = Path(os.environ["CODEX_HOME"]).expanduser().resolve(strict=True)
    adaptive_runner = resolve_adaptive_runner(codex_home)
    if not adaptive_runner.is_file():
        raise ValueError("adaptive runner is unavailable")
    codex_bin = os.environ.get(CODEX_BIN_ENV, "codex")
    timeout = int(os.environ.get(CHILD_TIMEOUT_ENV, "720"))
    cache_root = os.environ.get(CACHE_ROOT_ENV)
    if timeout <= 0:
        raise ValueError("adaptive child timeout is invalid")
    if not cache_root:
        raise ValueError("benchmark cache root binding is unavailable")
    source_root = args.workdir.expanduser().resolve(strict=True)
    cache_root = Path(cache_root)
    entry_pair = entry_pair_from_environment()
    workload_sha256 = sha256_bytes(prompt_text.encode("utf-8"))
    claim_adaptive_launch(cache_root, workload_sha256, entry_pair)
    workspace = prepare_read_only_workspace(source_root, cache_root)
    runtime_cache_root = workspace / "Cache" / "task-analyze"
    output_root = runtime_cache_root / "bridge-output"
    primary_result, primary_execution = run_adaptive_entry(adaptive_runner, entry_pair, source_root, workspace, runtime_cache_root / "primary", output_root, complexity_score, codex_bin, timeout, prompt_text)
    if primary_execution["selected_pair"] != primary_execution["route_signature"]["selected_pair"]:
        raise RuntimeError("adaptive bridge selected execution is inconsistent")
    return primary_result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bridge one frozen Auto benchmark workload into adaptive routing.")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    try:
        document = run_bridge(parse_args(argv))
    except (KeyError, OSError, UnicodeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"benchmark auto bridge failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
