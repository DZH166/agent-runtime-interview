"""Small Anthropic Messages adapter; the Agent loop lives in runtime.py.

Only this boundary knows the remote wire format. No automatic retries: an
HTTP timeout does not establish whether a remote request was processed.
"""
from __future__ import annotations

import json
import http.client
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .models import ModelError, ModelReply, ToolCall


MAX_RESPONSE_BYTES = 1_048_576
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Let urllib raise HTTPError instead of forwarding Authorization.
        return None


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _json_loads(raw):
    return json.loads(raw, parse_constant=_reject_constant)


def _name(value):
    return isinstance(value, str) and _TOOL_NAME.fullmatch(value) is not None


def _identifier(value):
    return isinstance(value, str) and bool(value.strip())


def _http_error(status):
    """Retain only a numeric status for structured traces, never the body."""
    if type(status) is int and 100 <= status <= 599:
        error = ModelError("model_http", f"模型接口返回 HTTP {status}。未自动重试。")
        error.http_status = status
        return error
    return ModelError("model_http", "模型接口返回异常 HTTP 状态。未自动重试。")


class MessagesModel:
    """A single synchronous HTTP request per complete() call.

    request_count counts attempted HTTP requests, including failures. last_usage
    contains only numeric token counters from the last valid reply, if supplied.
    Credentials and response bodies are never included in error messages.
    HTTP failures expose a numeric ModelError.http_status for structured traces.
    """

    def __init__(self, base_url, model, api_key, auth="bearer", timeout=40, max_tokens=1200):
        try:
            if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
                raise ValueError
            parts = urllib.parse.urlsplit(base_url)
            local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
            if (not parts.hostname or parts.username is not None or parts.password is not None
                    or parts.query or parts.fragment or "?" in base_url or "#" in base_url
                    or "\\" in base_url or (parts.scheme != "https" and not (parts.scheme == "http" and local))):
                raise ValueError
            _ = parts.port  # Also reject invalid/out-of-range ports.
            if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
                raise ValueError
            if not isinstance(api_key, str) or not api_key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
                raise ValueError
            if auth not in {"bearer", "x-api-key"}:
                raise ValueError
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
            if type(max_tokens) is not int or max_tokens <= 0:
                raise ValueError
        except (ValueError, TypeError):
            raise ModelError("model_config", "模型配置无效：需要 HTTPS 地址、模型、凭据及有效超时。") from None
        base = base_url.rstrip("/")
        self.endpoint = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        self.model = model.strip()
        self._api_key = api_key
        self.auth = auth
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.request_count = 0
        self.last_usage = {}
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(
            base_url=os.environ.get("AGENT_BASE_URL", ""),
            model=os.environ.get("AGENT_MODEL", ""),
            api_key=os.environ.get("AGENT_API_KEY", ""),
            auth=os.environ.get("AGENT_AUTH", "bearer"),
            max_tokens=int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "1200")),
            timeout=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "40")),
        )

    def _payload(self, messages, tools):
        """Translate complete history; never split a tool call/result group."""
        system, converted, pending, seen = [], [], set(), set()

        def append(role, blocks):
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})

        try:
            if not isinstance(messages, list) or not isinstance(tools, list):
                raise ValueError
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError
                role, content = message.get("role"), message.get("content")
                if role == "tool":
                    call_id = message.get("tool_call_id")
                    if not _identifier(call_id) or call_id not in pending or not isinstance(content, str):
                        raise ValueError
                    pending.remove(call_id)
                    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
                    # The registry's error envelope is still visible in content.
                    try:
                        parsed = _json_loads(content)
                        if isinstance(parsed, dict) and parsed.get("ok") is False:
                            block["is_error"] = True
                    except (ValueError, RecursionError):
                        pass
                    append("user", [block])
                    continue
                if pending:
                    raise ValueError
                if role == "system":
                    if not isinstance(content, str):
                        raise ValueError
                    system.append(content)
                    continue
                if role not in {"user", "assistant"} or (content is not None and not isinstance(content, str)):
                    raise ValueError
                blocks = [{"type": "text", "text": content}] if content else []
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list) or (calls and role != "assistant"):
                    raise ValueError
                for call in calls:
                    function = call["function"]
                    call_id = call["id"]
                    if call.get("type") != "function" or not _identifier(call_id) or call_id in seen or not _name(function["name"]):
                        raise ValueError
                    if not isinstance(function["arguments"], str):
                        raise ValueError
                    arguments = _json_loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError
                    pending.add(call_id)
                    seen.add(call_id)
                    blocks.append({"type": "tool_use", "id": call_id, "name": function["name"], "input": arguments})
                if not blocks:
                    raise ValueError
                append(role, blocks)
            if pending or not converted or converted[0]["role"] != "user" or converted[-1]["role"] != "user":
                raise ValueError
            translated_tools, tool_names = [], set()
            for tool in tools:
                function = tool["function"]
                if (tool.get("type") != "function" or not _name(function["name"])
                        or function["name"] in tool_names or not isinstance(function["parameters"], dict)
                        or not isinstance(function["description"], str)):
                    raise ValueError
                tool_names.add(function["name"])
                translated_tools.append({"name": function["name"], "description": function["description"], "input_schema": function["parameters"]})
            payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": converted}
            if system:
                payload["system"] = "\n\n".join(system)
            if translated_tools:
                payload["tools"] = translated_tools
            return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
            raise ModelError("model_input", "消息、工具定义或工具调用配对不符合协议。") from None

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json", "anthropic-version": "2023-06-01"}
        headers["Authorization" if self.auth == "bearer" else "x-api-key"] = "Bearer " + self._api_key if self.auth == "bearer" else self._api_key
        return headers

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.last_usage = {}
        body = self._payload(messages, tools)
        headers = self._headers()
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        self.request_count += 1
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise _http_error(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise _http_error(status) from None
        except (TimeoutError, socket.timeout):
            raise ModelError("model_timeout", "模型请求超时。未自动重试。") from None
        except urllib.error.URLError as exc:
            code = "model_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "model_network"
            raise ModelError(code, "模型请求超时。未自动重试。" if code == "model_timeout" else "模型网络请求失败。未自动重试。") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ModelError("model_network", "模型网络请求失败。未自动重试。") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelError("model_response_too_large", "模型响应超过大小限制。")
        return self._parse(raw)

    def _parse(self, raw):
        try:
            result = _json_loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                raise ValueError
            stop = result.get("stop_reason")
            if stop in {"max_tokens", "model_context_window_exceeded", "truncated"}:
                raise ModelError("model_truncated", "模型响应被截断，不能作为完整答案或工具调用执行。")
            if stop not in {"end_turn", "tool_use"}:
                raise ValueError
            texts, calls, ids = [], [], set()
            for block in result["content"]:
                if not isinstance(block, dict):
                    raise ValueError
                if block.get("type") == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                    if not _identifier(call_id) or call_id in ids or not _name(name) or not isinstance(arguments, dict):
                        raise ValueError
                    arguments_json = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
                    arguments_json.encode("utf-8")
                    ids.add(call_id)
                    calls.append(ToolCall(call_id, name, arguments_json))
                elif block.get("type") in {"thinking", "redacted_thinking"}:
                    # These blocks require lossless signed replay during tool
                    # use. This minimal Messages adapter does not support it.
                    raise ModelError("model_unsupported_thinking", "当前 Messages 适配器不支持 thinking 模式，不能省略其上下文后继续。")
                else:
                    raise ValueError
            content = "\n".join(texts).strip() or None
            if content:
                content.encode("utf-8")
            if (stop == "tool_use") != bool(calls) or (not content and not calls):
                raise ValueError
            usage = result.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {k: v for k, v in usage.items() if k in {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"} and type(v) is int and v >= 0}
            return ModelReply(content=content, tool_calls=calls)
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise ModelError("model_protocol", "模型响应不符合 Messages 协议，不能继续执行。") from None
