#!/usr/bin/env python3
"""Build and execute real-test Ending tasks with score-based model selection."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 4
BAND_ROLES = {"small": "weak_default", "standard": "balanced_default", "complex": "balanced_complex", "advanced": "frontier_complex"}
THREAD_TARGET = {"type": "projectless"}
TERMINAL_THREAD_POLICY = {"pass": "keep_visible", "fail": "keep_visible", "blocked": "keep_visible"}
CREATE_THREAD_TOOL = "codex_app__create_thread"
ORIGIN_SESSION_RESUME_CAPABILITY = "codex_app__send_message_to_thread"
LAUNCH_STATE_SCHEMA_VERSION = 1


def complexity_band(score):
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("complexity score must be an integer from 0 to 100")
    return "small" if score <= 24 else "standard" if score <= 49 else "complex" if score <= 74 else "advanced"


def _clean(value, field, maximum=160):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text[:maximum]


def _normalize_origin_session(raw, project_root):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("origin_session must be a JSON object")
    thread_id = _clean(raw.get("thread_id"), "origin_session.thread_id", 240)
    host_id = _clean(raw.get("host_id"), "origin_session.host_id", 160)
    project_id = _clean(raw.get("project_id"), "origin_session.project_id", 200)
    source_root = Path(raw.get("project_root") or project_root).expanduser().resolve()
    if source_root != project_root:
        raise ValueError("origin_session.project_root must match project_root")
    resume_capability = _clean(raw.get("resume_capability") or ORIGIN_SESSION_RESUME_CAPABILITY, "origin_session.resume_capability", 160)
    if resume_capability != ORIGIN_SESSION_RESUME_CAPABILITY:
        raise ValueError(f"origin_session.resume_capability must be {ORIGIN_SESSION_RESUME_CAPABILITY}")
    return {"thread_id": thread_id, "host_id": host_id, "project_id": project_id, "project_root": str(project_root), "resume_capability": resume_capability, "immutable": True}


def _registry():
    script = Path(__file__).resolve().parent / "model_registry.py"
    spec = importlib.util.spec_from_file_location("ending_verification_model_registry", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_registry()


def pair_for_score(score, registry=None):
    band = complexity_band(score)
    payload = registry or _registry()
    pair = payload["role_pairs"][BAND_ROLES[band]]
    model, effort = pair.split("|", 1)
    return {"complexity_score": score, "complexity_band": band, "selected_pair": pair, "model": model, "effort": effort}


def _inside(root, value, field):
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{field} must be inside project_root") from error
    return path


def normalize_check(raw, project_root, task_name, task_score, origin_session, registry=None):
    if not isinstance(raw, dict):
        raise ValueError("each check must be a JSON object")
    name = _clean(raw.get("name"), "check.name", 80)
    check_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "check"
    command = raw.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(value, str) or not value for value in command):
        raise ValueError("check.command must be a non-empty JSON string array")
    cwd = _inside(project_root, raw.get("cwd") or project_root, "check.cwd")
    score = raw.get("complexity_score", task_score)
    route = pair_for_score(score, registry)
    timeout = raw.get("timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError("check.timeout_seconds must be from 1 to 1800")
    expected_exit = raw.get("expected_exit_code", 0)
    if isinstance(expected_exit, bool) or not isinstance(expected_exit, int):
        raise ValueError("check.expected_exit_code must be an integer")
    acceptance = _clean(raw.get("acceptance") or name, "check.acceptance", 1200)
    repair_scope = _clean(raw.get("repair_scope") or "Only the original producer's authorized result scope.", "check.repair_scope", 600)
    return {
        "check_id": check_id,
        "name": name,
        "title": f"End Task-{task_name}-{name}",
        "thread_target": {**THREAD_TARGET, "project_root": str(project_root)},
        "terminal_thread_policy": TERMINAL_THREAD_POLICY,
        "cwd": str(cwd),
        "command": command,
        "expected_exit_code": expected_exit,
        "timeout_seconds": timeout,
        "independent": bool(raw.get("independent", True)),
        "acceptance": acceptance,
        "repair_scope": repair_scope,
        **route,
        "on_failure": {
            "action": "send_repair_prompt_to_origin_session_then_fresh_ending",
            "dispatch_tool": ORIGIN_SESSION_RESUME_CAPABILITY,
            "terminal_thread_policy": TERMINAL_THREAD_POLICY,
            "error_fields": ["exit_code", "stdout", "stderr", "timed_out"],
            "max_repair_attempts": 3,
            "origin_session_required": True,
            "origin_session": origin_session,
        },
    }


def build_plan(project_root, task_name, task_score, checks, origin_session=None):
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_root must be an existing directory")
    cleaned_task = _clean(task_name, "task_name", 80)
    registry = _registry()
    normalized_origin_session = _normalize_origin_session(origin_session, root)
    tasks = [normalize_check(check, root, cleaned_task, task_score, normalized_origin_session, registry) for check in checks]
    if not tasks:
        raise ValueError("at least one real verification check is required")
    ids = [task["check_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("check names must produce unique ids")
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "task_name": cleaned_task,
        "task_complexity": pair_for_score(task_score, registry),
        "verification_required": True,
        "execution": "separate_persistent_tasks",
        "thread_target": {**THREAD_TARGET, "project_root": str(root)},
        "terminal_thread_policy": TERMINAL_THREAD_POLICY,
        "all_checks_must_pass": True,
        "origin_session": normalized_origin_session,
        "repair_policy": {"action": "send_repair_prompt_to_origin_session_then_fresh_ending", "origin_session_required": True, "prompt_submission": "automatic", "dispatch_tool": ORIGIN_SESSION_RESUME_CAPABILITY, "max_repair_attempts": 3, "blocked_when": ["origin_session_unavailable", "prompt_submission_failed", "external_state_unavailable", "repair_attempt_limit_exhausted"]},
        "ending_tasks": tasks,
    }


def _atomic_write(path, payload):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    target.chmod(0o600)


def _read_json(path, field):
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} is not readable JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must contain a JSON object")
    return source, payload


def _repair_handoff(check, origin_session, observed, mismatch_summary=""):
    mismatch = _clean(mismatch_summary, "requirement_mismatch", 1200) if mismatch_summary else ""
    if not origin_session:
        return {"action": "blocked_origin_session_unavailable", "origin_session": None, "origin_session_required": True, "reason": "The exact producer session was not captured before Ending launched."}
    evidence_reason = mismatch or ("The real acceptance command did not produce the expected exit result." if observed.get("timed_out") or observed.get("exit_code") != check["expected_exit_code"] else "The observed final result did not satisfy the original acceptance contract.")
    prompt_lines = ["Continue the original producer task in this exact origin session.", "Do not restart Task Analyze, do not let the Ending verifier edit the result, and do not ask the user to copy this prompt.", f"Original acceptance contract: {check['acceptance']}", f"Observed Ending issue: {evidence_reason}", f"Observed exit code: {observed.get('exit_code')}", f"Observed stdout: {observed.get('stdout') or '<empty>'}", f"Observed stderr: {observed.get('stderr') or '<empty>'}", f"Repair scope: {check['repair_scope']}", "Repair the result so it satisfies the original user objective and acceptance contract, run the bounded Quick Check, and return the corrected result plus a new producer receipt to the parent lifecycle. The parent will launch a fresh projectless Ending for the same check; do not create or wait for that Ending here."]
    repair_prompt = "\n".join(prompt_lines)
    repair_dispatch = {"tool": ORIGIN_SESSION_RESUME_CAPABILITY, "arguments": {"threadId": origin_session["thread_id"], "hostId": origin_session["host_id"], "prompt": repair_prompt}, "required": True}
    return {"action": "send_repair_prompt_to_origin_session_then_fresh_ending", "origin_session": origin_session, "origin_session_required": True, "prompt_submission": "automatic", "repair_dispatch": repair_dispatch, "repair_prompt": repair_prompt, "observed_issue": evidence_reason, "original_acceptance": check["acceptance"], "repair_scope": check["repair_scope"], "fresh_ending": "required_after_new_result_and_receipt", "max_repair_attempts": check["on_failure"]["max_repair_attempts"]}


def _worker_prompt(plan_path, plan, check, evidence_output, memory_candidates_output, producer_receipt=None):
    project_root = Path(plan["project_root"]).expanduser().resolve()
    relative_plan = Path(plan_path).expanduser().resolve().relative_to(project_root)
    relative_cwd = Path(check["cwd"]).expanduser().resolve().relative_to(project_root)
    relative_evidence = Path(evidence_output).expanduser().resolve().relative_to(project_root)
    relative_memory_candidates = Path(memory_candidates_output).expanduser().resolve().relative_to(project_root)
    receipt_line = str(Path(producer_receipt).expanduser().resolve().relative_to(project_root)) if producer_receipt else "none"
    command_text = json.dumps(check["command"], ensure_ascii=False, separators=(",", ":"))
    return "\n".join(
        [
            "ENDING_TASK_WORKER",
            "Execute one independent persistent projectless End Task. Do not restart Task Analyze or Workflow.",
            f"Project root: {plan['project_root']}",
            f"Working directory relative to project root: {relative_cwd}",
            f"Verification plan relative to project root: {relative_plan}",
            f"Check id: {check['check_id']}",
            f"Evidence output relative to project root: {relative_evidence}",
            f"Personal memory candidates output relative to project root: {relative_memory_candidates}",
            f"Assigned pair: {check['selected_pair']}",
            f"Complexity: {check['complexity_score']}/100 ({check['complexity_band']})",
            f"Expected command: {command_text}",
            f"Original acceptance contract: {check['acceptance']}",
            f"Authorized repair scope: {check['repair_scope']}",
            f"Origin producer session (immutable): {json.dumps(plan.get('origin_session'), ensure_ascii=False, separators=(',', ':'))}",
            f"Producer receipt relative to project root: {receipt_line}",
            "Resolve CODEX_HOME, then use the platform Python launcher with skills/verify-skill/scripts/ending_verification_plan.py to run the plan's exact run-check command from the project root.",
            "Start and finish the lifecycle through CODEX_HOME skills/verify-skill/scripts/ending_task_ledger.py; when starting it, persist --project-id, --origin-thread-id, and --origin-host-id from origin_session and bind the producer receipt when present. Every terminal event updates local and Obsidian model history; without a receipt it records a non-learning assignment observation.",
            "PASS requires the new evidence file to report status=pass and the expected exit code. PASS/FAIL/BLOCKED must preserve exact evidence and keep this projectless global task visible.",
            "A command PASS is not enough when the final artifact/state differs from the original user objective or acceptance contract. In that case, mark the evidence as a correctness mismatch with the plan's mismatch command, then use the repair handoff.",
            "On FAIL or a correctness mismatch, do not edit the result in this Ending. Record exact evidence, then automatically submit the generated repair_prompt by calling codex_app__send_message_to_thread with repair_dispatch.arguments (threadId, hostId, and the exact prompt) to continue the original origin session. The prompt must identify the observed defect or requirement gap, original acceptance, evidence, authorized repair scope, and required next result. The origin session must repair, run Quick Check, and present a new result and producer receipt; the parent lifecycle then launches a fresh projectless Ending with the same check. Repeat this loop for at most three repair attempts; unavailable or failed origin-session submission is BLOCKED.",
            "The failed Ending and its repair handoff remain visible as the audit record; the original producer session is the only repair executor. Never open a replacement fixer session, self-repair, self-verify, create nested End/Fix tasks, or wait/poll for the origin session.",
            "Every Codex submission receives a bounded personal-memory scan. Analyze only explicit user preferences, repeated user corrections, or verified working patterns relevant to this task; do not infer sensitive traits and never copy raw prompts, raw results, paths, secrets, or chain-of-thought.",
            "If the scan finds no durable preference or technical working trait, do not create the personal memory candidates file and pass no --memory-candidates-file option. If it finds candidates, write only {\"candidates\":[...]} to the assigned file using kind=preference|technical-trait, area=ui|workflow|technical|general, basis=explicit_user_request|repeated_user_correction|verified_work_pattern, confidence=high|medium, source=ending, statement, and compact evidence; then pass --memory-candidates-file with the relative path to the terminal ledger event.",
            "Personal memory is separate from model-routing and project-change memory. A candidate write must not alter the requested result; an empty candidate set must be a strict no-op.",
            "After the terminal ledger event, print its structured model_assessment: task/check score and band, producer pair, Ending pair, attempt count, first-attempt or retry result, suitability, next routing action and pair, concise evidence reason, and Obsidian model-record link/status. Never expose private chain-of-thought.",
            "Never call set_thread_archived or delete this End Task automatically.",
        ]
    )


def build_launch_spec(plan_path, evidence_dir, project_id, producer_receipt=None):
    plan_file, plan = _read_json(plan_path, "plan")
    project_root = Path(plan.get("project_root", "")).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("plan.project_root must be an existing directory")
    project_value = _clean(project_id, "project_id", 200)
    _inside(project_root, plan_file, "plan")
    origin_session = _normalize_origin_session(plan.get("origin_session"), project_root)
    if not origin_session:
        raise ValueError("origin_session is required for a repair-capable Ending launch")
    if origin_session["project_id"] != project_value:
        raise ValueError("origin_session.project_id must match project_id")
    evidence_root = _inside(project_root, evidence_dir, "evidence_dir")
    receipt_path = None
    if producer_receipt:
        receipt_path = _inside(project_root, producer_receipt, "producer_receipt")
        if not receipt_path.is_file():
            raise ValueError("producer_receipt must be an existing file")
    tasks = plan.get("ending_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("plan must contain ending_tasks")
    launch_requests = []
    for check in tasks:
        check_id = check.get("check_id") if isinstance(check, dict) else None
        if not isinstance(check_id, str) or not check_id:
            raise ValueError("every ending task requires check_id")
        selected_pair = check.get("selected_pair")
        if not isinstance(selected_pair, str) or "|" not in selected_pair:
            raise ValueError(f"ending task {check_id} requires selected_pair")
        model, thinking = selected_pair.split("|", 1)
        evidence_output = evidence_root / f"{check_id}.json"
        memory_candidates_output = evidence_root / f"{check_id}.memory.json"
        prompt = _worker_prompt(plan_file, plan, check, evidence_output, memory_candidates_output, receipt_path)
        request = {
            "check_id": check_id,
            "title": check["title"],
            "selected_pair": selected_pair,
            "complexity_score": check["complexity_score"],
            "complexity_band": check["complexity_band"],
            "tool": CREATE_THREAD_TOOL,
            "arguments": {
                "target": dict(THREAD_TARGET),
                "title": check["title"],
                "model": model,
                "thinking": thinking,
                "prompt": prompt,
            },
            "evidence_output": str(evidence_output),
            "memory_candidates_output": str(memory_candidates_output),
            "project_id": project_value,
            "acknowledgement_required": True,
        }
        request["request_sha256"] = hashlib.sha256(
            json.dumps(request["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        launch_requests.append(request)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan": str(plan_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "project_root": str(project_root),
        "project_binding": {
            "project_id": project_value,
            "project_root": str(project_root),
            "environment": {"type": "local"},
            "resolver": "codex_app__list_projects exact canonical project root",
        },
        "execution": "host_persistent_create_thread",
        "thread_target": dict(THREAD_TARGET),
        "origin_session": origin_session,
        "repair_policy": plan.get("repair_policy"),
        "required_launch_count": len(launch_requests),
        "launch_requests": launch_requests,
        "launch_gate": "all_requests_require_thread_and_host_acknowledgement",
    }


def acknowledge_launch(launch_spec_path, check_id, thread_id, host_id, project_id, state_output):
    launch_file, launch_spec = _read_json(launch_spec_path, "launch_spec")
    request = next((item for item in launch_spec.get("launch_requests", []) if item.get("check_id") == check_id), None)
    if not isinstance(request, dict):
        raise ValueError(f"unknown check_id: {check_id}")
    thread_value = _clean(thread_id, "thread_id", 160)
    host_value = _clean(host_id, "host_id", 160)
    project_value = _clean(project_id, "project_id", 200)
    expected_project_id = launch_spec.get("project_binding", {}).get("project_id")
    if project_value != expected_project_id or request.get("project_id") != expected_project_id:
        raise ValueError("project_id does not match the launch specification")
    state_path = Path(state_output).expanduser().resolve()
    state = {
        "schema_version": LAUNCH_STATE_SCHEMA_VERSION,
        "launch_spec": str(launch_file),
        "launch_spec_sha256": hashlib.sha256(launch_file.read_bytes()).hexdigest(),
        "launches": [],
    }
    if state_path.is_file():
        _, state = _read_json(state_path, "launch_state")
        if state.get("launch_spec_sha256") != hashlib.sha256(launch_file.read_bytes()).hexdigest():
            raise ValueError("launch_state belongs to a different launch_spec")
    launches = [item for item in state.get("launches", []) if isinstance(item, dict) and item.get("check_id") != check_id]
    if any(item.get("thread_id") == thread_value for item in launches):
        raise ValueError("one End Task thread cannot acknowledge multiple checks")
    launches.append(
        {
            "check_id": check_id,
            "title": request["title"],
            "selected_pair": request["selected_pair"],
            "request_sha256": request["request_sha256"],
            "thread_id": thread_value,
            "host_id": host_value,
            "project_id": project_value,
            "status": "launched",
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    state["launches"] = sorted(launches, key=lambda item: item["check_id"])
    _atomic_write(state_path, state)
    return {"status": "acknowledged", "check_id": check_id, "thread_id": thread_value, "host_id": host_value, "state": str(state_path)}


def audit_launches(launch_spec_path, state_path):
    launch_file, launch_spec = _read_json(launch_spec_path, "launch_spec")
    launch_sha256 = hashlib.sha256(launch_file.read_bytes()).hexdigest()
    required = {item["check_id"]: item for item in launch_spec.get("launch_requests", []) if isinstance(item, dict) and item.get("check_id")}
    try:
        _, state = _read_json(state_path, "launch_state")
    except ValueError:
        return {
            "status": "blocked",
            "end_task_trigger_rate": "0%",
            "required_launch_count": len(required),
            "launched_count": 0,
            "threads": [],
            "failures": ["launch_state is unavailable; End Task has not been acknowledged"],
        }
    observed = {item["check_id"]: item for item in state.get("launches", []) if isinstance(item, dict) and item.get("check_id")}
    failures = []
    if state.get("launch_spec_sha256") != launch_sha256:
        failures.append("launch_state does not match launch_spec")
    missing = sorted(set(required) - set(observed))
    extra = sorted(set(observed) - set(required))
    if missing:
        failures.append("missing End Task launch acknowledgements: " + ", ".join(missing))
    if extra:
        failures.append("unexpected End Task launch acknowledgements: " + ", ".join(extra))
    thread_ids = []
    for check_id, request in required.items():
        launch = observed.get(check_id)
        if not launch:
            continue
        thread_ids.append(launch.get("thread_id"))
        if launch.get("status") != "launched" or not launch.get("thread_id") or not launch.get("host_id") or not launch.get("project_id"):
            failures.append(f"End Task {check_id} lacks a persistent thread acknowledgement")
        if any(launch.get(field) != request.get(field) for field in ("title", "selected_pair", "request_sha256", "project_id")):
            failures.append(f"End Task {check_id} acknowledgement does not match its launch request")
    if len(thread_ids) != len(set(thread_ids)):
        failures.append("End Task launch acknowledgements must use unique thread ids")
    required_count = len(required)
    launched_count = sum(1 for check_id in required if check_id in observed and observed[check_id].get("status") == "launched")
    trigger_rate = round(100 * launched_count / required_count) if required_count else 0
    return {
        "status": "pass" if not failures and launched_count == required_count else "blocked",
        "end_task_trigger_rate": f"{trigger_rate}%",
        "required_launch_count": required_count,
        "launched_count": launched_count,
        "threads": [observed[check_id] for check_id in sorted(required) if check_id in observed],
        "failures": failures,
    }


def run_check(plan_path, check_id, evidence_output):
    plan_file = Path(plan_path).expanduser().resolve()
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    check = next((item for item in plan.get("ending_tasks", []) if item.get("check_id") == check_id), None)
    if not check:
        raise ValueError(f"unknown check_id: {check_id}")
    started = datetime.now(timezone.utc)
    timed_out = False
    try:
        completed = subprocess.run(check["command"], cwd=check["cwd"], capture_output=True, text=True, timeout=check["timeout_seconds"], check=False)
        exit_code = completed.returncode
        stdout = completed.stdout[-12000:]
        stderr = completed.stderr[-12000:]
    except subprocess.TimeoutExpired as error:
        timed_out = True
        exit_code = None
        stdout = (error.stdout or "")[-12000:] if isinstance(error.stdout, str) else ""
        stderr = (error.stderr or "")[-12000:] if isinstance(error.stderr, str) else ""
    passed = not timed_out and exit_code == check["expected_exit_code"]
    finished = datetime.now(timezone.utc)
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if passed else "fail",
        "check_id": check_id,
        "title": check["title"],
        "selected_pair": check["selected_pair"],
        "complexity_score": check["complexity_score"],
        "complexity_band": check["complexity_band"],
        "failure_class": "none" if passed else "timeout" if timed_out else "execution",
        "repair_context": {"origin_session": plan.get("origin_session"), "acceptance": check["acceptance"], "repair_scope": check["repair_scope"], "max_repair_attempts": check["on_failure"]["max_repair_attempts"]},
        "command": check["command"],
        "cwd": check["cwd"],
        "expected_exit_code": check["expected_exit_code"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms": round((finished - started).total_seconds() * 1000),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
    }
    if not passed:
        evidence["repair_handoff"] = {**_repair_handoff(check, plan.get("origin_session"), evidence), "failed_ending_title": check["title"], "failed_check_id": check_id, "error": {key: evidence[key] for key in check["on_failure"]["error_fields"]}, "terminal_thread_policy": check["on_failure"]["terminal_thread_policy"]}
    _atomic_write(evidence_output, evidence)
    return evidence


def record_requirement_mismatch(evidence_path, mismatch_summary):
    evidence_file, evidence = _read_json(evidence_path, "evidence")
    if not evidence.get("repair_context"):
        raise ValueError("evidence does not contain repair_context")
    summary = _clean(mismatch_summary, "requirement_mismatch", 1200)
    evidence["status"] = "fail"
    evidence["failure_class"] = "correctness"
    evidence["error_fingerprint"] = "acceptance-mismatch"
    evidence["requirement_mismatch"] = summary
    context = evidence["repair_context"]
    check = {"title": evidence.get("title"), "check_id": evidence.get("check_id"), "expected_exit_code": evidence.get("expected_exit_code"), "acceptance": context["acceptance"], "repair_scope": context["repair_scope"], "on_failure": {"max_repair_attempts": context["max_repair_attempts"]}}
    observed = {"exit_code": evidence.get("exit_code"), "stdout": evidence.get("stdout"), "stderr": evidence.get("stderr"), "timed_out": evidence.get("timed_out")}
    evidence["repair_handoff"] = {**_repair_handoff(check, context.get("origin_session"), observed, summary), "failed_ending_title": evidence.get("title"), "failed_check_id": evidence.get("check_id"), "error": {"exit_code": evidence.get("exit_code"), "stdout": evidence.get("stdout"), "stderr": evidence.get("stderr"), "timed_out": evidence.get("timed_out")}}
    _atomic_write(evidence_file, evidence)
    return evidence


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Plan and execute real-test Ending tasks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--project-root", type=Path, required=True)
    plan.add_argument("--task-name", required=True)
    plan.add_argument("--complexity-score", type=int, required=True)
    plan.add_argument("--origin-session-json", required=True)
    plan.add_argument("--check-json", action="append", default=[])
    plan.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run-check")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--check-id", required=True)
    run.add_argument("--evidence-output", type=Path, required=True)
    mismatch = subparsers.add_parser("mismatch")
    mismatch.add_argument("--evidence", type=Path, required=True)
    mismatch.add_argument("--summary", required=True)
    launch = subparsers.add_parser("create-launches")
    launch.add_argument("--plan", type=Path, required=True)
    launch.add_argument("--evidence-dir", type=Path, required=True)
    launch.add_argument("--project-id", required=True)
    launch.add_argument("--producer-receipt", type=Path)
    launch.add_argument("--output", type=Path, required=True)
    acknowledge = subparsers.add_parser("ack-launch")
    acknowledge.add_argument("--launch-spec", type=Path, required=True)
    acknowledge.add_argument("--check-id", required=True)
    acknowledge.add_argument("--thread-id", required=True)
    acknowledge.add_argument("--host-id", required=True)
    acknowledge.add_argument("--project-id", required=True)
    acknowledge.add_argument("--state-output", type=Path, required=True)
    audit = subparsers.add_parser("audit-launches")
    audit.add_argument("--launch-spec", type=Path, required=True)
    audit.add_argument("--launch-state", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "plan":
        payload = build_plan(args.project_root, args.task_name, args.complexity_score, [json.loads(value) for value in args.check_json], json.loads(args.origin_session_json))
        _atomic_write(args.output, payload)
        output = {"status": "written", "output": str(args.output.expanduser().resolve()), "ending_tasks": len(payload["ending_tasks"]), "selected_pairs": [task["selected_pair"] for task in payload["ending_tasks"]]}
        code = 0
    elif args.command == "run-check":
        output = run_check(args.plan, args.check_id, args.evidence_output)
        code = 0 if output["status"] == "pass" else 1
    elif args.command == "mismatch":
        output = record_requirement_mismatch(args.evidence, args.summary)
        code = 0 if output["status"] == "pass" else 1
    elif args.command == "create-launches":
        output = build_launch_spec(args.plan, args.evidence_dir, args.project_id, args.producer_receipt)
        _atomic_write(args.output, output)
        output = {"status": "written", "output": str(args.output.expanduser().resolve()), "required_launch_count": output["required_launch_count"], "selected_pairs": [item["selected_pair"] for item in output["launch_requests"]]}
        code = 0
    elif args.command == "ack-launch":
        output = acknowledge_launch(args.launch_spec, args.check_id, args.thread_id, args.host_id, args.project_id, args.state_output)
        code = 0
    else:
        output = audit_launches(args.launch_spec, args.launch_state)
        code = 0 if output["status"] == "pass" else 1
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
