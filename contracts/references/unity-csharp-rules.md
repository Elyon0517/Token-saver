# Unity C# rules

Use `execution_domain=unity_csharp` in routing evidence for Unity C# work. Unity uses this file plus [`csharp-rules.md`](csharp-rules.md); the general rules apply unless a Unity-specific rule below is tighter. This domain shares `code-skill` with plain C# and Python but is not interchangeable with their evidence keys.

Apply these rules for Unity projects and C# code, including MonoBehaviours, ScriptableObjects, managers, gameplay systems, runtime scripts, reviews, explanations, refactors, and performance work.

## Workflow

1. Read repo-level `AGENTS.md`, local style notes, or direct user instructions, and let tighter local rules override this shared style.
2. Preserve core behavior unless the user explicitly asks for a functional change.
3. For optimize, simplify, or cleanup requests, keep edits inside the requested code path unless the user explicitly asks to broaden scope.
4. When independent repeated work is present or discovered during optimization, also read `references/parallelization.md`; parallelize only pure data work that preserves Unity main-thread rules and the same observable result.

## Output

- Return the final updated C# code first when the user asks for code.
- Then briefly explain what changed and why in 1-3 short sentences unless the user asks for code only.

## Unity Structure

- Use `Awake()` for data and component initialization owned by the script, such as `Rigidbody`, `Collider`, `Animator`, and internal fields.
- Use `Start()` for work that depends on other scripts, ScriptableObjects, services, or external references.
- Do not allocate new objects every frame in `Update()` if they can be reused.
- Store reusable objects in fields and initialize them once, usually in `Awake()`.

## C# Style

- For a single statement, keep the entire `if` on one line without braces.
- Use braces only when the block contains more than one statement.
- Use plain `if`/`else` for exactly two mutually exclusive outcomes.
- Use `switch`/`case`, a switch expression, or guarded switch cases for three or more outcomes.
- Do not write or keep `if`/`else if` chains for three or more outcomes when C# can express the same behavior with `switch`.
- Inline a value when it is used exactly once and remains readable.
- Do not create helper functions that are only used once, except `Update()` may call one helper used only there when it makes the per-frame flow clearer.
- Do not create a new C# script only to simplify a very small or easy structure.
- Keep function calls and log calls on one line.
- Keep spaces after commas.
- Do not fold argument lists across multiple lines unless explicitly requested.

## Fields And Names

- Default fields to `private`.
- Use underscore names for internal fields and internal locals, such as `_name`, `_currentY`, `_index`, `_offset`, `_damageValue`, `_randomSeed`, and `_enemyHealth`.
- Use clear full-word names instead of abbreviations like `dmg`, `rs`, or `hp`.
- Do not declare public fields by default.
- If something must be exposed, prefer a property with `get; set;`.
- If a field or value is initialized in `Awake()` or `Start()`, use it directly instead of adding repeated guards like `!= null` or `> 0`.

## Guardrails

- Do not add abstractions, wrappers, lifecycle changes, data-flow changes, or defensive checks for impossible states unless requested.
- Do not change multiple authoring, manager, or system scripts when the user asked only for local optimization; report the broader issue or ask before expanding scope.
- Do not let broader generic C# formatting advice override this style.
