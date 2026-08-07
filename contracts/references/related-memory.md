# Optional Related Memory

## Purpose

Use a quick memory lookup to improve one task with related past requirements, verified failures, retry lessons, project facts, and model-switch experience. Memory is advisory context, never a reason to delay or replace source inspection.

## Task-Start Search

Run `scripts/obsidian_memory_bridge.py search --query <generalized task summary>` during bounded read-only Task Analyze preflight when a vault is available. The bridge searches stable Skill, Project, Knowledge, and Knowledge/Preferences pages, returns only a bounded digest, and never returns raw transcripts or secrets.

- Use only matches that materially affect the current task.
- Prefer exact project, skill, failure, requirement, and verification nouns.
- Pass the concise digest only to nodes that need it.
- Recheck live source when memory may be stale.
- If the bridge returns `unavailable` or `no_matches`, continue normally without warning, setup work, or a blocked route.

Exact model/effort learning is separate from this broad advisory search. `project-memory-skill/scripts/obsidian_model_memory.py` merges the receipt-backed local event ledger with only the current project category, shared category, and exact-fingerprint project pages reached through native Obsidian wikilinks; it deduplicates their stable event IDs. The shared ladder supplies cold starts only when both histories are empty.

## Obsidian Vault Routing

Use the configured `CODEX_OBSIDIAN_VAULT`, or the default `MyAILLM` vault when available, only when the task depends on connected-project history, a repeated failure, a durable user rule, visual/UI verification, project routing, global skills, or a past-work lesson.

- Read the authoritative vault `AGENTS.md` and the relevant stable Knowledge/Skills page only as needed.
- Before a structured folder, read its `instruction.md`. Main active Wiki folders are `Projects/`, `Skills/`, `Knowledge/`, and `Knowledge/Preferences/`.
- Global-skill or workflow failures use `Skills/instruction.md`, `Skills/index.md`, `Skills/Failure Learning.md`, and `Skills/Update Protocol.md`.
- Before a project skill, read `Skills/Skill Ownership Map.md` and `Skills/ProjectSkills/index.md`; keep project skills within their owner unless the user asks to port them.
- Visual, UI, artwork, image, Unity preview, shader/VFX/animation, PDF/report-style, or visual-handoff work uses `Knowledge/Preferences/instruction.md` and `Knowledge/Preferences/index.md`.
- Project work uses `Projects/instruction.md` plus the matching project `instruction.md` and `index.md`. Known pages include MuseAI, ThisIsMyOregon, AIAnimation2D, AIShaderGraphic2D, AIVFX2D, Destiny, Mokozoo, TaggingAPILandingPage, AgentImageEditor, and UnityCodexTest.
- Confirm the live project root before edits; avoid stale backups and temporary worktrees unless selected by the user.
- Done means verified. UI, visual, Unity, shader, generation, browser, backup/recovery, automation, and deployment work needs concrete evidence such as command output, screenshot, generated path, preview, test, or diff.

For Mac Notes sync, read `Knowledge/Mac Notes Sync.md` and `raw/MacNotes/sync-state.md`. The default direction is Mac Notes to Obsidian and only the Apple Notes `ThisIsMyOregon` tree is retained under `Projects/ThisIsMyOregon/MacNotes/`; do not import general notes or create Apple Notes mirror folders unless asked.

When `Knowledge/` exists, it is the sole active knowledge root and stale `KnowledgeAreas/` or `AestheticTaste/` folders are ignored. Legacy reads are allowed only when `Knowledge/` is absent and at least one legacy root exists. Never dual-write, recreate, or treat a legacy root as a second authority after migration.

## Ending Update

Every user submission first receives a bounded personal-memory relevance scan. After the completed result is shown, Ending Task may persist only candidates directly supported by that submission: explicit user preferences, repeated user corrections, or verified technical working patterns. If no candidate exists, do not create a memory file or event. A supported candidate uses `project-memory-skill/scripts/personal_memory.py` through `ending_task_ledger.py event --memory-candidates-file`; it is separate from model-switch and project-change memory and never changes the requested result. Write model-switch experience only after Real Verify has produced a durable `pass` or `fail` verdict, using `project-memory-skill/scripts/obsidian_model_memory.py record` with the same project/task/module/file/symbol/code context and matched producer receipt. New records never accept or write a Mini status; central legacy entries remain read-only and are not migrated.

Never store raw prompts, results, absolute paths, thread/session IDs, receipt bodies, credentials, secrets, or unrelated task history. Missing Obsidian leaves a local record with pending projection and never blocks routing.
