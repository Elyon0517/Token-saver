#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "resolve_entry_model.py"
MODULE_SPEC = importlib.util.spec_from_file_location("resolve_entry_model", SCRIPT_PATH)
module = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(module)


class ResolveEntryModelTests(unittest.TestCase):
    def test_invalid_thread_id_returns_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions_dir = Path(temporary) / "sessions" / "2026" / "07" / "09"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            valid = sessions_dir / "session-12345678-1234-1234-1234-1234567890ab.jsonl"
            valid.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-abcd"}}), encoding="utf-8")
            invalid_file = sessions_dir / "session.jsonl"
            invalid_file.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-abcd"}}), encoding="utf-8")
            self.assertEqual(
                module.resolve_entry_model("thread-not-a-uuid", temporary),
                {"status": "unverified"},
            )
            self.assertEqual(
                module.resolve_entry_model("12345678-1234-1234-1234-1234567890ab", temporary),
                {"status": "unverified"},
            )

    def test_missing_sessions_root_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing_root = Path(temporary) / "missing"
            self.assertEqual(
                module.resolve_entry_model("12345678-1234-1234-1234-1234567890ab", missing_root),
                {"status": "unavailable"},
            )

    def test_near_id_match_does_not_leak_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            thread_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            archived = Path(temporary) / "archived_sessions" / f"session-{thread_id}.jsonl"
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-ab"}})
                + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "ultra"}}),
                encoding="utf-8",
            )
            self.assertEqual(module.resolve_entry_model("thread-a", temporary), {"status": "unverified"})
            other = Path(temporary) / "sessions" / "2026" / "07" / "09" / f"other-{thread_id}.jsonl"
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}})
                + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "low"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                module.resolve_entry_model(thread_id, temporary),
                {"status": "verified", "model": "gpt-5.6-terra", "effort": "low"},
            )

    def test_exact_suffix_match_and_open_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions_dir = Path(temporary) / "sessions" / "2026" / "07" / "09"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            thread_id = "11111111-1111-1111-1111-111111111111"
            matching = sessions_dir / f"match-{thread_id}.jsonl"
            near_match = sessions_dir / f"not-exact-{thread_id}-near.jsonl"
            decoy = sessions_dir / "session-foo.jsonl"
            archive_decoy = Path(temporary) / "archived_sessions" / "nope-abc.jsonl"
            archive_decoy.parent.mkdir(parents=True, exist_ok=True)
            events = [
                {"type": "session_meta", "payload": {"id": thread_id}},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
            ]
            matching.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            near_match.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}})
                + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "ultra"}}),
                encoding="utf-8",
            )
            decoy.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}})
                + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "high"}}),
                encoding="utf-8",
            )
            archive_decoy.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}})
                + "\n"
                + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "max"}}),
                encoding="utf-8",
            )

            opened = []
            original_open = Path.open

            def spy_open(self, *args, **kwargs):
                opened.append(str(self))
                return original_open(self, *args, **kwargs)

            with patch("pathlib.Path.open", spy_open):
                resolved = module.resolve_entry_model(thread_id, temporary)
            self.assertEqual(resolved, {"status": "verified", "model": "gpt-5.6-luna", "effort": "low"})
            resolved_matching = str(matching.resolve())
            self.assertIn(resolved_matching, [str(Path(path).resolve()) for path in opened])
            self.assertNotIn(str(near_match.resolve()), [str(Path(path).resolve()) for path in opened])
            self.assertNotIn(str(decoy.resolve()), [str(Path(path).resolve()) for path in opened])
            self.assertNotIn(str(archive_decoy.resolve()), [str(Path(path).resolve()) for path in opened])

    def test_exact_session_meta_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions_dir = Path(temporary) / "sessions" / "2026" / "07" / "09"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            thread_id = "22222222-2222-2222-2222-222222222222"
            matching = sessions_dir / f"session-{thread_id}.jsonl"
            events = [
                {"type": "session_meta", "payload": {"id": "not-the-thread"}},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "ultra"}},
            ]
            matching.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            self.assertEqual(module.resolve_entry_model(thread_id, temporary), {"status": "unverified"})
