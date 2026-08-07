# Benchmark And Savings Claims

No savings claim without evidence from `scripts/strategy_performance.py`. A route label,
a plan, or a plausible mechanism is not evidence.

## Run it

```bash
python3 scripts/benchmark_suite_runner.py \
  --suite-root <dir> \
  --direct-codex-home <dir> --global-codex-home <dir> \
  --tier-repeats simple=4,medium=2,complex=2
```

Two arms, identical in everything except routing:

- **Direct** — a fixed pair, engine disabled.
- **Auto** — entry pair, then the engine's own route.

Both arms must share prompt, inputs, cwd, sandbox, user/project configuration, output
contract, and acceptance. Anything else and the comparison is void.

`--resume` continues a pristine frozen suite. The runner pauses on account-quota pressure so a
throttled run does not silently contaminate timings.

## Report honestly

The primary claim compares **steady-state selected execution only**: after the route is frozen,
compare the chosen producer or graph critical path.

These are reported **separately and never folded into the savings number**:

- entry matching and controller work,
- calibration failures,
- retries, fallbacks, repairs,
- actual first-result latency,
- verification.

That separation is not a formality. On the upstream v47 cohort the controller cost 120.0s and
220,607 tokens; folding it in flips a +26.2% steady-state saving into a −79.5% end-to-end loss.
Both numbers are true. Publishing only the flattering one is not.

## Keep the regressions visible

The same cohort, per tier:

| Tier | Direct | Auto | Savings |
|---|---:|---:|---:|
| simple | 32,654 | 93,448 | **−186.2%** |
| medium | 49,451 | 91,398 | **−84.8%** |
| complex | 538,903 | 273,442 | +49.3% |

Do not average these away. The negative tiers are the finding — they are why `fast_path.py`
exists. A tier table that hides them cannot tell you where the engine is losing.

## Reject a claim when

- correctness does not pass,
- the selected pair is not frozen, receipt-backed, and `trial=false`,
- evidence is stale, incomplete, or from a different workload,
- either arm changed configuration,
- the improvement lives entirely in excluded diagnostics.

Reject the *claim*, not the route. A structurally valid route with weak evidence is still a
valid route; it just cannot be advertised as a saving.

## Before optimizing anything

Take a baseline on your own machine. The numbers above are one frozen cohort on someone else's
hardware — they are a worked example of the method, not a result you inherit.
