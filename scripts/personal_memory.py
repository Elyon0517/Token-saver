#!/usr/bin/env python3
"""Capture sanitized personal-memory candidates emitted by Ending.

This bridge keeps personal preference memory separate from project-change and
adaptive model-routing memory. It writes to the root-first Obsidian runtime
when available and queues only sanitized candidates when the vault is absent.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1
DEFAULT_LOCAL_STORE = Path.home() / ".codex" / "personal-memory" / "pending.jsonl"
KINDS = ("preference", "technical-trait")
AREAS = ("ui", "workflow", "technical", "general")
BASES = ("explicit_user_request", "repeated_user_correction", "verified_work_pattern")
CONFIDENCE = ("high", "medium")
VERIFICATION_STATUSES = ("passed", "partial", "failed", "not-run")
MEMORY_BLOCK_START = "<!-- BEGIN CODEX CAPTURED PREFERENCES -->"
MEMORY_BLOCK_END = "<!-- END CODEX CAPTURED PREFERENCES -->"
SENSITIVE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/:]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])/(?:Users|home)/[^\s]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])[A-Z]:\\Users\\[^\s]+", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _single_line(value, field, maximum=280):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS):
        raise ValueError(f"{field} contains private or secret-like content")
    if "```" in text or "[MEMORY_CANDIDATE]" in text:
        raise ValueError(f"{field} must be a compact statement")
    return text


def normalize_candidates(candidates):
    if candidates is None:
        return []
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    if len(candidates) > 8:
        raise ValueError("candidates may contain at most 8 items")
    normalized = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each candidate must be an object")
        allowed = {"kind", "area", "statement", "evidence", "basis", "confidence", "source"}
        unexpected = sorted(set(candidate) - allowed)
        if unexpected:
            raise ValueError(f"candidate contains unsupported fields: {', '.join(unexpected)}")
        value = {
            "kind": _single_line(candidate.get("kind"), "candidate kind", 40),
            "area": _single_line(candidate.get("area"), "candidate area", 40),
            "statement": _single_line(candidate.get("statement"), "candidate statement"),
            "evidence": _single_line(candidate.get("evidence"), "candidate evidence"),
            "basis": _single_line(candidate.get("basis"), "candidate basis", 80),
            "confidence": _single_line(candidate.get("confidence"), "candidate confidence", 20),
            "source": _single_line(candidate.get("source", "ending"), "candidate source", 20),
        }
        if value["kind"] not in KINDS or value["area"] not in AREAS or value["basis"] not in BASES or value["confidence"] not in CONFIDENCE or value["source"] != "ending":
            raise ValueError("candidate uses an unsupported kind, area, basis, confidence, or source")
        key = (value["kind"], value["area"], value["statement"].casefold())
        if key in seen:
            raise ValueError("candidates must not contain duplicate statements")
        seen.add(key)
        normalized.append(value)
    return normalized


def _config_paths():
    home = Path.home()
    if os.name == "nt":
        return [home / "AppData" / "Roaming" / "obsidian" / "obsidian.json"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    return [home / ".config" / "obsidian" / "obsidian.json"]


def resolve_vault(vault=None):
    if vault is not None:
        explicit = Path(vault).expanduser()
        return explicit.resolve() if explicit.is_dir() else None
    configured = os.environ.get("CODEX_OBSIDIAN_VAULT", "").strip()
    if configured and Path(configured).expanduser().is_dir():
        return Path(configured).expanduser().resolve()
    for config_path in _config_paths():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vaults = payload.get("vaults", {}) if isinstance(payload, dict) else {}
        if not isinstance(vaults, dict):
            continue
        entries = list(vaults.values())
        entries.sort(key=lambda entry: (not bool(entry.get("open")), str(entry.get("path", ""))) if isinstance(entry, dict) else (True, ""))
        for entry in entries:
            candidate = Path(entry.get("path", "")).expanduser() if isinstance(entry, dict) and entry.get("path") else None
            if candidate is not None and candidate.is_dir():
                return candidate.resolve()
    return None


def _runtime(vault_path):
    runtime_path = Path(vault_path) / "AI Memory" / "ai_memory.py"
    if not runtime_path.is_file():
        raise ValueError("root-first Obsidian runtime is unavailable")
    spec = importlib.util.spec_from_file_location("codex_personal_memory_runtime", runtime_path)
    if spec is None or spec.loader is None:
        raise ValueError("root-first Obsidian runtime cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "record_memory_candidates", None)) and not callable(getattr(module, "record_event", None)):
        raise ValueError("root-first Obsidian runtime has no supported memory writer")
    return module


def _owner_path(vault_path, candidate):
    preferences_root = Path(vault_path) / "Preferences"
    if candidate.get("area") == "ui" and (preferences_root / "UI Style Preferences.md").exists():
        return preferences_root / "UI Style Preferences.md"
    return preferences_root / "AI Captured Preferences.md"


def _replace_owner_block(existing, candidates):
    rows = []
    for candidate in candidates:
        rows.append(f"- {candidate['statement']} — evidence: {candidate['evidence']}; basis: {candidate['basis']}; confidence: {candidate['confidence']}; occurrences: 1")
    block = "\n".join([MEMORY_BLOCK_START, "## Ending-confirmed memory", "", *rows, "", MEMORY_BLOCK_END])
    if MEMORY_BLOCK_START not in existing or MEMORY_BLOCK_END not in existing:
        return f"{existing.rstrip()}\n\n{block}\n" if existing.strip() else block + "\n"
    prefix, remainder = existing.split(MEMORY_BLOCK_START, 1)
    _, suffix = remainder.split(MEMORY_BLOCK_END, 1)
    old_block = remainder.split(MEMORY_BLOCK_END, 1)[0]
    old_lines = old_block.splitlines()
    combined = list(old_lines)
    for candidate in candidates:
        replacement = f"- {candidate['statement']} — evidence: {candidate['evidence']}; basis: {candidate['basis']}; confidence: {candidate['confidence']}; occurrences: 1"
        matching = next((index for index, line in enumerate(combined) if line.startswith(f"- {candidate['statement']} —")), None)
        if matching is None:
            combined.append(replacement)
        else:
            combined[matching] = replacement
    updated_block = "\n".join([MEMORY_BLOCK_START, *combined, MEMORY_BLOCK_END])
    return f"{prefix.rstrip()}\n\n{updated_block}\n\n{suffix.lstrip()}".rstrip() + "\n"


def _write_legacy_owner_pages(vault_path, candidates):
    grouped = {}
    for candidate in candidates:
        grouped.setdefault(_owner_path(vault_path, candidate), []).append(candidate)
    written = []
    for path, rows in grouped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else "# AI Captured Preferences\n"
        updated = _replace_owner_block(existing, rows)
        if updated != existing:
            pending = path.with_name(path.name + ".pending")
            pending.write_text(updated, encoding="utf-8")
            pending.replace(path)
            written.append(path.relative_to(vault_path).as_posix())
    return written


def _record_with_runtime(runtime, vault_path, candidates, project, module, verification_status):
    recorder = getattr(runtime, "record_memory_candidates", None)
    if callable(recorder):
        result = recorder(candidates, project=project, module=module, verification_status=verification_status, preferences_root=vault_path)
        owner_documents = result.get("owner_documents", [])
    else:
        legacy_recorder = getattr(runtime, "record_event", None)
        if not callable(legacy_recorder):
            raise ValueError("root-first Obsidian runtime has no supported memory writer")
        decisions = [f"{candidate['kind']}/{candidate['area']}: {candidate['statement']} | evidence: {candidate['evidence']} | basis: {candidate['basis']} | confidence: {candidate['confidence']}" for candidate in candidates]
        result = legacy_recorder(project, module, "preference", f"Captured {len(candidates)} Ending-confirmed personal memory candidates", "Ending found explicit preference or verified working-pattern evidence.", "Candidates passed bounded privacy and evidence validation.", verification_status, working_line="global-personal-memory", verification=["Ending candidate analysis"], decisions=decisions)
        owner_documents = _write_legacy_owner_pages(vault_path, candidates) if result.get("status") in {"written", "updated", "duplicate"} else []
    render = getattr(runtime, "render_views", None)
    render_status = "skipped"
    if callable(render) and result.get("status") in {"written", "updated", "duplicate"}:
        render()
        render_status = "written"
    return {**result, "owner_documents": owner_documents, "render": render_status}


def _store_path(local_store=None):
    return Path(local_store).expanduser().resolve() if local_store else DEFAULT_LOCAL_STORE.expanduser().resolve()


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.with_name(path.name + ".lock")).open("a", encoding="utf-8") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _queue(candidates, project, module, verification_status, local_store):
    path = _store_path(local_store)
    envelope = {"schema_version": SCHEMA_VERSION, "queued_at": _now(), "project": project, "module": module, "verification_status": verification_status, "candidates": candidates}
    with _lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def capture(candidates, *, project="Global Preferences", module="ending-memory", verification_status="passed", vault=None, local_store=None):
    normalized = normalize_candidates(candidates)
    if not normalized:
        return {"status": "no-candidates", "written": False, "candidates": 0}
    project_value = _single_line(project, "project", 160)
    module_value = _single_line(module, "module", 160)
    if verification_status not in VERIFICATION_STATUSES:
        raise ValueError(f"verification_status must be one of {', '.join(VERIFICATION_STATUSES)}")
    vault_path = resolve_vault(vault)
    if vault_path is not None:
        try:
            runtime = _runtime(vault_path)
            result = _record_with_runtime(runtime, vault_path, normalized, project_value, module_value, verification_status)
            return {"status": "written" if result.get("status") in {"written", "updated", "duplicate"} else result.get("status", "unavailable"), "written": result.get("status") in {"written", "updated"}, "candidates": len(normalized), "vault": "ready", "runtime": result}
        except (OSError, ValueError, TypeError, ImportError, json.JSONDecodeError) as error:
            queued_path = _queue(normalized, project_value, module_value, verification_status, local_store)
            return {"status": "queued", "written": False, "candidates": len(normalized), "reason": str(error), "pending": str(queued_path)}
    queued_path = _queue(normalized, project_value, module_value, verification_status, local_store)
    return {"status": "queued", "written": False, "candidates": len(normalized), "reason": "obsidian_vault_unavailable", "pending": str(queued_path)}


def _read_pending(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def replay_pending(*, vault=None, local_store=None):
    path = _store_path(local_store)
    pending = _read_pending(path)
    if not pending:
        return {"status": "no-pending", "written": False, "records": 0}
    vault_path = resolve_vault(vault)
    if vault_path is None:
        return {"status": "queued", "written": False, "records": len(pending), "reason": "obsidian_vault_unavailable"}
    remaining = []
    written = 0
    for envelope in pending:
        result = capture(envelope.get("candidates", []), project=envelope.get("project", "Global Preferences"), module=envelope.get("module", "ending-memory"), verification_status=envelope.get("verification_status", "partial"), vault=vault_path, local_store=path)
        if result.get("status") in {"written", "duplicate", "no-candidates"}:
            written += 1
        else:
            remaining.append(envelope)
    with _lock(path):
        if remaining:
            path.write_text("".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in remaining), encoding="utf-8")
        elif path.exists():
            path.unlink()
    return {"status": "written" if not remaining else "partial", "written": not remaining, "records": len(pending), "replayed": written, "remaining": len(remaining)}


def _candidate_file(path):
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    return payload.get("candidates") if isinstance(payload, dict) else payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persist sanitized Ending personal-memory candidates without storing raw task data.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--candidate-file", type=Path, required=True)
    capture_parser.add_argument("--project", default="Global Preferences")
    capture_parser.add_argument("--module", default="ending-memory")
    capture_parser.add_argument("--verification-status", choices=VERIFICATION_STATUSES, default="passed")
    capture_parser.add_argument("--vault", type=Path)
    capture_parser.add_argument("--local-store", type=Path)
    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--vault", type=Path)
    replay_parser.add_argument("--local-store", type=Path)
    args = parser.parse_args(argv)
    if args.command == "capture":
        output = capture(_candidate_file(args.candidate_file), project=args.project, module=args.module, verification_status=args.verification_status, vault=args.vault, local_store=args.local_store)
    else:
        output = replay_pending(vault=args.vault, local_store=args.local_store)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0 if output.get("status") in {"written", "duplicate", "no-candidates", "no-pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
