# Parallelization Rules

Apply these rules when code handles repeated independent work, batch file or asset processing, per-item transforms, network/API fan-out, expensive loops, test batches, generation pipelines, or a performance request in Python or C#.

## Decision

- Look for independent units: the same operation runs over many items, one item's output does not depend on another item's output, and work does not rely on shared mutable state.
- Parallelize immediately when all are true: each item can run independently, final output can be reconstructed in the original deterministic order, shared writes/logs/randomness/timing do not define behavior, the runtime has a safe parallel primitive, and a narrow real test can compare sequential and parallel results.
- Do not parallelize when results depend on execution order, shared mutable state, non-thread-safe services, transaction order, rate limits, file write collisions, global random state, floating-point reduction order where exact equality matters, Unity main-thread-only APIs, or when parallel overhead would likely make the path slower.
- If the opportunity is real but broad or risky, report it clearly and ask before widening the change.

## Implementation

- Preserve the sequential contract: input order, output values, exception behavior, cancellation, cleanup, and side effects must remain equivalent.
- Preserve deterministic ordering by using ordered APIs or carrying item indexes through the parallel work and sorting once before returning or writing output.
- Keep writes isolated: one output path per item, a single ordered writer after computation, or an existing thread-safe sink. Do not let workers append to the same file or mutate the same collection without a proven safe primitive.
- Keep error behavior equivalent. If the parallel API aggregates exceptions or changes failure order, adapt the wrapper to match the old observable contract where practical, then test it.
- For reductions, use a deterministic combine step. Parallelize only the independent map phase when exact reduction order affects the result.

## Python

- Use `concurrent.futures.ThreadPoolExecutor` for I/O-bound work such as files, subprocess calls, HTTP requests, image reads/writes, or API fan-out.
- Use `concurrent.futures.ProcessPoolExecutor` or `multiprocessing` only for CPU-bound pure functions whose inputs and outputs are safely picklable and whose platform/start-method constraints are acceptable.
- Use `asyncio` only when the surrounding code is already async or the user asks for an async interface.
- Prefer ordered result collection such as `executor.map(...)` or `(index, value)` pairs over `as_completed(...)` when output order matters.
- Cap worker counts deliberately instead of using unbounded fan-out, especially for file handles, APIs, subprocesses, or memory-heavy work.

## C# / Unity C#

- Keep `UnityEngine.Object`, scene, asset database, transform, renderer, physics, and most editor API access on the Unity main thread.
- Parallelize only pure data work or immutable snapshots taken from the main thread, then apply results back on the main thread in deterministic order.
- Prefer `Task.WhenAll`, `Parallel.ForEach`, or the Unity Job System/Burst only when they match the project's existing style and the data is safe for that primitive.
- Avoid background work that touches MonoBehaviour lifecycle state, serialized fields, GameObjects, ScriptableObjects, or editor objects directly.
- For gameplay logic, preserve frame timing semantics. Do not move behavior across `Awake()`, `Start()`, `Update()`, fixed-step physics, or coroutine boundaries unless the user explicitly asks for that behavioral change.

## Verification

- Run a real comparison whenever practical: sequential baseline input versus parallel output for empty, single-item, and multi-item cases.
- Verify output values, ordering, side effects, and error behavior, not just runtime speed or compilation.
- If no local baseline can be run, say the parallelization is structurally safe but the same-result guarantee remains unverified.
