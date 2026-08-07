# Token Saver

A model-routing engine for Codex. Scores every task, skips routing entirely when the task is
too small to pay for it, serves repeat work from cache, and picks the cheapest model rung
proven to work for that task shape.

Extracted from the routing core of qin-codex-skills. 634 tests, Python standard library only.

---

## Features

| Stage | Module | What it does |
|---|---|---|
| **Gate** | `fast_path.py` | Regex-only eligibility check. Small, low-risk, single-file tasks skip the router, the contract load, and the receipt entirely. Costs 0 tokens and ~20 ms. |
| **Cache** | `result_cache.py` | Returns a stored result when prompt + input file *contents* + model pair all match. Editing any input changes the key, so stale entries cannot be served. |
| **Rungs** | `local_producer.py` | Adds a free local-model rung: `local ($0) → priority producer → quality ladder`. Off unless configured. |
| **Router** | `obsidian_adaptive_model_runner.py` | Scores 0–100, resolves the entry pair, runs one producer. Multi-stage requests go through `task_route_dispatcher.py`. |
| **Learning** | `obsidian_model_memory.py` | Receipt-backed only. 2 PASS → down a rung, quality FAIL → up a rung, stable pair freezes. Operational failures are neutral. |
| **Contracts** | `contracts/` | 911-token resident kernel; 16 references load only when their situation occurs. |
| **Benchmark** | `benchmark_suite_runner.py` | Direct vs Auto A/B harness. `strategy_performance.py` is the only authority for a savings claim. |

---

## Baseline — the problem, not the result

Upstream's routing on a frozen 12-run cohort, **before the gate existed**:

| Tier | Direct tokens | Routed tokens | Savings |
|---|---:|---:|---:|
| simple | 32,654 | 93,448 | −186.2% |
| medium | 49,451 | 91,398 | −84.8% |
| complex | 538,903 | 273,442 | +49.3% |

Routing overhead was ~36,800 tokens per run against a simple-tier task worth ~16,300 total.
These tiers are what the gate is aimed at. They are not this engine's results.

## Measured here

| | Before | Here | How |
|---|---:|---:|---|
| Resident contract | ~10,985 tok | **~911 tok** | `wc -c` on both files, ÷4 |
| Gate modules loaded | 111 | **20** | `len(sys.modules)` before/after import |
| Gate wall time | 60 ms | **20 ms** | `time` over the CLI |
| Tests | — | **634** | full suite |

Direct measurements of this repo, taken on one machine.

## Not measured

**The tier table above has not been re-run with the gate in place.** Doing so requires
`benchmark_suite_runner.py`, which needs the Codex CLI and two configured cohort homes.

So the gate's effect on those numbers is *predicted*, not observed: gated tasks skip the
router, so the ~36,800-token overhead should not be charged to them. That reasoning is sound
but unverified. Until you run the benchmark yourself, treat the simple and medium tiers as
open questions rather than solved ones.

---

## Requirements

- Python 3.11+ (standard library only, no pip install)
- **Codex CLI on `PATH`** for the routing half

Codex-only is structural: receipt evidence is reconstructed from the `codex --json` event
stream, Codex's `threads` database, and its rollout files. Other agent CLIs do not expose
equivalent evidence, and the learning loop only moves on verified receipts.

The gate, the score, and the cache have no Codex dependency, but they exist to feed the
router — without it they have nothing to gate or cache.

---

## Install

```bash
git clone git@github.com:Elyon0517/Token-saver.git
cd Token-saver
python3 -m unittest discover -s tests -p 'test_*.py' -b
```

Merge `AGENTS.md` into `~/.codex/AGENTS.md`, replacing `<engine>` with your checkout path.
That file is the always-resident bootstrap: run the gate first, load the kernel only if the
gate says to route.

If your checkout path contains spaces, quote it in every command you paste into `AGENTS.md`.

---

## Configuration

All optional. Defaults keep the extras off.

### Local model rung

```bash
export TOKEN_SAVER_LOCAL_BASE_URL=http://127.0.0.1:8000/v1
export TOKEN_SAVER_LOCAL_MODEL=your-local-model
python3 scripts/local_producer.py --status
```

| Variable | Default | Effect |
|---|---|---|
| `TOKEN_SAVER_LOCAL_BASE_URL` | — | Enables the rung (required) |
| `TOKEN_SAVER_LOCAL_MODEL` | — | Model name (required) |
| `TOKEN_SAVER_LOCAL_API_KEY` | `none` | Bearer token |
| `TOKEN_SAVER_LOCAL_TIMEOUT` | `60` | Seconds |
| `TOKEN_SAVER_LOCAL_ENABLE_THINKING` | off | Allow a reasoning preamble |

Any OpenAI-compatible server works: LM Studio, Ollama, vLLM, MLX. Thinking is off by default
because reasoning models spend hundreds of tokens on a hidden preamble before answering.

