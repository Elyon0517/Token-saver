#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path


DEFAULT_VAULT = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "MyAILLM"
CANONICAL_SEARCH_FOLDERS = ("Skills", "Projects", "Knowledge")
LEGACY_SEARCH_FOLDERS = ("Skills", "Projects", "AestheticTaste", "KnowledgeAreas")
STOP_WORDS = {"about", "after", "also", "and", "any", "for", "from", "have", "into", "like", "more", "need", "only", "other", "should", "task", "that", "the", "then", "this", "use", "user", "with"}
SENSITIVE_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{8,}|api[_-]?key|password|token\s*=|-----BEGIN|/Users/|/home/|[A-Za-z]:\\)", re.IGNORECASE)


def resolve_vault(vault=None):
    selected = Path(vault).expanduser() if vault else Path(os.environ.get("CODEX_OBSIDIAN_VAULT", DEFAULT_VAULT)).expanduser()
    return selected.resolve() if selected.exists() and selected.is_dir() else None


def _query_terms(query):
    terms = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", query.lower()):
        if len(word) >= 3 and word not in STOP_WORDS and word not in terms:
            terms.append(word)
    return terms[:12]


def _bounded_text(path, limit=65536):
    try:
        size = path.stat().st_size
        with path.open(encoding="utf-8", errors="ignore") as handle:
            if size <= limit:
                return handle.read()
            head = handle.read(limit * 3 // 4)
            handle.seek(max(size - limit // 4, 0))
            return head + "\n" + handle.read(limit // 4)
    except OSError:
        return ""


def _search_folders(vault_path):
    canonical_root = vault_path / "Knowledge"
    legacy_detected = (vault_path / "KnowledgeAreas").exists() or (vault_path / "AestheticTaste").exists()
    if canonical_root.exists() or not legacy_detected:
        return CANONICAL_SEARCH_FOLDERS
    return LEGACY_SEARCH_FOLDERS


def _path_priority(relative_path):
    root = relative_path.parts[0] if relative_path.parts else ""
    return {"Skills": 30, "Projects": 30, "Knowledge": 26, "AestheticTaste": 26, "KnowledgeAreas": 26}.get(root, 0)


def _best_snippets(text, terms, limit=2):
    scored_lines = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or SENSITIVE_PATTERN.search(line):
            continue
        lowered = line.lower()
        hits = sum(1 for term in terms if term in lowered)
        if hits:
            scored_lines.append((hits, -line_number, line[:280]))
    scored_lines.sort(reverse=True)
    return [line for _, _, line in scored_lines[:limit]]


def search_memory(query, vault=None, max_results=6, max_chars=3500):
    vault_path = resolve_vault(vault)
    if vault_path is None:
        return {"status": "unavailable", "provider": "none", "matches": [], "digest": ""}
    terms = _query_terms(query)
    if not terms:
        return {"status": "no_matches", "provider": "obsidian", "matches": [], "digest": ""}
    candidates = []
    for folder_name in _search_folders(vault_path):
        folder = vault_path / folder_name
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md"))[:500]:
            relative_path = path.relative_to(vault_path)
            if path.name in {"History.md", "Activity Index.md", "index.md", "instruction.md"}:
                continue
            if SENSITIVE_PATTERN.search(relative_path.as_posix()):
                continue
            text = _bounded_text(path)
            lowered_text = text.lower()
            path_text = str(relative_path).lower()
            content_hits = sum(min(lowered_text.count(term), 5) for term in terms)
            path_hits = sum(1 for term in terms if term in path_text)
            unique_hits = sum(1 for term in terms if term in lowered_text or term in path_text)
            if not content_hits and not path_hits:
                continue
            snippets = _best_snippets(text, terms)
            score = _path_priority(relative_path) + unique_hits * 8 + path_hits * 12 + content_hits * 2
            candidates.append({"path": relative_path.as_posix(), "score": score, "snippets": snippets})
    candidates.sort(key=lambda match: (-match["score"], match["path"]))
    matches = candidates[:max(1, min(max_results, 12))]
    digest_lines = []
    used_chars = 0
    for match in matches:
        snippet = match["snippets"][0] if match["snippets"] else "Related page title/path match."
        line = f"[{match['path']}] {snippet}"
        if used_chars + len(line) > max_chars:
            break
        digest_lines.append(line)
        used_chars += len(line) + 1
    return {"status": "ok" if matches else "no_matches", "provider": "obsidian", "query_terms": terms, "matches": matches, "digest": "\n".join(digest_lines)}


def main():
    parser = argparse.ArgumentParser(description="Optional bounded Obsidian knowledge search")
    parser.add_argument("--vault", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--max-results", type=int, default=6)
    search_parser.add_argument("--max-chars", type=int, default=3500)
    args = parser.parse_args()
    output = search_memory(args.query, args.vault, args.max_results, args.max_chars)
    print(json.dumps(output, separators=(",", ":")))


if __name__ == "__main__":
    main()
