#!/usr/bin/env python3
"""Frozen execution-prompt contract for the entry-aware benchmark Auto arm."""

import hashlib


AUTO_BENCHMARK_ENTRY_MARKER = "AUTO_BENCHMARK_ENTRY"
AUTO_BENCHMARK_ENTRY_PAIR = "gpt-5.6-luna|max"
AUTO_BENCHMARK_ENTRY_PAIRS = frozenset({"gpt-5.6-luna|max", "gpt-5.6-sol|ultra"})
BOUND_WORKLOAD_SHA256 = "BOUND_WORKLOAD_SHA256"


def auto_benchmark_execution_prompt(raw_workload, entry_pair=AUTO_BENCHMARK_ENTRY_PAIR):
    """Wrap one unchanged workload in the benchmark-only bound-entry contract."""
    if not isinstance(raw_workload, str) or not raw_workload.strip():
        raise ValueError("raw benchmark workload must be non-empty text")
    if BOUND_WORKLOAD_SHA256 in raw_workload:
        raise ValueError("raw benchmark workload contains a reserved binding marker")
    if entry_pair not in AUTO_BENCHMARK_ENTRY_PAIRS:
        raise ValueError("benchmark entry pair is invalid")
    workload_sha256 = hashlib.sha256(raw_workload.encode("utf-8")).hexdigest()
    return f"""{AUTO_BENCHMARK_ENTRY_MARKER}
You are the fixed {entry_pair} controller. The workload body is deliberately absent.
Launch exactly one bridge process: invoke the executable in CODEX_AUTO_BENCHMARK_PYTHON directly, with no interpreter discovery or substitution, on `skills/task-analyze-skill/scripts/benchmark_auto_entry_bridge.py` resolved under CODEX_HOME; pass `--prompt-file` from CODEX_AUTO_BENCHMARK_PROMPT_PATH and `--workdir` as the current working directory. Use the longest supported initial wait. If the tool yields a running session, poll that same session until exit; polling is not a retry and must never launch a second process.
On exit 0, return the bridge stdout JSON byte-for-byte and stop. Do not read the prompt/source, solve, explain, verify, retry, launch another task, or run Ending/Fix.
{BOUND_WORKLOAD_SHA256}: {workload_sha256}
"""
