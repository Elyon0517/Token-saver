#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path


DEFAULT_SESSIONS_DIR = Path.home() / ".codex"
THREAD_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _is_valid_thread_id(thread_id):
    return isinstance(thread_id, str) and bool(THREAD_ID_PATTERN.fullmatch(thread_id.lower()))


def _parse_session_id(event):
    if not isinstance(event, dict) or event.get("type") != "session_meta":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id") or payload.get("id")
    return value.lower() if isinstance(value, str) else None


def _candidate_paths_for_thread(root, thread_id):
    normalized_id = thread_id.lower()
    candidates = []
    sessions_root = root / "sessions"
    if sessions_root.exists():
        for date_dir in sessions_root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]"):
            if not date_dir.is_dir():
                continue
            for path in date_dir.glob(f"*{normalized_id}.jsonl"):
                if path.is_file() and path.stem.lower().endswith(normalized_id):
                    candidates.append(path)

    archive_root = root / "archived_sessions"
    if archive_root.exists():
        for path in archive_root.glob(f"*{normalized_id}.jsonl"):
            if path.is_file() and path.stem.lower().endswith(normalized_id):
                candidates.append(path)
    return candidates


def resolve_entry_model(thread_id=None, sessions_dir=DEFAULT_SESSIONS_DIR):
    if not _is_valid_thread_id(thread_id):
        return {"status": "unverified"}
    root = Path(sessions_dir).expanduser().resolve()
    if not root.is_dir():
        return {"status": "unavailable"}
    resolved = None
    normalized_id = thread_id.lower()
    for path in _candidate_paths_for_thread(root, normalized_id):
        current_session = None
        latest_context = None
        seen_matching_session = False
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for raw_line in handle:
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    session_id = _parse_session_id(event)
                    if session_id is not None:
                        current_session = session_id
                        if current_session == normalized_id:
                            seen_matching_session = True
                    if current_session == normalized_id and event.get("type") == "turn_context":
                        payload = event.get("payload")
                        if (
                            isinstance(payload, dict)
                            and isinstance(payload.get("model"), str)
                            and isinstance(payload.get("effort"), str)
                        ):
                            latest_context = (payload.get("model"), payload.get("effort"))
        except OSError:
            continue
        if seen_matching_session and latest_context:
            resolved = {"status": "verified", "model": latest_context[0], "effort": latest_context[1]}
        elif seen_matching_session:
            resolved = None
    return resolved or {"status": "unverified"}


def main():
    parser = argparse.ArgumentParser(description="Resolve the verified entry model for one Codex thread")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    args = parser.parse_args()
    print(json.dumps(resolve_entry_model(os.environ.get("CODEX_THREAD_ID"), args.sessions_dir), separators=(",", ":")))


if __name__ == "__main__":
    main()
