"""Run the real provider codec + runtime with a fake HTTP transport."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent.memory import MemoryPolicy, SessionStore
from agent.provider import MessagesModel
from agent.responses_provider import ResponsesModel
from agent.runtime import Runtime
from agent.tools import make_default_registry


class Response(io.BytesIO):
    status = 200


class ProtocolTransport:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append(payload)
        blocks = payload["messages"][-1]["content"]
        if blocks[-1]["type"] == "text" and blocks[-1]["text"] == "calculate":
            content = [{"type": "tool_use", "id": f"call-{len(self.requests)}", "name": "calculator", "input": {"expression": "7+8"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "15"}]
            stop = "end_turn"
        return Response(json.dumps({"content": content, "stop_reason": stop}).encode())


class IntegrationTests(unittest.TestCase):
    def test_responses_opaque_context_persists_and_is_isolated(self):
        class ResponsesTransport:
            def __init__(self):
                self.requests = []
            def open(self, request, timeout):
                data = json.loads(request.data)
                self.requests.append(data)
                n = len(self.requests)
                if n == 1:
                    output = [{"type": "reasoning", "id": "rs-1", "encrypted_content": "opaque-only", "summary": []},
                              {"type": "function_call", "call_id": "fc-1", "name": "calculator", "arguments": '{"expression":"7+8"}'}]
                else:
                    output = [{"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "15"}]}]
                return Response(json.dumps({"status": "completed", "output": output}).encode())
        with tempfile.TemporaryDirectory() as temp:
            provider = ResponsesModel("https://example.com/v1", "mock-model", "test-key")
            transport = ResponsesTransport()
            provider._opener = transport
            store = SessionStore(Path(temp) / "state.db")
            runtime = Runtime(provider, make_default_registry(), store)
            self.assertEqual(runtime.ask("u", "a", "calculate").status, "completed")
            self.assertIn("opaque-only", json.dumps(transport.requests[1]))
            runtime = Runtime(provider, make_default_registry(), SessionStore(Path(temp) / "state.db"))
            self.assertEqual(runtime.ask("u", "a", "followup").status, "completed")
            self.assertIn("opaque-only", json.dumps(transport.requests[2]))
            self.assertEqual(runtime.ask("u", "b", "different session").status, "completed")
            self.assertNotIn("opaque-only", json.dumps(transport.requests[3]))

    def test_codec_runtime_persistence_and_compaction(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = MessagesModel("https://example.com/v1", "mock-model", "test-key")
            transport = ProtocolTransport()
            provider._opener = transport
            runtime = Runtime(provider, make_default_registry(), SessionStore(Path(temp) / "state.db"),
                              memory=MemoryPolicy(max_recent_turns=1))
            for prompt in ("calculate", "followup", "calculate", "recall"):
                result = runtime.ask("u", "a", prompt)
                self.assertEqual(result.status, "completed")
            self.assertEqual(provider.request_count, 6)
            result_blocks = transport.requests[1]["messages"][-1]["content"]
            self.assertEqual(result_blocks[0]["type"], "tool_result")
            self.assertEqual(json.loads(result_blocks[0]["content"])["data"]["value"], 15)
            self.assertIn("历史摘录", json.dumps(transport.requests[-1], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
