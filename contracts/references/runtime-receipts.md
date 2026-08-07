# Runtime Model Receipts

A diagram label or CLI request proves intent only. A useful local receipt combines the request, Codex runtime metadata, token events, and measured elapsed time without storing prompts or secrets. Deterministic model disclosure text comes from `scripts/model_identity_disclosure.py`; do not handcraft disclosure text to bypass the parser.

## Required Fields

- node/workload ID;
- SHA-256 of the prompt, never the raw prompt;
- requested model and effort;
- resolved model and effort from `turn_context`;
- effective model after any `model_reroute` event;
- reroute reason when present;
- thread ID and Codex CLI version;
- input, cached-input, output, reasoning-output, and total tokens;
- model-turn duration, time to first token, and whole-process elapsed time when available;
- sanitized availability metadata such as limit class and whether usable credits were reported, without balances or account identifiers;
- process exit code, match status, and sanitized errors.
- timeout completeness fields: `metrics_complete=false`, measured `process_elapsed_ms`, and `tokens_lower_bound=true` only when partial rollout usage exists.
- sanitized node role, entry-context status, and authorization source; never the environment or marker value.
- observable entry model, effort, pair, anchor pair, and entry source for adaptive producers and dispatcher nodes;
- selected pair, selection reason/state, switch direction, and recovery path when routing moves;
- for an adaptive producer only, one sanitized `model_learning_context` with project root, task/module/file/symbol/code/operation, modality, complexity, risk, ambiguity, and bounded task summary; no raw prompt or result.

Do not save raw stdout, stderr, prompts, response items, base instructions, environment, auth data, rate limits, credits, or rollout summaries.

## Entry-Context Authorization

`model_execution_receipt.py --entry-task` gives the spawned Codex controller an inherited entry-context marker. While that marker is present, an ordinary result producer needs `obsidian_adaptive_model_runner.py` in-process authorization after the current Obsidian project-context recommendation selects the exact shared-ladder pair; a multi-node dispatcher uses its distinct adaptive-result authorization after the locked recommendation matches its pair, trial flag, fingerprint, and proof. The old `adaptive_model_runner.py` authorization is legacy compatibility only. A plain fixed-pair or forged dispatcher receipt call is rejected before `codex exec`. Dispatcher verification, repair, and ending nodes have separate matching in-process authorization. Role labels alone are insufficient. Fixed-pair benchmark baselines work outside entry context and cannot be used as an entry fallback.

When an orchestrating parent needs the routed node's user-facing result, `model_execution_receipt.py run --result-output <task-cache-path>` may save only the final agent message in the active task cache. The sanitized receipt stores its hash and path, never raw stdout/stderr. A `LOCKED_ROUTE_NODE` owns only that result and its Quick Check: it must not create, launch, wait for, or summarize End/Fix tasks. The entry parent owns lifecycle after the passing receipt exists, preventing recursive routing and ensuring Ending can bind the actual producer pair.

After that result is shown, start Ending with `ending_task_ledger.py start --producer-receipt <receipt-path>`. The ledger validates and privately binds the learning context. A producer-bound `event pass` writes the matched Obsidian Model Switch record before terminal PASS; a producer-bound `event fail --failure-class <class>` writes lifecycle FAIL first and then the failed model outcome. Lifecycles without a producer receipt keep the ordinary non-learning behavior. Duplicate receipt/verdict records are idempotent success.

## Lifecycle Model Disclosure

Every lifecycle start stores a local audit-only `model_disclosure` record. It contains the assigned and current pairs, one `model_evidence` level (`runtime_receipt`, `verified_entry`, `task_assignment`, `configured_selection`, or `unavailable`), requested/resolved/effective pairs, `effective_evidence_level`, previous pair, route change, switch summary, and bounded reason. This record does not select a future model.

