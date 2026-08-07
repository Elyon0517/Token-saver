#!/usr/bin/env python3
"""Execute a bounded node against a local OpenAI-compatible endpoint.

Why a separate executor
-----------------------
The upstream quality ladder is entirely cloud models reached through `codex exec`, whose
receipt is reconstructed from a JSON event stream, a thread sqlite database, and a rollout
file. A local HTTP endpoint has none of those, so bolting it into that path would mean
faking thread state. Instead this module runs the call itself and emits a receipt in the
same shape, with the fields it can honestly fill and explicit nulls for the rest.

Where it sits
-------------
One rung below the priority producer, outside the quality ladder:

    local model ($0)  ->  priority producer  ->  quality ladder (weakest -> strongest)

Its tokens cost nothing, so an eligible small node should try it first. A zero-result
operational failure falls through to the next rung and is NOT learned as a quality
failure -- same contract the priority producer already has.

Configuration (environment, all optional except when enabling the rung):
    TOKEN_SAVER_LOCAL_BASE_URL    e.g. http://127.0.0.1:8000/v1   (enables the rung)
    TOKEN_SAVER_LOCAL_MODEL       model name to request
    TOKEN_SAVER_LOCAL_API_KEY     sent as a bearer token; defaults to "none"
    TOKEN_SAVER_LOCAL_TIMEOUT     seconds, default 60
    TOKEN_SAVER_LOCAL_ENABLE_THINKING   "1" to allow a reasoning preamble, default off
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from hashlib import sha256


BASE_URL_ENV = "TOKEN_SAVER_LOCAL_BASE_URL"
MODEL_ENV = "TOKEN_SAVER_LOCAL_MODEL"
API_KEY_ENV = "TOKEN_SAVER_LOCAL_API_KEY"
TIMEOUT_ENV = "TOKEN_SAVER_LOCAL_TIMEOUT"
THINKING_ENV = "TOKEN_SAVER_LOCAL_ENABLE_THINKING"

DEFAULT_TIMEOUT_SECONDS = 60
LOCAL_EFFORT = "low"
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def configured_base_url():
    value = (os.environ.get(BASE_URL_ENV) or "").strip()
    return value.rstrip("/") or None


def configured_model():
    return (os.environ.get(MODEL_ENV) or "").strip() or None


def rung_enabled():
    """The rung is opt-in: without a configured endpoint the engine behaves as before."""
    return bool(configured_base_url() and configured_model())


def local_pair():
    model = configured_model()
    return (model, LOCAL_EFFORT) if model else None


def handles_model(model):
    """Whether this model id is answered here rather than by the Codex subprocess.

    Matched against the *configured* local model, never by name resemblance: an
    unconfigured rung must never divert a node away from the real backend.
    """
    return bool(model) and rung_enabled() and model == configured_model()


def thinking_enabled():
    return (os.environ.get(THINKING_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def configured_timeout():
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    try:
        timeout = int(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS


def _sha256_text(value):
    return sha256(str(value).encode("utf-8")).hexdigest()


def friendly_error(error, base_url):
    """Name the endpoint and the next step instead of surfacing a raw exception."""
    if isinstance(error, urllib.error.HTTPError):
        return f"Local LLM backend at {base_url} returned HTTP {error.code}. Check the model name and endpoint path."
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return f"Local LLM backend at {base_url} did not respond in time. It may be overloaded, or the model too slow for this node."
        return f"Local LLM backend unreachable at {base_url} ({reason}). Check that it is running and that {BASE_URL_ENV} is correct."
    if isinstance(error, TimeoutError):
        return f"Local LLM backend at {base_url} did not respond in time."
    return f"Error querying the local LLM at {base_url}: {error}"


def _request_body(prompt, model, system_message):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    if not thinking_enabled():
        # Reasoning models otherwise spend hundreds of tokens on a hidden preamble before
        # answering. Backends that do not recognise the field ignore it.
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def call_endpoint(prompt, *, system_message="You are a helpful assistant. Provide concise, accurate responses.", opener=None):
    """Return (text, usage) or raise. `opener` exists so tests never touch the network."""
    base_url = configured_base_url()
    model = configured_model()
    if not base_url or not model:
        raise RuntimeError(f"local producer is not configured; set {BASE_URL_ENV} and {MODEL_ENV}")
    payload = json.dumps(_request_body(prompt, model, system_message)).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get(API_KEY_ENV) or 'none'}",
        },
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=configured_timeout()) as response:
        document = json.loads(response.read().decode("utf-8"))
    choices = document.get("choices") or []
    text = (choices[0].get("message", {}).get("content") if choices else None) or ""
    return text, document.get("usage") or {}


def _tokens_from_usage(usage):
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if input_tokens is None and output_tokens is None and total is None:
        return {}
    tokens = {field: 0 for field in TOKEN_FIELDS}
    tokens["input_tokens"] = input_tokens or 0
    tokens["uncached_input_tokens"] = input_tokens or 0
    tokens["output_tokens"] = output_tokens or 0
    tokens["total_tokens"] = total if total is not None else (input_tokens or 0) + (output_tokens or 0)
    return tokens


def run(prompt, *, workload_id="local-producer", system_message=None, opener=None, effort=None, allow_fallback=None):
    """Execute one node locally and return a receipt-shaped dict.

    A local run is real execution with real token accounting, so it carries a receipt.
    Fields the endpoint genuinely cannot supply (thread id, rollout-derived resolved
    pair) stay null rather than being invented.

    The rung offers exactly one effort. A node requesting any other effort is REFUSED
    rather than quietly run at LOCAL_EFFORT: the engine's own route contract says a
    delegated node's pair must not be silently substituted, and a receipt claiming a pair
    that never ran is exactly the kind of unearned evidence this codebase refuses
    elsewhere. Refusal is an operational failure, so the caller falls through a rung.
    """
    base_url = configured_base_url()
    model = configured_model()
    pair = f"{model}|{LOCAL_EFFORT}"
    started = time.perf_counter_ns()

    if effort is not None and effort != LOCAL_EFFORT:
        return _receipt(
            workload_id=workload_id,
            pair=f"{model}|{effort}",
            model=model,
            status="fail",
            failure_class="execution",
            tokens={},
            elapsed_ms=0,
            prompt=prompt,
            text="",
            error_message=f"Local producer offers only effort '{LOCAL_EFFORT}'; node requested '{effort}'. Refusing rather than substituting a different pair.",
        )
    if allow_fallback:
        # Fallback is the caller's ladder decision. Accepting the list here and ignoring
        # it would let a node believe it had a safety net that this executor never honors.
        return _receipt(
            workload_id=workload_id,
            pair=pair,
            model=model,
            status="fail",
            failure_class="execution",
            tokens={},
            elapsed_ms=0,
            prompt=prompt,
            text="",
            error_message="Local producer does not perform in-executor fallback; the caller's ladder must handle it. Refusing rather than silently dropping allow_fallback.",
        )
    kwargs = {"opener": opener}
    if system_message is not None:
        kwargs["system_message"] = system_message

    try:
        text, usage = call_endpoint(prompt, **kwargs)
    except Exception as error:  # noqa: BLE001 - every backend failure is operational here
        elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000)
        return _receipt(
            workload_id=workload_id,
            pair=pair,
            model=model,
            status="fail",
            # Operational, never a quality verdict: the model produced nothing, so there
            # is no answer to judge. This is what lets the caller fall through a rung
            # without recording a quality failure against the local model.
            failure_class="execution",
            tokens={},
            elapsed_ms=elapsed_ms,
            prompt=prompt,
            text="",
            error_message=friendly_error(error, base_url),
        )

    elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000)
    tokens = _tokens_from_usage(usage)
    produced = bool(text.strip())
    return _receipt(
        workload_id=workload_id,
        pair=pair,
        model=model,
        status="pass" if produced else "fail",
        failure_class=None if produced else "execution",
        tokens=tokens,
        elapsed_ms=elapsed_ms,
        prompt=prompt,
        text=text,
        error_message=None if produced else f"Local LLM backend at {base_url} returned an empty result.",
    )


def _receipt(*, workload_id, pair, model, status, failure_class, tokens, elapsed_ms, prompt, text, error_message):
    attempt = {
        "requested_pair": pair,
        "resolved_pair": pair if status == "pass" else None,
        "effective_pair": pair if status == "pass" else None,
        "executed_pair": pair,
        "status": status,
        "failure_class": failure_class,
        "model_match": status == "pass",
        "effort_match": status == "pass",
        "pair_match": status == "pass",
        "process_elapsed_ms": elapsed_ms,
        "model_turn_duration_ms": elapsed_ms,
        "time_to_first_token_ms": None,
        "tokens": tokens,
        "thread_id": None,
        "pre_execution_failure": False,
    }
    receipt = {
        "schema_version": 1,
        "proof_level": "local-operational-not-cryptographic",
        "workload_id": workload_id,
        "node_type": "local_producer",
        "executor": "local_openai_compatible",
        "requested_model": model,
        "requested_effort": LOCAL_EFFORT,
        "requested_pair": pair,
        "resolved_model": model if status == "pass" else None,
        "resolved_effort": LOCAL_EFFORT if status == "pass" else None,
        "effective_model": model if status == "pass" else None,
        "effective_pair": pair if status == "pass" else None,
        "allowed_fallback_pairs": [],
        "model_match": status == "pass",
        "effort_match": status == "pass",
        "pair_match": status == "pass",
        "tokens": tokens,
        "metrics_complete": bool(tokens),
        "tokens_lower_bound": not tokens,
        "process_elapsed_ms": elapsed_ms,
        "turn_completed": status == "pass",
        "status": status,
        "failure_class": failure_class,
        "route_attempts": [attempt],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "workload_prompt_sha256": _sha256_text(prompt),
        "outside_quality_ladder": True,
        "result_text": text,
        "error_message": error_message,
        "limitations": "Executed against a local endpoint: no thread state or rollout is available, so resolved/effective identity is asserted from the request rather than observed from runtime metadata.",
    }
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one node against the local OpenAI-compatible endpoint.")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--workload-id", default="local-producer")
    parser.add_argument("--system-message", default=None)
    parser.add_argument("--status", action="store_true", help="Report whether the rung is configured, without executing.")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps({"status": "pass", "enabled": rung_enabled(), "base_url": configured_base_url(), "model": configured_model(), "thinking": thinking_enabled(), "timeout_seconds": configured_timeout()}, separators=(",", ":"), sort_keys=True))
        return 0

    if not rung_enabled():
        print(json.dumps({"status": "fail", "reason": "local_producer_not_configured", "required": [BASE_URL_ENV, MODEL_ENV]}, separators=(",", ":")))
        return 1

    import sys

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not str(prompt).strip():
        print(json.dumps({"status": "fail", "reason": "prompt_required"}, separators=(",", ":")))
        return 1

    receipt = run(prompt, workload_id=args.workload_id, system_message=args.system_message)
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
