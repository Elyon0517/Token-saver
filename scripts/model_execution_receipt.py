#!/usr/bin/env python3
import argparse
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from tempfile import mkstemp

try:
    from routing_policy import MODEL_EFFORTS, parse_model_effort_pair, pair_text
except ModuleNotFoundError:
    import importlib.util

    _routing_policy_path = Path(__file__).with_name("routing_policy.py")
    _routing_policy_spec = importlib.util.spec_from_file_location("task_analyze_routing_policy", _routing_policy_path)
    _routing_policy = importlib.util.module_from_spec(_routing_policy_spec)
    _routing_policy_spec.loader.exec_module(_routing_policy)
    MODEL_EFFORTS = _routing_policy.MODEL_EFFORTS
    parse_model_effort_pair = _routing_policy.parse_model_effort_pair
    pair_text = _routing_policy.pair_text

try:
    from benchmark_prompt_contract import auto_benchmark_execution_prompt
except ModuleNotFoundError:
    import importlib.util

    _benchmark_prompt_path = Path(__file__).with_name("benchmark_prompt_contract.py")
    _benchmark_prompt_spec = importlib.util.spec_from_file_location("task_analyze_benchmark_prompt_contract", _benchmark_prompt_path)
    _benchmark_prompt_contract = importlib.util.module_from_spec(_benchmark_prompt_spec)
    _benchmark_prompt_spec.loader.exec_module(_benchmark_prompt_contract)
    auto_benchmark_execution_prompt = _benchmark_prompt_contract.auto_benchmark_execution_prompt

import importlib.util as _local_producer_importlib_util

_LOCAL_PRODUCER_SPEC = _local_producer_importlib_util.spec_from_file_location("token_saver_local_producer", Path(__file__).resolve().with_name("local_producer.py"))
local_producer = _local_producer_importlib_util.module_from_spec(_LOCAL_PRODUCER_SPEC)
_LOCAL_PRODUCER_SPEC.loader.exec_module(local_producer)

