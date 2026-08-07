# Result Cache

Same question, same inputs, same pair → return the stored answer. Zero tokens.

## Why it is safe

A hit returns a result that was already produced and accepted. It cannot change
correctness; it can only avoid re-deriving something. That guarantee holds **only** because
the key is exact.

## The key

SHA-256 over three parts:

1. the normalized prompt (whitespace collapsed),
2. the SHA-256 of the **content** of every input file, sorted, and
3. the `model|effort` pair that produced the result.

Consequences, each of them load-bearing:

| Rule | Why |
|---|---|
| Content, never path or mtime | Editing a file changes the key, so a stale entry can never be served. Invalidation is automatic — there is no invalidation logic to get wrong. |
| Sorted digests | The same file set keys identically regardless of the order the caller listed them. |
| Pair included | A weak model's answer is never served as if a stronger model produced it. |
| Unreadable input → miss | Never a partial hit against inputs that could not be read. |

Entries store hashes, the result text, and accounting. Raw file contents are never stored.

## Use

```bash
python3 scripts/result_cache.py lookup "<prompt>" --file a.py --pair "model|effort"   # exit 0 = hit
python3 scripts/result_cache.py store  "<prompt>" --result "<text>" --file a.py --pair "model|effort" --total-tokens 700
python3 scripts/result_cache.py stats
python3 scripts/result_cache.py purge [--max-age-days N]
```

Entries older than 30 days are dropped on lookup. Empty results are never stored — a hit must
be usable.

Disable entirely with `TOKEN_SAVER_RESULT_CACHE_DISABLED=1`. Relocate with
`TOKEN_SAVER_RESULT_CACHE=<path>`.

## Judging it

`stats` reports `hit_rate` and `tokens_avoided`. Below roughly a 20% hit rate the cache is not
earning its complexity — measure before keeping it.

`tokens_avoided` counts the tokens the stored entry originally cost, credited once per hit. It
is an estimate of avoided work, not a billing figure, and it belongs in diagnostics rather
than in a headline savings claim.
