#!/usr/bin/env python3
import argparse
import hashlib
import importlib.util
import json
import os
if os.name == "nt":
    import msvcrt
elif os.name == "posix":
    import fcntl
else:
    raise RuntimeError(f"Unsupported host OS for project memory locking: {os.name}")
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


DEFAULT_STORE = Path.home() / ".codex" / "project-change-memory"
DEFAULT_VAULT = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "MyAILLM"
SCHEMA_VERSION = 1
SCOPE_VALUES = ("project", "feature", "code", "file")
CHANGE_KIND_VALUES = ("add", "edit", "rename", "move", "delete", "mixed")
VERIFICATION_STATUS_VALUES = ("passed", "partial", "failed", "not-run")
CANONICAL_KNOWLEDGE_FOLDER = "Knowledge"
LEGACY_KNOWLEDGE_FOLDER = "KnowledgeAreas"


_SESSION_EFFORT_PATH = Path(__file__).resolve().parent / "session_effort.py"
_SESSION_EFFORT_SPEC = importlib.util.spec_from_file_location("project_memory_session_effort", _SESSION_EFFORT_PATH)
if _SESSION_EFFORT_SPEC is None or _SESSION_EFFORT_SPEC.loader is None:
    raise RuntimeError(f"Cannot load session scope runtime: {_SESSION_EFFORT_PATH}")
_SESSION_EFFORT = importlib.util.module_from_spec(_SESSION_EFFORT_SPEC)
_SESSION_EFFORT_SPEC.loader.exec_module(_SESSION_EFFORT)


def _memory_scope(project_key, module, task_name="", task_group="", session_id=""):
    resolved_session_id = _SESSION_EFFORT.resolve_session_id("", session_id)
    active_task_name = task_name or os.environ.get("CODEX_TASK_NAME", "")
    active_task_group = task_group or os.environ.get("CODEX_TASK_GROUP", "")
    normalized_task_name = _SESSION_EFFORT.normalize_task_name(active_task_name) if str(active_task_name or "").strip() else ""
    normalized_task_group = _SESSION_EFFORT.normalize_task_name(active_task_group, "group") if str(active_task_group or "").strip() else ""
    task_scope = _SESSION_EFFORT.task_scope_key(project_key, "project-change", module, normalized_task_name) if normalized_task_name else ""
    group_scope = _SESSION_EFFORT.task_group_key(project_key, normalized_task_group, normalized_task_name)
    return {"codex_session_key": _SESSION_EFFORT.session_key(resolved_session_id) if resolved_session_id else "", "task_name": normalized_task_name, "task_group": normalized_task_group, "task_scope_key": task_scope, "task_group_key": group_scope, "task_scope_mode": "session+task+group" if resolved_session_id and task_scope and group_scope else "session+task" if resolved_session_id and task_scope else "session+group" if resolved_session_id and group_scope else "session" if resolved_session_id else "task+group" if task_scope and group_scope else "task" if task_scope else "group" if group_scope else "unscoped"}


def _record_in_memory_scope(record, scope):
    if not (scope.get("codex_session_key") or scope.get("task_scope_key") or scope.get("task_group_key")):
        return True
    return _SESSION_EFFORT.scope_matches(record, session_key_value=scope.get("codex_session_key", ""), task_scope=scope.get("task_scope_key", ""), task_group_key_value=scope.get("task_group_key", ""))


def _acquire_file_lock(lock_handle):
    if os.name == "nt":
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write("\0")
            lock_handle.flush()
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)


def _single_line(value, field_name, required=True, max_length=1200):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field_name} is required")
    return text[:max_length]


def _slug(value, fallback="item"):
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-._")
    return normalized[:80] or f"{fallback}-{hashlib.sha256(str(value).encode()).hexdigest()[:10]}"


