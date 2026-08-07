# Catalog-Generated Model Selection

The shared source of truth is the saved `assets/model-capability-ladder.json`. Ordinary routing loads it without comparing or refreshing the local catalog. If the ladder is missing, `scripts/model_registry.py` may bootstrap it once from `~/.codex/models_cache.json` without network access. Only an explicit user model-update request may run `scripts/sync_model_capabilities.py` to rescan the local cache and replace the ladder. If that cache is unavailable, retain the last valid ladder. `--check` verifies the saved JSON and human snapshot agree; it does not update either file.

## Quality Order

The saved registry contains only the highest numeric GPT family seen during the last explicit local update plus the optional priority producer. Provider catalog priority orders that family's variants from weakest to strongest, and each variant contributes only its supported reasoning efforts. Older visible families may remain in the machine cache, but they are excluded from the saved registry, its semantic digest, and all routing candidates.

`low -> medium -> high -> xhigh -> max -> ultra`

Only efforts exposed for a model are included. Movement stays inside the generated pairs:

- One Real PASS retains the current pair; two receipt-matched Real PASS results trial one lower effort on the same model, then the strongest effort on the next weaker model.
- Quality/correctness failure trials one higher effort on the same model, then the lowest effort on the next stronger model.
- Repeated PASS freezes the generated minimum pair; any verified fail-to-pass recovery boundary freezes its lowest passing pair without an immediate untested-gap probe. Token/time measurements for multiple passing pairs remain diagnostics and never override that lowest-correct pair.
- Operational failure is quality-neutral.

## Cold Start, Score, And Spark Priority

Every submission has a deterministic `0-100` complexity score: `0-24` small, `25-49` standard, `50-74` complex, and `75-100` advanced. An explicit `complexity score: N` or `--complexity-score N` overrides inference. First resolve the observable entry pair, then decompose real compound work into independently measurable steps. Each step receives a sanitized fingerprint from `step_kind`, controlled capability tags, task/code/operation/modality/risk/ambiguity, and difficulty band. Exact matching history selects its receipt-proven boundary before the entry is considered: a prior low-pair success routes down even from Sol Ultra, while a prior low-pair failure followed by stronger success routes up even from Luna Max. Only when exact history is empty, derive a contextual pair and choose the weaker of it and the entry anchor. A quality failure upgrades one rung; a verified recovery pair is then reused directly for that fingerprint.

The catalog may also expose an optional priority producer. The current catalog resolves that role to Spark. A low-risk, low-ambiguity text/code/write/execute task segment scoring `0-24` tries it first based on its own score, not the parent task score; fixed disjoint read-only source branches may also use it. Spark remains outside the quality ladder. Its zero-result, zero-token operational failure falls back to the node's contextual quality pair. Its Ending correctness/quality failure suppresses Spark for the matching step-capability fingerprint and upgrades the next matching task to the quality pair. A published result never foreground-fallbacks.

## Learning Boundary

Every eligible single-node production task runs `obsidian_adaptive_model_runner.py`, including a cold context. A multi-node production task runs one `dynamic_task_graph` through the dispatcher. The runner and dispatcher read the generated ladder plus merged local history and a bounded native Obsidian route: current project category, shared category, and exact-fingerprint linked project categories. They produce a producer or aggregate graph receipt but never write learning.

After presentation, `ending_task_ledger.py start --producer-receipt <path>` binds that receipt to the lifecycle. Every lifecycle stores score/band locally. The terminal Ending event automatically records the matched producer verdict; entry, selected, effective, and next pairs; task type; step kind/tags/fingerprint; score/band; switch direction; and recovery path to Obsidian. This closes the former gap where cold-start tasks stayed inline and therefore could never create enough evidence to descend, ascend, freeze, or suppress an unsuitable Spark context.

Exact read-only, tool-only, image/mixed, verifier, and Ending work uses `task_complexity_score.py`, reports the score, stays inline, and never fabricates a producer receipt, except that two or three explicit independent read-only source files first cost-admit one contextual producer versus the parallel-source/fused-merge graph. Unavailable or unconfigured Obsidian continues from the local receipt-backed history and queues projection; both-empty history uses the entry-aware shared cold start. The local event ledger and native project → Model Switch → category → shared-category projection are deduplicated contextual evidence. Open-ended multi-node strategy and every savings claim remain separately performance-admitted.
