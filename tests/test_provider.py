"""Protocol boundary tests with local fake transport; no real API calls."""
import io
import http.client
import json
import os
import socket
import unittest
import urllib.error
from unittest.mock import Mock, patch

from agent.models import ModelError
from agent.provider import MAX_RESPONSE_BYTES, MessagesModel, _NoRedirect


SECRET = "test-secret-never-display"
USER = [{"role": "user", "content": "hello"}]
SCHEMA = [{"type": "function", "function": {"name": "calculator", "description": "Calculate", "parameters": {"type": "object"}}}]


def reply(content=None, stop="end_turn", **extra):
    return {"content": content if content is not None else [{"type": "text", "text": "hello"}], "stop_reason": stop, **extra}


def call(call_id="call_1", name="calculator", arguments=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": {"expression": "7+8"} if arguments is None else arguments}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.model = MessagesModel("https://example.com/v1", "test-model", SECRET)
        self.transport = Mock()
        self.model._opener = self.transport

    def response(self, payload, *, raw=None):
        response = Mock()
        response.status = 200
        response.read.return_value = raw if raw is not None else json.dumps(payload).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.transport.open.return_value = response
        return response

    def assert_error(self, code, payload):
        self.response(payload)
        with self.assertRaises(ModelError) as caught:
            self.model.complete(USER, SCHEMA)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_text_request_auth_and_usage(self):
        response = self.response(reply(usage={"input_tokens": 10, "output_tokens": 3, "untrusted": SECRET}))
        result = self.model.complete([{"role": "system", "content": "policy"}] + USER, SCHEMA)
        request = self.transport.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.com/v1/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(payload["system"], "policy")
        self.assertEqual(payload["tools"][0]["input_schema"], {"type": "object"})
        self.assertEqual(result.content, "hello")
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(self.model.last_usage, {"input_tokens": 10, "output_tokens": 3})
        self.assertEqual(self.model.request_count, 1)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_multiple_tool_results_become_single_user_message(self):
        history = USER + [{"role": "assistant", "content": "I will calculate.", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "calculator", "arguments": '{"expression":"7+8"}'}},
            {"id": "b", "type": "function", "function": {"name": "calculator", "arguments": '{"expression":"9+1"}'}}
        ]}, {"role": "tool", "tool_call_id": "a", "content": '{"ok":true,"data":15}'},
            {"role": "tool", "tool_call_id": "b", "content": '{"ok":false,"error":{"code":"failure"}}'}]
        self.response(reply())
        self.model.complete(history, SCHEMA)
        messages = json.loads(self.transport.open.call_args.args[0].data)["messages"]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["content"][1]["type"], "tool_use")
        self.assertEqual([b["tool_use_id"] for b in messages[2]["content"]], ["a", "b"])
        self.assertTrue(messages[2]["content"][1]["is_error"])

    def test_tool_reply_is_parsed(self):
        self.response(reply([call(), {"type": "text", "text": "Calculating"}], "tool_use"))
        result = self.model.complete(USER, SCHEMA)
        self.assertEqual(result.content, "Calculating")
        self.assertEqual(result.tool_calls[0].id, "call_1")
        self.assertEqual(json.loads(result.tool_calls[0].arguments), {"expression": "7+8"})

    def test_thinking_modes_are_rejected_without_exposing_content(self):
        for block in [{"type": "thinking", "thinking": SECRET, "signature": "signature"},
                      {"type": "redacted_thinking", "data": SECRET}]:
            for remaining, stop in [([call()], "tool_use"),
                                    ([{"type": "text", "text": "answer"}], "end_turn"),
                                    ([], "end_turn")]:
                with self.subTest(kind=block["type"], stop=stop, remaining=bool(remaining)):
                    self.assert_error("model_unsupported_thinking", reply([block] + remaining, stop))

    def test_truncation_is_not_executable(self):
        for stop in ["max_tokens", "truncated", "model_context_window_exceeded"]:
            with self.subTest(stop=stop):
                self.assert_error("model_truncated", reply([call()], stop))

    def test_invalid_response_shapes(self):
        cases = [[], {}, reply([]), reply([{"type": "text", "text": "  "}]),
                 reply([call(), call()], "tool_use"), reply([call(call_id="")], "tool_use"),
                 reply([call(arguments=[])], "tool_use"), reply([call(arguments="{}")], "tool_use"),
                 reply([call(name="bad name")], "tool_use"), reply([call()], "end_turn"),
                 reply(stop="tool_use"), reply(stop="pause_turn"),
                 reply([{"type": "unknown_block"}]), reply([None]),
                 reply([{"type": "text", "text": 15}])]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_error("model_protocol", payload)

    def test_malformed_json_nonfinite_and_oversize_body(self):
        for raw, code in [(b"not json " + SECRET.encode(), "model_protocol"),
                          (b'{"content":[],"number":NaN}', "model_protocol"),
                          (b"x" * (MAX_RESPONSE_BYTES + 1), "model_response_too_large")]:
            with self.subTest(code=code):
                self.response(None, raw=raw)
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER, SCHEMA)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_transport_errors_are_safe_and_not_retried(self):
        cases = [(urllib.error.HTTPError("https://example.com", 401, SECRET, {}, io.BytesIO(SECRET.encode())), "model_http"),
                 (urllib.error.HTTPError("https://example.com", 302, SECRET, {"Location": "https://elsewhere.invalid"}, io.BytesIO()), "model_http"),
                 (socket.timeout(SECRET), "model_timeout"),
                 (urllib.error.URLError(socket.timeout(SECRET)), "model_timeout"),
                 (urllib.error.URLError(SECRET), "model_network"),
                 (OSError(SECRET), "model_network"),
                 (http.client.IncompleteRead(SECRET.encode(), 999), "model_network")]
        for error, code in cases:
            with self.subTest(code=code):
                self.transport.open.reset_mock()
                self.transport.open.side_effect = error
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(USER, SCHEMA)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))
                if isinstance(error, urllib.error.HTTPError):
                    self.assertEqual(caught.exception.http_status, error.code)
                    self.assertIs(type(caught.exception.http_status), int)
                else:
                    self.assertFalse(hasattr(caught.exception, "http_status"))
                self.transport.open.assert_called_once()

    def test_non_200_transport_response_exposes_only_status(self):
        response = self.response(None, raw=SECRET.encode())
        response.status = 403
        with self.assertRaises(ModelError) as caught:
            self.model.complete(USER, SCHEMA)
        self.assertEqual(caught.exception.code, "model_http")
        self.assertEqual(caught.exception.http_status, 403)
        self.assertNotIn(SECRET, str(caught.exception))
        response.read.assert_not_called()

    def test_redirect_handler_refuses_all_targets(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid"))

    def test_invalid_input_fails_before_http(self):
        cases = [[{"role": "tool", "tool_call_id": "missing", "content": "{}"}],
                 [{"role": "user", "content": []}],
                 USER + [{"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "calculator", "arguments": "{}"}}]}],
                 USER + [{"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "calculator", "arguments": "[]"}}]}]]
        for messages in cases:
            with self.subTest(messages=messages):
                with self.assertRaises(ModelError) as caught:
                    self.model.complete(messages, SCHEMA)
                self.assertEqual(caught.exception.code, "model_input")
        self.transport.open.assert_not_called()
        self.assertEqual(self.model.request_count, 0)

    def test_url_validation_and_local_http_exception(self):
        invalid = ["http://example.com", "https://user:pass@example.com", "https://example.com?token=x", "https://example.com#x", "https://example.com?", "https://example.com#", "https://example.com:99999", "https://example.com\\other", "https://example.com\n", "file:///tmp/test", "https://"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ModelError):
                MessagesModel(url, "model", SECRET)
        for url in ["http://localhost:8000/v1", "http://127.0.0.1/v1", "http://[::1]/v1", "https://example.com", "https://example.com/v1/messages"]:
            with self.subTest(url=url):
                self.assertTrue(MessagesModel(url, "model", SECRET).endpoint.endswith("/v1/messages"))

    def test_env_and_alternative_auth(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://example.com/v1", "AGENT_MODEL": "model", "AGENT_API_KEY": SECRET, "AGENT_AUTH": "x-api-key"}, clear=True):
            model = MessagesModel.from_env()
        model._opener = self.transport
        self.response(reply())
        model.complete(USER, [])
        request = self.transport.open.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), SECRET)
        self.assertNotIn("tools", json.loads(request.data))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ModelError):
            MessagesModel.from_env()


if __name__ == "__main__":
    unittest.main()