The rung offers only effort `low`. A node requesting another effort is refused, not silently
downgraded. Unreachable backend, timeout, or empty output is an operational failure: it falls
through to the next rung and is not recorded as a quality failure.

### Cache

| Variable | Default | Effect |
|---|---|---|
| `TOKEN_SAVER_RESULT_CACHE` | `~/.codex/model-routing-memory/result-cache.sqlite` | Location |
| `TOKEN_SAVER_RESULT_CACHE_DISABLED` | off | Bypass reads and writes |

Entries expire after 30 days on lookup. Empty results are never stored.

### Storage and routing

| Variable | Default | Effect |
|---|---|---|
| `TOKEN_SAVER_FAST_PATH_LEDGER` | `~/.codex/model-routing-memory/fast-path-events.jsonl` | Gate ledger |
| `CODEX_MODEL_ROUTING_MEMORY` | `~/.codex/model-routing-memory/events.jsonl` | Routing ledger |
| `CODEX_OBSIDIAN_VAULT` | — | Optional Obsidian projection; absence is a no-op |
| `TOKEN_SAVER_REQUIRE_INSTALLED_SKILLS` | off | Require plan `skill` ids to exist on disk |
| `TOKEN_SAVER_ENGINE_ROOT` | — | Explicit engine root for the benchmark bridge |

### Model ladder

Built from the local Codex model cache. Ordinary routing never rescans it.

```bash
python3 scripts/sync_model_capabilities.py --update    # rescan ~/.codex/models_cache.json
python3 scripts/sync_model_capabilities.py --check     # verify JSON and snapshot agree
```

Never fetches over the network. If the cache is unavailable, the last valid ladder is kept.

---

## Commands

**Gate and score**

```bash
python3 scripts/fast_path.py evaluate "<task>"                 # exit 0; .eligible decides
python3 scripts/fast_path.py record "<task>" --outcome pass    # or: --outcome corrected
python3 scripts/fast_path.py suppressed                        # suppressed task shapes
python3 scripts/task_complexity_score.py "<task>"              # score only
```

A task is admitted when all hold: score ≤ 24, low risk, low ambiguity, at most one file, not
read-only, and no disqualifying signal (multi-file, structural, security, irreversible,
multi-stage, concurrency).

`--outcome corrected` suppresses that task shape. The fingerprint is a bounded category
(type + operation + band), not a per-task identity, so similar small tasks are suppressed too.

**Cache**

```bash
python3 scripts/result_cache.py lookup "<prompt>" --file a.py --pair "model|effort"
python3 scripts/result_cache.py store  "<prompt>" --result "<text>" --file a.py --pair "model|effort" --total-tokens 700
python3 scripts/result_cache.py stats            # hit_rate, hits, misses, tokens_avoided
python3 scripts/result_cache.py purge [--max-age-days N]
```

`lookup` exits 0 on a hit, 1 on a miss.

**Route and execute**

```bash
python3 scripts/obsidian_adaptive_model_runner.py --emit-result ...
python3 scripts/task_route_dispatcher.py run-plan <plan.json>
python3 scripts/local_producer.py --status
```

**Benchmark**

```bash
python3 scripts/benchmark_suite_runner.py \
  --suite-root <dir> \
  --direct-codex-home <dir> --global-codex-home <dir> \
  --tier-repeats simple=4,medium=2,complex=2

python3 scripts/strategy_performance.py
```

Both arms must share prompt, inputs, cwd, sandbox, configuration, output contract, and
acceptance. Controller overhead, retries, and verification are reported separately from the
steady-state savings figure.

---

## Layout

```
AGENTS.md                    always-resident bootstrap — merge into ~/.codex/AGENTS.md
contracts/
  routing-kernel.md          the only resident contract (~911 tok)
  references/                16 docs, loaded on demand
scripts/                     32 modules
  fast_path.py               gate
  result_cache.py            cache
  local_producer.py          local rung
  task_signals.py            dependency-free prompt signals
  routing_policy.py          rung admission and ladder movement
  model_registry.py          ladder built from the local model cache
  model_execution_receipt.py execution and receipt reconstruction
  obsidian_model_memory.py   receipt-backed learning
  task_route_dispatcher.py   multi-node graph execution
  benchmark_*.py             measurement harness
tests/                       634 tests
assets/                      model ladder, route fixtures, benchmark evidence
```

Private state — routing history, receipts, ledgers, caches — stays under
`~/.codex/model-routing-memory/` and is never committed.

---

## Not included

- **Context offloading** — passing file paths to a sub-agent so file contents never enter the
  parent context. The largest remaining lever; `source_allowlist` on graph nodes is the hook.
- **Backend portability** — see Requirements.
- **The upstream lifecycle skills** — `management`, `optimization`, `code`, `prompt`,
  `workflow`, and the prose validators.