ROUTE_MARKERS = {"LOCKED_ROUTE_NODE", "ENDING_TASK_WORKER"}
RESULT_READY_BEGIN = "RESULT_READY_BEGIN"
RESULT_READY_END = "RESULT_READY_END"
RUNTIME_FAILURES = {"availability", "timeout", "protocol", "telemetry", "execution", "receipt"}
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
BENCHMARK_RUN_ID_PATTERN = re.compile(r"^benchmark-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
ENTRY_CONTEXT_ENV = "CODEX_TASK_ANALYZE_ENTRY_CONTEXT"
NODE_ROLES = {"entry", "result-producer", "verification", "repair", "ending", "benchmark-baseline"}
DISPATCHER_FIXED_ROLES = {"verification", "repair", "ending"}
_NODE_AUTHORIZATION = contextvars.ContextVar("task_analyze_receipt_node_authorization", default=None)


def route_node_lifecycle_boundary(marker):
    if marker == "LOCKED_ROUTE_NODE":
        return (
            "This is the result node only. Do not create, launch, wait for, or summarize End/Fix tasks; "
            "the entry parent owns post-result lifecycle after this node's passing receipt."
        )
    if marker == "ENDING_TASK_WORKER":
        return "Execute only the assigned Ending check; never create a nested End/Fix task or restart routing."
    raise ValueError(f"unsupported route marker {marker}")


class ReceiptAuthorizationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def entry_context_active():
    return ENTRY_CONTEXT_ENV in os.environ


def receipt_node_role(args):
    if getattr(args, "entry_task", False):
        return "entry"
    if getattr(args, "direct_task", False) or getattr(args, "bootstrap_task", False):
        return "result-producer"
    explicit_role = getattr(args, "node_role", None)
    if explicit_role is not None:
        if explicit_role not in NODE_ROLES - {"entry"}:
            raise ReceiptAuthorizationError("node_role_invalid")
        return explicit_role
    return "ending" if getattr(args, "route_marker", "LOCKED_ROUTE_NODE") == "ENDING_TASK_WORKER" else "result-producer"


def receipt_node_type(args):
    if getattr(args, "entry_task", False):
        return "task-analyze-entry"
    if getattr(args, "direct_task", False):
        return "direct-task"
    if getattr(args, "bootstrap_task", False):
        return "bootstrap-task"
    return "locked-route-node"


@contextmanager
def adaptive_producer_authorization():
    token = _NODE_AUTHORIZATION.set(("adaptive-runner", "result-producer"))
    try:
        yield
    finally:
        _NODE_AUTHORIZATION.reset(token)


@contextmanager
def dispatcher_adaptive_result_authorization():
    token = _NODE_AUTHORIZATION.set(("dispatcher-adaptive-recommendation", "result-producer"))
    try:
        yield
    finally:
        _NODE_AUTHORIZATION.reset(token)


@contextmanager
def dispatcher_node_authorization(node_role):
    if node_role not in DISPATCHER_FIXED_ROLES:
        raise ReceiptAuthorizationError("dispatcher_node_role_invalid")
    token = _NODE_AUTHORIZATION.set(("dispatcher", node_role))
    try:
        yield
    finally:
        _NODE_AUTHORIZATION.reset(token)


def authorize_receipt_run(args):
    node_role = receipt_node_role(args)
    inherited_context_active = entry_context_active()
    direct_task = bool(getattr(args, "direct_task", False))
    bootstrap_task = bool(getattr(args, "bootstrap_task", False))
    entry_task = bool(getattr(args, "entry_task", False))
    benchmark_run_id = getattr(args, "benchmark_run_id", None)
    stream_result_ready = bool(getattr(args, "stream_result_ready", False))
    if sum((direct_task, bootstrap_task, entry_task)) > 1:
        raise ReceiptAuthorizationError("benchmark_task_mode_conflict")
    if benchmark_run_id is not None and not (direct_task or bootstrap_task):
        raise ReceiptAuthorizationError("benchmark_run_id_requires_benchmark_task")
    if stream_result_ready and node_role != "result-producer":
        raise ReceiptAuthorizationError("stream_result_ready_requires_result_producer")
    if stream_result_ready and getattr(args, "result_output", None) is None:
        raise ReceiptAuthorizationError("stream_result_ready_requires_result_output")
    if direct_task or bootstrap_task:
        if getattr(args, "node_role", None) not in {None, "result-producer"}:
            raise ReceiptAuthorizationError("direct_task_node_role_invalid" if direct_task else "bootstrap_task_node_role_invalid")
        if inherited_context_active:
            raise ReceiptAuthorizationError("direct_task_entry_context_forbidden" if direct_task else "bootstrap_task_entry_context_forbidden")
        if not isinstance(benchmark_run_id, str) or BENCHMARK_RUN_ID_PATTERN.fullmatch(benchmark_run_id) is None:
            raise ReceiptAuthorizationError("direct_task_benchmark_run_id_required" if direct_task else "bootstrap_task_benchmark_run_id_required")
        if benchmark_run_id != f"benchmark-{args.workload_id}":
            raise ReceiptAuthorizationError("benchmark_run_id_workload_mismatch")
        context_active = False
        source = "benchmark-direct" if direct_task else "benchmark-global-inline"
    else:
        context_active = bool(entry_task or inherited_context_active)
        if entry_task and inherited_context_active:
            raise ReceiptAuthorizationError("recursive_entry_task_forbidden")
        if entry_task:
            source = "entry-launch"
        elif not context_active:
            source = "outside-entry-context"
        else:
            authorization = _NODE_AUTHORIZATION.get()
            if node_role == "result-producer" and authorization == ("adaptive-runner", "result-producer"):
                source = "adaptive-runner"
            elif node_role == "result-producer" and authorization == ("dispatcher-adaptive-recommendation", "result-producer"):
                source = "dispatcher-adaptive-recommendation"
            elif node_role in DISPATCHER_FIXED_ROLES and authorization == ("dispatcher", node_role):
                source = "dispatcher"
            else:
                raise ReceiptAuthorizationError("entry_context_adaptive_runner_required" if node_role in {"result-producer", "benchmark-baseline"} else "entry_context_dispatcher_authorization_required")
    args._receipt_node_role = node_role
    args._receipt_entry_context_active = context_active
    args._receipt_authorization_source = source
    return {"node_role": node_role, "entry_context_active": context_active, "authorization_status": "authorized", "authorization_source": source}


def receipt_authorization_fields(args, status=None, source=None, reason=None):
    authorization_source = source if source is not None else getattr(args, "_receipt_authorization_source", None)
    authorization_status = status if status is not None else "authorized" if authorization_source is not None else "not-evaluated"
    fields = {"node_role": getattr(args, "_receipt_node_role", receipt_node_role(args)), "entry_context_active": bool(getattr(args, "_receipt_entry_context_active", getattr(args, "entry_task", False) or entry_context_active())), "authorization_status": authorization_status, "authorization_source": authorization_source}
    if reason is not None:
        fields["authorization_reason"] = reason
    return fields


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_fallback_pairs(values):
    if not isinstance(values, list):
        raise ValueError("allow_fallback must be a list")
    pairs = []
    for value in values:
        pair = parse_model_effort_pair(value)
        if pair not in pairs:
            pairs.append(pair)
    return [pair_text(model, effort) for model, effort in pairs]


def immediate_operational_fallback(receipt):
    if not isinstance(receipt, dict) or receipt.get("status") == "pass" or receipt.get("result_published") is True or receipt.get("turn_completed") is True:
        return False
    tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
    if tokens.get("total_tokens") != 0 or receipt.get("failure_class") not in RUNTIME_FAILURES:
        return False
    if receipt.get("pre_execution_failure") is True:
        return True
    return not any(receipt.get(field) for field in ("resolved_model", "resolved_pair", "effective_model", "effective_pair"))


def annotate_operational_fallback(receipt):
    if not isinstance(receipt, dict):
        return receipt
    tokens = receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
    no_runtime_identity = not any(receipt.get(field) for field in ("resolved_model", "resolved_pair", "effective_model", "effective_pair"))
    if (
        receipt.get("status") != "pass"
        and receipt.get("turn_completed") is not True
        and no_runtime_identity
        and receipt.get("failure_class") in RUNTIME_FAILURES
        and tokens.get("total_tokens") is None
        and not any(isinstance(tokens.get(field), int) and tokens.get(field) > 0 for field in TOKEN_FIELDS)
    ):
        tokens = {field: 0 for field in TOKEN_FIELDS}
        receipt["tokens"] = tokens
    if receipt.get("status") != "pass" and receipt.get("turn_completed") is not True and tokens.get("total_tokens") == 0 and no_runtime_identity:
        receipt["pre_execution_failure"] = True
    receipt["failure_stage"] = "pre_execution" if receipt.get("pre_execution_failure") is True else "completed" if receipt.get("turn_completed") is True else "runtime"
    receipt["fallback_eligible"] = immediate_operational_fallback(receipt)
    attempts = receipt.get("route_attempts")
    if isinstance(attempts, list) and attempts:
        attempts[-1]["pre_execution_failure"] = bool(receipt.get("pre_execution_failure") is True)
        attempts[-1]["fallback_eligible"] = receipt["fallback_eligible"]
        attempts[-1]["failure_stage"] = receipt["failure_stage"]
    return receipt


def infer_failure_class(process, status, turn_completed, turn_failed, availability_failure, model_match, effort_match, pair_match, token_consistent):
    if status == "pass":
        return None
    if availability_failure:
        return "availability"
    if process is None:
        return "execution"
    if process.returncode != 0 or turn_failed or not turn_completed:
        return "execution"
    if not model_match or not effort_match or not pair_match:
        return "protocol"
    if not token_consistent:
        return "protocol"
    return "execution"


def route_attempt_summary(
    requested_pair,
    resolved_pair,
    effective_pair,
    status,
    model_match,
    effort_match,
    pair_match,
    process_elapsed_ms,
    task_complete,
    execution_failure_class,
    tokens=None,
    thread_id=None,
):
    resolved_pair = pair_text(*resolved_pair) if resolved_pair else None
    effective_pair = pair_text(*effective_pair) if effective_pair else None
    if (
        status != "pass"
        and execution_failure_class == "execution"
        and not effective_pair
        and not resolved_pair
    ):
        executed_pair = requested_pair
    else:
        executed_pair = effective_pair or resolved_pair or requested_pair
    return {
        "requested_pair": requested_pair,
        "resolved_pair": resolved_pair,
        "effective_pair": effective_pair,
        "executed_pair": executed_pair,
        "status": "pass" if status == "pass" else "fail",
        "failure_class": execution_failure_class if status != "pass" else None,
        "model_match": bool(model_match),
        "effort_match": bool(effort_match),
        "pair_match": bool(pair_match),
        "process_elapsed_ms": process_elapsed_ms,
        "model_turn_duration_ms": task_complete.get("duration_ms"),
        "time_to_first_token_ms": task_complete.get("time_to_first_token_ms"),
        "tokens": dict(tokens or {}),
        "thread_id": thread_id,
        "pre_execution_failure": False,
    }


def failed_run_receipt(args, failure_class):
    requested_pair = f"{args.model}|{args.effort}"
    attempt = {
        "requested_pair": requested_pair,
        "resolved_pair": None,
        "effective_pair": None,
        "executed_pair": requested_pair,
        "status": "fail",
        "failure_class": failure_class,
        "model_match": False,
        "effort_match": False,
        "pair_match": False,
        "process_elapsed_ms": None,
        "model_turn_duration_ms": None,
        "time_to_first_token_ms": None,
        "tokens": {},
        "thread_id": None,
        "pre_execution_failure": False,
    }
    receipt = {
        "schema_version": 1,
        "proof_level": "local-operational-not-cryptographic",
        "workload_id": args.workload_id,
        "node_type": receipt_node_type(args),
        "requested_model": args.model,
        "requested_effort": args.effort,
        "requested_pair": requested_pair,
        "resolved_model": None,
        "resolved_effort": None,
        "effective_model": None,
        "effective_pair": None,
        "allowed_fallback_pairs": normalize_fallback_pairs(getattr(args, "allow_fallback", [])),
        "model_match": False,
        "effort_match": False,
        "pair_match": False,
        "tokens": {},
        "metrics_complete": False,
        "tokens_lower_bound": False,
        "process_elapsed_ms": None,
        "turn_completed": False,
        "status": "fail",
        "failure_class": failure_class,
        "route_attempts": [attempt],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "limitations": "Execution failed before complete runtime metadata was available; no model execution success is claimed.",
    }
    receipt.update(receipt_authorization_fields(args))
    if getattr(args, "direct_task", False) or getattr(args, "bootstrap_task", False):
        receipt["benchmark_run_id"] = getattr(args, "benchmark_run_id", None)
    return annotate_operational_fallback(receipt)


def rejected_run_receipt(args, error):
    receipt = failed_run_receipt(args, "authorization")
    receipt.update(receipt_authorization_fields(args, status="rejected", source=None, reason=error.code))
    receipt["limitations"] = "Execution was rejected before a model launch because the entry-context node authorization contract was not satisfied."
    return receipt


def parse_stdout_events(stdout_text):
    summary = {"thread_id": None, "usage": {}, "output_hash": None, "turn_completed": False, "turn_failed": False, "availability_failure": False}
    for raw_line in stdout_text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            summary["thread_id"] = event.get("thread_id")
        elif event_type == "turn.completed":
            summary["usage"] = event.get("usage", {})
            summary["turn_completed"] = True
            summary["turn_failed"] = False
        elif event_type in {"turn.failed", "error"}:
            summary["turn_failed"] = True
            summary["turn_completed"] = False
            failure_message = event.get("message") or (event.get("error") or {}).get("message") or ""
            summary["availability_failure"] = summary["availability_failure"] or any(marker in failure_message.lower() for marker in ("usage limit", "rate limit", "purchase more credits", "capacity", "temporarily unavailable"))
        elif event_type == "item.completed" and isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message":
            summary["output_hash"] = sha256_text(event["item"].get("text", ""))
    return summary


def extract_last_agent_message(stdout_text):
    last_message = None
    for raw_line in stdout_text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message":
            last_message = event["item"].get("text", "")
    return last_message


def atomic_write_private_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def emit_benchmark_result_ready_event(args, result_path, child_result_ready_monotonic_ns, main_thread_id):
    event = {"schema_version": 2, "stage": "result-ready", "workload_id": args.workload_id, "benchmark_run_id": args.benchmark_run_id, "result_path": str(result_path), "child_result_ready_monotonic_ns": child_result_ready_monotonic_ns, "main_thread_id": main_thread_id}
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


def reject_duplicate_json_keys(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def reject_nonstandard_json_constant(_value):
    raise ValueError("nonstandard JSON constant")


def strict_json_loads(payload):
    return json.loads(payload, object_pairs_hook=reject_duplicate_json_keys, parse_constant=reject_nonstandard_json_constant)


def completed_agent_message(raw_line):
    try:
        event = json.loads(raw_line)
    except (TypeError, json.JSONDecodeError):
        return None
    item = event.get("item") if isinstance(event, dict) and event.get("type") == "item.completed" else None
    return item.get("text") if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str) else None


def is_turn_completed_event(raw_line):
    try:
        event = json.loads(raw_line)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(event, dict) and event.get("type") == "turn.completed"


def matching_benchmark_agent_message(raw_line):
    try:
        message_text = completed_agent_message(raw_line)
        candidate_document = strict_json_loads(message_text) if isinstance(message_text, str) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return message_text if isinstance(candidate_document, dict) else None


def matching_production_agent_message(raw_line):
    message_text = completed_agent_message(raw_line)
    if not isinstance(message_text, str):
        return None
    message_lines = message_text.splitlines()
    if len(message_lines) < 3 or message_lines[0].strip() != RESULT_READY_BEGIN or message_lines[-1].strip() != RESULT_READY_END:
        return None
    result_text = "\n".join(message_lines[1:-1])
    return result_text if result_text.strip() else None


def matching_stream_result(raw_line, stream_mode):
    return matching_benchmark_agent_message(raw_line) if stream_mode == "benchmark" else matching_production_agent_message(raw_line)


def run_streaming_result_process(command, execution_prompt, args, command_environment, stream_mode):
    stdout_lines = []
    stderr_lines = []
    agent_messages = []
    result_messages = []
    result_ready_monotonic_ns = []
    thread_ids = []
    duplicate_result_detected = []
    stream_errors = []
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=args.workdir, env=command_environment, shell=False, bufsize=1)

    def publish_result(result_message):
        if result_messages:
            duplicate_result_detected.append(True)
            return
        atomic_write_private_text(args.result_output, result_message + "\n")
        if not result_ready_monotonic_ns:
            result_ready_monotonic_ns.append(time.monotonic_ns())
        result_messages.append(result_message)
        result_ready_callback = getattr(args, "result_ready_callback", None)
        if callable(result_ready_callback):
            if stream_mode == "benchmark":
                result_ready_callback(args.result_output, result_ready_monotonic_ns[0], thread_ids[0] if len(thread_ids) == 1 else None)
            else:
                result_ready_callback(args.result_output, result_ready_monotonic_ns[0])

    def drain_stdout():
        try:
            for raw_line in process.stdout:
                stdout_lines.append(raw_line)
                try:
                    stdout_event = json.loads(raw_line)
                except json.JSONDecodeError:
                    stdout_event = None
                if isinstance(stdout_event, dict) and stdout_event.get("type") == "thread.started" and isinstance(stdout_event.get("thread_id"), str) and stdout_event["thread_id"] and stdout_event["thread_id"] not in thread_ids:
                    thread_ids.append(stdout_event["thread_id"])
                agent_message = completed_agent_message(raw_line)
                if agent_message is not None:
                    agent_messages.append(agent_message)
                result_message = matching_stream_result(raw_line, stream_mode)
                if result_message is not None:
                    publish_result(result_message)
                elif stream_mode == "production" and is_turn_completed_event(raw_line) and not result_messages and agent_messages:
                    publish_result(agent_messages[-1])
        except (OSError, ValueError) as error:
            stream_errors.append(error)
        finally:
            process.stdout.close()

    def drain_stderr():
        try:
            for raw_line in process.stderr:
                stderr_lines.append(raw_line)
        except OSError as error:
            stream_errors.append(error)
        finally:
            process.stderr.close()

    stdout_thread = threading.Thread(target=drain_stdout, name=f"{stream_mode}-receipt-stdout", daemon=True)
    stderr_thread = threading.Thread(target=drain_stderr, name=f"{stream_mode}-receipt-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        process.stdin.write(execution_prompt)
        process.stdin.close()
        process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    except (BrokenPipeError, OSError):
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
    if stream_errors:
        raise stream_errors[0]
    final_result_message = result_messages[0] if result_messages else None
    return {"process": process, "stdout": "".join(stdout_lines), "stderr": "".join(stderr_lines), "timed_out": timed_out, "matching_result": final_result_message, "result_ready_monotonic_ns": result_ready_monotonic_ns[0] if result_ready_monotonic_ns else None, "duplicate_result_detected": bool(duplicate_result_detected)}


def read_thread_state(state_db_path, thread_id):
    if not state_db_path.exists() or not thread_id:
        return None
    for attempt in range(20):
        connection = None
        operational_error = None
        row = None
        try:
            connection = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
            row = connection.execute("SELECT rollout_path, model, reasoning_effort, tokens_used, cli_version, model_provider, source FROM threads WHERE id = ?", (thread_id,)).fetchone()
        except sqlite3.OperationalError as error:
            operational_error = error
        finally:
            if connection is not None:
                connection.close()
        immutable_fallback_allowed = not Path(f"{state_db_path}-wal").exists() and not Path(f"{state_db_path}-shm").exists()
        if operational_error is not None and immutable_fallback_allowed:
            connection = None
            try:
                connection = sqlite3.connect(f"file:{state_db_path}?mode=ro&immutable=1", uri=True)
                row = connection.execute("SELECT rollout_path, model, reasoning_effort, tokens_used, cli_version, model_provider, source FROM threads WHERE id = ?", (thread_id,)).fetchone()
                operational_error = None
            except sqlite3.OperationalError as error:
                operational_error = error
            finally:
                if connection is not None:
                    connection.close()
        if operational_error is not None:
            if attempt == 19:
                raise operational_error
            time.sleep(0.1)
            continue
        if row:
            return {"rollout_path": Path(row[0]), "model": row[1], "effort": row[2], "tokens_used": row[3], "cli_version": row[4], "model_provider": row[5], "source": row[6]}
        time.sleep(0.1)
    return None


def parse_rollout_allowlist(rollout_path):
    observed = {"turn_context": None, "reroutes": [], "usage": None, "task_complete": None, "availability": None}
    if not rollout_path or not rollout_path.exists():
        return observed
    with rollout_path.open(encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "turn_context":
                observed["turn_context"] = {"turn_id": payload.get("turn_id"), "model": payload.get("model"), "effort": payload.get("effort")}
            elif event_type == "event_msg" and payload.get("type") == "model_reroute":
                observed["reroutes"].append({"from_model": payload.get("from_model"), "to_model": payload.get("to_model"), "reason": payload.get("reason")})
            elif event_type == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
                observed["usage"] = payload["info"].get("total_token_usage")
            if event_type == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("rate_limits"), dict):
                rate_limits = payload["rate_limits"]
                credits = rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else {}
                observed["availability"] = {"limit_id": rate_limits.get("limit_id"), "has_credits": credits.get("has_credits"), "unlimited": credits.get("unlimited"), "rate_limit_reached_type": rate_limits.get("rate_limit_reached_type")}
            elif event_type == "event_msg" and payload.get("type") == "task_complete":
                observed["task_complete"] = {"duration_ms": payload.get("duration_ms"), "time_to_first_token_ms": payload.get("time_to_first_token_ms")}
    return observed


def normalize_usage(usage):
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    cached_input_tokens = usage.get("cached_input_tokens", 0) if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    reasoning_output_tokens = usage.get("reasoning_output_tokens", 0) if isinstance(usage, dict) else None
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    total_tokens = input_tokens + output_tokens if total_tokens is None and input_tokens is not None and output_tokens is not None else total_tokens
    uncached_input_tokens = input_tokens - cached_input_tokens if input_tokens is not None and cached_input_tokens is not None else None
    return {"input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens, "uncached_input_tokens": uncached_input_tokens, "output_tokens": output_tokens, "reasoning_output_tokens": reasoning_output_tokens, "total_tokens": total_tokens}


def benchmark_auto_launch_evidence(cache_root, workload_sha256, expected_entry_pair):
    empty_evidence = {"verified": False, "workspace_count": 0, "bridge_result_verified": False, "bridge_result_text": None, "selected_execution": None}
    try:
        claim = json.loads((cache_root / "adaptive-entry-launch.json").read_text(encoding="utf-8"))
        workspaces = sorted(path for path in cache_root.glob("source-copy-*") if path.is_dir())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty_evidence
    verified = isinstance(claim, dict) and set(claim) == {"schema_version", "workload_sha256", "entry_pair"} and claim.get("schema_version") == 3 and claim.get("workload_sha256") == workload_sha256 and claim.get("entry_pair") == expected_entry_pair and len(workspaces) == 1
    result_text = None
    bridge_result_verified = False
    if verified:
        output_root = workspaces[0] / "Cache" / "task-analyze" / "bridge-output"
        try:
            candidate_text = (output_root / "result.json").read_text(encoding="utf-8")
            bridge_receipt = strict_json_loads((output_root / "receipt.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            receipt_result = candidate_text[:-1] if candidate_text.endswith("\n") else candidate_text
            result_payload = receipt_result.lstrip("\ufeff")
            if result_payload.startswith("Complexity:"):
                disclosure_end = result_payload.find("\n\n")
                result_payload = result_payload[disclosure_end + 2:] if disclosure_end >= 0 else ""
            try:
                candidate_document = strict_json_loads(result_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate_document = None
            selected_execution = None
            try:
                from benchmark_auto_entry_bridge import receipt_projection
            except ModuleNotFoundError:
                bridge_module_path = Path(__file__).with_name("benchmark_auto_entry_bridge.py")
                bridge_spec = __import__("importlib.util").util.spec_from_file_location("benchmark_auto_entry_bridge_for_receipt", bridge_module_path)
                bridge_module = __import__("importlib.util").util.module_from_spec(bridge_spec)
                bridge_spec.loader.exec_module(bridge_module)
                receipt_projection = bridge_module.receipt_projection
            try:
                selected_execution = receipt_projection(bridge_receipt)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                selected_execution = None
            bridge_result_verified = isinstance(candidate_document, dict) and isinstance(bridge_receipt, dict) and bridge_receipt.get("status") == "pass" and bridge_receipt.get("result_published") is True and bridge_receipt.get("duplicate_result_detected") is False and bridge_receipt.get("output_sha256") == sha256_text(result_payload) and selected_execution is not None
            if bridge_result_verified:
                result_text = result_payload
    return {"verified": verified, "workspace_count": len(workspaces), "bridge_result_verified": bridge_result_verified, "bridge_result_text": result_text, "selected_execution": selected_execution if bridge_result_verified else None}


def run_receipt(args, prompt_text):
    authorization = authorize_receipt_run(args)
    if local_producer.handles_model(getattr(args, "model", None)):
        # The local endpoint is answered in-process: no subprocess, no JSON event stream,
        # no thread database, no rollout. None of the reconstruction below applies, so
        # local_producer builds its own receipt from what it can actually observe.
        # Effort and fallback are passed through rather than dropped -- it refuses a pair
        # it cannot honor instead of substituting its own.
        return local_producer.run(
            prompt_text,
            workload_id=getattr(args, "workload_id", "local-producer"),
            effort=getattr(args, "effort", None),
            allow_fallback=normalize_fallback_pairs(getattr(args, "allow_fallback", [])),
        )
    benchmark_prompt_path = None
    if getattr(args, "bootstrap_task", False):
        try:
            benchmark_prompt_path = Path(args.benchmark_prompt_path).expanduser().resolve(strict=True)
        except (OSError, TypeError):
            raise ReceiptAuthorizationError("bootstrap_task_prompt_path_invalid")
        if not benchmark_prompt_path.is_file() or sha256_text(benchmark_prompt_path.read_text(encoding="utf-8")) != sha256_text(prompt_text):
            raise ReceiptAuthorizationError("bootstrap_task_prompt_path_mismatch")
    requested_pair_tuple = parse_model_effort_pair(pair_text(args.model, args.effort))
    allowed_fallback_pairs = normalize_fallback_pairs(getattr(args, "allow_fallback", []))
    requested_pair = requested_pair_tuple
    allowed_pairs = [requested_pair] + [parse_model_effort_pair(value) for value in allowed_fallback_pairs]
    # Everything from here down is Codex-shaped on purpose: the command, the `--json`
    # event stream parsed out of stdout, the thread database read, and the rollout file.
    # They are one unit -- the receipt's evidence comes from all four agreeing -- so the
    # command is built inline rather than behind an abstraction that could imply the rest
    # is swappable. It is not. See README "What this engine runs on".
    command = [
        args.codex_bin,
        "exec",
        "--model",
        args.model,
        "-c",
        f'model_reasoning_effort="{args.effort}"',
        "-c",
        "features.multi_agent=false",
        "--sandbox",
        args.sandbox,
        "--skip-git-repo-check",
        "--json",
    ]
    command.extend(["--ignore-user-config"] if args.ignore_user_config else [])
    command.append("-")
    if getattr(args, "bootstrap_task", False):
        execution_prompt = auto_benchmark_execution_prompt(prompt_text, pair_text(args.model, args.effort))
    elif args.entry_task or getattr(args, "direct_task", False):
        execution_prompt = prompt_text
    else:
        marker = getattr(args, "route_marker", "LOCKED_ROUTE_NODE")
        lifecycle_boundary = route_node_lifecycle_boundary(marker)
        canonical_workdir = Path(args.workdir).expanduser().resolve()
        execution_prompt = f"{marker}\nThis is a bounded node from an already-returned Task Analyze route. Execute the assigned node directly; do not restart Task Analyze or redesign the route. {lifecycle_boundary} Your process is already in the canonical working directory `{canonical_workdir}`. Use that current directory directly; do not reconstruct, shorten, or guess another absolute workdir in tool calls.\n\n{prompt_text}"
    stream_result_ready = bool(getattr(args, "stream_result_ready", False))
    if stream_result_ready:
        execution_prompt += f"\n\nWhen the requested result is complete, emit exactly one complete agent message in this shape and do not use these markers for progress/commentary:\n{RESULT_READY_BEGIN}\n<complete user-facing result>\n{RESULT_READY_END}"
    started = time.perf_counter_ns()
    timed_out = False
    benchmark_stream_ready = bool((getattr(args, "direct_task", False) or getattr(args, "bootstrap_task", False)) and getattr(args, "result_output", None) is not None)
    streamed_matching_result = None
    result_ready_monotonic_ns = None
    duplicate_result_detected = False
    if benchmark_stream_ready and not callable(getattr(args, "result_ready_callback", None)):
        args.result_ready_callback = partial(emit_benchmark_result_ready_event, args)
    if (benchmark_stream_ready or stream_result_ready) and args.result_output.exists():
        args.result_output.unlink()
    command_environment = None
    benchmark_cache_root = None
    if args.entry_task:
        command_environment = os.environ.copy()
        command_environment[ENTRY_CONTEXT_ENV] = "1"
    elif getattr(args, "direct_task", False) or getattr(args, "bootstrap_task", False):
        command_environment = os.environ.copy()
        command_environment.pop(ENTRY_CONTEXT_ENV, None)
        if getattr(args, "bootstrap_task", False):
            command_environment["CODEX_AUTO_BENCHMARK_PROMPT_PATH"] = str(benchmark_prompt_path)
            command_environment["CODEX_AUTO_BENCHMARK_WORKLOAD_SHA256"] = sha256_text(prompt_text)
            command_environment["CODEX_AUTO_BENCHMARK_CODEX_BIN"] = args.codex_bin
            command_environment["CODEX_AUTO_BENCHMARK_CHILD_TIMEOUT"] = str(max(args.timeout - 90, 60))
            command_environment["CODEX_AUTO_BENCHMARK_PYTHON"] = str(Path(sys.executable).resolve(strict=True))
            command_environment["CODEX_AUTO_BENCHMARK_ENTRY_MODEL"] = args.model
            command_environment["CODEX_AUTO_BENCHMARK_ENTRY_EFFORT"] = args.effort
            benchmark_cache_root = Path(args.result_output).parent / "auto-route-cache" if args.result_output is not None else Path(args.workdir) / "Cache" / "task-analyze" / args.workload_id
            command_environment["CODEX_AUTO_BENCHMARK_CACHE_ROOT"] = str(benchmark_cache_root)
    if benchmark_stream_ready or stream_result_ready:
        stream_mode = "benchmark" if benchmark_stream_ready else "production"
        streamed_process = run_streaming_result_process(command, execution_prompt, args, command_environment or os.environ.copy(), stream_mode)
        process = streamed_process["process"]
        timed_out = streamed_process["timed_out"]
        process_stdout = streamed_process["stdout"]
        process_stderr = streamed_process["stderr"]
        streamed_matching_result = streamed_process["matching_result"]
        result_ready_monotonic_ns = streamed_process["result_ready_monotonic_ns"]
        duplicate_result_detected = streamed_process["duplicate_result_detected"]
    else:
        try:
            process = subprocess.run(command, input=execution_prompt, text=True, cwd=args.workdir, capture_output=True, check=False, shell=False, timeout=args.timeout, **({"env": command_environment} if command_environment is not None else {}))
        except subprocess.TimeoutExpired as error:
            process = None
            timed_out = True
            process_stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            process_stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
        else:
            process_stdout = process.stdout.decode("utf-8", errors="replace") if isinstance(process.stdout, bytes) else process.stdout or ""
            process_stderr = process.stderr.decode("utf-8", errors="replace") if isinstance(process.stderr, bytes) else process.stderr or ""
    benchmark_launch_required = bool(getattr(args, "bootstrap_task", False) and args.result_output is not None)
    benchmark_launch_evidence = benchmark_auto_launch_evidence(benchmark_cache_root, sha256_text(prompt_text), pair_text(args.model, args.effort)) if benchmark_launch_required else None
    stdout_summary = parse_stdout_events(process_stdout)
    benchmark_result_source = "controller_final" if benchmark_stream_ready and streamed_matching_result is not None else None
    terminal_agent_message = extract_last_agent_message(process_stdout)
    bridge_handoff_allowed = benchmark_launch_required and streamed_matching_result is None and not duplicate_result_detected and not timed_out and process is not None and process.returncode == 0 and stdout_summary["turn_completed"] and not stdout_summary["turn_failed"] and isinstance(stdout_summary.get("thread_id"), str) and bool(stdout_summary["thread_id"]) and terminal_agent_message is not None and not terminal_agent_message.strip() and benchmark_launch_evidence["bridge_result_verified"] is True
    if bridge_handoff_allowed:
        streamed_matching_result = benchmark_launch_evidence["bridge_result_text"]
        atomic_write_private_text(args.result_output, streamed_matching_result + "\n")
        result_ready_monotonic_ns = time.monotonic_ns()
        result_ready_callback = getattr(args, "result_ready_callback", None)
        if callable(result_ready_callback):
            result_ready_callback(args.result_output, result_ready_monotonic_ns, stdout_summary["thread_id"])
        benchmark_result_source = "adaptive_bridge_handoff"
    elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000)
    if benchmark_stream_ready or stream_result_ready:
        stdout_summary["output_hash"] = sha256_text(streamed_matching_result) if streamed_matching_result is not None else None
    thread_state = read_thread_state(args.state_db, stdout_summary["thread_id"])
    rollout = parse_rollout_allowlist(thread_state["rollout_path"] if thread_state else None)
    turn_context = rollout["turn_context"] if rollout.get("turn_context") else {}
    reroutes = rollout["reroutes"]
    has_turn_context = bool(turn_context.get("model") and turn_context.get("effort"))
    if has_turn_context:
        resolved_model = turn_context.get("model")
        resolved_effort = turn_context.get("effort")
        effective_model = reroutes[-1]["to_model"] if reroutes else resolved_model
    else:
        resolved_model = None
        resolved_effort = None
        effective_model = None
    usage = normalize_usage(rollout["usage"] or stdout_summary["usage"])
    allowed_models = {model for model, _ in allowed_pairs}
    allowed_efforts = {effort for _, effort in allowed_pairs}
    model_match = resolved_model in allowed_models
    effort_match = resolved_effort in allowed_efforts
    pair_match = (effective_model, resolved_effort) in allowed_pairs if effective_model and resolved_effort else False
    token_consistent = thread_state is not None and usage["total_tokens"] == thread_state.get("tokens_used")
    streamed_result_required = benchmark_stream_ready or stream_result_ready
    streamed_result_match = not streamed_result_required or streamed_matching_result is not None
    benchmark_launch_verified = not benchmark_launch_required or benchmark_launch_evidence["verified"] is True
    status = "pass" if not timed_out and process.returncode == 0 and stdout_summary["turn_completed"] and not stdout_summary["turn_failed"] and pair_match and token_consistent and streamed_result_match and not duplicate_result_detected and benchmark_launch_verified else "fail"
    task_complete = rollout["task_complete"] or {}
    failure_class = "timeout" if timed_out else "protocol" if (streamed_result_required and (streamed_matching_result is None or duplicate_result_detected)) or not benchmark_launch_verified else infer_failure_class(process, status, stdout_summary["turn_completed"], stdout_summary["turn_failed"], stdout_summary["availability_failure"], model_match, effort_match, pair_match, token_consistent)
    requested_pair = f"{args.model}|{args.effort}"
    attempt = route_attempt_summary(requested_pair=requested_pair, resolved_pair=(resolved_model, resolved_effort) if resolved_model and resolved_effort else None, effective_pair=(effective_model, resolved_effort) if effective_model and resolved_effort else None, status=status, model_match=model_match, effort_match=effort_match, pair_match=pair_match, process_elapsed_ms=elapsed_ms, task_complete=task_complete, execution_failure_class=failure_class, tokens=usage, thread_id=stdout_summary["thread_id"])
    if status == "pass":
        failure_class = None
    receipt = {"schema_version": 1, "proof_level": "local-operational-not-cryptographic", "workload_id": args.workload_id, "node_type": receipt_node_type(args), **authorization, "workload_prompt_sha256": sha256_text(prompt_text), "prompt_sha256": sha256_text(execution_prompt), "output_sha256": stdout_summary["output_hash"], "thread_id": stdout_summary["thread_id"], "requested_model": args.model, "requested_effort": args.effort, "requested_pair": f"{args.model}|{args.effort}", "resolved_model": resolved_model, "resolved_effort": resolved_effort, "resolved_pair": f"{resolved_model}|{resolved_effort}" if resolved_model and resolved_effort else None, "effective_model": effective_model, "effective_effort": resolved_effort, "effective_pair": f"{effective_model}|{resolved_effort}" if effective_model and resolved_effort else None, "reroutes": reroutes, "allowed_fallback_pairs": allowed_fallback_pairs, "model_match": model_match, "effort_match": effort_match, "pair_match": pair_match, "tokens": usage, "metrics_complete": not timed_out and stdout_summary["turn_completed"] and token_consistent, "tokens_lower_bound": timed_out and usage["total_tokens"] is not None, "pre_execution_failure": False, "availability": rollout.get("availability"), "state_tokens_used": (thread_state or {}).get("tokens_used"), "token_total_consistent": token_consistent, "model_turn_duration_ms": task_complete.get("duration_ms"), "time_to_first_token_ms": task_complete.get("time_to_first_token_ms"), "process_elapsed_ms": elapsed_ms, "exit_code": process.returncode if process is not None else None, "turn_completed": False if timed_out else stdout_summary["turn_completed"], "stderr_line_count": len(process_stderr.splitlines()), "cli_version": (thread_state or {}).get("cli_version"), "model_provider": (thread_state or {}).get("model_provider"), "source": (thread_state or {}).get("source"), "status": status, "recorded_at": datetime.now(timezone.utc).isoformat(), "limitations": "Resolved/effective values come from local Codex runtime metadata and reroute events; this is not a cryptographically signed backend attestation."}
    if getattr(args, "direct_task", False) or getattr(args, "bootstrap_task", False):
        receipt["benchmark_run_id"] = args.benchmark_run_id
        if benchmark_stream_ready:
            receipt["benchmark_result_source"] = benchmark_result_source
    if getattr(args, "bootstrap_task", False):
        receipt["benchmark_prompt_file_verified"] = True
        if benchmark_launch_required:
            receipt["benchmark_auto_launch_verified"] = benchmark_launch_evidence["verified"]
            receipt["benchmark_auto_workspace_count"] = benchmark_launch_evidence["workspace_count"]
            receipt["benchmark_auto_bridge_result_verified"] = benchmark_launch_evidence["bridge_result_verified"]
            receipt["benchmark_selected_execution"] = benchmark_launch_evidence["selected_execution"]
    receipt["failure_class"] = failure_class
    receipt["route_attempts"] = [attempt]
    if streamed_result_required:
        receipt["result_published"] = streamed_matching_result is not None and args.result_output.is_file()
        receipt["result_ready_monotonic_ns"] = result_ready_monotonic_ns
        receipt["duplicate_result_detected"] = duplicate_result_detected
        if receipt["result_published"]:
            receipt["result_output_path"] = str(args.result_output)
    last_message = extract_last_agent_message(process_stdout) if args.result_output and not timed_out and not streamed_result_required else None
    if args.result_output and last_message is not None:
        args.result_output.parent.mkdir(parents=True, exist_ok=True)
        args.result_output.write_text(last_message + "\n", encoding="utf-8")
        receipt["result_output_path"] = str(args.result_output)
    return annotate_operational_fallback(receipt)


def aggregate_token_maps(token_maps):
    aggregated = {}
    for field in TOKEN_FIELDS:
        values = [tokens.get(field) for tokens in token_maps]
        aggregated[field] = sum(values) if values and all(isinstance(value, int) and value >= 0 for value in values) else None
    return aggregated


def receipt_strategy_metrics(receipt):
    tokens = receipt.get("strategy_tokens") if isinstance(receipt.get("strategy_tokens"), dict) else receipt.get("tokens") if isinstance(receipt.get("tokens"), dict) else {}
    elapsed_ms = receipt.get("strategy_elapsed_ms") if isinstance(receipt.get("strategy_elapsed_ms"), int) else receipt.get("process_elapsed_ms")
    return {"tokens": tokens, "elapsed_ms": elapsed_ms}


def strategy_receipt_id(receipt):
    explicit_id = receipt.get("receipt_id")
    if isinstance(explicit_id, str) and explicit_id:
        return explicit_id
    thread_id = receipt.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return f"thread:{thread_id}"
    identity = {key: receipt.get(key) for key in ("workload_id", "prompt_sha256", "requested_pair", "recorded_at", "node_role")}
    return f"receipt:{sha256_text(json.dumps(identity, sort_keys=True, separators=(',', ':')))}"


def aggregate_strategy_bundle(bundle):
    receipts = bundle.get("receipts") if isinstance(bundle, dict) else None
    if not isinstance(receipts, list) or not receipts or any(not isinstance(receipt, dict) for receipt in receipts):
        raise ValueError("strategy bundle receipts must be a non-empty list of receipt objects")
    unique_receipts = {}
    for receipt in receipts:
        unique_receipts.setdefault(strategy_receipt_id(receipt), receipt)
    critical_path_ids = bundle.get("critical_path_receipt_ids")
    if critical_path_ids is not None and (not isinstance(critical_path_ids, list) or any(not isinstance(receipt_id, str) or receipt_id not in unique_receipts for receipt_id in critical_path_ids) or len(critical_path_ids) != len(set(critical_path_ids))):
        raise ValueError("critical_path_receipt_ids must uniquely identify receipts in the strategy bundle")
    metrics_by_id = {receipt_id: receipt_strategy_metrics(receipt) for receipt_id, receipt in unique_receipts.items()}
    token_maps = [metrics["tokens"] for metrics in metrics_by_id.values()]
    aggregated_tokens = aggregate_token_maps(token_maps)
    critical_metrics = [metrics_by_id[receipt_id] for receipt_id in critical_path_ids] if critical_path_ids is not None else list(metrics_by_id.values())
    elapsed_values = [metrics["elapsed_ms"] for metrics in critical_metrics]
    critical_path_elapsed_ms = sum(elapsed_values) if elapsed_values and all(isinstance(value, int) and value >= 0 for value in elapsed_values) else None
    return {"receipt_count": len(unique_receipts), "receipt_ids": list(unique_receipts), "tokens": aggregated_tokens, "critical_path_elapsed_ms": critical_path_elapsed_ms, "metrics_complete": aggregated_tokens.get("total_tokens") is not None and critical_path_elapsed_ms is not None}


def comparison_metrics(value):
    if isinstance(value, dict) and isinstance(value.get("receipts"), list):
        return aggregate_strategy_bundle(value)
    metrics = receipt_strategy_metrics(value)
    return {"receipt_count": 1, "receipt_ids": [strategy_receipt_id(value)], "tokens": metrics["tokens"], "critical_path_elapsed_ms": metrics["elapsed_ms"], "metrics_complete": metrics["tokens"].get("total_tokens") is not None and isinstance(metrics["elapsed_ms"], int)}


def compare_receipts(routed, baseline, acceptance_evidence=None):
    failures = []
    workload_prompt_sha256 = routed.get("workload_prompt_sha256")
    if not workload_prompt_sha256 or workload_prompt_sha256 != baseline.get("workload_prompt_sha256"):
        failures.append("workload prompt hash mismatch")
    if routed.get("workload_id") != baseline.get("workload_id"):
        failures.append("workload ID mismatch")
    if routed.get("status") != "pass" or baseline.get("status") != "pass":
        failures.append("both receipts must pass before comparison")
    output_hash_match = bool(routed.get("output_sha256") and routed.get("output_sha256") == baseline.get("output_sha256"))
    external_acceptance_pass = bool(acceptance_evidence and acceptance_evidence.get("status") == "pass" and acceptance_evidence.get("workload_id") == routed.get("workload_id") and acceptance_evidence.get("same_acceptance_criteria") is True)
    if not output_hash_match and not external_acceptance_pass:
        failures.append("outputs differ and no matching external acceptance evidence passed")
    routed_metrics = comparison_metrics(routed)
    baseline_metrics = comparison_metrics(baseline)
    routed_tokens = routed_metrics["tokens"]
    baseline_tokens = baseline_metrics["tokens"]
    token_savings = baseline_tokens.get("total_tokens") - routed_tokens.get("total_tokens") if baseline_tokens.get("total_tokens") is not None and routed_tokens.get("total_tokens") is not None else None
    uncached_input_savings = baseline_tokens.get("uncached_input_tokens") - routed_tokens.get("uncached_input_tokens") if baseline_tokens.get("uncached_input_tokens") is not None and routed_tokens.get("uncached_input_tokens") is not None else None
    routed_elapsed_ms = routed_metrics["critical_path_elapsed_ms"]
    baseline_elapsed_ms = baseline_metrics["critical_path_elapsed_ms"]
    elapsed_savings_ms = baseline_elapsed_ms - routed_elapsed_ms if baseline_elapsed_ms is not None and routed_elapsed_ms is not None else None
    token_savings_percent = round(token_savings / baseline_tokens["total_tokens"] * 100, 2) if token_savings is not None and baseline_tokens.get("total_tokens") else None
    elapsed_savings_percent = round(elapsed_savings_ms / baseline_elapsed_ms * 100, 2) if elapsed_savings_ms is not None and baseline_elapsed_ms else None
    performance_failures = []
    if token_savings is None or token_savings <= 0:
        performance_failures.append("strategy must use fewer complete total tokens")
    if elapsed_savings_ms is None or elapsed_savings_ms <= 0:
        performance_failures.append("strategy must have lower complete critical-path time")
    performance_eligible = not failures and not performance_failures and routed_metrics["metrics_complete"] and baseline_metrics["metrics_complete"]
    return {"schema_version": 1, "valid_like_for_like_smoke": not failures, "performance_eligible": performance_eligible, "failures": failures, "performance_failures": performance_failures, "workload_id": routed.get("workload_id"), "workload_prompt_sha256": workload_prompt_sha256, "acceptance": {"output_hash_match": output_hash_match, "external_evidence_pass": external_acceptance_pass, "evidence_type": "exact-output-hash" if output_hash_match else "external-semantic-verification" if external_acceptance_pass else "missing"}, "routed": {"model": routed.get("effective_model"), "effort": routed.get("resolved_effort"), "receipt_count": routed_metrics["receipt_count"], "total_tokens": routed_tokens.get("total_tokens"), "uncached_input_tokens": routed_tokens.get("uncached_input_tokens"), "process_elapsed_ms": routed_elapsed_ms}, "entry_model_leakage_baseline": {"model": baseline.get("effective_model"), "effort": baseline.get("resolved_effort"), "receipt_count": baseline_metrics["receipt_count"], "total_tokens": baseline_tokens.get("total_tokens"), "uncached_input_tokens": baseline_tokens.get("uncached_input_tokens"), "process_elapsed_ms": baseline_elapsed_ms}, "measured_savings": {"total_tokens": token_savings, "total_tokens_percent": token_savings_percent, "uncached_input_tokens": uncached_input_savings, "process_elapsed_ms": elapsed_savings_ms, "process_elapsed_percent": elapsed_savings_percent}, "interpretation": "Performance eligibility requires both fewer complete total tokens and lower complete critical-path time. Tokens are a usage proxy, not a currency claim. One pair is a smoke result; alternate repeated runs and compare medians for a durable claim."}


def run_command_summary(args, result):
    summary = {"output": str(args.output), "status": result.get("status", "fail")}
    if getattr(args, "emit_result", False) and args.result_output and result.get("status") == "pass" and args.result_output.exists():
        summary["result"] = args.result_output.read_text(encoding="utf-8").rstrip("\n")
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Capture sanitized Codex model receipts and compare like-for-like runs.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--model", choices=sorted(MODEL_EFFORTS), required=True)
    run_parser.add_argument("--effort", required=True)
    run_parser.add_argument("--workload-id", required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--result-output", type=Path, help="Optional task-cache path for the final child result. Raw stdout/stderr are never stored in the receipt.")
    run_parser.add_argument("--emit-result", action="store_true", help="Return the saved final result in the command summary for a bounded parent fast path; never stores it in the receipt.")
    run_parser.add_argument("--workdir", type=Path, default=Path.cwd())
    run_parser.add_argument("--state-db", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state_5.sqlite")
    run_parser.add_argument("--codex-bin", default="codex")
    run_parser.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="read-only")
    run_parser.add_argument("--allow-fallback", action="append", default=[])
    run_parser.add_argument("--ignore-user-config", action="store_true")
    task_mode = run_parser.add_mutually_exclusive_group()
    task_mode.add_argument("--entry-task", action="store_true", help="Use only when capturing the Task Analyze entry itself; downstream nodes receive the LOCKED_ROUTE_NODE marker by default.")
    task_mode.add_argument("--direct-task", action="store_true", help="Benchmark-only raw Direct arm. Requires --benchmark-run-id beginning with benchmark- and is forbidden inside Task Analyze entry context.")
    task_mode.add_argument("--bootstrap-task", action="store_true", help="Benchmark-only Global inline-bootstrap arm. Runs outside Task Analyze entry context and requires --benchmark-run-id.")
    run_parser.add_argument("--benchmark-run-id", help="Required sanitized benchmark-* identifier for --direct-task or --bootstrap-task; rejected for every other mode.")
    run_parser.add_argument("--benchmark-prompt-path", type=Path, help="Frozen raw workload file required by --bootstrap-task; its UTF-8 content must exactly match stdin.")
    run_parser.add_argument("--route-marker", choices=sorted(ROUTE_MARKERS), default="LOCKED_ROUTE_NODE")
    run_parser.add_argument("--timeout", type=int, default=900)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--routed", type=Path, required=True)
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--acceptance-evidence", type=Path)
    compare_parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    if args.command == "run":
        prompt_text = sys.stdin.read()
        if not prompt_text.strip():
            raise SystemExit("prompt must be supplied on stdin")
        try:
            result = run_receipt(args, prompt_text)
        except ReceiptAuthorizationError as error:
            result = rejected_run_receipt(args, error)
        except subprocess.TimeoutExpired:
            result = failed_run_receipt(args, "timeout")
        except OSError:
            result = failed_run_receipt(args, "execution")
    else:
        acceptance_evidence = json.loads(args.acceptance_evidence.read_text(encoding="utf-8")) if args.acceptance_evidence else None
        result = compare_receipts(json.loads(args.routed.read_text(encoding="utf-8")), json.loads(args.baseline.read_text(encoding="utf-8")), acceptance_evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(run_command_summary(args, result) if args.command == "run" else {"output": str(args.output), "status": "pass" if result.get("valid_like_for_like_smoke") else "fail"}))
    return 0 if result.get("status", "pass" if result.get("valid_like_for_like_smoke") else "fail") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
