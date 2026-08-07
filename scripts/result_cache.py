#!/usr/bin/env python3
"""Reuse a previous result when the exact same question is asked of the exact same inputs.

Why this exists
---------------
Routing decides *who* answers and the fast path decides *whether to route at all*, but
neither stops the same question being paid for twice. Within a session the same file gets
read repeatedly and the same "what does this do" gets asked again; across sessions the
work restarts from nothing. A hit here costs zero tokens.

This is the lowest-risk saving in the engine: a hit returns a result that was already
produced and accepted, so it cannot change correctness -- it can only avoid re-deriving
something. That is only true if the key is exact, which is what most of this file is about.

Key discipline
--------------
The key is a hash of:
  * the normalized prompt,
  * the SHA-256 of the *content* of every input file (never its path or mtime), and
  * the model/effort pair that produced it.

Keying on content is what makes invalidation automatic: edit a file and the key changes,
so a stale entry can never be served. Keying on the pair keeps a cheap model's answer from
being served as if a stronger model had produced it. A missing or unreadable input file is
a miss, never a hit against partial inputs.

Entries store no raw file contents -- only hashes, the result text, and accounting.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path


DEFAULT_CACHE_PATH = Path.home() / ".codex" / "model-routing-memory" / "result-cache.sqlite"
CACHE_PATH_ENV = "TOKEN_SAVER_RESULT_CACHE"
DISABLE_ENV = "TOKEN_SAVER_RESULT_CACHE_DISABLED"
DEFAULT_MAX_AGE_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    cache_key TEXT PRIMARY KEY,
    prompt_sha256 TEXT NOT NULL,
    inputs_sha256 TEXT NOT NULL,
    pair TEXT NOT NULL,
    result_text TEXT NOT NULL,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    stored_at TEXT NOT NULL,
    last_hit_at TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS savings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL,
    tokens_avoided INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


class MissingInput(Exception):
    """An input file could not be read, so no exact key can be formed."""


def cache_enabled():
    return (os.environ.get(DISABLE_ENV) or "").strip().lower() not in {"1", "true", "yes", "on"}


def resolve_cache_path(cache_path=None):
    return Path(cache_path or os.environ.get(CACHE_PATH_ENV) or DEFAULT_CACHE_PATH).expanduser().resolve()


def _connect(cache_path=None):
    path = resolve_cache_path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def _normalized_prompt(prompt):
    return re.sub(r"\s+", " ", str(prompt or "")).strip()


def file_content_hash(path):
    """Hash a file's bytes. Raises MissingInput rather than silently keying on nothing."""
    resolved = Path(path).expanduser()
    try:
        return sha256(resolved.read_bytes()).hexdigest()
    except OSError as error:
        raise MissingInput(f"cache input is unreadable: {path}") from error


def inputs_hash(file_paths):
    """Order-independent hash over input file *contents*.

    Sorting the per-file digests means the same set of files keys identically regardless
    of the order the caller listed them.
    """
    digests = sorted(file_content_hash(path) for path in (file_paths or []))
    return sha256("\0".join(digests).encode("utf-8")).hexdigest()


def cache_key(prompt, file_paths=None, pair=""):
    prompt_digest = sha256(_normalized_prompt(prompt).encode("utf-8")).hexdigest()
    input_digest = inputs_hash(file_paths)
    key = sha256("\0".join((prompt_digest, input_digest, str(pair or ""))).encode("utf-8")).hexdigest()
    return key, prompt_digest, input_digest


def _record_lookup(connection, outcome):
    """Count every lookup outcome.

    Without this, a hit rate has no honest denominator: misses that never lead to a store
    are invisible, so the number would understate or overstate depending on usage. The
    cache's keep/drop decision is made on this number, so it has to be the real one.
    """
    connection.execute(
        "INSERT INTO lookups (outcome, occurred_at) VALUES (?, ?)",
        (outcome, datetime.now(timezone.utc).isoformat()),
    )


