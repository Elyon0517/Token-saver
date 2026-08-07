# Token Saver — Agent Bootstrap

Merge this section into your agent's always-loaded instruction file
(`~/.codex/AGENTS.md`, `~/CLAUDE.md`, or the equivalent). It is the only part of this
engine that is always resident. Replace `<engine>` with your checkout path.

## Every submission starts here

```
python3 <engine>/scripts/fast_path.py evaluate "<task>"
```

Regex only. Zero tokens, zero model calls. Run it **before** loading any other contract.

**`eligible: true`** → do the task inline and stop. No contract loads, no router, no routing
memory, no receipt. Then:

```
python3 <engine>/scripts/fast_path.py record "<task>" --outcome pass
```

If the result had to be corrected, record `--outcome corrected` instead — that permanently
suppresses this task *shape* from the fast path. The fingerprint is a bounded category
(type + operation + band), not a per-task identity, so this suppresses similar small
tasks too. That errs toward routing, which is the safe direction.

**`eligible: false`** → load `<engine>/contracts/routing-kernel.md` and follow it.

## Why the gate comes first

Routing is not free. Deciding how to route costs tokens before the task is attempted. On the
upstream v47 cohort that overhead was ~36,800 tokens per run against a simple-tier task worth
~16,300 tokens in total — the router cost more than twice what it was trying to save, a −186%
regression on that tier.

Never route work too small to pay for the routing.

## Loading discipline

Load `contracts/routing-kernel.md` only when the gate says to route. Load a file from
`contracts/references/` only when its specific situation occurs. Never load the reference set
speculatively — that is exactly the cost this layering exists to remove.

## Claims

Never state a token or time saving without evidence from `scripts/strategy_performance.py`.
Never present a planned pair as an executed one: without a runtime receipt, disclose it as
`task_assignment` / `UNVERIFIED (no runtime receipt)`.
