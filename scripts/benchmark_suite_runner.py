#!/usr/bin/env python3
"""Run a frozen alternating Direct/Global benchmark and invoke its strict gate."""

import argparse
import hashlib
import importlib.util
import json
import os
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


TIERS = ("simple", "medium", "complex")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
ENTRY_CONTEXT_ENV = "CODEX_TASK_ANALYZE_ENTRY_CONTEXT"
RUNNER_CONFIG_NAME = "runner-config.json"
PAUSED_EXIT_CODE = 75
QUOTA_RESPONSE_ID = 2
DEFAULT_QUOTA_PAUSE_AT_PERCENT = 80.0
DEFAULT_QUOTA_APP_SERVER_TIMEOUT = 10
RUNTIME_CENSUS_TIMEOUT_SECONDS = 120.0
RUNTIME_CENSUS_BUSY_TIMEOUT_MS = 2000
RUNTIME_CENSUS_RETRY_INTERVAL_SECONDS = 0.1
RUNTIME_CENSUS_QUIESCENCE_SECONDS = 0.2
RESULT_READY_EVENT_KEYS = frozenset({"schema_version", "stage", "workload_id", "benchmark_run_id", "result_path", "child_result_ready_monotonic_ns", "main_thread_id"})
FROZEN_ROUTING_MEMORY_NAME = "model-routing-events.jsonl"
FROZEN_OBSIDIAN_VAULT_NAME = "obsidian-vault"


class BenchmarkRunnerError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class QuotaStatusError(ValueError):
    pass


class BenchmarkPaused(RuntimeError):
    def __init__(self, reason, resumes_at=None, next_pair_id=None):
        super().__init__(reason)
        self.reason = reason
        self.resumes_at = resumes_at
        self.next_pair_id = next_pair_id

    def as_dict(self):
        return {
            "schema_version": 1,
            "status": "paused",
            "reason": self.reason,
            "resumes_at": self.resumes_at,
            "next_pair_id": self.next_pair_id,
        }


def load_gate_module():
    gate_path = Path(__file__).with_name("benchmark_suite_gate.py")
    module_spec = importlib.util.spec_from_file_location("benchmark_suite_runner_gate", gate_path)
    gate_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(gate_module)
    return gate_module


benchmark_suite_gate = load_gate_module()


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def require_quota_number(value, field_name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise QuotaStatusError(f"quota_{field_name}_invalid")
    return float(value)


def require_quota_timestamp(value, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuotaStatusError(f"quota_{field_name}_invalid")
    return value


def parse_quota_response(response):
    if not isinstance(response, dict) or response.get("id") != QUOTA_RESPONSE_ID or not isinstance(response.get("result"), dict):
        raise QuotaStatusError("quota_response_invalid")
    rate_limits = response["result"].get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise QuotaStatusError("quota_rate_limits_missing")
    limit_id = rate_limits.get("limitId")
    if not isinstance(limit_id, str) or not limit_id:
        raise QuotaStatusError("quota_limit_id_invalid")
    reached_type = rate_limits.get("rateLimitReachedType")
    if reached_type is not None and (not isinstance(reached_type, str) or not reached_type):
        raise QuotaStatusError("quota_reached_type_invalid")
    windows = {}
    for window_name in ("primary", "secondary"):
        if window_name not in rate_limits:
            raise QuotaStatusError(f"quota_{window_name}_missing")
        window = rate_limits[window_name]
        if window_name == "secondary" and window is None:
            continue
        if not isinstance(window, dict):
            raise QuotaStatusError(f"quota_{window_name}_missing")
        window_minutes = window.get("windowDurationMins")
        if isinstance(window_minutes, bool) or not isinstance(window_minutes, int) or window_minutes <= 0:
            raise QuotaStatusError(f"quota_{window_name}_window_invalid")
        windows[window_name] = {
            "used_percent": require_quota_number(window.get("usedPercent"), f"{window_name}_used_percent"),
            "window_minutes": window_minutes,
            "resets_at": require_quota_timestamp(window.get("resetsAt"), f"{window_name}_resets_at"),
        }
    return {"limit_id": limit_id, "rate_limit_reached_type": reached_type, **windows}


def read_quota_status(args, codex_home):
    command_environment = os.environ.copy()
    command_environment["CODEX_HOME"] = str(codex_home)
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            [args.codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            cwd=str(args.suite_root),
            env=command_environment,
            shell=False,
            bufsize=0,
        )
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "benchmark-preflight", "version": "1.0.0"}, "capabilities": {}}},
            {"jsonrpc": "2.0", "id": QUOTA_RESPONSE_ID, "method": "account/rateLimits/read", "params": {}},
        ]
        for request in requests:
            process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        process.stdin.flush()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + args.quota_app_server_timeout
        response_buffer = b""
        while time.monotonic() < deadline:
            ready = selector.select(timeout=max(0, deadline - time.monotonic()))
            if not ready:
                break
            response_chunk = os.read(process.stdout.fileno(), 65536)
            if not response_chunk:
                break
            response_buffer += response_chunk
            while b"\n" in response_buffer:
                raw_line, response_buffer = response_buffer.split(b"\n", 1)
                try:
                    response = json.loads(raw_line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(response, dict) and response.get("id") == QUOTA_RESPONSE_ID:
                    if response.get("error") is not None:
                        raise QuotaStatusError("quota_app_server_error")
                    return parse_quota_response(response)
        raise QuotaStatusError("quota_app_server_timeout")
    except OSError as error:
        raise QuotaStatusError("quota_app_server_unavailable") from error
    finally:
        selector.close()
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def quota_pause(status, pause_at_percent, next_pair_id):
    exhausted_windows = [name for name in ("primary", "secondary") if name in status and status[name]["used_percent"] >= pause_at_percent]
    if status["rate_limit_reached_type"] is not None and not exhausted_windows:
        exhausted_windows = ["primary"]
    if status["rate_limit_reached_type"] is None and not exhausted_windows:
        return None
    resumes_at = max(status[name]["resets_at"] for name in exhausted_windows)
    reason = "quota_reached" if status["rate_limit_reached_type"] is not None else "quota_headroom_low"
    return BenchmarkPaused(reason, resumes_at=resumes_at, next_pair_id=next_pair_id)


def require_quota_headroom(args, codex_home, next_pair_id):
    try:
        status = read_quota_status(args, codex_home)
    except QuotaStatusError:
        raise BenchmarkPaused("quota_status_unknown", next_pair_id=next_pair_id)
    pause = quota_pause(status, args.quota_pause_at_percent, next_pair_id)
    if pause is not None:
        raise pause
    return status


def require_file(path, failure_code):
    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        raise BenchmarkRunnerError(failure_code)
    if not resolved_path.is_file():
        raise BenchmarkRunnerError(failure_code)
    return resolved_path


def require_directory(path, failure_code, create=False):
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        raise BenchmarkRunnerError(failure_code)
    if not resolved_path.is_dir():
        raise BenchmarkRunnerError(failure_code)
    return resolved_path


def thread_id_snapshot_sha256(thread_ids):
    return sha256_bytes(benchmark_suite_gate.canonical_json(sorted(thread_ids)).encode("utf-8"))


def sqlite_main_database_is_wal(state_db_path):
    try:
        with state_db_path.open("rb") as database_handle:
            header = database_handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02"


def read_runtime_rollout_snapshot(codex_home):
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return {"available": False, "complete": True, "thread_ids": set()}
    if not sessions_root.is_dir():
        return {"available": True, "complete": False, "thread_ids": set()}
    thread_ids = set()
    try:
        rollout_paths = sorted(sessions_root.glob("**/*.jsonl"))
        for rollout_path in rollout_paths:
            with rollout_path.open(encoding="utf-8") as rollout_handle:
                first_event = json.loads(rollout_handle.readline())
            payload = first_event.get("payload") if isinstance(first_event, dict) and first_event.get("type") == "session_meta" and isinstance(first_event.get("payload"), dict) else None
            thread_id = payload.get("id") if payload is not None else None
            if not isinstance(thread_id, str) or not thread_id or thread_id in thread_ids:
                return {"available": True, "complete": False, "thread_ids": set()}
            thread_ids.add(thread_id)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"available": True, "complete": False, "thread_ids": set()}
    return {"available": True, "complete": True, "thread_ids": thread_ids}


