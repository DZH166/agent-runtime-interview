"""OpenAI Responses adapter sharing the bounded HTTP transport in provider.py.

The runtime owns session history. Native output items travel with their own
assistant message, so encrypted context cannot accidentally cross sessions.
We preserve opaque encrypted reasoning; never request or expose a reasoning
transcript. Official format: https://developers.openai.com/api/docs/guides/function-calling
"""
from __future__ import annotations

import json
import os

from .models import ModelError, ModelReply, ToolCall
from .provider import MessagesModel, _identifier, _json_loads, _name


def _output_items(output):
    """Validate native output and keep only replayable, supported fields."""
    if not isinstance(output, list):
        raise ValueError
    texts, calls, replay, seen = [], [], [], set()
    for item in output:
        if not isinstance(item, dict):
            raise ValueError
        kind = item.get("type")
        status = item.get("status", "completed")
        if status != "completed" and not (kind == "reasoning" and status is None):
            raise ValueError
        if kind == "function_call":
            call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
            if (not _identifier(call_id) or call_id in seen or not _name(name)
                    or not isinstance(arguments, str) or not isinstance(_json_loads(arguments), dict)):
                raise ValueError
            arguments.encode("utf-8")
            seen.add(call_id)
            calls.append(ToolCall(call_id, name, arguments))
            native = {"type": kind, "call_id": call_id, "name": name, "arguments": arguments}
        elif kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ValueError
            blocks = []
            for block in item["content"]:
                if not isinstance(block, dict):
                    raise ValueError
                if block.get("type") == "refusal":
                    raise ModelError("model_refusal", "模型拒绝了本次请求，未执行工具。")
                if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    raise ValueError
                block["text"].encode("utf-8")
                texts.append(block["text"])
                blocks.append({"type": "output_text", "text": block["text"], "annotations": []})
            if not blocks:
                raise ValueError
            native = {"type": "message", "role": "assistant", "content": blocks}
            if item.get("phase") is not None:
                if item["phase"] not in {"commentary", "final_answer"}:
                    raise ValueError
                native["phase"] = item["phase"]
        elif kind == "reasoning":
            encrypted = item.get("encrypted_content")
            if not _identifier(item.get("id")) or not _identifier(encrypted):
                raise ModelError("model_context_missing", "推理响应缺少可回传的加密上下文，无法保证完整工具往返。")
            encrypted.encode("utf-8")
            # Intentionally discard plaintext content and summaries. The model
            # service alone can interpret encrypted_content on the next call.
            native = {"type": "reasoning", "summary": [], "encrypted_content": encrypted}
        else:
            raise ValueError
        if "id" in item:
            if not _identifier(item["id"]):
                raise ValueError
            native["id"] = item["id"]
        if item.get("status") is not None:
            native["status"] = "completed"
        replay.append(native)
    text = "\n".join(texts).strip() or None
    if not text and not calls:
        raise ValueError
    return text, calls, replay


class ResponsesModel(MessagesModel):
    """Responses wire protocol; inherits URL checks, timeout and no retries.

    reasoning_effort is optional: omit it unless the configured model supports
    the explicitly selected value. No model is changed or inferred here.
    """

    def __init__(self, base_url, model, api_key, auth="bearer", timeout=40, max_tokens=1200, *, reasoning_effort=None):
        super().__init__(base_url, model, api_key, auth, timeout, max_tokens)
        base = base_url.rstrip("/")
        self.endpoint = base if base.endswith("/responses") else base + ("/responses" if base.endswith("/v1") else "/v1/responses")
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}):
            raise ModelError("model_config", "推理强度配置无效。")
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_env(cls):
        return cls(
            base_url=os.environ.get("AGENT_BASE_URL", ""),
            model=os.environ.get("AGENT_MODEL", ""),
            api_key=os.environ.get("AGENT_API_KEY", ""),
            auth=os.environ.get("AGENT_AUTH", "bearer"),
            max_tokens=int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "1200")),
            timeout=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "40")),
            reasoning_effort=os.environ.get("AGENT_REASONING_EFFORT") or None,
        )

    def _headers(self):
        headers = super()._headers()
        headers.pop("anthropic-version", None)
        return headers

    def _payload(self, messages, tools):
        # Reuse existing validation of roles, schemas and complete call/result
        # pairs before considering native replay data. This prevents replay
        # fields from bypassing the runtime's internal message contract.
        validated = _json_loads(super()._payload(messages, tools))
        try:
            inputs = []
            for message in messages:
                role = message["role"]
                if role == "system":
                    continue
                if role == "tool":
                    inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
                    continue
                native = message.get("provider_items", [])
                if not isinstance(native, list) or (native and role != "assistant"):
                    raise ValueError
                if native:
                    native_text, native_calls, replay = _output_items(native)
                    expected_calls = [(c["id"], c["function"]["name"], _json_loads(c["function"]["arguments"])) for c in message.get("tool_calls", [])]
                    actual_calls = [(c.id, c.name, _json_loads(c.arguments)) for c in native_calls]
                    if actual_calls != expected_calls or native_text != ((message.get("content") or "").strip() or None):
                        raise ValueError
                    inputs.extend(replay)
                    continue
                if message.get("content"):
                    inputs.append({"role": role, "content": message["content"]})
                for call in message.get("tool_calls", []):
                    inputs.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
            payload = {"model": self.model, "input": inputs, "max_output_tokens": self.max_tokens, "store": False,
                       "include": ["reasoning.encrypted_content"]}
            if "system" in validated:
                payload["instructions"] = validated["system"]
            if tools:
                payload["tools"] = [{"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"], "strict": False} for tool in validated["tools"]]
            if self.reasoning_effort is not None:
                payload["reasoning"] = {"effort": self.reasoning_effort}
            return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
            raise ModelError("model_input", "Responses 历史或原生上下文与工具调用配对不一致。") from None

    def _parse(self, raw):
        try:
            result = _json_loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            if result.get("status") == "incomplete":
                raise ModelError("model_truncated", "模型响应未完成，不能执行其中的工具调用。")
            if (result.get("status") != "completed" or result.get("error") is not None
                    or result.get("incomplete_details") is not None):
                raise ValueError
            text, calls, replay = _output_items(result.get("output"))
            # A completed HTTP response can contain only a progress note.
            # Check new replies here, not in _output_items: history replay may
            # legitimately include earlier commentary alongside later results.
            text_messages = [item for item in replay if item["type"] == "message"
                             and any(block["text"].strip() for block in item["content"])]
            if not calls and all(item.get("phase") == "commentary" for item in text_messages):
                raise ModelError("model_incomplete", "模型只返回了进展说明，尚未提供最终答案或工具调用。")
            usage = result.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {k: v for k, v in usage.items() if k in {"input_tokens", "output_tokens", "total_tokens"} and type(v) is int and v >= 0}
            return ModelReply(content=text, tool_calls=calls, provider_items=replay)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
            raise ModelError("model_protocol", "模型响应不符合 Responses 协议，不能继续执行。") from None