`--selected-pair` remains visible as the assigned pair without a runtime receipt. Its requested, resolved, and effective values stay concrete when known, with `effective_evidence_level` set to `UNVERIFIED (no runtime receipt)`. This is the known assigned/configured/verified-entry pair, never `unverified | unverified`. A no-switch assignment records `previous_pair=\"same as current\"`, `route_change=no_switch`, and `switch_summary=\"No model switch\"`.

When a validated producer receipt is bound, its executed pair is the receipt-backed current/effective identity. The ledger retains the resolved pair for audit history. If those pairs conflict, it records `runtime_receipt` evidence, `operational_fallback`, and a conflict reason instead of discarding either value. With no assignment or receipt, the lifecycle records `unknown|unknown` (displayed as `unknown | unknown`) for every pair, `model_evidence=unavailable`, `effective_evidence_level=unavailable`, and `previous_pair=none` only when the resolver explicitly reports unavailable identity.

The ledger stores only bounded model labels and sanitized reasons. It never stores raw prompts, results, stdout, stderr, secrets, or receipt payloads.

On timeout, preserve the prompt/workload hashes, measured elapsed time, partial thread identity, resolved/effective pair, last allowlisted token count, and availability when recoverable. Partial tokens are a lower bound, never a completed-run total. Do not replace recoverable evidence with an empty generic receipt.

## Local Evidence Level

`codex exec --json` provides thread and token events, but not resolved model/effort by itself. A persistent run can be joined to `~/.codex/state_5.sqlite`, and its rollout can be allowlist-parsed for `turn_context`, `model_reroute`, `token_count`, and `task_complete` fields.

This is strong operational evidence from the local Codex runtime. It is not a cryptographically signed backend attestation. State that limitation exactly; do not overclaim.

## Savings Comparison

Use exactly two performance worlds. Direct runs the exact raw task fixed on `gpt-5.6-sol|ultra` with no Skill and no detached verifier. Auto binds the same raw workload by prompt path plus SHA-256 but exposes only that hash to the frozen `AUTO_BENCHMARK_ENTRY` controller on `gpt-5.6-luna|max`; the bridge reads the bound workload and selects the lowest history-proven pair for each task step, upgrading when quality requires it. Hold raw workload, inputs, cwd, sandbox, frozen catalogs/memory, user/project configuration, output contract, acceptance criteria, and execution order constant; the intentional entry pair and audited wrapper policy are the only arm differences. After the Luna Auto result, run one strict Sol-ultra entry probe per tier outside both performance worlds. The probe must return the exact expected result and the same stable route signature/capability assignment; its time and tokens are diagnostics only.

Run the Direct arm through `model_execution_receipt.py run --direct-task --benchmark-run-id benchmark-<run-id>` at the frozen Sol-ultra pair and pass the exact raw task prompt over stdin. Run the Auto arm through `--bootstrap-task --benchmark-run-id benchmark-<run-id>` at the frozen Luna-max entry with the same raw prompt; the receipt worker replaces the body with the deterministic hash-only `benchmark_prompt_contract.py` controller prompt, binds its current Python runtime in `CODEX_AUTO_BENCHMARK_PYTHON`, and forbids the controller from discovering or substituting another interpreter. `benchmark_auto_entry_bridge.py` verifies that interpreter binding and the bound workload file, copies the frozen source outside that source tree, and atomically claims exactly one adaptive launch/workspace. The Auto controller launches that bridge process once with the longest supported initial wait; if the process yields a running session, it polls that same session until exit, and polling is never a retry or permission to launch a second process. The isolated `CODEX_HOME/AGENTS.md` authorizes the marker to return the adaptive child JSON without running Ending inline. If that controller completes with an explicitly empty final, the receipt worker may publish the bridge output only when the one-workspace launch claim is valid, the bridge output contains one strict JSON object after its optional model disclosure, and its passing child receipt reports the same JSON-payload hash with no duplicate result. It records `benchmark_result_source=adaptive_bridge_handoff`; any non-empty wrong controller result remains authoritative and fails instead of being replaced. Both modes intentionally add no `LOCKED_ROUTE_NODE`, run outside Task Analyze entry context, and use `result-producer`; `direct-task` versus `bootstrap-task`, the exact arm entry pair, and the frozen AGENTS hash prove the arm. Reserve `--entry-task` for a real full Task Analyze entry. Keep the same user/project configuration; `--ignore-user-config` changes more than routing and is not a fair strategy baseline. Primary steady tokens/time use the complete fixed Direct task census versus the clean `trial=false` Auto selected-execution receipt. Auto entry/controller and route-selection overhead, prior calibration failures, retries, fallbacks, repairs, the Sol entry probe, and Ending are separately counted diagnostics and never hidden. Actual `first_result_elapsed_ms` remains an end-to-end wait diagnostic; do not call it saved unless that metric itself is lower. The receipt runner flushes one sanitized `result-ready` event immediately after the atomic result write; the benchmark controller stamps that event in its own monotonic clock, stores the child-local monotonic value only as a diagnostic, and binds the runner-owned timestamp exactly across evidence and receipt.