def _project_identity(project_root):
    root = Path(project_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("project_root must be an existing directory")
    name = root.name or "project"
    path_hash = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    return {
        "name": name,
        "root": ".",
        "key": f"{_slug(name, 'project')}-{path_hash}",
        "owner": _registered_owner(root) or "",
    }


def _normalize_files(project_root, file_values):
    root = Path(project_root).expanduser().resolve()
    normalized = []
    for file_value in file_values:
        candidate = Path(file_value).expanduser()
        relative = candidate.resolve().relative_to(root) if candidate.is_absolute() else PurePosixPath(candidate.as_posix())
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"file must be inside project_root: {file_value}")
        relative_text = relative.as_posix()
        if relative_text not in normalized:
            normalized.append(relative_text)
    if not normalized:
        raise ValueError("at least one --file is required")
    return normalized


def _project_file_index_path(project_dir, relative_file):
    parts = [_slug(part, "path") for part in PurePosixPath(relative_file).parts]
    leaf_hash = hashlib.sha256(relative_file.encode()).hexdigest()[:10]
    return project_dir / "files" / Path(*parts[:-1]) / f"{parts[-1]}-{leaf_hash}.jsonl"


def _run_git(*command, cwd):
    try:
        result = subprocess.run(
            command,
            cwd=Path(cwd),
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


def _canonicalize_git_remote(remote):
    if not remote:
        return ""
    normalized = remote.strip()
    if normalized.startswith("git@"):
        normalized = normalized.replace(":", "/", 1)
        normalized = normalized.replace("git@", "https://", 1)
    if normalized.startswith("ssh://"):
        normalized = normalized.replace("ssh://", "https://", 1)
    normalized = normalized[:-4] if normalized.endswith(".git") else normalized
    return normalized.rstrip("/")


def _derive_working_line(project_root):
    root = Path(project_root).expanduser().resolve()
    branch = _run_git("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if branch == "HEAD":
        branch = "DETACHED_HEAD"
    commit = _run_git("git", "rev-parse", "HEAD", cwd=root)
    remote = _run_git("git", "remote", "get-url", "origin", cwd=root)
    if not remote:
        remotes = _run_git("git", "remote", "-v", cwd=root).splitlines()
        for remote_line in remotes:
            if "\t" in remote_line:
                candidate, location = remote_line.split("\t", 1)
                if not candidate:
                    continue
                remote = location.split()[0] if location else ""
                break
    version = _run_git("git", "describe", "--tags", "--exact-match", "--abbrev=0", "HEAD", cwd=root)
    canonical_remote = _canonicalize_git_remote(remote)
    if canonical_remote and branch and commit:
        identity_scope = "scoped"
    else:
        identity_scope = "unscoped"
    return {
        "identity_scope": identity_scope,
        "canonical_remote": canonical_remote,
        "branch": branch,
        "commit": commit,
        "version": version,
    }


def _has_sufficient_working_line(line):
    return bool(line.get("canonical_remote") and line.get("branch") and line.get("commit") and line.get("identity_scope") == "scoped")


def _working_line_key(line):
    if not line:
        return ""
    signature = {
        "canonical_remote": line.get("canonical_remote", ""),
        "branch": line.get("branch", ""),
        "commit": line.get("commit", ""),
    }
    if line.get("version"):
        signature["version"] = line.get("version", "")
    return json.dumps(signature, sort_keys=True, separators=(",", ":"))


def _working_lines_conflict(left, right):
    if not _has_sufficient_working_line(left) or not _has_sufficient_working_line(right):
        return False
    if left.get("canonical_remote") != right.get("canonical_remote"):
        return True
    if left.get("branch") != right.get("branch"):
        return True
    if left.get("commit") != right.get("commit"):
        return True
    left_version = left.get("version", "")
    right_version = right.get("version", "")
    if left_version and right_version and left_version != right_version:
        return True
    return False


def _should_exclude_from_auto_match(record, current_line, include_ambiguous=False):
    record_line = record.get("project", {}).get("working_line") or record.get("working_line") or {}
    if not include_ambiguous:
        if not _has_sufficient_working_line(current_line) or not _has_sufficient_working_line(record_line):
            return True
        if _working_lines_conflict(record_line, current_line):
            return True
    return False


def _read_records(index_path):
    if not index_path.exists():
        return []
    return [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _resolve_vault(vault):
    selected = Path(vault).expanduser() if vault else Path(os.environ.get("CODEX_OBSIDIAN_VAULT", DEFAULT_VAULT)).expanduser()
    return selected.resolve() if selected.exists() and selected.is_dir() else None


def _markdown_entry(record):
    files = "\n".join(f"  - `{file_path}`" for file_path in record["files"])
    verifications = "; ".join(record["verification"]) or "none supplied"
    decisions = "; ".join(record["decisions"]) or "none"
    risks = "; ".join(record["risks"]) or "none"
    supersedes = record["supersedes"] or "none"
    anchor = _event_anchor(record["id"])
    return f"### {record['recorded_at']} — {record['summary']}\n\n- Record ID: `{record['id']}`\n- Module: {record['module']}\n- Task scope: {record.get('task_scope_mode', 'unscoped')} / `{record.get('task_scope_key', '')}`\n- Task group: {record.get('task_group', '')} / `{record.get('task_group_key', '')}`\n- Scope: {record['scope']}\n- Change kind: {record['change_kind']}\n- What changed: {record['summary']}\n- Why: {record['reason']}\n- Result: {record['result']}\n- Verification: {record['verification_status']} — {verifications}\n- Decisions: {decisions}\n- Remaining risks: {risks}\n- Supersedes: `{supersedes}`\n- Files:\n{files}\n\n^{anchor}\n\n"


HISTORY_SECTIONS = (
    "Rules and Decisions",
    "Implementation and Changes",
    "Errors and Failures",
    "Retries and Repairs",
    "Verification and Results",
    "Documentation, Assets and Releases",
    "Relationships",
)


def _event_anchor(record_id):
    """Stable, linkable anchor for the one canonical History entry."""
    return "change-" + _slug(record_id, "record")


def _normalized_registered_root(project_root):
    root = _normalize_project_root(project_root)
    home = _normalize_project_root(Path.home())
    if root == home:
        return "/"
    if root.startswith(home + "/"):
        return root[len(home):]
    return root


def _normalize_project_root(value):
    path = Path(value)
    if str(value).startswith("~/"):
        path = Path.home() / str(value)[2:]
    return path.resolve().as_posix().lower().rstrip("/")


def _sorted_registered_owner_entries(entries):
    return tuple(sorted(entries, key=lambda item: len(item[0]), reverse=True))


HOME_PROJECT_OWNER_ROOTS = ((".codex", "Global Codex Skills"),)

# Paths are deliberately relative to the user's Documents folder. Old and
# current locations may coexist here so moving a repository does not split its
# durable change history or adaptive model learning.
DOCUMENT_PROJECT_OWNER_ROOTS = (
    ("AIProject/qin-codex-skills", "Global Codex Skills"),
    ("Muse/SVGDrawer", "SVGDrawer"),
    ("Muse/MuseAI", "MuseAI"),
    ("Muse/UserExamples", "MuseAI"),
    ("YofaGames/ThisIsMyOregon", "ThisIsMyOregon"),
    ("YofaGames/AIAnimation2D", "AIAnimation2D"),
    ("YofaGames/AIShaderGraphic2D", "AIShaderGraphic2D"),
    ("YofaGames/AIVFX2D", "AIVFX2D"),
    ("FilesManagement/Destiny", "Destiny"),
    ("YofaGames/Destiny", "Destiny"),
    ("YofaGames/FunctionWebsite", "FunctionWebsite"),
    ("Unity3DPersonalProject/MetaStory", "MetaStory"),
    ("YofaGames/MetaStory", "MetaStory"),
    ("Unity3DPersonalProject/UnityCodexTest", "UnityCodexTest"),
    ("YofaGames/UnityCodexTest", "UnityCodexTest"),
    ("PythonProject/XNews", "XNews"),
    ("YofaGames/XNews", "XNews"),
    ("Muse/taggingapilandingpage", "TaggingAPILandingPage"),
    ("PythonProject/Agent-ImageEdtior", "AgentImageEditor"),
    ("DockerProject/Docker-Mokozoo", "Mokozoo"),
)


def _registered_project_owner_paths():
    home = Path.home()
    entries = [(home / relative, owner) for relative, owner in HOME_PROJECT_OWNER_ROOTS]
    entries.extend((home / "Documents" / relative, owner) for relative, owner in DOCUMENT_PROJECT_OWNER_ROOTS)
    return tuple(entries)


def _registered_project_owners():
    return _sorted_registered_owner_entries(
        (_normalized_registered_root(prefix), owner)
        for prefix, owner in _registered_project_owner_paths()
    )


def _registered_owner_alias(record_root):
    return _normalized_registered_root(record_root)


def _registered_owner(record_root):
    root = _registered_owner_alias(record_root)
    for registered_root, owner in _registered_project_owners():
        if root == registered_root + "/cache" or root.startswith(registered_root + "/cache/"):
            continue
        if root == registered_root or root.startswith(registered_root + "/"):
            return owner
    return None


def _project_key_for_root(project_root):
    root = Path(project_root).expanduser().resolve()
    path_hash = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    return f"{_slug(root.name or 'project', 'project')}-{path_hash}"


def _registered_owner_project_keys(owner):
    if not owner:
        return set()
    return {
        _project_key_for_root(root)
        for root, registered_owner in _registered_project_owner_paths()
        if registered_owner == owner
    }


def _record_matches_project(record, project):
    """Match exact identity or a registered old/current root for one owner."""
    project_key = project.get("key")
    record_project = record.get("project") if isinstance(record.get("project"), dict) else {}
    record_key = record_project.get("key") or record.get("project_key")
    if project_key and record_key == project_key:
        return True
    owner = project.get("owner") or _registered_owner(project.get("root", ""))
    if not owner:
        return False
    if record.get("project_owner") == owner:
        return True
    if record_project.get("owner") == owner:
        return True
    record_root = record_project.get("root")
    if record_root and _registered_owner(record_root) == owner:
        return True
    return record_key in _registered_owner_project_keys(owner)


def _active_knowledge_root(vault_path):
    """Use legacy knowledge only when the vault has not adopted Knowledge/."""
    canonical_root = vault_path / CANONICAL_KNOWLEDGE_FOLDER
    legacy_root = vault_path / LEGACY_KNOWLEDGE_FOLDER
    if canonical_root.exists() or not legacy_root.exists():
        return canonical_root
    return legacy_root


def _canonical_history_target(record, vault_path, project_root=None):
    """Return the only broad History page permitted for an Obsidian projection."""
    resolved_project_root = Path(project_root).expanduser().resolve() if project_root is not None else Path(record["project"]["root"]).expanduser().resolve()
    if resolved_project_root == vault_path.resolve():
        return _active_knowledge_root(vault_path) / "Source Ingest and Wiki Maintenance.md", "Source Ingest and Wiki Maintenance"
    owner = record.get("project", {}).get("owner") or _registered_owner(resolved_project_root)
    if owner == "Global Codex Skills":
        return vault_path / "Skills" / "Global Codex Skills History.md", "Global Codex Skills"
    if owner is None:
        return None, ""
    return vault_path / "Projects" / owner / "History.md", owner


def _history_section(record):
    text = " ".join([record.get("summary", ""), record.get("reason", ""), record.get("result", ""), record.get("verification_status", ""), record.get("change_kind", "")]).lower()
    if any(word in text for word in ("rule", "design", "architecture", "contract", "ownership", "decision", "strategy")):
        return HISTORY_SECTIONS[0]
    if any(word in text for word in ("retry", "repair", "rollback", "supersed", "recovery", " fix ")):
        return HISTORY_SECTIONS[3]
    if record.get("verification_status") in {"failed", "partial"} or any(word in text for word in ("fail", "bug", "crash", "regression", "mismatch", "blocked")):
        return HISTORY_SECTIONS[2]
    if any(word in text for word in ("test", "audit", "benchmark", "verify", "verification", "acceptance", "measured", "visual proof")):
        return HISTORY_SECTIONS[4]
    if any(word in text for word in ("publish", "readme", "docs", "repository", "release", "asset-only")):
        return HISTORY_SECTIONS[5]
    return HISTORY_SECTIONS[1]


def _insert_history_entry(path, title, section, entry, record_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = "# " + title + " History\n\n" + "\n\n".join("## " + heading for heading in HISTORY_SECTIONS) + "\n"
    if f"Record ID: `{record_id}`" in text:
        return
    marker = "## " + section
    position = text.find(marker)
    if position < 0:
        text = text.rstrip() + "\n\n" + marker + "\n"
        position = text.find(marker)
    next_position = text.find("\n## ", position + len(marker))
    insertion = next_position if next_position >= 0 else len(text)
    text = text[:insertion].rstrip() + "\n\n" + entry.rstrip() + "\n\n" + text[insertion:].lstrip("\n")
    path.write_text(text, encoding="utf-8")


def _activity_index_target(history_target, vault_path):
    if history_target.parent.parent.name == "Projects":
        return history_target.parent / "Activity Index.md"
    if history_target.parent.name == "Skills":
        return vault_path / "Skills" / "Activity Index.md"
    return history_target.parent / "Activity Index.md"


def _write_activity_pointer(history_target, vault_path, record, section):
    target = _activity_index_target(history_target, vault_path)
    relative_history = history_target.relative_to(vault_path).with_suffix("").as_posix()
    pointer = f"- {record['recorded_at'][:10]} · {record['change_kind']} · [[{relative_history}#^{_event_anchor(record['id'])}|{record['summary']}]] · {record['verification_status'].upper()}"
    text = target.read_text(encoding="utf-8") if target.exists() else "# Activity Index\n\nChronological pointers to canonical History detail.\n\n"
    if pointer not in text:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip() + "\n" + pointer + "\n", encoding="utf-8")


def _journal_pointer(history_target, vault_path, record):
    relative_history = history_target.relative_to(vault_path).with_suffix("").as_posix()
    return f"- {record['recorded_at'][:10]} · [[{relative_history}#^{_event_anchor(record['id'])}|{record['summary']}]] · {record['verification_status'].upper()}"


def _write_journal_pointer(history_target, vault_path, record):
    journal = vault_path / "Journal"
    log = journal / "log.md"
    index = journal / "index.md"
    pointer = _journal_pointer(history_target, vault_path, record)
    log_text = log.read_text(encoding="utf-8") if log.exists() else "# Journal Log\n\nPointer-only chronology. Canonical event bodies remain in their owning History.\n"
    if pointer not in log_text:
        journal.mkdir(parents=True, exist_ok=True)
        log_text = log_text.rstrip() + "\n" + pointer + "\n"
        log.write_text(log_text, encoding="utf-8")

    pointer_lines = [line for line in log_text.splitlines() if line.startswith("- ")]
    recent = "\n".join(pointer_lines[-20:])
    begin = "<!-- BEGIN BOUNDED RECENT POINTERS -->"
    end = "<!-- END BOUNDED RECENT POINTERS -->"
    index_text = index.read_text(encoding="utf-8") if index.exists() else "# Journal\n\nChronological navigation only; canonical detail remains in History.\n\n## Recent\n\n" + begin + "\n" + end + "\n"
    if begin not in index_text or end not in index_text:
        index_text = index_text.rstrip() + "\n\n## Recent\n\n" + begin + "\n" + end + "\n"
    prefix, remainder = index_text.split(begin, 1)
    _, suffix = remainder.split(end, 1)
    updated_index = prefix + begin + "\n" + recent + "\n" + end + suffix
    if updated_index != index_text:
        index.write_text(updated_index, encoding="utf-8")


def _write_root_first_memory(record, vault_path):
    runtime_path = vault_path / "AI Memory" / "ai_memory.py"
    specification = importlib.util.spec_from_file_location("myaillm_ai_memory_runtime", runtime_path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Cannot load root-first AI memory runtime: {runtime_path}")
    runtime = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(runtime)
    text = " ".join([record.get("summary", ""), record.get("reason", ""), record.get("result", ""), record.get("verification_status", "")]).lower()
    if record.get("verification_status") in {"failed", "partial"} or any(word in text for word in ("bug", "fix", "repair", "failure", "error", "regression")):
        event_type = "bug-fix"
    elif any(word in text for word in ("architecture", "schema", "ownership", "contract", "structure")):
        event_type = "architecture"
    else:
        event_type = "general"
    project = record.get("project", {}).get("owner") or record.get("project", {}).get("name") or "Unknown"
    working_line = json.dumps(record.get("project", {}).get("working_line") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    output = runtime.record_event(project, record["module"], event_type, record["summary"], record["reason"], record["result"], record["verification_status"], working_line=working_line, files=record["files"], verification=record["verification"], decisions=record["decisions"], risks=record["risks"], task_name=record.get("task_name", ""), task_group=record.get("task_group", ""), codex_session_key=record.get("codex_session_key", ""), task_scope_key=record.get("task_scope_key", ""), task_group_key=record.get("task_group_key", ""))
    runtime.render_views()
    return {"status": "written", "written": True, "root": "AI Memory/events.jsonl", "event_status": output.get("status"), "event_id": output.get("event_id", "")}


def _write_obsidian(record, vault, project_root=None):
    vault_path = _resolve_vault(vault)
    if vault_path is None:
        return {"status": "unavailable", "written": False}
    if (vault_path / "AI Memory" / "ai_memory.py").is_file():
        return _write_root_first_memory(record, vault_path)
    entry = _markdown_entry(record)
    target, title = _canonical_history_target(record, vault_path, project_root)
    if target is None:
        return {"status": "no-op", "written": False, "reason": "unregistered_project_root"}
    section = _history_section(record)
    _insert_history_entry(target, title, section, entry, record["id"])
    _write_activity_pointer(target, vault_path, record, section)
    _write_journal_pointer(target, vault_path, record)
    if target.parent.parent.name == "Projects":
        index = target.parent / "index.md"
        if not index.exists():
            index.write_text(f"# {title}\n\n- [[Projects/{title}/History]]\n- [[Projects]]\n", encoding="utf-8")
        projects_index = vault_path / "Projects" / "index.md"
        line = f"- [[Projects/{title}/index|{title}]]"
        existing = projects_index.read_text(encoding="utf-8") if projects_index.exists() else "# Projects\n\n"
        if line not in existing:
            projects_index.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")
    return {"status": "written", "written": True, "root": target.relative_to(vault_path).as_posix()}


def _fingerprint(record):
    payload = {key: record[key] for key in ("project", "module", "scope", "change_kind", "summary", "reason", "result", "verification_status", "verification", "decisions", "risks", "files", "supersedes")}
    payload.update({key: record.get(key, "") for key in ("codex_session_key", "task_name", "task_group", "task_scope_key", "task_group_key", "task_scope_mode")})
    if record.get("project", {}).get("working_line"):
        payload["working_line"] = record["project"]["working_line"]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_change(project_root, module, scope, change_kind, summary, reason, result, verification_status, files, verification=None, decisions=None, risks=None, supersedes="", store=DEFAULT_STORE, vault=None, recorded_at=None, task_name="", session_id="", task_group=""):
    resolved_project_root = Path(project_root).expanduser().resolve()
    project = _project_identity(project_root)
    timestamp = recorded_at or datetime.now(timezone.utc)
    working_line = _derive_working_line(project_root)
    scope_fields = _memory_scope(project["key"], module, task_name, task_group, session_id)
    record = {"schema_version": SCHEMA_VERSION, "id": "", "recorded_at": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"), "project": project, "module": _single_line(module, "module", max_length=160), "scope": scope, "change_kind": change_kind, "summary": _single_line(summary, "summary"), "reason": _single_line(reason, "reason"), "result": _single_line(result, "result"), "verification_status": verification_status, **scope_fields, "verification": [_single_line(value, "verification", max_length=600) for value in (verification or [])], "decisions": [_single_line(value, "decision", max_length=600) for value in (decisions or [])], "risks": [_single_line(value, "risk", max_length=600) for value in (risks or [])], "files": _normalize_files(resolved_project_root, files), "supersedes": _single_line(supersedes, "supersedes", required=False, max_length=120)}
    record["project"]["working_line"] = working_line
    if scope not in SCOPE_VALUES:
        raise ValueError(f"scope must be one of {', '.join(SCOPE_VALUES)}")
    if change_kind not in CHANGE_KIND_VALUES:
        raise ValueError(f"change_kind must be one of {', '.join(CHANGE_KIND_VALUES)}")
    if verification_status not in VERIFICATION_STATUS_VALUES:
        raise ValueError(f"verification_status must be one of {', '.join(VERIFICATION_STATUS_VALUES)}")
    if verification_status != "not-run" and not record["verification"]:
        raise ValueError("at least one --verification is required unless verification-status is not-run")
    record["fingerprint"] = _fingerprint(record)
    record["id"] = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{record['fingerprint'][:12]}"
    store_path = Path(store).expanduser().resolve()
    store_path.mkdir(parents=True, exist_ok=True)
    lock_path = store_path / ".lock"
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        _acquire_file_lock(lock_handle)
        existing_records = _read_records(store_path / "index.jsonl")
        if record["supersedes"]:
            superseded = next((existing for existing in existing_records if existing.get("id") == record["supersedes"]), None)
            if not superseded:
                raise ValueError("supersedes must reference an existing record")
            if not _record_matches_project(superseded, project):
                raise ValueError("supersedes must reference the same project")
            if superseded.get("module") != record["module"]:
                raise ValueError("supersedes must reference the same module")
            if _working_lines_conflict(record["project"].get("working_line", {}), superseded.get("project", {}).get("working_line", {})):
                raise ValueError("supersedes must reference the same project working line")
            if not set(superseded.get("files", [])) & set(record["files"]):
                raise ValueError("supersedes must overlap at least one touched file")
        duplicate = next((existing for existing in reversed(existing_records) if existing.get("fingerprint") == record["fingerprint"]), None)
        if duplicate:
            return {"status": "duplicate", "record_id": duplicate["id"], "project": duplicate["project"], "files": duplicate["files"], "local": {"written": True, "store": store_path.name}, "obsidian": {"status": "not-rewritten", "written": False}}
        project_dir = store_path / "projects" / project["key"]
        record_path = project_dir / "records" / timestamp.strftime("%Y") / timestamp.strftime("%m") / f"{record['id']}.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _append_jsonl(store_path / "index.jsonl", record)
        _append_jsonl(project_dir / "index.jsonl", record)
        _append_jsonl(project_dir / "modules" / _slug(record["module"], "module") / "index.jsonl", record)
        for relative_file in record["files"]:
            _append_jsonl(_project_file_index_path(project_dir, relative_file), record)
        obsidian_status = _write_obsidian(record, vault, resolved_project_root)
    return {"status": "written", "record_id": record["id"], "project": project, "files": record["files"], "local": {"written": True, "store": store_path.name, "record": record_path.relative_to(store_path).as_posix()}, "obsidian": obsidian_status}


def search_records(project_root=None, module="", files=None, query="", max_results=8, store=DEFAULT_STORE, include_ambiguous=False, task_name="", session_id="", task_group=""):
    project = _project_identity(project_root) if project_root else None
    normalized_files = _normalize_files(project_root, files) if project_root and files else list(files or [])
    current_line = _derive_working_line(project_root) if project_root else {}
    scope = _memory_scope(project["key"], module or "project-wide", task_name, task_group, session_id) if project else {"codex_session_key": "", "task_scope_key": "", "task_group_key": ""}
    terms = [term for term in re.findall(r"[\w.+-]+", query.lower()) if len(term) >= 2][:12]
    matches = []
    for record in reversed(_read_records(Path(store).expanduser().resolve() / "index.jsonl")):
        if project and not _record_matches_project(record, project):
            continue
        if not _record_in_memory_scope(record, scope):
            continue
        if project and _should_exclude_from_auto_match(record, current_line, include_ambiguous=include_ambiguous):
            continue
        if module and record["module"].lower() != module.strip().lower():
            continue
        if normalized_files and not any(file_path in record["files"] for file_path in normalized_files):
            continue
        searchable = " ".join([record["summary"], record["reason"], record["result"], record["module"], *record["files"], *record["verification"], *record["decisions"], *record["risks"]]).lower()
        if terms and not all(term in searchable for term in terms):
            continue
        relation = _SESSION_EFFORT.scope_relation(record, session_key_value=scope.get("codex_session_key", ""), task_scope=scope.get("task_scope_key", ""), task_group_key_value=scope.get("task_group_key", "")) if scope.get("codex_session_key") or scope.get("task_scope_key") or scope.get("task_group_key") else {"reason": "unscoped_query"}
        matches.append({**{key: record.get(key, "") for key in ("id", "recorded_at", "project", "module", "scope", "change_kind", "summary", "reason", "result", "verification_status", "verification", "decisions", "risks", "files", "supersedes", "codex_session_key", "task_name", "task_group", "task_scope_key", "task_group_key", "task_scope_mode")}, "relation_reason": relation["reason"], "source_session_key": record.get("codex_session_key", "") or record.get("session_key", "")})
        if len(matches) >= max(1, min(max_results, 25)):
            break
    return {"status": "ok" if matches else "no-matches", "matches": matches}


def memory_status(store=DEFAULT_STORE, vault=None):
    store_path = Path(store).expanduser().resolve()
    records = _read_records(store_path / "index.jsonl")
    vault_path = _resolve_vault(vault)
    return {"status": "ready", "local": {"store": store_path.name, "records": len(records)}, "obsidian": {"status": "available" if vault_path else "unavailable", "vault": vault_path.name if vault_path else ""}}


def main():
    parser = argparse.ArgumentParser(description="Record and recall file-level project change rationale")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--vault", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--project-root", type=Path)
    search_parser.add_argument("--module", default="")
    search_parser.add_argument("--file", action="append", default=[])
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--max-results", type=int, default=8)
    search_parser.add_argument("--include-ambiguous", action="store_true")
    search_parser.add_argument("--task-name", default=os.environ.get("CODEX_TASK_NAME", ""))
    search_parser.add_argument("--task-group", default=os.environ.get("CODEX_TASK_GROUP", ""))
    search_parser.add_argument("--session-id", default="")
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--project-root", type=Path, required=True)
    record_parser.add_argument("--module", required=True)
    record_parser.add_argument("--scope", choices=SCOPE_VALUES, required=True)
    record_parser.add_argument("--change-kind", choices=CHANGE_KIND_VALUES, required=True)
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--reason", required=True)
    record_parser.add_argument("--result", required=True)
    record_parser.add_argument("--verification-status", choices=VERIFICATION_STATUS_VALUES, required=True)
    record_parser.add_argument("--verification", action="append", default=[])
    record_parser.add_argument("--decision", action="append", default=[])
    record_parser.add_argument("--risk", action="append", default=[])
    record_parser.add_argument("--file", action="append", required=True)
    record_parser.add_argument("--supersedes", default="")
    record_parser.add_argument("--task-name", default=os.environ.get("CODEX_TASK_NAME", ""))
    record_parser.add_argument("--task-group", default=os.environ.get("CODEX_TASK_GROUP", ""))
    record_parser.add_argument("--session-id", default="")
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "search":
        output = search_records(args.project_root, args.module, args.file, args.query, args.max_results, args.store, args.include_ambiguous, args.task_name, args.session_id, args.task_group)
    elif args.command == "record":
        output = record_change(args.project_root, args.module, args.scope, args.change_kind, args.summary, args.reason, args.result, args.verification_status, args.file, args.verification, args.decision, args.risk, args.supersedes, args.store, args.vault, None, args.task_name, args.session_id, args.task_group)
    else:
        output = memory_status(args.store, args.vault)
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
