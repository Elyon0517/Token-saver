#!/usr/bin/env python3
"""Pure prompt-derived signals: complexity score, band, task type, operation.

Why this module is dependency-free
----------------------------------
These four functions are what the zero-cost gate needs, and the gate is the first thing
every task runs. It previously reached them through obsidian_adaptive_model_runner, which
transitively loads the dispatcher, the receipt runner, routing memory, and the model
ladder -- ~111 modules, and a hard failure if the ladder JSON is unreadable. A gate that
cannot answer because an unrelated file is corrupt is worse than no gate.

So this module imports nothing but `re`. Nothing here reads a file, calls a model, or
touches configuration. The runner and the memory module import *from* here, which keeps
one definition of each signal rather than a copy that can drift.
"""

import re


COMPLEXITY_BANDS = (("small", 0, 24), ("standard", 25, 49), ("complex", 50, 74), ("advanced", 75, 100))


def complexity_band(complexity_score):
    if isinstance(complexity_score, bool) or not isinstance(complexity_score, int) or not 0 <= complexity_score <= 100:
        raise ValueError("complexity_score must be an integer from 0 to 100")
    return next(name for name, minimum, maximum in COMPLEXITY_BANDS if minimum <= complexity_score <= maximum)


def infer_complexity_score(prompt):
    """Score task complexity from 0 to 100 without reading task files."""
    text = re.sub(r"\s+", " ", str(prompt or "")).strip().lower()
    explicit = re.search(r"\bcomplexity(?: score)?\s*[:=]\s*(100|[1-9]?\d)\b", text)
    if explicit:
        return int(explicit.group(1))
    score = 30
    scope_weight = 0
    weighted_signals = (
        (r"\b(?:multi[- ]file|multiple files|six[- ]file|across files|project[- ]wide|cross[- ]project)\b", 18),
        (r"\b(?:architecture|migration|distributed|rollback)\b", 18),
        (r"\b(?:integration|pipeline|workflow graph|orchestration)\b", 12),
        (r"\b(?:security|database|concurrency|parallelism|performance|external api)\b", 15),
        (r"\b(?:lifecycle|model routing|global skill|global skills|global task)\b", 18),
        (r"\b(?:verification|verify|testing|tests|benchmark|regression|render|visual)\b", 10),
        (r"\b(?:repair loops?|retry|fresh verifier|downgrade|upgrade|fallback)\b", 8),
        (r"\b(?:separate tasks?|independent (?:checks?|tasks?)|subtasks?|own model)\b", 8),
    )
    for pattern, weight in weighted_signals:
        if re.search(pattern, text):
            scope_weight += weight
    system_terms = sum(term in text for term in ("skill", "script", "validator", "documentation", "tests", "memory", "obsidian", "model", "ending"))
    if system_terms >= 3:
        scope_weight += min(20, (system_terms - 2) * 4)
    score += scope_weight
    if scope_weight < 20 and re.search(r"\b(?:small|tiny|simple|localized|one[- ]line|single[- ]file|exact (?:function|method|symbol)|typo|copy edit|text edit)\b", text):
        score -= 14
    if scope_weight < 20 and re.search(r"\b(?:edit|change|fix|modify|rename|replace|update)\b", text):
        score -= 6
    if re.search(r"\b(?:large[- ]file|large|heavy|exhaustive)\b", text):
        score += 12
    numeric_signals = sum(marker in text for marker in ("decimal", "round_half_up", "round half up", "tax", "currency", "cents", "percent"))
    if numeric_signals >= 2:
        score += 20
    file_count = len(set(re.findall(r"(?<![\w./-])[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml)(?![\w/-])", text)))
    if file_count > 1:
        score += min(20, (file_count - 1) * 5)
    word_count = len(text.split())
    simple_question = (
        scope_weight == 0
        and word_count <= 24
        and (
            bool(re.match(r"^(?:what|who|when|where|which|why|how|calculate|convert|is|are|can|does|do)\b", text))
            or text.endswith("?")
        )
        and not re.search(r"\b(?:edit|change|fix|modify|rename|replace|update|write|implement|file|script)\b", text)
    )
    if simple_question:
        score = min(score, 12)
    if word_count >= 80:
        score += 8
    if word_count >= 160:
        score += 8
    return max(0, min(100, score))


def infer_complexity(prompt):
    return "complex" if infer_complexity_score(prompt) >= 50 else "easy"


def infer_task_type(prompt):
    text = re.sub(r"\s+", " ", str(prompt or "")).strip().lower()
    if re.search(r"\b(?:edit|change|fix|modify|rename|replace|update|write|implement)\b", text):
        return "code"
    if re.search(r"[\w./-]+\.(?:py|cs|js|ts|tsx|json|md|yaml|yml)\b", text):
        return "code"
    if re.match(r"^(?:what|who|when|where|which|why|how|calculate|convert|is|are|can|does|do)\b", text) or text.endswith("?"):
        return "question"
    return "code"


def infer_operation(prompt):
    text = re.sub(r"\s+", " ", str(prompt or "")).strip().lower()
    for operation in ("rename", "replace", "update", "modify", "edit", "fix", "write"):
        if re.search(rf"\b{operation}\b", text):
            return operation
    if re.search(r"\bchang(?:e|ing)\b", text):
        return "edit"
    if infer_task_type(prompt) == "question":
        return "answer"
    return "work"