Every formal cohort freezes the complete model-visible environment before the plan is written. Each arm uses real suite-local `skills/` and `plugins/` directories, suite-local marketplace roots, copied model/memory snapshots, byte-identical `config.toml`, and immutable catalog files; only the Direct empty AGENTS versus Auto production bootstrap and their contractually fixed entry pairs may differ. Never symlink a benchmark catalog to live `~/.codex` or another mutable cache. Prewarm plugin discovery without model traffic, compare normalized discovery plus catalog hashes, and validate the environment immediately before every arm. Any other skills/plugin/marketplace/config/memory drift invalidates the cohort as an operational failure and cannot be resumed or learned as model quality.

The benchmark gate must reject the wrong node type, active entry context, wrong authorization source, mismatched `benchmark_run_id`, mismatched workload ID, an unverified one-workspace launch claim, an invalid result source or bridge handoff, or an execution-prompt hash that differs from the reconstructed arm contract. Direct execution hash equals the raw workload hash; Auto execution hash equals the frozen controller wrapper containing that workload's hash. A mode label without those bindings is not proof.

Compare `workload_prompt_sha256` across arms to prove the bounded raw task is identical, and independently verify each `prompt_sha256` against its deterministic arm execution contract. Never compare Auto's wrapper hash to Direct's raw hash as if they represented different workloads.

Primary optimization compares aggregate `steady_state_logical_tokens` and `steady_state_execution_elapsed_ms` only after every task result and Ending is correct, each Auto selected route is stable and `trial=false`, and Luna/Sol entry invariance passes. Both aggregate steady metrics must be lower for the optimization verdict. Per-tier totals, medians, paired wins/regressions, actual first-result wait, route-selection overhead, calibration cost, controller/probe cost, and total wall time remain visible diagnostics. Continue optimization only while a reproducible deterministic waste source remains; stop when repeated evidence is noise-bound or no correctness-preserving change remains. The benchmark receipt worker emits one flushed result-ready event immediately after the atomic result write; the parent runner stamps that event on its own monotonic clock, so file polling, receipt finalization, telemetry, and Ending do not alter that end-to-end diagnostic. `benchmark_suite_gate.py` derives acceptance from raw result/evidence/receipts; manifests may not supply their own pass status. Before publication, `benchmark_public_export.py` re-evaluates every run from the raw files and exact-matches the regenerated manifests and summary; missing, stale, or tampered raw evidence fails closed.

- Cached input is part of input tokens; do not add it again.
- Reasoning output is part of output tokens; do not add it again.
- For sequential work, sum elapsed time.
- For parallel work, compare scheduler critical-path elapsed time, not the sum of branch durations.
- One pair is a smoke result. Prefer alternating order and median of at least three runs for a durable claim.
