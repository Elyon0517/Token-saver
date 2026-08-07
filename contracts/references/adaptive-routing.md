# Adaptive Model Learning

The learner chooses a verified contextual `model|effort` boundary for matching project/task/module/file/symbol/code context. Correctness wins over token or time savings. Every ordinary route calls `recommend`, which follows the native project category, shared category, and exact-fingerprint linked project categories. CLI callers request a compact route capsule with documents, pages read, bytes read, candidates, and elapsed time; I/O bytes are not model-token savings.

## Two Routing Authorities

- Shared: saved `assets/model-capability-ladder.json` contains the last explicitly refreshed local Codex model order, supported efforts, source digest, cold starts, schedule-producer policy, and movement rules, with no user history.
- Project scoped: Obsidian stores receipt-backed contextual evidence in six stable category pages reached through the native project index → `Model Switch.md` → category links; project/task/module/file/symbol/code remain fields.

The shared registry may be atomically bootstrapped from the local cache when missing. Ordinary tasks never refresh it. Only an explicit user model-update request may rescan the local cache and replace a valid registry; this workflow never fetches over the network. If the cache is unavailable, preserve the last valid registry. If both are absent, routing fails clearly instead of inventing models.

## Start And End Flow

1. Every submission and every dynamic graph node receives a deterministic `0-100` score and band. Eligible text/code production resolves the observable entry pair, then reads the generated registry plus matching Obsidian context for that task type, step, scope, and difficulty.
2. Matching receipt-backed history selects the proven lowest-correct boundary regardless of entry. Without matching history, choose the weaker of the contextual cold-start pair and the entry anchor: a Sol/high entry may begin lower, while a Luna-max/lower entry begins no higher than itself and upgrades only after quality failure. A low-risk, low-ambiguity text/code/write/execute node scoring `0-24` still tries Spark first even when its parent task has a higher score. Spark may also serve an admitted disjoint source branch.
3. A zero-result, zero-token Spark operational failure may run the contextual quality pair in the same receipt. A quality-pair operational failure may run one stronger quality pair.
4. The result is presented immediately.
5. The eligible Ending lifecycle starts with score/band and `--producer-receipt` when present; its Ending PASS/FAIL event stores the score locally and automatically writes a receipt-backed producer outcome, switch direction, and next pair to Obsidian.
6. The next matching task moves exactly one rung, freezes a verified floor/boundary, or reuses a frozen pair.

The automatic Ending write stores sanitized entry, selected, effective, and next model/effort pairs; entry source; task type; `step_kind`; controlled capability tags and fingerprint; complexity score/band; quality verdict; tokens; timing; switch direction; recovery path; and receipt hash in the matching stable category page. Project, task, module, file, symbol, and code remain record fields; no per-context hierarchy notes are created. `recommend_model` exposes the compact entry as `model_switch_document`, its direct `model_switch_link`, and a bounded route capsule listing the native pages actually consulted. The fingerprint hashes only bounded categories and never raw prompts, raw results, credentials, or secrets. A verifier pair is never learned as the producer.

Each terminal write rebuilds the compact `Model Switch.md` entry, its matching stable category page, and the shared category backlinks. Categories are exactly `normal-script-update`, `code-design`, `finding-bugs`, `tests-verification`, `documentation-instructions`, and `general-work`; public `switch_direction` values are exactly `initial`, `upgrade`, `downgrade`, `freeze`, `no_switch`, and `operational_fallback`. The initial attempt is displayed separately from quality-ladder movement. No JSON sidecar is created.

## Movement

- First PASS at a pair: retain it and collect evidence. Second PASS at that pair: one rung down, effort before model.
- Quality/correctness FAIL: one rung up, effort before stronger model; record the failure before repair.
- Spark quality/correctness FAIL: suppress Spark for the matching step-capability fingerprint and upgrade the next matching task to its contextual quality pair.
- Operational FAIL: neutral; it does not create a quality boundary.
- Like-for-like token/time evidence remains diagnostic; route selection always keeps the lowest Real-passing rung above the strongest failed rung.
- Lowest passing pair or closed pass/fail recovery boundary: freeze with `trial=false` until a later quality failure or catalog/policy drift. When a weaker pair failed and a stronger pair passed, the next exact-capability match starts on that successful pair instead of probing an untested gap.

Obsidian evidence and same-name/display-page evidence never cross project keys. Local receipt evidence may transfer across modules or roots only when the exact step-capability fingerprint and difficulty context match. Distinctive tags such as image generation plus tool control, local testing, browser/API work, or visual verification permit cross-module reuse; generic work also requires the same module. The recommendation reports its cross-project specificity and keeps foreign Obsidian rows isolated. The receipt-backed local event ledger is the durable fast history; the compact project entry, project category, shared category, and exact-fingerprint project backlinks are its native Obsidian projection. Recommendations deduplicate stable event IDs across both authorities. `strategy_performance.py` remains the separate authority for multi-node Global-versus-Direct admission and savings claims.

An unavailable vault or unregistered project owner disables private projection but does not disable execution. The runner merges any local receipt history, then uses the weaker of the shared cold-start pair and entry anchor when history is empty, and marks `memory_available=false`; dispatcher proof records the corresponding selection basis. A structurally valid bounded task graph may execute without performance history. Dependency-ready independent nodes run concurrently; shared-state and output-dependent nodes remain linear. A read-only list of two or three independent project-relative sources retains its source-byte admission and dependency-only-or-fused merge constraints. Public savings claims still require `strategy_performance.py` evidence.
