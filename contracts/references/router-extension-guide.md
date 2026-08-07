# Router extension guide

`skills/task-analyze-skill/scripts/routing_policy.py::EXECUTION_DOMAINS` is the single authoritative registry for active execution domains. Domain IDs are immutable evidence keys: do not rename an existing ID, and use `code_unspecified` only for migration and historical evidence.

## Add one active domain

For a new registry-owned code domain, make one additive registry row with all nine required metadata fields: `display_name`, `kind`, `language_aliases`, `owner_skill`, `owner_enforced`, `spark_first`, `reference_path`, `active`, and `history_only`. Then add the matching reference page and generic routing/validator coverage that reads the registry. Do not edit every validator, scenario, or skill description just to enumerate the new domain. A new additive domain value does not require a schema-version bump.

The normal seam is:

1. Add one `EXECUTION_DOMAINS` row with an immutable ID and reference path.
2. Add the domain-specific executor reference and, when applicable, a language/style reference under `code-skill/references/`.
3. Add one representative routing scenario and generic registry-driven tests for valid, unknown, and migration-only domains.
4. Update concise user documentation only where the active domain list or extension seam is explained.

The stable `tiny-code`, `code-easy`, and `code-complex` profile presets are domain-parameterized. A new active code-domain registry row becomes usable through those presets automatically; do not duplicate profile rows per language. Python, C#, and Unity C# are current examples of the same extension seam.

Discover the current non-mutating registry view with:

```bash
python3 skills/task-analyze-skill/scripts/model_routing_history.py domains
```

Keep language rules in executor references, not in registry metadata. A domain may share an executor with another domain while retaining separate evidence and rules. Current code examples are `python`, `csharp`, and `unity_csharp`; `general` is the non-code default.

## Evidence and migration

`execution_domain` is part of adaptive-profile identity. New records use the exact registry ID. When reading legacy records with no domain, infer `code_unspecified` for legacy code evidence and `general` otherwise; never reinterpret old evidence as a newly named active domain. `code_unspecified` is not an extension target.

The current model|effort executes ordinary inline work and is not a model-quality learning feature. Only after full Task Analyze is explicitly activated does that pair also become route-coordination metadata. Inline tool/model work presents its completed result with no foreground verifier, child receipt, or adaptive producer sample. Admitted delegated routes carry complete `routing_recommendation` and end-to-end performance-admission proof, then record the producer receipt and Ending Real outcome; deterministic controller recording needs no decorative Luna call.

Execution domain describes the requested execution work, not every language found in inspected sources. A read-only repository answer can inspect Python, C#, or Unity C# while remaining `execution_domain=general`; use a code domain only for code creation, changes, execution, or code-path validation.

## Canonical policy

Correctness is the gate. Ordinary work defaults to inline current-model execution regardless of apparent complexity. Two or three explicit independent read-only sources first cost-admit one producer; only context pressure or an explicit latency contract may run disjoint allowlists plus a dependency-results-only or exact-owned fused-final merge. Dependency-coupled work remains one producer. Consider any other frozen calibrated child or graph only after the complete Global path has positive current admission for the same entry/configuration/workload cohort. Tokens and elapsed time are receipt evidence for like-for-like optimization and never override quality, safety, or authority boundaries.