def read_runtime_thread_snapshot(state_db_path, required_thread_id=None, timeout_seconds=RUNTIME_CENSUS_TIMEOUT_SECONDS, diagnostics=None, quiescence_seconds=RUNTIME_CENSUS_QUIESCENCE_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    stable_signature = None
    started = time.monotonic()
    if diagnostics is not None:
        diagnostics.update({"attempt_count": 0, "successful_read_count": 0, "sqlite_error_count": 0, "normal_sqlite_error_count": 0, "immutable_attempt_count": 0, "immutable_success_count": 0, "immutable_error_count": 0, "last_sqlite_error_code": None, "last_sqlite_error_name": None, "last_sqlite_error_category": None, "status": "running", "elapsed_ms": None})
    while True:
        retry_interval_seconds = RUNTIME_CENSUS_RETRY_INTERVAL_SECONDS
        if diagnostics is not None:
            diagnostics["attempt_count"] += 1
        if not state_db_path.exists():
            if required_thread_id is None:
                if diagnostics is not None:
                    diagnostics.update({"status": "complete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                return {"complete": True, "threads": {}}
        else:
            remaining_seconds = max(0.001, deadline - time.monotonic())
            busy_timeout_ms = min(RUNTIME_CENSUS_BUSY_TIMEOUT_MS, max(1, int(remaining_seconds * 1000)))
            connection = None
            try:
                connection = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=busy_timeout_ms / 1000)
                connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                rows = connection.execute("SELECT id, rollout_path, source, model, reasoning_effort, tokens_used FROM threads").fetchall()
            except sqlite3.Error as error:
                rows = None
                error_text = str(error).lower()
                if diagnostics is not None:
                    sqlite_error_name = getattr(error, "sqlite_errorname", None)
                    if sqlite_error_name is None and "locked" in error_text:
                        sqlite_error_name = "SQLITE_BUSY_OR_LOCKED"
                    if "locked" in error_text or "busy" in error_text:
                        sqlite_error_category = "busy_or_locked"
                    elif "disk i/o" in error_text:
                        sqlite_error_category = "disk_io"
                    elif "unable to open" in error_text:
                        sqlite_error_category = "open_failed"
                    else:
                        sqlite_error_category = "operational"
                    diagnostics["sqlite_error_count"] += 1
                    diagnostics["normal_sqlite_error_count"] += 1
                    diagnostics["last_sqlite_error_code"] = getattr(error, "sqlite_errorcode", None)
                    diagnostics["last_sqlite_error_name"] = sqlite_error_name or error.__class__.__name__
                    diagnostics["last_sqlite_error_category"] = sqlite_error_category
            finally:
                if connection is not None:
                    connection.close()
            wal_path = Path(f"{state_db_path}-wal")
            try:
                immutable_safe_before = sqlite_main_database_is_wal(state_db_path) and (not wal_path.exists() or wal_path.stat().st_size == 0)
            except OSError:
                immutable_safe_before = False
            if rows is None and immutable_safe_before:
                immutable_connection = None
                if diagnostics is not None:
                    diagnostics["immutable_attempt_count"] += 1
                try:
                    immutable_connection = sqlite3.connect(f"file:{state_db_path}?mode=ro&immutable=1", uri=True)
                    immutable_rows = immutable_connection.execute("SELECT id, rollout_path, source, model, reasoning_effort, tokens_used FROM threads").fetchall()
                    immutable_safe_after = not wal_path.exists() or wal_path.stat().st_size == 0
                    rows = immutable_rows if immutable_safe_after else None
                    if rows is not None and diagnostics is not None:
                        diagnostics["immutable_success_count"] += 1
                except (OSError, sqlite3.Error) as error:
                    rows = None
                    if diagnostics is not None:
                        diagnostics["immutable_error_count"] += 1
                        diagnostics["last_sqlite_error_code"] = getattr(error, "sqlite_errorcode", None)
                        diagnostics["last_sqlite_error_name"] = getattr(error, "sqlite_errorname", None) or error.__class__.__name__
                        diagnostics["last_sqlite_error_category"] = "immutable_read_failed"
                finally:
                    if immutable_connection is not None:
                        immutable_connection.close()
            if rows is not None:
                if diagnostics is not None:
                    diagnostics["successful_read_count"] += 1
                threads = {row[0]: {"thread_id": row[0], "rollout_path": row[1], "source": row[2], "model": row[3], "effort": row[4], "tokens_used": row[5]} for row in rows if isinstance(row[0], str) and row[0]}
                if required_thread_id is None:
                    if diagnostics is not None:
                        diagnostics.update({"status": "complete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                    return {"complete": True, "threads": threads}
                if required_thread_id in threads:
                    if quiescence_seconds <= 0:
                        if diagnostics is not None:
                            diagnostics.update({"status": "complete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                        return {"complete": True, "threads": threads}
                    current_signature = tuple(sorted((thread_id, thread["tokens_used"]) for thread_id, thread in threads.items()))
                    if current_signature == stable_signature:
                        if diagnostics is not None:
                            diagnostics.update({"status": "complete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
                        return {"complete": True, "threads": threads}
                    stable_signature = current_signature
                    retry_interval_seconds = quiescence_seconds
                else:
                    stable_signature = None
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            if diagnostics is not None:
                diagnostics.update({"status": "incomplete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
            return {"complete": False, "threads": {}}
        time.sleep(min(retry_interval_seconds, remaining_seconds))


def runtime_source_identity(source):
    try:
        source_document = json.loads(source) if isinstance(source, str) else None
    except json.JSONDecodeError:
        source_document = None
    spawn = source_document.get("subagent", {}).get("thread_spawn") if isinstance(source_document, dict) and isinstance(source_document.get("subagent"), dict) else None
    if isinstance(spawn, dict):
        parent_thread_id = spawn.get("parent_thread_id")
        return "subagent", parent_thread_id if isinstance(parent_thread_id, str) and parent_thread_id else None
    return "root", None


def rollout_observation(rollout_path):
    observation = {"rollout_sha256": None, "rollout_model": None, "rollout_effort": None, "rollout_total_tokens": None, "turn_completed": False}
    try:
        rollout_bytes = Path(rollout_path).read_bytes()
        rollout_text = rollout_bytes.decode("utf-8")
        events = [json.loads(raw_line) for raw_line in rollout_text.splitlines() if raw_line.strip()]
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
        return observation
    last_task_started_index = -1
    last_task_complete_index = -1
    valid_jsonl = all(isinstance(event, dict) for event in events)
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("type") == "turn_context":
            observation["rollout_model"] = payload.get("model")
            observation["rollout_effort"] = payload.get("effort")
        elif event.get("type") == "event_msg" and payload.get("type") == "task_started":
            last_task_started_index = event_index
        elif event.get("type") == "event_msg" and payload.get("type") == "task_complete":
            last_task_complete_index = event_index
        elif event.get("type") == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
            total_usage = payload["info"].get("total_token_usage")
            observation["rollout_total_tokens"] = total_usage.get("total_tokens") if isinstance(total_usage, dict) else None
    observation["rollout_sha256"] = sha256_bytes(rollout_bytes)
    observation["turn_completed"] = valid_jsonl and last_task_complete_index >= 0 and last_task_complete_index > last_task_started_index
    return observation


def runtime_session_records(before_snapshot, after_snapshot):
    if before_snapshot["complete"] is not True or after_snapshot["complete"] is not True:
        return []
    new_thread_ids = sorted(set(after_snapshot["threads"]) - set(before_snapshot["threads"]))
    records = []
    for thread_id in new_thread_ids:
        thread = after_snapshot["threads"][thread_id]
        source_kind, parent_thread_id = runtime_source_identity(thread["source"])
        observation = rollout_observation(thread["rollout_path"])
        records.append({"thread_id": thread_id, "parent_thread_id": parent_thread_id, "source_kind": source_kind, "model": thread["model"], "effort": thread["effort"], "tokens_used": thread["tokens_used"], **observation})
    return records


def foreground_session_records(before_snapshot, foreground_snapshot):
    if before_snapshot["complete"] is not True or foreground_snapshot["complete"] is not True:
        return []
    records = []
    for thread_id in sorted(set(foreground_snapshot["threads"]) - set(before_snapshot["threads"])):
        thread = foreground_snapshot["threads"][thread_id]
        source_kind, parent_thread_id = runtime_source_identity(thread["source"])
        records.append({"thread_id": thread_id, "parent_thread_id": parent_thread_id, "source_kind": source_kind, "model": thread["model"], "effort": thread["effort"], "tokens_used": thread["tokens_used"]})
    return records


def state_snapshot_evidence(before_snapshot, after_snapshot):
    before_thread_ids = list(before_snapshot["threads"])
    after_thread_ids = list(after_snapshot["threads"])
    return {"before_complete": before_snapshot["complete"], "after_complete": after_snapshot["complete"], "before_thread_count": len(before_thread_ids) if before_snapshot["complete"] else None, "after_thread_count": len(after_thread_ids) if after_snapshot["complete"] else None, "before_thread_ids_sha256": thread_id_snapshot_sha256(before_thread_ids) if before_snapshot["complete"] else None, "after_thread_ids_sha256": thread_id_snapshot_sha256(after_thread_ids) if after_snapshot["complete"] else None}


def environment_snapshot(codex_home, workdir, sandbox, receipt_runner):
    codex_home = require_directory(codex_home, "codex_home_invalid", create=True)
    config_path = require_file(codex_home / "config.toml", "codex_config_missing")
    agents_path = require_file(codex_home / "AGENTS.md", "codex_agents_missing")
    models_cache_path = require_file(codex_home / "models_cache.json", "codex_models_cache_missing")
    memories_root = require_directory(codex_home / "memories", "codex_memories_missing")
    require_file(memories_root / FROZEN_ROUTING_MEMORY_NAME, "codex_routing_memory_missing")
    require_directory(memories_root / FROZEN_OBSIDIAN_VAULT_NAME, "codex_obsidian_vault_missing")
    catalog = benchmark_suite_gate.catalog_snapshot(codex_home, config_path)
    return {"codex_home": str(codex_home), "config_path": str(config_path), "config_sha256": sha256_bytes(config_path.read_bytes()), "agents_path": str(agents_path), "agents_sha256": sha256_bytes(agents_path.read_bytes()), "models_cache_path": str(models_cache_path), "models_cache_sha256": benchmark_suite_gate.models_cache_sha256(models_cache_path), "memories_root": str(memories_root), "memories_sha256": benchmark_suite_gate.sha256_source_tree(memories_root), "workdir": str(workdir), "sandbox": sandbox, "receipt_runner_path": str(receipt_runner), "receipt_runner_sha256": sha256_bytes(receipt_runner.read_bytes()), **catalog}


def validate_suite_local_catalogs(suite_root, environment):
    codex_home = Path(environment["codex_home"])
    for catalog_name in ("skills", "plugins"):
        catalog_path = codex_home / catalog_name
        if catalog_path.is_symlink():
            raise BenchmarkRunnerError(f"{catalog_name}_catalog_symlink_forbidden")
    catalog_roots = [Path(environment["skills_catalog_root"]), Path(environment["plugins_catalog_root"]), *(Path(source["root"]) for source in environment["marketplace_catalog_sources"])]
    for catalog_root in catalog_roots:
        try:
            catalog_root.resolve(strict=True).relative_to(suite_root)
        except (OSError, ValueError):
            raise BenchmarkRunnerError("catalog_root_outside_suite")


def source_pointer_for_expected(expected_document):
    if isinstance(expected_document.get("source_files"), (str, list)):
        return "/source_files"
    if isinstance(expected_document.get("source"), str):
        return "/source"
    raise BenchmarkRunnerError("expected_source_pointer_required")


def ensure_fresh_outputs(suite_root):
    output_paths = [suite_root / "suite-plan.json", suite_root / RUNNER_CONFIG_NAME, suite_root / "summary.json", suite_root / "raw", suite_root / "manifests"]
    if any(path.exists() for path in output_paths):
        raise BenchmarkRunnerError("suite_outputs_already_exist")


def parse_tier_repeat_counts(value):
    parts = [part.strip() for part in value.split(",") if part.strip()]
    parsed_counts = {}
    for part in parts:
        if part.count("=") != 1:
            raise BenchmarkRunnerError("tier_repeats_invalid")
        tier, count_text = [component.strip() for component in part.split("=", 1)]
        if tier not in TIERS or tier in parsed_counts:
            raise BenchmarkRunnerError("tier_repeats_invalid")
        try:
            repeat_count = int(count_text)
        except ValueError:
            raise BenchmarkRunnerError("tier_repeats_invalid")
        if repeat_count < 2 or repeat_count % 2 != 0:
            raise BenchmarkRunnerError("tier_repeats_invalid")
        parsed_counts[tier] = repeat_count
    if set(parsed_counts) != set(TIERS):
        raise BenchmarkRunnerError("tier_repeats_invalid")
    return parsed_counts


def repeat_counts_from_args(args):
    if args.tier_repeats is not None:
        return parse_tier_repeat_counts(args.tier_repeats)
    if args.repeat_count < 2 or args.repeat_count % 2 != 0:
        raise BenchmarkRunnerError("repeat_count_must_be_even")
    return {tier: args.repeat_count for tier in TIERS}


def build_frozen_plan(args, require_fresh=True):
    suite_root = require_directory(args.suite_root, "suite_root_invalid")
    snapshot_root = require_directory(suite_root / "snapshot", "snapshot_missing")
    prompts_root = require_directory(suite_root / "prompts", "prompts_missing")
    expected_root = require_directory(suite_root / "expected", "expected_missing")
    receipt_runner = require_file(args.receipt_runner, "receipt_runner_missing")
    tier_repeat_counts = repeat_counts_from_args(args)
    if args.timeout <= 0:
        raise BenchmarkRunnerError("timeout_invalid")
    direct_home = require_directory(args.direct_codex_home, "direct_codex_home_invalid", create=True)
    global_home = require_directory(args.global_codex_home, "global_codex_home_invalid", create=True)
    if direct_home == global_home:
        raise BenchmarkRunnerError("codex_homes_must_differ")
    if require_fresh:
        ensure_fresh_outputs(suite_root)
    source_snapshot_sha256 = benchmark_suite_gate.sha256_source_tree(snapshot_root)
    direct_environment = environment_snapshot(direct_home, snapshot_root, args.sandbox, receipt_runner)
    global_environment = environment_snapshot(global_home, snapshot_root, args.sandbox, receipt_runner)
    validate_suite_local_catalogs(suite_root, direct_environment)
    validate_suite_local_catalogs(suite_root, global_environment)
    arm_pairs = {"direct": f"{args.direct_model}|{args.direct_effort}", "global": f"{args.auto_entry_model}|{args.auto_entry_effort}"}
    if arm_pairs != benchmark_suite_gate.ARM_ENTRY_PAIRS:
        raise BenchmarkRunnerError("benchmark_pair_contract")
    suite_identity = {"suite_root": str(suite_root), "source_snapshot_sha256": source_snapshot_sha256, "tier_repeat_counts": tier_repeat_counts, "arm_pairs": arm_pairs, "direct": {"config_sha256": direct_environment["config_sha256"], "agents_sha256": direct_environment["agents_sha256"], "visible_catalog_sha256": direct_environment["visible_catalog_sha256"]}, "global": {"config_sha256": global_environment["config_sha256"], "agents_sha256": global_environment["agents_sha256"], "visible_catalog_sha256": global_environment["visible_catalog_sha256"]}}
    suite_digest = sha256_bytes(benchmark_suite_gate.canonical_json(suite_identity).encode("utf-8"))[:16]
    suite_id = f"benchmark-suite-{suite_digest}"
    tier_inputs = {}
    for tier in TIERS:
        prompt_path = require_file(prompts_root / f"{tier}.txt", f"prompt_missing_{tier}")
        expected_path = require_file(expected_root / f"{tier}.json", f"expected_missing_{tier}")
        try:
            prompt_bytes = prompt_path.read_bytes()
            prompt_text = prompt_bytes.decode("utf-8")
            expected_document = benchmark_suite_gate.strict_json_loads(expected_path.read_bytes())
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise BenchmarkRunnerError(f"input_invalid_{tier}")
        if not prompt_text.strip() or not isinstance(expected_document, dict):
            raise BenchmarkRunnerError(f"input_invalid_{tier}")
        source_pointer = source_pointer_for_expected(expected_document)
        try:
            benchmark_suite_gate.validate_source_files(expected_document, snapshot_root, source_pointer)
        except benchmark_suite_gate.BenchmarkGateError as error:
            raise BenchmarkRunnerError(f"expected_source_invalid_{tier}_{error.code}")
        tier_inputs[tier] = {"prompt_path": prompt_path, "prompt_text": prompt_text, "prompt_sha256": sha256_bytes(prompt_bytes), "expected_path": expected_path, "expected_sha256": sha256_bytes(expected_path.read_bytes()), "source_files_pointer": source_pointer}
    runs = []
    order_index = 1
    for repeat_index in range(1, max(tier_repeat_counts.values()) + 1):
        arm_order = ("direct", "global") if repeat_index % 2 == 1 else ("global", "direct")
        for tier in TIERS:
            if repeat_index > tier_repeat_counts[tier]:
                continue
            tier_input = tier_inputs[tier]
            pair_id = f"{tier}-r{repeat_index:02d}"
            for arm in arm_order:
                run_id = f"{pair_id}-{arm}"
                raw_root = suite_root / "raw" / run_id
                receipt_path = raw_root / "receipt.json"
                result_path = raw_root / "result.json"
                evidence_path = raw_root / "evidence.json"
                role = "result-producer"
                environment = direct_environment if arm == "direct" else global_environment
                selected_pair = arm_pairs[arm]
                receipt_spec = {"path": str(receipt_path), "pair": selected_pair, "role": role, "bind_result": True, "workload_prompt_sha256": tier_input["prompt_sha256"]}
                run_plan = {"run_id": run_id, "pair_id": pair_id, "tier": tier, "repeat_index": repeat_index, "arm": arm, "order_index": order_index, "prompt_path": str(tier_input["prompt_path"]), "prompt_sha256": tier_input["prompt_sha256"], "expected_result_path": str(tier_input["expected_path"]), "expected_sha256": tier_input["expected_sha256"], "result_path": str(result_path), "evidence_path": str(evidence_path), "receipts": [receipt_spec], "selected_entry_pair": selected_pair, "entry_execution_mode": "executed", "source_root": str(snapshot_root), "source_files_pointer": tier_input["source_files_pointer"], "source_snapshot_sha256": source_snapshot_sha256, "environment": environment}
                if arm == "global" and repeat_index == 1:
                    probe_root = suite_root / "entry-invariance-probes" / tier
                    run_plan["dual_entry_probe"] = {"schema_version": 1, "entry_pair": benchmark_suite_gate.SOL_ENTRY_PROBE_PAIR, "receipt_path": str(probe_root / "receipt.json"), "result_path": str(probe_root / "result.json"), "summary_path": str(probe_root / "summary.json")}
                runs.append(run_plan)
                order_index += 1
    plan = {"schema_version": benchmark_suite_gate.SCHEMA_VERSION, "suite_id": suite_id, "tier_repeat_counts": tier_repeat_counts, "runs": runs} if args.tier_repeats is not None else {"schema_version": benchmark_suite_gate.SCHEMA_VERSION, "suite_id": suite_id, "repeat_count": args.repeat_count, "runs": runs}
    benchmark_suite_gate.validate_plan(plan)
    return suite_root, tier_inputs, plan


def resolve_executable(command):
    resolved = shutil.which(command)
    if resolved is None:
        raise BenchmarkRunnerError("codex_bin_missing")
    return require_file(Path(resolved), "codex_bin_missing")


def runner_config(args, plan):
    runner_path = Path(__file__).resolve(strict=True)
    gate_path = runner_path.with_name("benchmark_suite_gate.py").resolve(strict=True)
    codex_bin_path = resolve_executable(args.codex_bin)
    quota_home = require_directory(args.quota_codex_home, "quota_codex_home_invalid")
    return {
        "schema_version": 1,
        "plan_sha256": sha256_bytes((benchmark_suite_gate.canonical_json(plan) + "\n").encode("utf-8")),
        "benchmark_runner_path": str(runner_path),
        "benchmark_runner_sha256": sha256_bytes(runner_path.read_bytes()),
        "benchmark_gate_path": str(gate_path),
        "benchmark_gate_sha256": sha256_bytes(gate_path.read_bytes()),
        "codex_bin_path": str(codex_bin_path),
        "codex_bin_sha256": sha256_bytes(codex_bin_path.read_bytes()),
        "direct_pair": f"{args.direct_model}|{args.direct_effort}",
        "auto_entry_pair": f"{args.auto_entry_model}|{args.auto_entry_effort}",
        "timeout": args.timeout,
        "outer_timeout_grace": args.outer_timeout_grace,
        "poll_interval_ms": args.poll_interval_ms,
        "quota_pause_at_percent": args.quota_pause_at_percent,
        "quota_app_server_timeout": args.quota_app_server_timeout,
        "quota_codex_home": str(quota_home),
    }


def canonical_document_bytes(document):
    return (benchmark_suite_gate.canonical_json(document) + "\n").encode("utf-8")


def load_immutable_json(path, failure_code):
    try:
        document_bytes = path.read_bytes()
        document = benchmark_suite_gate.strict_json_loads(document_bytes)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise BenchmarkRunnerError(failure_code)
    if not isinstance(document, dict) or document_bytes != canonical_document_bytes(document):
        raise BenchmarkRunnerError(failure_code)
    return document, document_bytes


def load_resume_plan(args):
    suite_root = require_directory(args.suite_root, "suite_root_invalid")
    if (suite_root / "summary.json").exists() or (suite_root / "manifests").exists():
        raise BenchmarkRunnerError("suite_already_finalized")
    saved_plan, saved_plan_bytes = load_immutable_json(suite_root / "suite-plan.json", "resume_plan_invalid")
    try:
        benchmark_suite_gate.validate_plan(saved_plan)
    except benchmark_suite_gate.BenchmarkGateError as error:
        raise BenchmarkRunnerError(f"resume_{error.code}")
    rebuilt_root, tier_inputs, rebuilt_plan = build_frozen_plan(args, require_fresh=False)
    if rebuilt_root != suite_root or rebuilt_plan != saved_plan:
        raise BenchmarkRunnerError("resume_plan_drift")
    saved_config, _ = load_immutable_json(suite_root / RUNNER_CONFIG_NAME, "resume_runner_config_invalid")
    if saved_config != runner_config(args, saved_plan):
        raise BenchmarkRunnerError("resume_runner_config_drift")
    return suite_root, tier_inputs, saved_plan, saved_plan_bytes


def load_optional_receipt(receipt_path):
    try:
        receipt_document = benchmark_suite_gate.strict_json_loads(receipt_path.read_bytes())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return receipt_document if isinstance(receipt_document, dict) else None


def parse_result_ready_event_line(raw_line):
    try:
        event = benchmark_suite_gate.strict_json_loads(raw_line)
    except (TypeError, ValueError):
        return None
    return event if isinstance(event, dict) and event.get("stage") == "result-ready" else None


def ordered_pair_groups(plan):
    ordered_runs = sorted(plan["runs"], key=lambda value: value["order_index"])
    groups = []
    for index in range(0, len(ordered_runs), 2):
        pair_runs = ordered_runs[index:index + 2]
        if len(pair_runs) != 2 or pair_runs[0]["pair_id"] != pair_runs[1]["pair_id"]:
            raise BenchmarkRunnerError("plan_pair_order_not_contiguous")
        groups.append(pair_runs)
    return groups


def planned_run_paths(run_plan):
    receipt_paths = [Path(spec["path"]) for spec in run_plan["receipts"]]
    required_paths = [*receipt_paths, Path(run_plan["result_path"]), Path(run_plan["evidence_path"])]
    raw_root = receipt_paths[0].parent
    return raw_root, required_paths


def existing_run_state(plan_root, suite_id, plan_sha256, run_plan):
    raw_root, required_paths = planned_run_paths(run_plan)
    if not raw_root.exists() and not any(path.exists() for path in required_paths):
        return "absent", None
    if not raw_root.is_dir() or not all(path.is_file() for path in required_paths):
        return "contaminated", None
    manifest = benchmark_suite_gate.evaluate_run(plan_root, suite_id, plan_sha256, run_plan)
    if manifest["acceptance_status"] != "pass" or manifest["completion"] != "complete" or manifest["metrics_complete"] is not True:
        return "contaminated", manifest
    return "complete", manifest


def completed_pair_prefix(suite_root, plan, plan_bytes):
    plan_sha256 = sha256_bytes(plan_bytes)
    completed_runs = []
    pending_seen = False
    for pair_runs in ordered_pair_groups(plan):
        pair_states = [existing_run_state(suite_root, plan["suite_id"], plan_sha256, run_plan)[0] for run_plan in pair_runs]
        if pair_states == ["complete", "complete"]:
            if pending_seen:
                raise BenchmarkRunnerError("cohort_contaminated_nonprefix")
            completed_runs.extend(pair_runs)
            continue
        if pair_states == ["absent", "absent"]:
            pending_seen = True
            continue
        raise BenchmarkRunnerError("cohort_contaminated_partial_pair")
    return completed_runs


def existing_run_summary(run_plan):
    receipt = load_optional_receipt(Path(run_plan["receipts"][0]["path"])) or {}
    return {
        "run_id": run_plan["run_id"],
        "arm": run_plan["arm"],
        "exit_code": receipt.get("exit_code"),
        "timed_out": receipt.get("failure_class") == "timeout",
        "thread_id_present": isinstance(receipt.get("thread_id"), str) and bool(receipt.get("thread_id")),
        "resumed_existing": True,
    }


def execute_run(args, run_plan, prompt_text):
    receipt_path = Path(run_plan["receipts"][0]["path"])
    result_path = Path(run_plan["result_path"])
    evidence_path = Path(run_plan["evidence_path"])
    receipt_path.parent.mkdir(parents=True, exist_ok=False)
    environment = run_plan["environment"]
    codex_home = Path(environment["codex_home"])
    state_db_path = (codex_home / "state_5.sqlite").absolute()
    run_model, run_effort = run_plan["selected_entry_pair"].split("|", 1)
    command = [sys.executable, environment["receipt_runner_path"], "run", "--model", run_model, "--effort", run_effort, "--workload-id", run_plan["run_id"], "--output", str(receipt_path), "--result-output", str(result_path), "--workdir", environment["workdir"], "--state-db", str(state_db_path), "--codex-bin", args.codex_bin, "--sandbox", args.sandbox, "--timeout", str(args.timeout)]
    if run_plan["arm"] == "direct":
        command.extend(["--direct-task", "--benchmark-run-id", f"benchmark-{run_plan['run_id']}"])
    else:
        command.extend(["--bootstrap-task", "--benchmark-run-id", f"benchmark-{run_plan['run_id']}", "--benchmark-prompt-path", run_plan["prompt_path"]])
    command_environment = os.environ.copy()
    command_environment.pop(ENTRY_CONTEXT_ENV, None)
    command_environment["CODEX_HOME"] = str(codex_home)
    command_environment["CODEX_MODEL_ROUTING_MEMORY"] = str(codex_home / "memories" / FROZEN_ROUTING_MEMORY_NAME)
    command_environment["CODEX_OBSIDIAN_VAULT"] = str(codex_home / "memories" / FROZEN_OBSIDIAN_VAULT_NAME)
    stdout_path = receipt_path.parent / "runner.stdout.log"
    stderr_path = receipt_path.parent / "runner.stderr.log"
    before_census_diagnostics = {}
    before_rollout_snapshot = read_runtime_rollout_snapshot(codex_home)
    before_snapshot = read_runtime_thread_snapshot(state_db_path, diagnostics=before_census_diagnostics)
    started_ns = time.monotonic_ns()
    result_ready_events = []
    result_ready_event_failures = []
    process_exit_code = 127
    outer_timed_out = False
    process = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_handle, text=True, cwd=environment["workdir"], env=command_environment, shell=False, bufsize=1)
            process.stdin.write(prompt_text)
            process.stdin.close()
            deadline_ns = started_ns + (args.timeout + args.outer_timeout_grace) * 1_000_000_000
            output_selector = selectors.DefaultSelector()
            output_selector.register(process.stdout, selectors.EVENT_READ)
            while output_selector.get_map():
                for selected_key, _selected_mask in output_selector.select(timeout=args.poll_interval_ms / 1000):
                    raw_line = selected_key.fileobj.readline()
                    if not raw_line:
                        output_selector.unregister(selected_key.fileobj)
                        continue
                    stdout_handle.write(raw_line)
                    stdout_handle.flush()
                    event = parse_result_ready_event_line(raw_line)
                    if event is not None:
                        child_ready_ns = event.get("child_result_ready_monotonic_ns")
                        main_thread_id = event.get("main_thread_id")
                        event_valid = set(event) == RESULT_READY_EVENT_KEYS and event.get("schema_version") == 2 and event.get("workload_id") == run_plan["run_id"] and event.get("benchmark_run_id") == f"benchmark-{run_plan['run_id']}" and event.get("result_path") == str(result_path) and not isinstance(child_ready_ns, bool) and isinstance(child_ready_ns, int) and child_ready_ns >= 0 and isinstance(main_thread_id, str) and bool(main_thread_id)
                        if not event_valid or result_ready_events:
                            result_ready_event_failures.append("result_ready_event_invalid")
                        else:
                            runner_monotonic_ns = time.monotonic_ns()
                            foreground_census_diagnostics = {}
                            foreground_snapshot = read_runtime_thread_snapshot(state_db_path, required_thread_id=main_thread_id, diagnostics=foreground_census_diagnostics, quiescence_seconds=0)
                            result_ready_events.append({"event": event, "runner_monotonic_ns": runner_monotonic_ns, "foreground_snapshot": foreground_snapshot, "foreground_census_diagnostics": foreground_census_diagnostics})
                if time.monotonic_ns() >= deadline_ns and process.poll() is None:
                    outer_timed_out = True
                    process.kill()
            process.wait()
            process_exit_code = process.returncode if process.returncode is not None else 124
            output_selector.close()
            process.stdout.close()
    except OSError:
        process_exit_code = 127
    producer_finished_ns = time.monotonic_ns()
    receipt = load_optional_receipt(receipt_path)
    if len(result_ready_events) != 1 or result_ready_event_failures:
        raise BenchmarkRunnerError("receipt_result_ready_event_invalid")
    result_ready_event = result_ready_events[0]["event"]
    first_result_ns = result_ready_events[0]["runner_monotonic_ns"]
    foreground_snapshot = result_ready_events[0]["foreground_snapshot"]
    foreground_census_diagnostics = result_ready_events[0]["foreground_census_diagnostics"]
    if not isinstance(receipt, dict):
        raise BenchmarkRunnerError("receipt_result_ready_event_invalid")
    child_result_ready_ns = receipt.get("result_ready_monotonic_ns")
    thread_id = receipt.get("thread_id") if isinstance(receipt.get("thread_id"), str) and receipt.get("thread_id") else None
    if child_result_ready_ns != result_ready_event["child_result_ready_monotonic_ns"] or thread_id != result_ready_event["main_thread_id"]:
        raise BenchmarkRunnerError("receipt_result_ready_event_invalid")
    if not started_ns <= first_result_ns <= producer_finished_ns:
        raise BenchmarkRunnerError("receipt_result_ready_timing_invalid")
    receipt["child_result_ready_monotonic_ns"] = child_result_ready_ns
    receipt["result_ready_monotonic_ns"] = first_result_ns
    receipt["result_ready_clock"] = "benchmark-runner-monotonic"
    receipt["result_ready_event_sequence"] = 1
    benchmark_suite_gate.atomic_write_json(receipt_path, receipt)
    receipt_timed_out = bool(receipt and receipt.get("failure_class") == "timeout")
    timed_out = outer_timed_out or receipt_timed_out
    route_attempts = receipt.get("route_attempts") if receipt and isinstance(receipt.get("route_attempts"), list) else []
    reroutes = receipt.get("reroutes") if receipt and isinstance(receipt.get("reroutes"), list) else []
    after_census_diagnostics = {}
    after_snapshot = read_runtime_thread_snapshot(state_db_path, required_thread_id=thread_id, diagnostics=after_census_diagnostics)
    after_rollout_snapshot = read_runtime_rollout_snapshot(codex_home)
    rollout_new_thread_ids = after_rollout_snapshot["thread_ids"] - before_rollout_snapshot["thread_ids"] if before_rollout_snapshot["complete"] and after_rollout_snapshot["complete"] else set()
    database_new_thread_ids = set(after_snapshot["threads"]) - set(before_snapshot["threads"]) if before_snapshot["complete"] and after_snapshot["complete"] else set()
    rollout_cross_check_passed = thread_id is None or (after_rollout_snapshot["available"] and before_rollout_snapshot["complete"] and after_rollout_snapshot["complete"] and before_snapshot["complete"] and after_snapshot["complete"] and rollout_new_thread_ids == database_new_thread_ids)
    after_census_diagnostics.update({"rollout_file_census_available": after_rollout_snapshot["available"], "rollout_file_census_complete": before_rollout_snapshot["complete"] and after_rollout_snapshot["complete"], "rollout_new_session_count": len(rollout_new_thread_ids), "rollout_db_cross_check": "pass" if rollout_cross_check_passed else "fail"})
    if not rollout_cross_check_passed:
        after_snapshot = {"complete": False, "threads": {}}
    runtime_sessions = runtime_session_records(before_snapshot, after_snapshot)
    launched_session_ids = [runtime_session["thread_id"] for runtime_session in runtime_sessions]
    retry_session_ids = [thread_id] if thread_id is not None and len(route_attempts) > 1 else []
    fallback_session_ids = [thread_id] if thread_id is not None and reroutes else []
    repair_session_ids = [thread_id] if thread_id is not None and receipt.get("node_role") == "repair" else []
    producer_complete = process_exit_code == 0 and not timed_out
    evidence = {"schema_version": benchmark_suite_gate.SCHEMA_VERSION, "run_id": run_plan["run_id"], "started_monotonic_ns": started_ns, "first_result_monotonic_ns": first_result_ns, "producer_finished_monotonic_ns": producer_finished_ns, "producer_process_exit_code": process_exit_code, "producer_timed_out": timed_out, "producer_complete": producer_complete, "foreground_main_thread_id": result_ready_event["main_thread_id"], "foreground_state_snapshot": state_snapshot_evidence(before_snapshot, foreground_snapshot), "foreground_sessions": foreground_session_records(before_snapshot, foreground_snapshot), "launched_session_ids": launched_session_ids, "retry_session_ids": retry_session_ids, "fallback_session_ids": fallback_session_ids, "repair_session_ids": repair_session_ids, "state_snapshot": state_snapshot_evidence(before_snapshot, after_snapshot), "runtime_sessions": runtime_sessions}
    benchmark_suite_gate.atomic_write_json(evidence_path, evidence)
    benchmark_suite_gate.atomic_write_json(receipt_path.parent / "census-diagnostics.json", {"schema_version": 2, "before": before_census_diagnostics, "foreground": foreground_census_diagnostics, "after": after_census_diagnostics})
    return {"run_id": run_plan["run_id"], "arm": run_plan["arm"], "exit_code": process_exit_code, "timed_out": timed_out, "thread_id_present": thread_id is not None}


def execute_dual_entry_probe(args, run_plan, prompt_text):
    probe = run_plan.get("dual_entry_probe")
    if probe is None:
        return None
    receipt_path = Path(probe["receipt_path"])
    result_path = Path(probe["result_path"])
    summary_path = Path(probe["summary_path"])
    probe_root = summary_path.parent
    probe_root.mkdir(parents=True, exist_ok=False)
    environment = run_plan["environment"]
    codex_home = Path(environment["codex_home"])
    probe_run_id = f"{run_plan['run_id']}-sol-entry-probe"
    command = [sys.executable, environment["receipt_runner_path"], "run", "--model", "gpt-5.6-sol", "--effort", "ultra", "--workload-id", probe_run_id, "--output", str(receipt_path), "--result-output", str(result_path), "--workdir", environment["workdir"], "--state-db", str((codex_home / "state_5.sqlite").absolute()), "--codex-bin", args.codex_bin, "--sandbox", args.sandbox, "--timeout", str(args.timeout), "--bootstrap-task", "--benchmark-run-id", f"benchmark-{probe_run_id}", "--benchmark-prompt-path", run_plan["prompt_path"]]
    command_environment = os.environ.copy()
    command_environment.pop(ENTRY_CONTEXT_ENV, None)
    command_environment["CODEX_HOME"] = str(codex_home)
    command_environment["CODEX_MODEL_ROUTING_MEMORY"] = str(codex_home / "memories" / FROZEN_ROUTING_MEMORY_NAME)
    command_environment["CODEX_OBSIDIAN_VAULT"] = str(codex_home / "memories" / FROZEN_OBSIDIAN_VAULT_NAME)
    summary = {"schema_version": 1, "status": "fail", "reason": "probe_execution_failed", "tier": run_plan["tier"], "primary_run_id": run_plan["run_id"], "primary_entry_pair": run_plan["selected_entry_pair"], "probe_entry_pair": probe["entry_pair"], "expected_result_sha256": run_plan["expected_sha256"], "primary_result_sha256": None, "probe_result_sha256": None, "primary_route_signature": None, "probe_route_signature": None, "route_signature_match": False, "capability_assignment_match": False}
    try:
        process = subprocess.run(command, input=prompt_text, text=True, capture_output=True, cwd=environment["workdir"], env=command_environment, shell=False, timeout=args.timeout + args.outer_timeout_grace, check=False)
        primary_receipt = load_optional_receipt(Path(run_plan["receipts"][0]["path"]))
        primary_result_bytes = Path(run_plan["result_path"]).read_bytes()
        probe_receipt = load_optional_receipt(receipt_path)
        probe_result_bytes = result_path.read_bytes()
        primary_execution = primary_receipt.get("benchmark_selected_execution") if isinstance(primary_receipt, dict) else None
        probe_execution = probe_receipt.get("benchmark_selected_execution") if isinstance(probe_receipt, dict) else None
        primary_signature = primary_execution.get("route_signature") if isinstance(primary_execution, dict) else None
        probe_signature = probe_execution.get("route_signature") if isinstance(probe_execution, dict) else None
        summary.update({"primary_result_sha256": sha256_bytes(primary_result_bytes), "probe_result_sha256": sha256_bytes(probe_result_bytes), "primary_route_signature": primary_signature, "probe_route_signature": probe_signature, "route_signature_match": primary_signature == probe_signature, "capability_assignment_match": isinstance(primary_signature, dict) and isinstance(probe_signature, dict) and primary_signature.get("capability_assignment") == probe_signature.get("capability_assignment")})
        probe_status_fields = {"status": "pass", "failure_class": None, "node_type": "bootstrap-task", "requested_pair": probe["entry_pair"], "effective_pair": probe["entry_pair"], "benchmark_prompt_file_verified": True, "benchmark_auto_launch_verified": True, "benchmark_auto_workspace_count": 1, "benchmark_auto_bridge_result_verified": True, "result_published": True, "duplicate_result_detected": False}
        probe_result_text = probe_result_bytes.decode("utf-8")
        probe_result_text = probe_result_text[:-1] if probe_result_text.endswith("\n") else probe_result_text
        probe_receipt_valid = isinstance(probe_receipt, dict) and all(probe_receipt.get(field) == expected_value for field, expected_value in probe_status_fields.items()) and probe_receipt.get("output_sha256") == sha256_bytes(probe_result_text.encode("utf-8"))
        exact_output = summary["primary_result_sha256"] == run_plan["expected_sha256"] and summary["probe_result_sha256"] == run_plan["expected_sha256"]
        if process.returncode == 0 and probe_receipt_valid and exact_output and summary["route_signature_match"] and summary["capability_assignment_match"]:
            summary.update({"status": "pass", "reason": "exact_output_and_route_signature_match"})
        elif process.returncode == 0:
            summary["reason"] = "probe_contract_mismatch"
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    benchmark_suite_gate.atomic_write_json(summary_path, summary)
    return summary


def validate_runtime_args(args):
    if args.outer_timeout_grace < 0 or args.poll_interval_ms < 1 or args.quota_app_server_timeout < 1:
        raise BenchmarkRunnerError("runner_timing_options_invalid")
    if isinstance(args.quota_pause_at_percent, bool) or not 0 < args.quota_pause_at_percent <= 100:
        raise BenchmarkRunnerError("quota_pause_threshold_invalid")


def run_suite(args):
    validate_runtime_args(args)
    args.codex_bin = str(resolve_executable(args.codex_bin))
    if args.resume:
        suite_root, tier_inputs, plan, plan_bytes = load_resume_plan(args)
        completed_runs = completed_pair_prefix(suite_root, plan, plan_bytes)
        if len(completed_runs) == len(plan["runs"]):
            raise BenchmarkRunnerError("suite_execution_already_complete")
    else:
        suite_root, tier_inputs, plan = build_frozen_plan(args)
        plan_bytes = canonical_document_bytes(plan)
        completed_runs = []
    pair_groups = ordered_pair_groups(plan)
    completed_run_ids = {run_plan["run_id"] for run_plan in completed_runs}
    pending_groups = [pair_runs for pair_runs in pair_groups if pair_runs[0]["run_id"] not in completed_run_ids]
    next_pair_id = pending_groups[0][0]["pair_id"]
    require_quota_headroom(args, require_directory(args.quota_codex_home, "quota_codex_home_invalid"), next_pair_id)
    plan_path = suite_root / "suite-plan.json"
    if not args.resume:
        benchmark_suite_gate.atomic_write_json(plan_path, plan)
        benchmark_suite_gate.atomic_write_json(suite_root / RUNNER_CONFIG_NAME, runner_config(args, plan))
    run_summaries = [existing_run_summary(run_plan) for run_plan in completed_runs]
    first_pending_pair = True
    for pair_runs in pending_groups:
        if not first_pending_pair:
            require_quota_headroom(args, require_directory(args.quota_codex_home, "quota_codex_home_invalid"), pair_runs[0]["pair_id"])
        first_pending_pair = False
        for run_plan in pair_runs:
            try:
                benchmark_suite_gate.validate_environment_snapshot(run_plan["environment"])
            except benchmark_suite_gate.BenchmarkGateError as error:
                raise BenchmarkRunnerError(f"cohort_contaminated_gate_{error.code}")
            run_summary = execute_run(args, run_plan, tier_inputs[run_plan["tier"]]["prompt_text"])
            run_summaries.append(run_summary)
            probe_summary = execute_dual_entry_probe(args, run_plan, tier_inputs[run_plan["tier"]]["prompt_text"])
            if probe_summary is not None:
                run_summary["sol_entry_probe_status"] = probe_summary["status"]
            run_manifest = benchmark_suite_gate.evaluate_run(suite_root, plan["suite_id"], sha256_bytes(plan_bytes), run_plan)
            receipt = load_optional_receipt(Path(run_plan["receipts"][0]["path"]))
            failure_class = receipt.get("failure_class") if receipt else None
            if failure_class == "availability":
                raise BenchmarkRunnerError("cohort_contaminated_availability")
            if run_manifest["acceptance_status"] != "pass" or run_manifest["completion"] != "complete" or run_manifest["metrics_complete"] is not True:
                gate_failures = run_manifest["gate"]["failures"]
                gate_failure = gate_failures[0] if gate_failures else "run_rejected"
                raise BenchmarkRunnerError(f"cohort_contaminated_gate_{gate_failure}")
    summary = benchmark_suite_gate.evaluate_suite(plan_path, suite_root / "manifests", suite_root / "summary.json")
    return {"schema_version": 1, "suite_id": plan["suite_id"], "plan": str(plan_path), "summary": str(suite_root / "summary.json"), "overall_status": summary["overall_status"], "run_count": len(run_summaries), "runs": run_summaries}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run a frozen repeated Direct/Global benchmark with strict generated acceptance.")
    parser.add_argument("--suite-root", type=Path, required=True)
    repeat_group = parser.add_mutually_exclusive_group()
    repeat_group.add_argument("--repeat-count", type=int, default=6)
    repeat_group.add_argument("--tier-repeats", help="Even per-tier counts, for example simple=4,medium=2,complex=2.")
    parser.add_argument("--direct-model", default="gpt-5.6-sol")
    parser.add_argument("--direct-effort", default="ultra")
    parser.add_argument("--auto-entry-model", default="gpt-5.6-luna")
    parser.add_argument("--auto-entry-effort", default="max")
    parser.add_argument("--direct-codex-home", type=Path, required=True)
    parser.add_argument("--global-codex-home", type=Path, required=True)
    parser.add_argument("--receipt-runner", type=Path, default=Path(__file__).with_name("model_execution_receipt.py"))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--sandbox", choices=SANDBOXES, default="danger-full-access")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--outer-timeout-grace", type=int, default=60)
    parser.add_argument("--poll-interval-ms", type=int, default=10)
    parser.add_argument("--resume", action="store_true", help="Resume only a pristine frozen suite at the next unstarted complete pair.")
    parser.add_argument("--quota-codex-home", type=Path, default=Path.home() / ".codex", help="Live Codex home used only for no-model account quota reads; cohort homes remain untouched.")
    parser.add_argument("--quota-pause-at-percent", type=float, default=DEFAULT_QUOTA_PAUSE_AT_PERCENT, help="Pause before a pair when either account window reaches this used percentage.")
    parser.add_argument("--quota-app-server-timeout", type=int, default=DEFAULT_QUOTA_APP_SERVER_TIMEOUT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        result = run_suite(args)
    except BenchmarkPaused as paused:
        print(json.dumps(paused.as_dict(), separators=(",", ":")))
        return PAUSED_EXIT_CODE
    except (BenchmarkRunnerError, benchmark_suite_gate.BenchmarkGateError) as error:
        print(json.dumps({"schema_version": 1, "status": "error", "failure": error.code}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["overall_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
