# Cached Model Capabilities

This snapshot and the shared JSON registry come from the local Codex catalog. They change only when the user explicitly runs the manual update command; ordinary routing reads the saved registry without scanning the catalog.

- Source: `~/.codex/models_cache.json`
- Codex client version: `0.147.0`
- Local catalog snapshot: `2026-08-05T23:02:42.823500Z`
- Semantic catalog SHA-256: `89a08bd6f50209ae92bf9ef10304318a082a960cfd345798c77997ddd3dde11b`
- Registry schema: `2`
- Active quality family: `gpt-5.6` (highest numeric GPT family)

## Quality ladder

Only the highest registered numeric GPT family is active. Within that family, models are weakest to strongest using the provider's current priority order.

| Rank | Display name | Model ID | Role | Inputs | Context | API | Default effort | Supported efforts | Speed tiers |
|---:|---|---|---|---|---:|---|---|---|---|
| 1 | GPT-5.6-Luna | `gpt-5.6-luna` | weak | text, image | 272,000 | yes | `medium` | low, medium, high, xhigh, max | fast |
| 2 | GPT-5.6-Terra | `gpt-5.6-terra` | balanced | text, image | 272,000 | yes | `medium` | low, medium, high, xhigh, max, ultra | fast |
| 3 | GPT-5.6-Sol | `gpt-5.6-sol` | frontier | text, image | 272,000 | yes | `low` | low, medium, high, xhigh, max, ultra | fast |

## Catalog-visible models

Only the highest numeric GPT family and the priority producer are retained in this routing snapshot. Older visible models may be present in the machine cache, but they are excluded from the registry, semantic digest, and routing candidates so background catalog refreshes cannot reintroduce them.

| Display name | Model ID | Catalog role | Provider priority | Supported efforts |
|---|---|---|---:|---|
| GPT-5.6-Sol | `gpt-5.6-sol` | active_quality | 1 | low, medium, high, xhigh, max, ultra |
| GPT-5.6-Terra | `gpt-5.6-terra` | active_quality | 2 | low, medium, high, xhigh, max, ultra |
| GPT-5.6-Luna | `gpt-5.6-luna` | active_quality | 3 | low, medium, high, xhigh, max |
| GPT-5.3-Codex-Spark | `gpt-5.3-codex-spark` | priority_producer | 26 | low, medium, high, xhigh |

## Priority text/code producer

- Model: `gpt-5.3-codex-spark` (GPT-5.3-Codex-Spark)
- Positioning: Ultra-fast coding model.
- Inputs: text; API: no
- Easy / complex effort: `low` / `high`
- This producer is attempted before eligible low-risk text/code/write/execute task segments scoring 0-24, regardless of parent score, and is not part of the weakest-to-strongest quality ladder.

## Private learning contract

- Authority: `dual_local_and_obsidian`
- Local path: `~/.codex/model-routing-memory/events.jsonl`
- Obsidian entry: `Model Switch.md`
- Obsidian records: `<owner-routing>/<Category>.md`
- Native wikilink graph: `true`; stable categories: normal-script-update, code-design, finding-bugs, tests-verification, documentation-instructions, general-work.
- Stable event dedupe: `true`
- Specificity: project_task / module / file / symbol
- Context stays in fields: `true`; per-context hierarchy notes: `false`; legacy local JSON: `read_only_inactive`.

## Dynamic defaults

- Floor: `gpt-5.6-luna|low`
- Balanced cold start: `gpt-5.6-terra|medium`
- Balanced complex: `gpt-5.6-terra|high`
- Frontier complex: `gpt-5.6-sol|high`

| Task type | Easy | Complex |
|---|---|---|
| question | `gpt-5.6-luna|low` | `gpt-5.6-terra|medium` |
| summary | `gpt-5.6-luna|low` | `gpt-5.6-terra|medium` |
| spreadsheet | `gpt-5.6-terra|medium` | `gpt-5.6-terra|high` |
| document | `gpt-5.6-luna|medium` | `gpt-5.6-terra|high` |
| code | `gpt-5.6-terra|medium` | `gpt-5.6-terra|high` |
| debug | `gpt-5.6-terra|medium` | `gpt-5.6-sol|high` |
| integration | `gpt-5.6-terra|high` | `gpt-5.6-sol|high` |
| prompt | `gpt-5.6-terra|medium` | `gpt-5.6-sol|high` |
| visual | `gpt-5.6-terra|medium` | `gpt-5.6-sol|high` |
| script | `gpt-5.6-terra|medium` | `gpt-5.6-terra|high` |
| normal-script-update | `gpt-5.6-terra|medium` | `gpt-5.6-terra|high` |
| code-design | `gpt-5.6-terra|medium` | `gpt-5.6-terra|high` |
| finding-bugs | `gpt-5.6-terra|medium` | `gpt-5.6-sol|high` |
| documentation-instructions | `gpt-5.6-luna|medium` | `gpt-5.6-terra|high` |

## Effort compatibility

- `gpt-5.6-sol` (active_quality): low, medium, high, xhigh, max, ultra.
- `gpt-5.6-terra` (active_quality): low, medium, high, xhigh, max, ultra.
- `gpt-5.6-luna` (active_quality): low, medium, high, xhigh, max.
- `gpt-5.3-codex-spark` (priority_producer): low, medium, high, xhigh.
- Unsupported efforts are normalized within the selected model's advertised effort list.

## Manual update

```bash
python3 scripts/sync_model_capabilities.py --update
python3 scripts/sync_model_capabilities.py --check
```
