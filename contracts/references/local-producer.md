# Local Producer Rung

The free rung below the priority producer. Off unless configured.

```
local model ($0)  →  priority producer  →  quality ladder (weakest → strongest)
```

## Enable

```bash
export TOKEN_SAVER_LOCAL_BASE_URL=http://127.0.0.1:8000/v1
export TOKEN_SAVER_LOCAL_MODEL=your-local-model
```

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_SAVER_LOCAL_API_KEY` | `none` | Bearer token |
| `TOKEN_SAVER_LOCAL_TIMEOUT` | `60` | Seconds |
| `TOKEN_SAVER_LOCAL_ENABLE_THINKING` | off | Allow a reasoning preamble |

Check status without executing:

```bash
python3 scripts/local_producer.py --status
```

Works with any OpenAI-compatible server: LM Studio, Ollama, vLLM, MLX.

## Thinking is off by default

Reasoning models spend hundreds of tokens on a hidden chain-of-thought before answering.
`chat_template_kwargs.enable_thinking=false` is sent unless you opt in. Backends that do not
recognise the field ignore it. Turn it on only when a node genuinely needs the reasoning —
for a rung admitting only 0–24 work, it rarely does.

## Admission

Identical to the priority producer's, deliberately: anything cheap enough for that rung is
cheap enough to try for free first.

- complexity score 0–24
- modality `text`
- eligible task type **and** operation, or an eligible task-segment purpose
- not an excluded read-only operation (`audit`, `lookup`, `read`, `review`, `status`, `verify`)

`routing_policy.free_first_ladder(...)` returns the ordered pre-ladder attempts. Both entries
sit outside the quality ladder.

## Failure semantics

A zero-result failure — unreachable backend, timeout, empty completion — is **operational**.
It falls through to the next rung and is **not** learned as a quality failure. Nothing was
produced, so there is nothing to judge.

Only a real quality failure on a produced answer moves the ladder.

## Receipt honesty

A local run carries a receipt with real token accounting. Fields the endpoint cannot supply —
thread id, rollout-derived resolved pair — stay `null` rather than being invented. The receipt
carries `executor: local_openai_compatible`, `outside_quality_ladder: true`, and a
`limitations` string stating that identity is asserted from the request, not observed from
runtime metadata.

Do not present a local receipt as equivalent evidence to a rollout-backed one.
