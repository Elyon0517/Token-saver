#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PureWindowsPath


FENCED_BLOCK_PATTERN = re.compile(r"```(?P<label>[^\r\n`]*)\r?\n(?P<body>.*?)\r?\n?```", re.DOTALL)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class GateFailure(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json_object(path, failure_code):
    try:
        parsed_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise GateFailure(failure_code)
    if not isinstance(parsed_value, dict):
        raise GateFailure(failure_code)
    return parsed_value


def extract_json_object(result_text):
    stripped_text = result_text.strip()
    fenced_blocks = list(FENCED_BLOCK_PATTERN.finditer(stripped_text))
    if fenced_blocks:
        final_block = fenced_blocks[-1]
        if len(fenced_blocks) != 1 or stripped_text[final_block.end():].strip():
            raise GateFailure("result_json_ambiguous")
        prefix_text = stripped_text[:final_block.start()]
        decoder = json.JSONDecoder()
        for object_start in (match.start() for match in re.finditer(r"\{", prefix_text)):
            try:
                prefix_value, _ = decoder.raw_decode(prefix_text[object_start:])
            except json.JSONDecodeError:
                continue
            if isinstance(prefix_value, dict):
                raise GateFailure("result_json_ambiguous")
        if final_block.group("label").strip().lower() not in {"", "json"}:
            raise GateFailure("result_fence_language")
        json_text = final_block.group("body").strip()
        container = "final_fence"
    else:
        json_text = stripped_text
        container = "plain"
    try:
        parsed_object = json.loads(json_text)
    except json.JSONDecodeError:
        raise GateFailure("result_json_invalid")
    if not isinstance(parsed_object, dict):
        raise GateFailure("result_json_not_object")
    return parsed_object, container


def validate_receipt(receipt, result_text):
    if receipt.get("schema_version") != 1 or receipt.get("node_type") != "locked-route-node":
        raise GateFailure("receipt_contract")
    if receipt.get("status") != "pass" or receipt.get("failure_class") is not None:
        raise GateFailure("receipt_status")
    if receipt.get("turn_completed") is not True or receipt.get("exit_code") != 0 or receipt.get("metrics_complete") is not True:
        raise GateFailure("receipt_incomplete")
    if receipt.get("model_match") is not True or receipt.get("effort_match") is not True or receipt.get("pair_match") is not True:
        raise GateFailure("receipt_model_mismatch")
    requested_model = receipt.get("requested_model")
    requested_effort = receipt.get("requested_effort")
    resolved_model = receipt.get("resolved_model")
    resolved_effort = receipt.get("resolved_effort")
    effective_model = receipt.get("effective_model")
    if not all(isinstance(value, str) and value for value in [requested_model, requested_effort, resolved_model, resolved_effort, effective_model]):
        raise GateFailure("receipt_model_incomplete")
    if receipt.get("requested_pair") != f"{requested_model}|{requested_effort}" or receipt.get("effective_pair") != f"{effective_model}|{resolved_effort}":
        raise GateFailure("receipt_pair_inconsistent")
    output_hash = receipt.get("output_sha256")
    if not isinstance(output_hash, str) or SHA256_PATTERN.fullmatch(output_hash) is None:
        raise GateFailure("receipt_output_hash")
    receipt_message = result_text[:-1] if result_text.endswith("\n") else result_text
    if sha256_text(receipt_message) != output_hash:
        raise GateFailure("result_hash_mismatch")


def decode_json_pointer(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise GateFailure("json_pointer_invalid")
    tokens = []
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise GateFailure("json_pointer_invalid")
        tokens.append(raw_token.replace("~1", "/").replace("~0", "~"))
    return tokens


def resolve_json_pointer(document, pointer):
    current_values = [document]
    for token in decode_json_pointer(pointer):
        next_values = []
        for current_value in current_values:
            if token == "*" and isinstance(current_value, list):
                next_values.extend(current_value)
            elif isinstance(current_value, dict) and token in current_value:
                next_values.append(current_value[token])
            elif isinstance(current_value, list) and token.isdigit() and int(token) < len(current_value):
                next_values.append(current_value[int(token)])
            else:
                raise GateFailure("json_pointer_missing")
        current_values = next_values
    if not current_values:
        raise GateFailure("json_pointer_no_match")
    return current_values


def lexical_json_key(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_required_keys(document, required_keys):
    if any(key not in document for key in required_keys):
        raise GateFailure("json_required_key_missing")


def validate_key_order(document, expected_key_order):
    if expected_key_order is not None and list(document) != expected_key_order:
        raise GateFailure("json_key_order")


def validate_sorted_arrays(document, pointers):
    checked_arrays = 0
    for pointer in pointers:
        for pointer_value in resolve_json_pointer(document, pointer):
            if not isinstance(pointer_value, list):
                raise GateFailure("sorted_pointer_not_array")
            lexical_values = [lexical_json_key(value) for value in pointer_value]
            if lexical_values != sorted(lexical_values):
                raise GateFailure("json_array_unsorted")
            checked_arrays += 1
    return checked_arrays


def resolve_source_root(source_root):
    try:
        resolved_root = source_root.resolve(strict=True)
    except OSError:
        raise GateFailure("source_root_invalid")
    if not resolved_root.is_dir():
        raise GateFailure("source_root_invalid")
    return resolved_root


def resolve_relative_source(resolved_root, relative_path_text):
    if not isinstance(relative_path_text, str) or not relative_path_text or Path(relative_path_text).is_absolute() or PureWindowsPath(relative_path_text).is_absolute() or ".." in Path(relative_path_text).parts:
        raise GateFailure("source_path_traversal")
    try:
        resolved_source = (resolved_root / relative_path_text).resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError):
        raise GateFailure("source_missing_or_outside_root")
    if not resolved_source.is_file():
        raise GateFailure("source_missing_or_outside_root")
    return resolved_source


def validate_source_files(document, source_root, source_files_pointer):
    if (source_root is None) != (source_files_pointer is None):
        raise GateFailure("source_arguments_incomplete")
    if source_root is None:
        return 0
    resolved_root = resolve_source_root(source_root)
    checked_files = 0
    for pointer_value in resolve_json_pointer(document, source_files_pointer):
        if not isinstance(pointer_value, list):
            raise GateFailure("source_pointer_not_array")
        for relative_path_text in pointer_value:
            resolve_relative_source(resolved_root, relative_path_text)
            checked_files += 1
    return checked_files


def parse_comma_list(value):
    if value is None:
        return None
    parsed_values = [part.strip() for part in value.split(",") if part.strip()]
    if len(parsed_values) != len(set(parsed_values)):
        raise GateFailure("duplicate_json_key_option")
    return parsed_values


def validate_grounded_result(receipt_path, result_path, required_keys=None, expected_key_order=None, sorted_json_pointers=None, source_root=None, source_files_pointer=None):
    receipt = load_json_object(receipt_path, "receipt_invalid")
    try:
        result_text = result_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise GateFailure("result_unreadable")
    validate_receipt(receipt, result_text)
    document, container = extract_json_object(result_text)
    required_keys = required_keys or []
    sorted_json_pointers = sorted_json_pointers or []
    validate_required_keys(document, required_keys)
    validate_key_order(document, expected_key_order)
    sorted_arrays_checked = validate_sorted_arrays(document, sorted_json_pointers)
    source_files_checked = validate_source_files(document, source_root, source_files_pointer)
    return {"schema_version": 1, "status": "pass", "receipt": {"turn_completed": True, "pair_match": True}, "result": {"container": container, "object_sha256": sha256_text(json.dumps(document, ensure_ascii=False, separators=(",", ":"))), "key_count": len(document)}, "checks": {"required_keys": len(required_keys), "key_order": expected_key_order is not None, "sorted_arrays": sorted_arrays_checked, "source_files": source_files_checked}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Deterministically validate a receipt-backed grounded JSON result without reading source contents.")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--json-required-keys")
    parser.add_argument("--json-key-order")
    parser.add_argument("--sorted-json-pointer", action="append", default=[])
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source-files-pointer")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        verdict = validate_grounded_result(receipt_path=args.receipt, result_path=args.result, required_keys=parse_comma_list(args.json_required_keys), expected_key_order=parse_comma_list(args.json_key_order), sorted_json_pointers=args.sorted_json_pointer, source_root=args.source_root, source_files_pointer=args.source_files_pointer)
    except GateFailure as failure:
        verdict = {"schema_version": 1, "status": "fail", "failure": failure.code}
    print(json.dumps(verdict, separators=(",", ":")))
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
