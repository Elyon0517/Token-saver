import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "personal_memory.py"
SPEC = importlib.util.spec_from_file_location("personal_memory", SCRIPT_PATH)
MEMORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEMORY)


def candidate(statement="Prefer compact layouts.", area="ui"):
    return {
        "kind": "preference",
        "area": area,
        "statement": statement,
        "evidence": "The user explicitly requested this durable setting.",
        "basis": "explicit_user_request",
        "confidence": "high",
        "source": "ending",
    }


class PersonalMemoryTests(unittest.TestCase):
    def test_empty_candidates_are_a_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = MEMORY.capture([], local_store=Path(temporary) / "pending.jsonl")
            self.assertEqual(result, {"status": "no-candidates", "written": False, "candidates": 0})
            self.assertFalse((Path(temporary) / "pending.jsonl").exists())

    def test_missing_vault_queues_only_sanitized_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.jsonl"
            result = MEMORY.capture([candidate()], vault=Path(temporary) / "missing-vault", local_store=pending)
            payload = json.loads(pending.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "queued")
        self.assertEqual(payload["candidates"][0]["statement"], "Prefer compact layouts.")
        self.assertNotIn("raw", payload)

    def test_available_root_first_runtime_receives_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path = root / "AI Memory" / "ai_memory.py"
            runtime_path.parent.mkdir(parents=True)
            runtime_path.write_text("""
import json
from pathlib import Path
def record_memory_candidates(candidates, **kwargs):
    Path(__file__).with_name('received.json').write_text(json.dumps({'candidates': candidates, 'kwargs': kwargs}, default=str), encoding='utf-8')
    return {'status': 'written', 'owner_documents': ['Preferences/UI Style Preferences.md']}
""", encoding="utf-8")
            result = MEMORY.capture([candidate()], vault=root, local_store=root / "pending.jsonl")
            received = json.loads((root / "AI Memory" / "received.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "written")
        self.assertEqual(received["candidates"][0]["area"], "ui")
        self.assertEqual(received["kwargs"]["project"], "Global Preferences")

    def test_legacy_root_first_runtime_uses_record_event_and_owner_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path = root / "AI Memory" / "ai_memory.py"
            runtime_path.parent.mkdir(parents=True)
            runtime_path.write_text("""
import json
from pathlib import Path
def record_event(project, module, event_type, summary, reason, result, verification_status, **kwargs):
    Path(__file__).with_name('received.json').write_text(json.dumps({'project': project, 'module': module, 'decisions': kwargs['decisions']}), encoding='utf-8')
    return {'status': 'written', 'event_id': 'event-1'}
def render_views():
    return {'status': 'written'}
""", encoding="utf-8")
            result = MEMORY.capture([candidate()], vault=root, local_store=root / "pending.jsonl")
            received = json.loads((root / "AI Memory" / "received.json").read_text(encoding="utf-8"))
            owner_text = (root / "Preferences" / "AI Captured Preferences.md").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "written")
        self.assertEqual(received["module"], "ending-memory")
        self.assertIn("Prefer compact layouts.", owner_text)

    def test_private_candidate_is_rejected(self):
        with self.assertRaises(ValueError):
            MEMORY.normalize_candidates([candidate(f"Use {Path('/', 'Users', 'example', 'secret')}.")])


if __name__ == "__main__":
    unittest.main()
