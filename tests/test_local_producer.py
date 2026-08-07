#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from _support import QuietCliMixin

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local_producer = load_module("local_producer")
routing_policy = load_module("routing_policy")


CONFIGURED = {
    local_producer.BASE_URL_ENV: "http://127.0.0.1:8000/v1",
    local_producer.MODEL_ENV: "local-small",
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def fake_opener(payload):
    captured = {}

    def opener(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    opener.captured = captured
    return opener


def completion(text, usage=None):
    document = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        document["usage"] = usage
    return document


class LocalProducerConfigTests(unittest.TestCase):
    def test_rung_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(local_producer.rung_enabled())
            self.assertIsNone(local_producer.local_pair())

    def test_rung_requires_both_url_and_model(self):
        with patch.dict(os.environ, {local_producer.BASE_URL_ENV: "http://x/v1"}, clear=True):
            self.assertFalse(local_producer.rung_enabled())
        with patch.dict(os.environ, {local_producer.MODEL_ENV: "m"}, clear=True):
            self.assertFalse(local_producer.rung_enabled())
        with patch.dict(os.environ, CONFIGURED, clear=True):
            self.assertTrue(local_producer.rung_enabled())
            self.assertEqual(local_producer.local_pair(), ("local-small", "low"))

    def test_trailing_slash_is_stripped(self):
        with patch.dict(os.environ, {local_producer.BASE_URL_ENV: "http://127.0.0.1:8000/v1/"}, clear=True):
            self.assertEqual(local_producer.configured_base_url(), "http://127.0.0.1:8000/v1")

    def test_invalid_timeout_falls_back_to_default(self):
        with patch.dict(os.environ, {local_producer.TIMEOUT_ENV: "abc"}, clear=True):
            self.assertEqual(local_producer.configured_timeout(), local_producer.DEFAULT_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {local_producer.TIMEOUT_ENV: "0"}, clear=True):
            self.assertEqual(local_producer.configured_timeout(), local_producer.DEFAULT_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {local_producer.TIMEOUT_ENV: "5"}, clear=True):
            self.assertEqual(local_producer.configured_timeout(), 5)


class LocalProducerRequestTests(unittest.TestCase):
    def test_thinking_is_disabled_by_default(self):
        opener = fake_opener(completion("hi"))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            local_producer.call_endpoint("say hi", opener=opener)
        self.assertEqual(opener.captured["body"]["chat_template_kwargs"], {"enable_thinking": False})

    def test_thinking_can_be_enabled(self):
        opener = fake_opener(completion("hi"))
        with patch.dict(os.environ, {**CONFIGURED, local_producer.THINKING_ENV: "1"}, clear=True):
            local_producer.call_endpoint("say hi", opener=opener)
        self.assertNotIn("chat_template_kwargs", opener.captured["body"])

    def test_request_targets_chat_completions(self):
        opener = fake_opener(completion("hi"))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            local_producer.call_endpoint("say hi", opener=opener)
        self.assertEqual(opener.captured["url"], "http://127.0.0.1:8000/v1/chat/completions")

    def test_unconfigured_call_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                local_producer.call_endpoint("x", opener=fake_opener(completion("hi")))


class LocalProducerReceiptTests(unittest.TestCase):
    def test_successful_run_produces_a_passing_receipt_with_tokens(self):
        opener = fake_opener(completion("done", usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=opener)
        self.assertEqual(receipt["status"], "pass")
        self.assertIsNone(receipt["failure_class"])
        self.assertEqual(receipt["effective_pair"], "local-small|low")
        self.assertEqual(receipt["tokens"]["total_tokens"], 15)
        self.assertTrue(receipt["metrics_complete"])
        self.assertTrue(receipt["outside_quality_ladder"])
        self.assertEqual(receipt["result_text"], "done")

    def test_total_tokens_are_derived_when_the_backend_omits_them(self):
        opener = fake_opener(completion("done", usage={"prompt_tokens": 12, "completion_tokens": 3}))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=opener)
        self.assertEqual(receipt["tokens"]["total_tokens"], 15)

    def test_backend_without_usage_reports_lower_bound_tokens(self):
        opener = fake_opener(completion("done"))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=opener)
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["tokens"], {})
        self.assertFalse(receipt["metrics_complete"])
        self.assertTrue(receipt["tokens_lower_bound"])

    def test_empty_result_is_an_operational_failure_not_a_quality_failure(self):
        # This is the contract that lets the caller fall through to the next rung
        # without recording a quality verdict against the local model.
        opener = fake_opener(completion("   "))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=opener)
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "execution")
        self.assertIsNone(receipt["effective_pair"])

    def test_unreachable_backend_yields_an_actionable_message(self):
        def failing_opener(request, timeout=None):
            raise urllib.error.URLError("Connection refused")

        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=failing_opener)
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure_class"], "execution")
        self.assertIn("unreachable", receipt["error_message"])
        self.assertIn("http://127.0.0.1:8000/v1", receipt["error_message"])

    def test_http_error_names_the_endpoint(self):
        def failing_opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("fix the typo", opener=failing_opener)
        self.assertIn("HTTP 404", receipt["error_message"])

    def test_receipt_records_the_workload_hash_not_the_prompt(self):
        opener = fake_opener(completion("done"))
        with patch.dict(os.environ, CONFIGURED, clear=True):
            receipt = local_producer.run("a secret-looking prompt", opener=opener)
        self.assertRegex(receipt["workload_prompt_sha256"], r"^[0-9a-f]{64}$")


class LocalRungLadderTests(unittest.TestCase):
    ELIGIBLE = {"task_type": "code", "operation": "edit", "complexity_score": 10}

    def test_local_rung_is_absent_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(routing_policy.local_first_pair(**self.ELIGIBLE))
            ladder = routing_policy.free_first_ladder(**self.ELIGIBLE)
            self.assertEqual(ladder, [routing_policy.priority_first_pair(**self.ELIGIBLE)])

    def test_local_rung_precedes_the_priority_producer(self):
        with patch.dict(os.environ, CONFIGURED, clear=True):
            ladder = routing_policy.free_first_ladder(**self.ELIGIBLE)
        self.assertEqual(ladder[0], ("local-small", "low"))
        self.assertEqual(ladder[1], routing_policy.priority_first_pair(**self.ELIGIBLE))

    def test_local_rung_shares_priority_producer_admission(self):
        # Above the small-edit ceiling neither rung applies.
        with patch.dict(os.environ, CONFIGURED, clear=True):
            self.assertIsNone(routing_policy.local_first_pair(task_type="code", operation="edit", complexity_score=80))
            self.assertEqual(routing_policy.free_first_ladder(task_type="code", operation="edit", complexity_score=80), [])

    def test_local_rung_rejects_excluded_read_only_operations(self):
        with patch.dict(os.environ, CONFIGURED, clear=True):
            self.assertIsNone(routing_policy.local_first_pair(task_type="code", operation="review", complexity_score=10))

    def test_local_rung_rejects_non_text_modality(self):
        with patch.dict(os.environ, CONFIGURED, clear=True):
            self.assertIsNone(routing_policy.local_first_pair(task_type="code", operation="edit", modality="image", complexity_score=10))


class LocalProducerCliTests(QuietCliMixin, unittest.TestCase):
    def test_status_reports_disabled_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_producer.main(["--status"]), 0)

    def test_run_without_configuration_exits_nonzero(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_producer.main(["do a thing"]), 1)


if __name__ == "__main__":
    unittest.main()
