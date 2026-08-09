---
name: token-saver
description: "Use only when a task is large enough to be worth farming out to a separate model session: coordinated work across three or more files, an architecture or migration decision, a dependency graph with two or more independently checkable result stages, or an explicit request to route/benchmark/pick a model. Do NOT use for single-file edits, typo or docstring fixes, renames, reading or summarising code, answering questions, running a command, or any change you can finish in one pass — measured cost of routing one task is 65,000-97,000 tokens regardless of task size, so routing small work loses more than any model choice can save. When unsure, do not use this skill."
---

# Token Saver

Routes a task to the cheapest model rung proven to work for its shape, and refuses to route
work too small to pay for the routing.

## Read this first

Spawning a routed session costs **65,000–97,000 logical tokens before the task starts**,
measured on real runs (see `contracts/references/measurements.md`). That is the price of
entry, independent of task size. Model and effort selection operate *on top of* that fixed
cost and cannot recover it.

So the only question that matters is: **is this task worth 65k+ to hand off?**

If the answer is not clearly yes, stop and do the work directly in this session. That is not
a fallback — it is the cheaper path, and for most tasks it is the right one.

## Confirm before routing

The skill description filters by judgment. Confirm it deterministically:

```
python3 <engine>/scripts/fast_path.py evaluate "<task>"
```

Regex only, no model call, no tokens.

- `eligible: true` → **the task is below the routing break-even point.** Do it here and stop.
  Then record it so the engine learns:
  `python3 <engine>/scripts/fast_path.py record "<task>" --outcome pass`
  (Use `--outcome corrected` instead if the result needed fixing; that suppresses this task
  shape from the fast path. The fingerprint is a bounded category — type + operation + band —
  so similar small tasks are suppressed too, which errs toward routing.)
- `eligible: false` → continue below.

## Route

Score and band come from the gate output; do not re-score.

| Band | Score |
|---|---|
| small | 0–24 |
| standard | 25–49 |
| complex | 50–74 |
| advanced | 75–100 |

Run one producer:

```
python3 <engine>/scripts/obsidian_adaptive_model_runner.py --emit-result ...
```

Two or more materially distinct result stages instead build one `dynamic_task_graph` and call
`scripts/task_route_dispatcher.py run-plan <plan>` once.

Selection: exact receipt-backed history for this capability fingerprint wins over the entry
pair, in both directions. With no matching history, take the weaker of the contextual cold
start and the entry anchor — never start above the entry.

Free rungs first for any low-risk, low-ambiguity node scoring 0–24:

```
local model ($0)  →  priority producer  →  quality ladder (weakest → strongest)
```

Both sit outside the quality ladder. A zero-result operational failure at either falls through
to the next rung and is **not** learned as a quality failure.

## Learn

Only a real runtime receipt moves the ladder.

- 1 Real PASS → keep the pair.
- 2 receipt-matched Real PASS → trial one rung down (effort before model).
- Quality/correctness FAIL → one rung up (effort before model).
- Operational failure → neutral, never a quality boundary.
- Stable lowest passing pair → freeze with `trial=false`.

Without a receipt, disclose the pair as `task_assignment` / `UNVERIFIED (no runtime receipt)`
and do not let it move routing. A label is not proof.

## Deliver

Produce and show the main result first. Verification runs after, never before, and only when
the result is complex or important enough to need a real check.

## Load on demand

| Situation | Reference |
|---|---|
| What routing actually costs | `contracts/references/measurements.md` |
| Ladder movement, memory, cold start | `contracts/references/adaptive-routing.md` |
| Current models, efforts, cold-start pairs | `contracts/references/model-capabilities.md` |
| Plan/node schema, disclosure format | `contracts/references/route-contract.md` |
| Receipt fields and evidence levels | `contracts/references/runtime-receipts.md` |
| Registering a new execution domain | `contracts/references/router-extension-guide.md` |
| Local-model rung setup | `contracts/references/local-producer.md` |
| Result cache | `contracts/references/result-cache.md` |
| Benchmark and savings claims | `contracts/references/benchmark.md` |
| Python / C# / Unity C# work | `contracts/references/python-rules.md`, `csharp-rules.md`, `unity-csharp-rules.md` |

Never load the reference set speculatively.
