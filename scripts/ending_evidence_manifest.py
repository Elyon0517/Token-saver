#!/usr/bin/env python3
"""Build a deterministic, hash-bound evidence manifest for an Ending audit."""

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_manifest(run_id, main_receipt_path, producer_receipt_path, result_path, test_evidence_path, expected_entry_pair, expected_result, expected_test_count):
    paths = {
        "main_receipt": Path(main_receipt_path).resolve(),
        "producer_receipt": Path(producer_receipt_path).resolve(),
        "published_result": Path(result_path).resolve(),
        "test_evidence": Path(test_evidence_path).resolve(),
    }
    main_receipt = _read_json(paths["main_receipt"])
    producer_receipt = _read_json(paths["producer_receipt"])
    published_result = json.loads(paths["published_result"].read_text(encoding="utf-8"))
    test_evidence = _read_json(paths["test_evidence"])
    checks = {
        "entry_receipt": bool(
            main_receipt.get("status") == "pass"
            and main_receipt.get("requested_pair") == expected_entry_pair
            and main_receipt.get("effective_pair") == expected_entry_pair
        ),
        "producer_receipt": bool(
            producer_receipt.get("status") == "pass"
            and isinstance(producer_receipt.get("effective_pair"), str)
            and producer_receipt.get("effective_pair")
        ),
        "published_result": published_result == expected_result,
        "quick_check": bool(
            test_evidence.get("exit_code") == 0
            and test_evidence.get("count") == expected_test_count
        ),
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expected": {
            "entry_pair": expected_entry_pair,
            "result": expected_result,
            "test_count": expected_test_count,
        },
        "observed": {
            "entry_status": main_receipt.get("status"),
            "entry_requested_pair": main_receipt.get("requested_pair"),
            "entry_effective_pair": main_receipt.get("effective_pair"),
            "producer_status": producer_receipt.get("status"),
            "producer_effective_pair": producer_receipt.get("effective_pair"),
            "result": published_result,
            "test_exit_code": test_evidence.get("exit_code"),
            "test_count": test_evidence.get("count"),
        },
        "checks": checks,
        "all_checks_pass": all(checks.values()),
        "sources": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in paths.items()
        },
    }


def _atomic_write(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    path.chmod(0o600)


def validate_manifest(path, expected_run_id):
    reasons = []
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as error:
        return False, [f"manifest_unreadable:{type(error).__name__}"]
    if payload.get("schema_version") != 1:
        reasons.append("schema_version")
    if payload.get("run_id") != expected_run_id:
        reasons.append("run_id")
    expected_source_keys = {"main_receipt", "producer_receipt", "published_result", "test_evidence"}
    sources = payload.get("sources")
    if not isinstance(sources, dict) or set(sources) != expected_source_keys:
        reasons.append("sources_shape")
    else:
        for name, source in sources.items():
            source_path = source.get("path") if isinstance(source, dict) else None
            digest = source.get("sha256") if isinstance(source, dict) else None
            if not isinstance(source_path, str) or not Path(source_path).is_absolute():
                reasons.append(f"{name}_path")
                continue
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                reasons.append(f"{name}_sha256")
                continue
            try:
                if _sha256(source_path) != digest:
                    reasons.append(f"{name}_changed")
            except OSError:
                reasons.append(f"{name}_unavailable")
    expected_check_keys = {"entry_receipt", "producer_receipt", "published_result", "quick_check"}
    checks = payload.get("checks")
    if not isinstance(checks, dict) or set(checks) != expected_check_keys:
        reasons.append("checks_shape")
    elif any(checks[key] is not True for key in expected_check_keys):
        reasons.append("check_failed")
    if payload.get("all_checks_pass") is not True:
        reasons.append("all_checks_pass")
    return not reasons, reasons


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--run-id", required=True)
    build.add_argument("--main-receipt", type=Path, required=True)
    build.add_argument("--producer-receipt", type=Path, required=True)
    build.add_argument("--result", type=Path, required=True)
    build.add_argument("--test-evidence", type=Path, required=True)
    build.add_argument("--expected-entry-pair", required=True)
    build.add_argument("--expected-result-json", required=True)
    build.add_argument("--expected-test-count", type=int, required=True)
    build.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--run-id", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "validate":
        passed, reasons = validate_manifest(args.manifest, args.run_id)
        result = {"status": "PASS" if passed else "BLOCKED", "run_id": args.run_id, "checks": 4 if passed else 0}
        if reasons:
            result["reasons"] = reasons
        print(json.dumps(result, separators=(",", ":")))
        return 0 if passed else 1
    payload = build_manifest(
        args.run_id,
        args.main_receipt,
        args.producer_receipt,
        args.result,
        args.test_evidence,
        args.expected_entry_pair,
        json.loads(args.expected_result_json),
        args.expected_test_count,
    )
    _atomic_write(args.output, payload)
    print(json.dumps({"status": "written", "all_checks_pass": payload["all_checks_pass"], "output": str(args.output.resolve())}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
