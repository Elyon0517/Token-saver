# What Routing Actually Costs

Measured on real runs against a live Codex CLI. Receipts trace to rows in Codex's own
`state_5.sqlite`, so these are observed, not estimated.

| Task | Total | Uncached input | Output | Output share |
|---|---:|---:|---:|---:|
| List 5 function names (read-only) | 64,980 | 15,425 | 403 | 0.62% |
| Fix one typo, `teh` → `the` | 96,584 | 16,190 | 1,034 | 1.07% |

Entry `gpt-5.6-luna|max`; executed `luna|low` and `luna|max` respectively.

## What this means

Routing any task through `codex exec` costs **65k–97k logical tokens before the task starts**,
independent of how small the task is. Over 98% of that is not the answer.

Two consequences:

**Model and effort selection are second-order.** Choosing `luna|low` over `sol|ultra` operates
on top of a 65k floor it cannot recover. The ladder is worth having, but it is not where the
money is.

**Routing small work is a straight loss.** The overhead is not a cost the engine reduces — it
is a cost the engine *introduces*. The gate exists to stop the engine from spending it on work
that never needed a second session.

This is the same shape as the upstream v47 cohort's simple tier (−186.2%), now confirmed
locally rather than inherited.

## Reading the numbers honestly

`total_tokens` is a logical count, not a bill. Most of the input is cached — 79,360 of 95,550
on the edit — and cached input is normally priced well below uncached. On uncached input alone
the edit still cost 16,190 tokens to produce 1,034.

Both tasks are deliberately trivial: they measure the floor, not a representative workload. A
task large enough to justify routing will have a much better output share. Finding where that
crossover sits for your own work is the point of taking your own baseline.

## Reproducing

```bash
export TOKEN_SAVER_MODEL_LADDER=~/.codex/token-saver/model-capability-ladder.json

echo "<task>" | python3 scripts/obsidian_adaptive_model_runner.py \
  --ladder "$TOKEN_SAVER_MODEL_LADDER" \
  --codex-bin <path-to-codex> \
  --project-root <dir> --workdir <dir> \
  --task-type code --operation fix --complexity-score 10 \
  --entry-model gpt-5.6-luna --entry-effort max \
  --sandbox read-only --receipt-output /tmp/receipt.json --emit-result
```

`--entry-model`/`--entry-effort` are required when running outside a Codex session: the entry
resolver reads `CODEX_THREAD_ID` and the session rollout to verify which model is driving, and
refuses to route when it cannot. Supplying the pair explicitly is the documented bypass and is
what the benchmark harness does.

Token counts are in `route_attempts[0].tokens` of the receipt.
