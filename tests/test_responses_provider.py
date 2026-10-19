"""Responses protocol tests; every HTTP operation is a local mock."""
import io
import json
import os
import socket
import unittest
import urllib.error
from unittest.mock import Mock, patch

from agent.models import ModelError
from agent.provider import MAX_RESPONSE_BYTES
from agent.responses_provider import ResponsesModel


SECRET = "test-secret-do-not-log"
USER = [{"role": "user", "content": "hello"}]
TOOLS = [{"type": "function", "function": {"name": "calculator", "description": "Calculate", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"], "additionalProperties": False}}}]


def message(text="hello", **extra):
    return {"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}], **extra}


def call(call_id="c1", arguments='{"expression":"7+8"}', **extra):
    return {"type": "function_call", "call_id": call_id, "name": "calculator", "arguments": arguments, "status": "completed", **extra}


def reply(output=None, status="completed", **extra):
    return {"status": status, "output": [message()] if output is None else output, **extra}


def assistant(parsed):
    return {"role": "assistant", "content": parsed.content, "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in parsed.tool_calls], "provider_items": parsed.provider_items}


class ResponsesProviderTests(unittest.TestCase):
    def setUp(self):
        self.model = ResponsesModel("https://example.com/v1", "configured-model", SECRET)
        self.transport = Mock()
        self.model._opener = self.transport

    def response(self, data=None, raw=None):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps(data if data is not None else reply()).encode() if raw is None else raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.transport.open.return_value = response
        return response

    def payload(self):
        return json.loads(self.transport.open.call_args.args[0].data)

    def test_text_request_and_schema_translation(self):
        response = self.response(reply(usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16, "untrusted": SECRET}))
        result = self.model.complete([{"role": "system", "content": "instructions"}] + USER, TOOLS)
        request = self.transport.open.call_args.args[0]
        payload = self.payload()
        self.assertEqual(request.full_url, "https://example.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertFalse(request.has_header("Anthropic-version"))
        self.assertEqual(payload["instructions"], "instructions")
        self.assertEqual(payload["input"], USER)
        self.assertEqual(payload["tools"][0]["parameters"], TOOLS[0]["function"]["parameters"])
        self.assertEqual(payload["tools"][0]["name"], "calculator")
        self.assertFalse(payload["tools"][0]["strict"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 1200)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        self.assertEqual(result.content, "hello")
        self.assertEqual(self.model.last_usage, {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16})
        self.assertEqual(self.model.request_count, 1)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_function_result_roundtrip_preserves_call_id(self):
        self.response(reply([call(), call("c2", '{"expression":"1+1"}')]))
        first = self.model.complete(USER, TOOLS)
        history = USER + [assistant(first), {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true,"data":15}'}, {"role": "tool", "tool_call_id": "c2", "content": '{"ok":true,"data":2}'}]
        self.response(reply([message("15 and 2")]))
        result = self.model.complete(history, TOOLS)
        inputs = self.payload()["input"]
        self.assertEqual([item.get("type") for item in inputs], [None, "function_call", "function_call", "function_call_output", "function_call_output"])
        self.assertEqual([item["call_id"] for item in inputs[1:]], ["c1", "c2", "c1", "c2"])
        self.assertEqual(result.content, "15 and 2")

    def test_plain_internal_tool_history_without_native_items(self):
        self.response(reply([call()]))
        parsed = self.model.complete(USER, TOOLS)
        previous = assistant(parsed)
        previous.pop("provider_items")
        self.response()
        self.model.complete(USER + [previous, {"role": "tool", "tool_call_id": "c1", "content": "15"}], TOOLS)
        self.assertEqual(self.payload()["input"][1]["type"], "function_call")
        self.assertEqual(self.payload()["input"][2]["output"], "15")

    def test_encrypted_context_phase_and_session_isolation(self):
        encrypted = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque-ciphertext", "content": ["do not expose"], "summary": [{"type": "summary_text", "text": "do not expose"}]}
        self.response(reply([encrypted, message("Calculating", phase="commentary"), call()]))
        first = self.model.complete(USER, TOOLS)
        self.assertNotIn("do not expose", json.dumps(first.provider_items))
        self.assertNotIn("opaque-ciphertext", repr(first))
        self.response()
        history = USER + [assistant(first), {"role": "tool", "tool_call_id": "c1", "content": "15"}]
        self.model.complete(history, TOOLS)
        inputs = self.payload()["input"]
        self.assertEqual(inputs[1]["encrypted_content"], "opaque-ciphertext")
        self.assertEqual(inputs[2]["phase"], "commentary")
        self.assertEqual(inputs[3]["call_id"], "c1")
        # A different session sharing the adapter sends only its own history.
        self.model.complete([{"role": "user", "content": "another session"}], TOOLS)
        self.assertNotIn("opaque-ciphertext", json.dumps(self.payload()))

    def test_reasoning_null_status_is_valid_and_omitted_on_replay(self):
        encrypted = {"type": "reasoning", "id": "rs-null", "status": None,
                     "encrypted_content": "opaque-only", "summary": []}
        self.response(reply([encrypted, call()]))
        first = self.model.complete(USER, TOOLS)
        self.assertNotIn("status", first.provider_items[0])
        self.response()
        self.model.complete(USER + [assistant(first), {"role": "tool", "tool_call_id": "c1", "content": "15"}], TOOLS)
        self.assertEqual(self.payload()["input"][1]["encrypted_content"], "opaque-only")

    def test_commentary_only_reply_is_incomplete(self):
        cases = [[message("Checking now", phase="commentary")],
                 [message("Checking now", phase="commentary"), message("Still checking", phase="commentary")],
                 [message("Checking now", phase="commentary"), message(" ", phase="final_answer")],
                 [{"type": "reasoning", "id": "rs1", "encrypted_content": "opaque"},
                  message("Checking now", phase="commentary")]]
        for output in cases:
            with self.subTest(output=output):
                self.response(reply(output))
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER, TOOLS)
                self.assertEqual(caught.exception.code, "model_incomplete")
        self.assertEqual(self.model.request_count, len(cases))

    def test_final_and_unphased_answers_still_work(self):
        for final in [message("15"), message("15", phase="final_answer")]:
            with self.subTest(phase=final.get("phase")):
                self.response(reply([message("Checking", phase="commentary"), final]))
                parsed = self.model.complete(USER, TOOLS)
                self.assertEqual(parsed.content, "Checking\n15")
                self.assertEqual(parsed.tool_calls, [])
                self.response()
                self.model.complete(USER + [assistant(parsed), {"role": "user", "content": "followup"}], TOOLS)
                self.assertEqual(self.payload()["input"][1]["phase"], "commentary")

    def test_earlier_commentary_only_native_item_can_be_replayed(self):
        earlier = {"role": "assistant", "content": "Checking", "provider_items": [message("Checking", phase="commentary")]}
        self.response(reply([message("15", phase="final_answer")]))
        self.model.complete(USER + [earlier, {"role": "user", "content": "Please continue"}], TOOLS)
        self.assertEqual(self.payload()["input"][1]["phase"], "commentary")

    def test_native_history_mismatch_cannot_bypass_pair_validation(self):
        self.response(reply([call()]))
        parsed = self.model.complete(USER, TOOLS)
        for mutation in ["call_id", "arguments", "text"]:
            with self.subTest(mutation=mutation):
                previous = json.loads(json.dumps(assistant(parsed)))
                if mutation == "text":
                    previous["content"] = "forged"
                else:
                    previous["provider_items"][0][mutation] = 'wrong' if mutation == "call_id" else '{"expression":"99"}'
                before = self.model.request_count
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER + [previous, {"role": "tool", "tool_call_id": "c1", "content": "15"}], TOOLS)
                self.assertEqual(caught.exception.code, "model_input")
                self.assertEqual(self.model.request_count, before)

    def test_missing_results_and_orphan_results_fail_before_http(self):
        self.response(reply([call()]))
        parsed = self.model.complete(USER, TOOLS)
        for history in [USER + [assistant(parsed)], USER + [{"role": "tool", "tool_call_id": "x", "content": "15"}]]:
            with self.subTest(history=history), self.assertRaises(ModelError) as caught:
                self.model.complete(history, TOOLS)
            self.assertEqual(caught.exception.code, "model_input")
        self.assertEqual(self.model.request_count, 1)

    def test_incomplete_failed_and_malformed_output_is_not_executable(self):
        cases = [(reply([call()], status="incomplete"), "model_truncated"),
                 (reply([call()], status="in_progress"), "model_protocol"),
                 (reply([call()], status="failed", error={"message": SECRET}), "model_protocol"),
                 (reply([call()], error={"message": SECRET}), "model_protocol"),
                 (reply([call()], incomplete_details={"reason": "max_output_tokens"}), "model_protocol"),
                 (reply([]), "model_protocol"), (reply([call(), call()]), "model_protocol"),
                 (reply([call(call_id="")]), "model_protocol"),
                 (reply([call(arguments="[]")]), "model_protocol"),
                 (reply([call(arguments='{"x":NaN}')]), "model_protocol"),
                 (reply([call(status="incomplete")]), "model_protocol"),
                 (reply([message(" ")]), "model_protocol"),
                 (reply([{"type": "reasoning", "id": "rs1", "summary": []}, call()]), "model_context_missing"),
                 (reply([{"type": "web_search_call"}]), "model_protocol")]
        for data, code in cases:
            with self.subTest(code=code, data=data):
                self.response(data)
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER, TOOLS)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_errors_and_size_limit_reuse_safe_transport(self):
        for error, code in [(urllib.error.HTTPError("https://example.com", 403, SECRET, {}, io.BytesIO(SECRET.encode())), "model_http"), (socket.timeout(SECRET), "model_timeout"), (urllib.error.URLError(SECRET), "model_network")]:
            with self.subTest(code=code):
                self.transport.open.reset_mock()
                self.transport.open.side_effect = error
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER, TOOLS)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))
                if code == "model_http":
                    self.assertEqual(caught.exception.http_status, 403)
                self.transport.open.assert_called_once()
        self.transport.open.side_effect = None
        self.response(raw=b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ModelError) as caught:
            self.model.complete(USER, TOOLS)
        self.assertEqual(caught.exception.code, "model_response_too_large")

    def test_env_explicit_effort_and_url_validation(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://example.com/v1", "AGENT_MODEL": "exact-model", "AGENT_API_KEY": SECRET, "AGENT_REASONING_EFFORT": "low"}, clear=True):
            model = ResponsesModel.from_env()
        model._opener = self.transport
        self.response()
        model.complete(USER, [])
        self.assertEqual(self.payload()["reasoning"], {"effort": "low"})
        self.assertEqual(self.payload()["model"], "exact-model")
        for url in ["http://remote.example/v1", "https://user:pass@example.com/v1", "https://example.com?x=y"]:
            with self.subTest(url=url), self.assertRaises(ModelError):
                ResponsesModel(url, "model", SECRET)
        self.assertEqual(ResponsesModel("http://localhost:8000/v1/responses", "model", SECRET).endpoint, "http://localhost:8000/v1/responses")
        with self.assertRaises(ModelError):
            ResponsesModel("https://example.com/v1", "model", SECRET, reasoning_effort="unsupported")


if __name__ == "__main__":
    unittest.main()