def lookup(prompt, file_paths=None, pair="", *, cache_path=None, max_age_days=DEFAULT_MAX_AGE_DAYS):
    """Return the cached entry, or None. Any doubt resolves to a miss."""
    if not cache_enabled():
        return None
    try:
        key, _, _ = cache_key(prompt, file_paths, pair)
    except MissingInput:
        # Unreadable inputs cannot form a key, so this is not a cache miss to hold the
        # cache accountable for; it is not counted either way.
        return None
    connection = _connect(cache_path)
    try:
        row = connection.execute(
            "SELECT cache_key, result_text, total_tokens, stored_at, hit_count FROM results WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            _record_lookup(connection, "miss")
            connection.commit()
            return None
        stored_at = datetime.fromisoformat(row[3])
        age_days = (datetime.now(timezone.utc) - stored_at).total_seconds() / 86400
        if max_age_days is not None and age_days > max_age_days:
            connection.execute("DELETE FROM results WHERE cache_key = ?", (key,))
            _record_lookup(connection, "miss")
            connection.commit()
            return None
        now = datetime.now(timezone.utc).isoformat()
        _record_lookup(connection, "hit")
        connection.execute(
            "UPDATE results SET hit_count = hit_count + 1, last_hit_at = ? WHERE cache_key = ?",
            (now, key),
        )
        connection.execute(
            "INSERT INTO savings (cache_key, tokens_avoided, occurred_at) VALUES (?, ?, ?)",
            (key, row[2], now),
        )
        connection.commit()
        return {
            "cache_key": row[0],
            "result_text": row[1],
            "tokens_avoided": row[2],
            "stored_at": row[3],
            "hit_count": row[4] + 1,
        }
    finally:
        connection.close()


def store(prompt, result_text, file_paths=None, pair="", total_tokens=0, *, cache_path=None):
    """Record an accepted result. Storing an empty result is refused: a hit must be usable."""
    if not cache_enabled():
        return None
    if not str(result_text).strip():
        return None
    try:
        key, prompt_digest, input_digest = cache_key(prompt, file_paths, pair)
    except MissingInput:
        return None
    connection = _connect(cache_path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO results (cache_key, prompt_sha256, inputs_sha256, pair, result_text, total_tokens, stored_at, last_hit_at, hit_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (key, prompt_digest, input_digest, str(pair or ""), result_text, int(total_tokens or 0), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
        return key
    finally:
        connection.close()


def stats(*, cache_path=None):
    """Hit rate and tokens avoided. Below ~20% hit rate the cache is not earning its keep."""
    connection = _connect(cache_path)
    try:
        entries, stored_hits = connection.execute("SELECT COUNT(*), COALESCE(SUM(hit_count), 0) FROM results").fetchone()
        tokens_avoided = connection.execute("SELECT COALESCE(SUM(tokens_avoided), 0) FROM savings").fetchone()[0]
        hits = connection.execute("SELECT COUNT(*) FROM lookups WHERE outcome = 'hit'").fetchone()[0]
        misses = connection.execute("SELECT COUNT(*) FROM lookups WHERE outcome = 'miss'").fetchone()[0]
        lookups = hits + misses
        return {
            "entries": entries,
            "hits": hits,
            "misses": misses,
            "lookups": lookups,
            "stored_hit_count": stored_hits,
            "tokens_avoided": tokens_avoided,
            "hit_rate": round(hits / lookups, 4) if lookups else 0.0,
            "cache_path": str(resolve_cache_path(cache_path)),
        }
    finally:
        connection.close()


def purge(*, cache_path=None, max_age_days=None):
    connection = _connect(cache_path)
    try:
        if max_age_days is None:
            removed = connection.execute("DELETE FROM results").rowcount
            connection.execute("DELETE FROM savings")
            connection.execute("DELETE FROM lookups")
        else:
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
            keys = [
                row[0]
                for row in connection.execute("SELECT cache_key, stored_at FROM results")
                if datetime.fromisoformat(row[1]).timestamp() < cutoff
            ]
            removed = len(keys)
            connection.executemany("DELETE FROM results WHERE cache_key = ?", [(key,) for key in keys])
        connection.commit()
        return removed
    finally:
        connection.close()


def _emit(payload):
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exact-input result cache.")
    parser.add_argument("--cache-path", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lookup_parser = subparsers.add_parser("lookup")
    lookup_parser.add_argument("prompt")
    lookup_parser.add_argument("--file", action="append", default=[], dest="files")
    lookup_parser.add_argument("--pair", default="")
    lookup_parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)

    store_parser = subparsers.add_parser("store")
    store_parser.add_argument("prompt")
    store_parser.add_argument("--result", required=True)
    store_parser.add_argument("--file", action="append", default=[], dest="files")
    store_parser.add_argument("--pair", default="")
    store_parser.add_argument("--total-tokens", type=int, default=0)

    subparsers.add_parser("stats")

    purge_parser = subparsers.add_parser("purge")
    purge_parser.add_argument("--max-age-days", type=int, default=None)

    args = parser.parse_args(argv)

    if args.command == "stats":
        _emit({"status": "pass", **stats(cache_path=args.cache_path)})
        return 0
    if args.command == "purge":
        _emit({"status": "pass", "removed": purge(cache_path=args.cache_path, max_age_days=args.max_age_days)})
        return 0
    if args.command == "lookup":
        entry = lookup(args.prompt, args.files, args.pair, cache_path=args.cache_path, max_age_days=args.max_age_days)
        if entry is None:
            _emit({"status": "pass", "hit": False})
            return 1
        _emit({"status": "pass", "hit": True, **entry})
        return 0

    key = store(args.prompt, args.result, args.files, args.pair, args.total_tokens, cache_path=args.cache_path)
    if key is None:
        _emit({"status": "fail", "reason": "not_cacheable"})
        return 1
    _emit({"status": "pass", "cache_key": key})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
