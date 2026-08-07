#!/usr/bin/env python3
"""Decide, at zero token cost, whether a task may skip the routing pipeline entirely.

Why this exists
---------------
The routing pipeline costs real tokens before the task is even attempted: loading the
routing contract, resolving the entry pair, reading routing memory, spawning a child
producer, and writing a receipt. Measured on the upstream v47 benchmark cohort that
overhead was ~36,800 tokens per Auto run, against a simple-tier task whose entire
direct cost was ~16,300 tokens. That is where the simple-tier -186% token regression
came from: the router cost more than twice what it was trying to save.

The fix is not a better router. It is not running the router on work too small to pay
for it. `evaluate()` answers "is this task below the routing break-even point?" using
only regex over the prompt -- no model call, no memory read, no filesystem walk -- so
asking the question is free.

Eligible tasks are executed inline by the entry model and recorded with one JSONL line.
Everything else routes normally.

Learning boundary
-----------------
The fast path deliberately writes to its OWN ledger, never to the receipt-backed
quality-ladder history. A fast-path run produces no runtime receipt, and this engine
does not fabricate receipts to make unverified work look verified. What it does learn
is narrower and honest: if a fast-pathed result had to be corrected, `suppress` records
that its capability fingerprint is not safe to fast-path again.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = SCRIPT_DIR.parent

# The gate imports ONLY the dependency-free signal module. It must be able to answer even
# when the model ladder, routing memory, or dispatcher is missing or corrupt -- a gate that
# cannot say "this is too small to route" because an unrelated file is broken is worse than
# no gate at all.
_signals = _load_file("fast_path_signals", SCRIPT_DIR / "task_signals.py")


def _capability_profiler():
    """Load the router's fingerprint profiler lazily, returning None if unavailable.

    Fingerprints must match the router's exactly or suppression would never match
    anything, so this reuses the real profiler rather than reimplementing it. But that
    profiler lives in the heavy routing stack, so it is imported only when a fingerprint
    is actually needed and its absence degrades to "no suppression" rather than a crash.
    """
    global _memory
    if _memory is not None:
        return _memory
    try:
        _memory = _load_file("fast_path_memory", SCRIPT_DIR / "obsidian_model_memory.py")
    except Exception:  # noqa: BLE001 - any failure here must not take the gate down
        _memory = False
    return _memory or None


_memory = None

# Same ceiling the priority producer uses for small edits, so the two admissions agree.
MAX_FAST_PATH_SCORE = 24

# Reading/auditing already stays inline in the normal flow, so admitting it here would
# credit the fast path with work that never routed. This is matched against the prompt
# rather than against infer_operation(): that inferrer only ever returns an edit verb,
# "answer", or "work", so it can never report a read-only operation.
READ_ONLY_PATTERN = re.compile(r"\b(?:audit|look ?up|read|review|inspect|check|show|list|status|verify|explain|summari[sz]e)\b")

# Any of these in the prompt means the task is not the small, self-contained edit this
# path is for, regardless of what the numeric score came out to.
DISQUALIFYING_PATTERNS = (
    (r"\b(?:multi[- ]file|multiple files|across files|project[- ]wide|cross[- ]project)\b", "multi_file_scope"),
    (r"\b(?:architecture|migration|refactor|redesign|rewrite)\b", "structural_change"),
    (r"\b(?:security|credential|secret|token|password|auth)\b", "security_sensitive"),
    (r"\b(?:migrate|deploy|publish|release|rollback|delete|drop)\b", "irreversible_operation"),
    (r"\b(?:and then|after that|followed by|first.{0,40}\bthen\b)\b", "multi_stage"),
    (r"\b(?:concurrency|parallel|distributed|race condition)\b", "concurrency"),
)

FILE_PATTERN = re.compile(r"(?<![\w./-])[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml)(?![\w/-])")

DEFAULT_LEDGER = Path.home() / ".codex" / "model-routing-memory" / "fast-path-events.jsonl"
LEDGER_ENV = "TOKEN_SAVER_FAST_PATH_LEDGER"


def resolve_ledger(ledger=None):
    return Path(ledger or os.getenv(LEDGER_ENV) or DEFAULT_LEDGER).expanduser().resolve()


def _normalized(prompt):
    return re.sub(r"\s+", " ", str(prompt or "")).strip().lower()


def capability_fingerprint(prompt, *, risk="low", ambiguity="low"):
    """Stable fingerprint for this task shape, or None when the profiler is unavailable.

    Note this is a *bucket*, not a per-task identity: the profiler hashes bounded
    categories (task type, operation, band, risk, ambiguity) and never raw prompt text.
    Distinct tasks of the same shape therefore share a fingerprint, so suppressing one
    suppresses the whole bucket. That errs toward routing, which is the safe direction.
    """
    memory = _capability_profiler()
    if memory is None:
        return None
    score = _signals.infer_complexity_score(prompt)
    profile = memory.task_capability_profile(
        _signals.infer_task_type(prompt),
        "general",
        _signals.infer_operation(prompt),
        "text",
        score,
        risk,
        ambiguity,
        "",
        "",
        None,
    )
    return profile["capability_fingerprint"]


def suppressed_fingerprints(ledger=None):
    path = resolve_ledger(ledger)
    if not path.is_file():
        return frozenset()
    suppressed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "suppress" and isinstance(event.get("capability_fingerprint"), str):
            suppressed.add(event["capability_fingerprint"])
    return frozenset(suppressed)


def evaluate(prompt, *, risk="low", ambiguity="low", ledger=None, file_count=None):
    """Return the fast-path decision for one prompt.

    Free: no model call, and no I/O beyond reading the small suppression ledger.

    `capability_fingerprint` in the result is **lazy** -- it is None unless the ledger
    holds at least one suppression to match against, because resolving it requires the
    heavy routing stack this gate deliberately avoids loading. Call
    capability_fingerprint() directly when you need it unconditionally.
    """
    text = _normalized(prompt)
    score = _signals.infer_complexity_score(prompt)
    band = _signals.complexity_band(score)
    operation = _signals.infer_operation(prompt)
    task_type = _signals.infer_task_type(prompt)
    referenced_files = file_count if file_count is not None else len(set(FILE_PATTERN.findall(text)))

    reasons = []
    if not text:
        reasons.append("empty_prompt")
    if score > MAX_FAST_PATH_SCORE:
        reasons.append(f"score_above_{MAX_FAST_PATH_SCORE}")
    if risk != "low":
        reasons.append("risk_not_low")
    if ambiguity != "low":
        reasons.append("ambiguity_not_low")
    if referenced_files > 1:
        reasons.append("multiple_files_referenced")
    if READ_ONLY_PATTERN.search(text):
        reasons.append("read_only_operation_already_inline")
    for pattern, label in DISQUALIFYING_PATTERNS:
        if re.search(pattern, text):
            reasons.append(label)

    # Only pay for the fingerprint when there is something to match it against. This keeps
    # the common path free of the heavy profiler import entirely.
    suppressed = suppressed_fingerprints(ledger)
    fingerprint = capability_fingerprint(prompt, risk=risk, ambiguity=ambiguity) if suppressed else None
    if fingerprint is not None and fingerprint in suppressed:
        reasons.append("fingerprint_suppressed_by_prior_correction")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "complexity_score": score,
        "complexity_band": band,
        "task_type": task_type,
        "operation": operation,
        "referenced_file_count": referenced_files,
        "capability_fingerprint": fingerprint,
        "decision_cost_tokens": 0,
    }


def append_event(event, ledger=None):
    path = resolve_ledger(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    return path


def record(prompt, *, outcome, risk="low", ambiguity="low", ledger=None, note=""):
    """Record that a fast-pathed task completed. `outcome` is pass or corrected.

    Unlike evaluate(), this always resolves the fingerprint: it is what a later
    suppression has to match on, so a record written without one would be inert.
    """
    score = _signals.infer_complexity_score(prompt)
    fingerprint = capability_fingerprint(prompt, risk=risk, ambiguity=ambiguity)
    event = {
        "event": "record",
        "outcome": outcome,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "capability_fingerprint": fingerprint,
        "complexity_score": score,
        "complexity_band": _signals.complexity_band(score),
        "task_type": _signals.infer_task_type(prompt),
        "operation": _signals.infer_operation(prompt),
        "note": note[:200],
    }
    if fingerprint is None:
        # Be explicit rather than writing a silently inert record.
        event["fingerprint_unavailable"] = True
    append_event(event, ledger)
    # A correction is the only real quality signal available without a receipt: the
    # entry model's inline answer was not good enough, so stop fast-pathing this shape.
    if outcome == "corrected" and fingerprint is not None:
        append_event(
            {
                "event": "suppress",
                "recorded_at": event["recorded_at"],
                "capability_fingerprint": fingerprint,
                "reason": "fast_path_result_required_correction",
            },
            ledger,
        )
    return event


def _emit(payload):
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser("evaluate", help="Decide whether a prompt may skip routing.")
    evaluate_parser.add_argument("prompt", nargs="?")
    evaluate_parser.add_argument("--risk", default="low")
    evaluate_parser.add_argument("--ambiguity", default="low")

    record_parser = subparsers.add_parser("record", help="Record a completed fast-path task.")
    record_parser.add_argument("prompt", nargs="?")
    record_parser.add_argument("--outcome", choices=("pass", "corrected"), required=True)
    record_parser.add_argument("--risk", default="low")
    record_parser.add_argument("--ambiguity", default="low")
    record_parser.add_argument("--note", default="")

    subparsers.add_parser("suppressed", help="List suppressed capability fingerprints.")

    args = parser.parse_args(argv)

    if args.command == "suppressed":
        _emit({"status": "pass", "suppressed": sorted(suppressed_fingerprints(args.ledger))})
        return 0

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not str(prompt).strip():
        _emit({"status": "fail", "reason": "prompt_required"})
        return 1

    if args.command == "evaluate":
        decision = evaluate(prompt, risk=args.risk, ambiguity=args.ambiguity, ledger=args.ledger)
        _emit({"status": "pass", **decision})
        return 0

    event = record(prompt, outcome=args.outcome, risk=args.risk, ambiguity=args.ambiguity, ledger=args.ledger, note=args.note)
    _emit({"status": "pass", **event})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
