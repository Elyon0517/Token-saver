# Routing Kernel

The only always-resident contract. Everything else loads on demand.

## 1. Gate (free)

Every submission, before loading anything else:

```
python3 scripts/fast_path.py evaluate "<task>"
```

Pure regex. Zero tokens, zero model calls.

- `eligible: true` → **do the task inline. Stop here.** No reference loads, no runner, no
  routing memory, no receipt. Afterwards: `fast_path.py record "<task>" --outcome pass`.
  If the result had to be corrected, record `--outcome corrected` instead — that suppresses
  this task shape from the fast path permanently.
- `eligible: false` → continue to §2.

This gate exists because routing is not free. On the upstream v47 cohort the router spent
~36,800 tokens per run deciding, against a simple-tier task worth ~16,300 tokens total —
a −186% regression. Never route work too small to pay for the routing.

## 2. Route

`evaluate` already returned `complexity_score` and `complexity_band`. Reuse them; do not
re-score.

| Band | Score |
|---|---|
| small | 0–24 |
| standard | 25–49 |
| complex | 50–74 |
| advanced | 75–100 |

Resolve the observable entry pair, then run the producer once:

```
python3 scripts/obsidian_adaptive_model_runner.py --emit-result ...
```

Two or more materially distinct result stages instead build one `dynamic_task_graph` and
call `scripts/task_route_dispatcher.py run-plan <plan>` once.

Selection order: exact receipt-backed history for this capability fingerprint wins over the
entry pair, in both directions. With no matching history, take the weaker of the contextual
cold start and the entry anchor — never start above the entry.

Free rungs first, cheapest first, for any low-risk low-ambiguity text/code/write/execute
node scoring 0–24 — including nodes inside a larger graph:

```
local model ($0)  →  priority producer  →  quality ladder (weakest → strongest)
```

Both pre-ladder rungs sit outside the quality ladder. A zero-result operational failure at
either falls through to the next rung and is **not** learned as a quality failure.

## 3. Learn

Only a real runtime receipt moves the ladder.

- 1 Real PASS → keep the pair.
- 2 receipt-matched Real PASS → trial one rung down (effort before model).
- Quality/correctness FAIL → one rung up (effort before model).
- Operational failure → neutral, never a quality boundary.
- Stable lowest passing pair → freeze with `trial=false`.

A label is not proof. Without a receipt, disclose the pair as `task_assignment` /
`UNVERIFIED (no runtime receipt)` and do not let it move routing.

## 4. Deliver

Produce and show the main result first. Verification runs after, never before, and only
when the result is complex or important enough to need a real check.

## Load on demand

Load a reference only when its situation actually occurs:

| Situation | Reference |
|---|---|
| Ladder movement, memory, cold start | `references/adaptive-routing.md` |
| Current models, efforts, cold-start pairs | `references/model-capabilities.md` |
| Plan/node schema, disclosure format | `references/route-contract.md` |
| Receipt fields and evidence levels | `references/runtime-receipts.md` |
| Registering a new execution domain | `references/router-extension-guide.md` |
| Local-model rung setup | `references/local-producer.md` |
| Result cache | `references/result-cache.md` |
| Benchmark and savings claims | `references/benchmark.md` |
| Python / C# / Unity C# work | `references/python-rules.md`, `references/csharp-rules.md`, `references/unity-csharp-rules.md` |

Never load the whole reference set speculatively. That is the cost this kernel exists to avoid.
