# Plain C# rules

Use `execution_domain=csharp` for non-Unity C#. These rules apply to ordinary libraries, services, tools, and tests. Unity-specific lifecycle, serialization, main-thread, and asset rules belong in `unity-csharp-rules.md`; Unity work uses `execution_domain=unity_csharp` and reads both files.

- Preserve the repository's local style and public behavior.
- Keep fields private by default; use clear full-word names and underscore-prefixed private fields/locals when that matches the surrounding code.
- Keep one-statement `if` expressions on one line without braces; use braces for multi-statement blocks.
- Use `switch` or a switch expression for three or more outcomes instead of long `else if` chains.
- Keep calls and log calls on one line unless the project style explicitly requires wrapping.
- Avoid one-use helpers and new abstractions unless they make the requested behavior clearer or are required by the existing design.
- Preserve exception behavior, ordering, side effects, async/threading semantics, and public contracts unless the request changes them.
- Keep changes surgical. Before presentation, run the smallest safe local smoke when the changed function is light. For API, large-file, expensive-build, Unity-runtime, or side-effect-heavy work, skip the heavy producer run and check syntax plus changed method, variable, namespace, and direct-reference names. Present `CODE READY`, then create separate scored/modelled End Tasks for each independent real build, test, Unity-runtime, or integration check. All required checks must PASS; FAIL or acceptance mismatch sends the exact repair prompt to the immutable origin session, which performs the authorized repair and starts a fresh verifier, for at most three attempts.
