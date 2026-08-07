# Token Saver

A routing engine that decides **how cheaply a task can be done** — and, first, whether it is
worth deciding at all.

Built on the routing core of [qin-codex-skills](https://github.com/) with four changes: a
zero-cost gate in front of the router, a 92%-smaller resident contract, a free local-model
rung, and an exact-input result cache.

**634 tests, all passing.**

---

## The problem this exists to solve

Model routing is usually pitched as free money: send easy work to a cheap model, keep the
expensive one for hard work. The upstream project measured that claim honestly, and the result
is the most useful thing in it:

| Tier | Direct tokens | Routed tokens | Savings |
|---|---:|---:|---:|
| simple | 32,654 | 93,448 | **−186.2%** |
| medium | 49,451 | 91,398 | **−84.8%** |
| complex | 538,903 | 273,442 | +49.3% |

Routing *lost* on two tiers out of three. The cause is arithmetic, not a bug: deciding how to
route cost ~36,800 tokens per run, while a whole simple-tier task was worth ~16,300. **The
router cost more than twice what it was trying to save.**

So this engine's first move is not to route better. It is to *not route* work too small to pay
for the routing — and to make asking that question cost nothing.

---

## How it works

```
task
 │
 ├─▶ ① fast path gate ── regex only, 0 tokens, 0 model calls
 │      eligible? ──yes──▶ do it inline. stop. no contract, no router, no receipt.
 │         │no
 │         ▼
 ├─▶ ② result cache ── same prompt + same file contents + same pair? return stored answer
 │         │miss
 │         ▼
 └─▶ ③ route ── score 0-100, then cheapest capable rung first:
            local model ($0) → priority producer → quality ladder (weak → strong)
                                                        │
                                                        ▼
            ④ learn ── receipt-backed only: 2 PASS ↓ a rung, quality FAIL ↑ a rung, freeze
```

Each stage exists to keep work away from the stage below it, which is more expensive.

### ① Fast path — the fix for −186%

`scripts/fast_path.py` answers "is this below the routing break-even point?" using regex over
the prompt. No model call, no memory read, no filesystem walk. Asking is free.

Admitted only when **all** hold: score ≤ 24, low risk, low ambiguity, at most one file, not
read-only work, and no disqualifying signal (multi-file, structural, security, irreversible,
multi-stage, concurrency).

```bash
python3 scripts/fast_path.py evaluate "fix the typo in config.py"
# {"eligible":true,"complexity_score":10,"decision_cost_tokens":0,...}
```

**Learning without lying.** A fast-pathed run produces no runtime receipt, and this engine
never fabricates one to make unverified work look verified. So the fast path keeps its own
ledger and learns one narrow, honest thing: if a result had to be corrected, that task *shape*
is suppressed from the fast path permanently. The fingerprint is a bounded category
(type + operation + band), not a per-task identity, so similar small tasks are suppressed too —
which errs toward routing, the safe direction.

```bash
python3 scripts/fast_path.py record "<task>" --outcome pass       # or: --outcome corrected
```

### ② Result cache — the free lunch nobody was eating

Neither source project had one. Same question, same inputs, same pair → the stored answer, at
zero tokens.

A hit cannot change correctness — it returns something already produced and accepted. That
holds only because the key is exact: a hash of the normalized prompt, the **content** hashes of
every input file, and the `model|effort` pair.

Keying on content is what makes invalidation automatic — edit a file and the key changes, so a
stale entry can never be served. There is no invalidation logic to get wrong.

```bash
python3 scripts/result_cache.py stats     # hit_rate, tokens_avoided
```

Below ~20% hit rate it is not earning its complexity. Measure before keeping it.

### ③ Local model rung — free tokens

The upstream ladder was entirely cloud. This adds a rung below it:

```
local model ($0)  →  priority producer  →  quality ladder
```

Admission is identical to the priority producer's, deliberately: anything cheap enough for that
rung is cheap enough to try for free first. Off unless configured.

```bash
export TOKEN_SAVER_LOCAL_BASE_URL=http://127.0.0.1:8000/v1
export TOKEN_SAVER_LOCAL_MODEL=your-local-model
python3 scripts/local_producer.py --status
```

Works with LM Studio, Ollama, vLLM, MLX — anything OpenAI-compatible. Thinking mode is off by
default: reasoning models otherwise spend hundreds of tokens on a hidden preamble before
answering, and a rung admitting only 0–24 work rarely needs it.

A zero-result failure (unreachable, timeout, empty) is **operational**: it falls through to the
next rung and is *not* learned as a quality failure. Nothing was produced, so there is nothing
to judge.

### ④ Learning

Only a real runtime receipt moves the ladder.

| Signal | Movement |
|---|---|
| 1 Real PASS | keep the pair |
| 2 receipt-matched Real PASS | one rung down (effort before model) |
| Quality/correctness FAIL | one rung up (effort before model) |
| Operational failure | neutral — never a quality boundary |
| Stable lowest passing pair | freeze (`trial=false`) |

Without a receipt, a pair is disclosed as `task_assignment` / `UNVERIFIED (no runtime receipt)`
and cannot move routing. A label is not proof.

---

## Contract layering

The engine's own prose was its largest fixed cost. Upstream loaded a ~10,985-token routing
contract to make routine decisions.

| | Upstream | Here |
|---|---:|---:|
| Resident contract | ~10,985 tok | **~911 tok** |
| | | **−91.7%** |

`contracts/routing-kernel.md` is the only always-resident file. Everything else lives in
`contracts/references/` and loads **only when its situation occurs**.

`tests/test_contracts.py` asserts the kernel stays under budget, that the free gate is
ordered before the router, and that every reference the kernel names exists. The layering is
enforced, not merely intended.

---

## Install

Requires Python 3.11+ (standard library only — no pip install). The routing half also needs
the `codex` CLI on `PATH` — see "What this engine runs on" below.

```bash
git clone git@github.com:Elyon0517/Token-saver.git
cd Token-saver
python3 -m unittest discover -s tests -p 'test_*.py' -b
```

Then merge `AGENTS.md` into your agent's always-loaded instruction file
(`~/.codex/AGENTS.md`, `~/CLAUDE.md`, or equivalent), replacing `<engine>` with your checkout
path. That file is small on purpose: it only tells the agent to run the free gate first and
where to go if the gate says to route.

---

## Commands

**Gate and score**

```bash
python3 scripts/fast_path.py evaluate "<task>"                    # exit 0; .eligible decides
python3 scripts/fast_path.py record "<task>" --outcome pass       # or corrected
python3 scripts/fast_path.py suppressed                           # suppressed task shapes
python3 scripts/task_complexity_score.py "<task>"                 # score only, no routing
```

**Cache**

```bash
python3 scripts/result_cache.py lookup "<prompt>" --file a.py --pair "model|effort"
python3 scripts/result_cache.py store  "<prompt>" --result "<text>" --file a.py --pair "model|effort" --total-tokens 700
python3 scripts/result_cache.py stats
python3 scripts/result_cache.py purge [--max-age-days N]
```

**Route and execute**

```bash
python3 scripts/obsidian_adaptive_model_runner.py --emit-result ...   # one producer
python3 scripts/task_route_dispatcher.py run-plan <plan.json>         # multi-node graph
python3 scripts/local_producer.py --status                            # local rung
```

**Model ladder**

```bash
python3 scripts/sync_model_capabilities.py --update    # rescan local model cache
python3 scripts/sync_model_capabilities.py --check     # verify JSON and snapshot agree
```

Ordinary routing never rescans. Only an explicit update does, and it never reaches the network.

**Benchmark**

```bash
python3 scripts/benchmark_suite_runner.py \
  --suite-root <dir> \
  --direct-codex-home <dir> --global-codex-home <dir> \
  --tier-repeats simple=4,medium=2,complex=2

python3 scripts/strategy_performance.py     # the only authority for a savings claim
```

---

## Configuration

All optional. Defaults keep the engine standalone and the extras off.

| Variable | Default | Effect |
|---|---|---|
| `TOKEN_SAVER_LOCAL_BASE_URL` | — | Enables the local rung |
| `TOKEN_SAVER_LOCAL_MODEL` | — | Local model name (required with the URL) |
| `TOKEN_SAVER_LOCAL_API_KEY` | `none` | Bearer token |
| `TOKEN_SAVER_LOCAL_TIMEOUT` | `60` | Seconds |
| `TOKEN_SAVER_LOCAL_ENABLE_THINKING` | off | Allow a reasoning preamble |
| `TOKEN_SAVER_RESULT_CACHE` | `~/.codex/model-routing-memory/result-cache.sqlite` | Cache location |
| `TOKEN_SAVER_RESULT_CACHE_DISABLED` | off | Bypass the cache entirely |
| `TOKEN_SAVER_FAST_PATH_LEDGER` | `~/.codex/model-routing-memory/fast-path-events.jsonl` | Fast-path ledger |
| `TOKEN_SAVER_REQUIRE_INSTALLED_SKILLS` | off | Require plan `skill` ids to exist on disk |
| `TOKEN_SAVER_ENGINE_ROOT` | — | Explicit engine root for the benchmark bridge |
| `CODEX_MODEL_ROUTING_MEMORY` | `~/.codex/model-routing-memory/events.jsonl` | Routing ledger |
| `CODEX_OBSIDIAN_VAULT` | — | Optional Obsidian projection |

---

## Layout

```
AGENTS.md                    always-loaded bootstrap (~450 tok) — merge into your agent
contracts/
  routing-kernel.md          the only resident contract (~911 tok)
  references/                16 docs, loaded only on demand
scripts/                     32 modules, 17,472 lines
  fast_path.py               ① zero-cost gate                        [new]
  result_cache.py            ② exact-input cache                     [new]
  local_producer.py          ③ local rung, answered in-process       [new]
  task_signals.py            dependency-free prompt signals          [new]
  routing_policy.py          ladder movement, rung admission
  model_registry.py          ladder built from the local model cache
  obsidian_adaptive_model_runner.py   single-producer route
  task_route_dispatcher.py   multi-node graph execution
  obsidian_model_memory.py   receipt-backed learning
  model_execution_receipt.py execution + receipt reconstruction
  benchmark_*.py             measurement harness
tests/                       regression-tested; 634 tests
assets/                      model ladder, route fixtures, benchmark evidence
```

---

## What this engine runs on

**Codex, and only Codex.** That is not an oversight to route around — it follows from what
the engine is built on.

Routing here is not "tell a model what to do". It is "tell a model what to do, then **prove
what actually ran**". The learning loop moves a rung on receipt-backed evidence only, so a
receipt that cannot be verified is worse than none: the engine would learn from fiction.

That proof comes from three Codex-specific sources, all read *after* the command is issued:

| Question | Answered by |
|---|---|
| Did it actually run? | the `codex --json` event stream on stdout |
| Which model and effort *really* served it? | Codex's `threads` table in `state.sqlite` |
| How many tokens? | Codex's rollout `.jsonl` |

Claude Code, Cline, and friends have no `threads` table and no rollout file. Swapping the
launch command is easy; supplying evidence of that strength is the actual problem.

The local-model rung works because it does not travel this path at all — it is answered
in-process and builds its own, explicitly weaker receipt (`limitations` states that identity
is asserted from the request, not observed from runtime metadata).

An earlier version of this repo shipped a backend registry that made this look pluggable. It
was removed: it only ever worked for the in-process case, and an abstraction that advertises
portability it does not have is worse than an honest hard dependency.

Making it real means putting receipt reconstruction behind the same seam — and first deciding
a policy question, not a coding one: **may a backend with weaker evidence participate in
ladder learning at all?**

---

## What was deliberately left out

**Context offloading** — passing file *paths* to a sub-agent so file contents never enter
the parent's context. This is the single largest token lever available (order-of-magnitude, not
percentage) and this engine does not have it. `source_allowlist` on graph nodes is the hook it
would attach to.

**Backend portability** — see "What this engine runs on" above. The half-finished registry
that implied it was removed rather than left in place.

**The upstream lifecycle skills** — `management`, `optimization`, `code`, `prompt`, `workflow`,
and the verification-prose validators. They are a Codex workflow system, not a token-saving
engine. Their scripts, tests, and the 8-skill bundle shape are not here.

**Fabricated receipts** — the fast path and the local rung both produce work without
rollout-backed evidence, and neither pretends otherwise.

---

## Before you claim it saves anything

The tier table at the top is one frozen cohort on someone else's hardware. It is a worked
example of a method, not a result you inherit.

Take a baseline on your own machine first, then change one thing at a time and re-measure.
Report steady-state execution separately from controller overhead — folding them together turns
a +26.2% steady-state saving into a −79.5% end-to-end loss on the same data. Both are true.
Publishing only the flattering one is not.

`contracts/references/benchmark.md` has the full method and the conditions under which a
savings claim must be rejected.
