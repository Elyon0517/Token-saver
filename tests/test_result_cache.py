#!/usr/bin/env python3
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from _support import QuietCliMixin

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


result_cache = load_module("result_cache")


class ResultCacheTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.cache = self.root / "cache.sqlite"
        self.source = self.root / "app.py"
        self.source.write_text("def f():\n    return 1\n", encoding="utf-8")
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class KeyingTests(ResultCacheTestCase):
    def test_hit_returns_the_stored_result(self):
        result_cache.store("what does f return", "1", [self.source], "m|low", 500, cache_path=self.cache)
        entry = result_cache.lookup("what does f return", [self.source], "m|low", cache_path=self.cache)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["result_text"], "1")
        self.assertEqual(entry["tokens_avoided"], 500)

    def test_editing_an_input_file_invalidates_the_entry(self):
        # The whole safety argument rests on this: content keying means a stale entry
        # cannot be served after an edit.
        result_cache.store("what does f return", "1", [self.source], "m|low", 500, cache_path=self.cache)
        self.source.write_text("def f():\n    return 2\n", encoding="utf-8")
        self.assertIsNone(result_cache.lookup("what does f return", [self.source], "m|low", cache_path=self.cache))

    def test_a_different_pair_is_a_miss(self):
        result_cache.store("q", "cheap answer", [self.source], "weak|low", 10, cache_path=self.cache)
        self.assertIsNone(result_cache.lookup("q", [self.source], "strong|high", cache_path=self.cache))

    def test_a_different_prompt_is_a_miss(self):
        result_cache.store("q one", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertIsNone(result_cache.lookup("q two", [self.source], "m|low", cache_path=self.cache))

    def test_whitespace_differences_still_hit(self):
        result_cache.store("what   does\nf return", "1", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertIsNotNone(result_cache.lookup("what does f return", [self.source], "m|low", cache_path=self.cache))

    def test_input_order_does_not_change_the_key(self):
        other = self.root / "b.py"
        other.write_text("x = 1\n", encoding="utf-8")
        result_cache.store("q", "a", [self.source, other], "m|low", 10, cache_path=self.cache)
        self.assertIsNotNone(result_cache.lookup("q", [other, self.source], "m|low", cache_path=self.cache))

    def test_same_content_at_a_different_path_still_hits(self):
        copy = self.root / "copy.py"
        copy.write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertIsNotNone(result_cache.lookup("q", [copy], "m|low", cache_path=self.cache))

    def test_missing_input_is_a_miss_not_a_partial_hit(self):
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.source.unlink()
        self.assertIsNone(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache))

    def test_storing_with_a_missing_input_is_refused(self):
        self.assertIsNone(result_cache.store("q", "a", [self.root / "absent.py"], "m|low", 10, cache_path=self.cache))

    def test_prompt_only_entries_work(self):
        result_cache.store("2+2?", "4", [], "m|low", 5, cache_path=self.cache)
        self.assertEqual(result_cache.lookup("2+2?", [], "m|low", cache_path=self.cache)["result_text"], "4")


class PolicyTests(ResultCacheTestCase):
    def test_empty_results_are_not_cached(self):
        self.assertIsNone(result_cache.store("q", "   ", [self.source], "m|low", 10, cache_path=self.cache))
        self.assertIsNone(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache))

    def test_expired_entries_are_dropped(self):
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertIsNone(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache, max_age_days=0))
        # The expired row is removed, so it stays a miss even with a generous window.
        self.assertIsNone(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache, max_age_days=365))

    def test_disable_switch_bypasses_both_reads_and_writes(self):
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        with patch.dict(os.environ, {result_cache.DISABLE_ENV: "1"}):
            self.assertIsNone(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache))
            self.assertIsNone(result_cache.store("q2", "b", [self.source], "m|low", 10, cache_path=self.cache))
        self.assertIsNone(result_cache.lookup("q2", [self.source], "m|low", cache_path=self.cache))

    def test_restore_overwrites_rather_than_duplicating(self):
        result_cache.store("q", "first", [self.source], "m|low", 10, cache_path=self.cache)
        result_cache.store("q", "second", [self.source], "m|low", 20, cache_path=self.cache)
        self.assertEqual(result_cache.stats(cache_path=self.cache)["entries"], 1)
        self.assertEqual(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache)["result_text"], "second")


class AccountingTests(ResultCacheTestCase):
    def test_stats_track_hits_and_tokens_avoided(self):
        result_cache.store("q", "a", [self.source], "m|low", 700, cache_path=self.cache)
        result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache)
        result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache)
        report = result_cache.stats(cache_path=self.cache)
        self.assertEqual(report["entries"], 1)
        self.assertEqual(report["hits"], 2)
        self.assertEqual(report["tokens_avoided"], 1400)
        self.assertGreater(report["hit_rate"], 0)

    def test_misses_do_not_count_as_savings(self):
        result_cache.lookup("never stored", [], "m|low", cache_path=self.cache)
        self.assertEqual(result_cache.stats(cache_path=self.cache)["tokens_avoided"], 0)

    def test_hit_count_increments_on_the_entry(self):
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertEqual(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache)["hit_count"], 1)
        self.assertEqual(result_cache.lookup("q", [self.source], "m|low", cache_path=self.cache)["hit_count"], 2)

    def test_purge_clears_everything(self):
        result_cache.store("q", "a", [self.source], "m|low", 10, cache_path=self.cache)
        self.assertEqual(result_cache.purge(cache_path=self.cache), 1)
        self.assertEqual(result_cache.stats(cache_path=self.cache)["entries"], 0)


class CliTests(QuietCliMixin, ResultCacheTestCase):
    def test_miss_exits_nonzero_and_hit_exits_zero(self):
        args = ["--cache-path", str(self.cache)]
        self.assertEqual(result_cache.main(args + ["lookup", "q", "--pair", "m|low"]), 1)
        self.assertEqual(result_cache.main(args + ["store", "q", "--result", "a", "--pair", "m|low"]), 0)
        self.assertEqual(result_cache.main(args + ["lookup", "q", "--pair", "m|low"]), 0)

    def test_stats_and_purge_commands(self):
        args = ["--cache-path", str(self.cache)]
        self.assertEqual(result_cache.main(args + ["stats"]), 0)
        self.assertEqual(result_cache.main(args + ["purge"]), 0)


if __name__ == "__main__":
    unittest.main()
