"""An explicit offline simulator, never evidence of real model intelligence."""
import json
import re
import uuid
from .models import ModelReply, ToolCall


class DemoModel:
    def complete(self, messages, tools):
        latest = messages[-1]
        if latest["role"] == "tool":
            return ModelReply("[离线模拟] 工具返回：" + latest["content"])
        text = latest["content"]
        for prefix, name, field in (("计算 ", "calculator", "expression"),
                                    ("搜索 ", "search", "query"),
                                    ("文档 ", "read_docs", "document_id")):
            if text.startswith(prefix):
                return ModelReply("[离线模拟] 按固定规则调用工具。", [ToolCall(
                    uuid.uuid4().hex, name, json.dumps({field: text[len(prefix):]}, ensure_ascii=False))])
        if text == "回顾":
            previous = [m["content"] for m in messages[:-1] if m["role"] == "user"]
            return ModelReply("[离线模拟] 本会话可见的用户历史：" + json.dumps(previous, ensure_ascii=False))
        return ModelReply("[离线模拟] 收到。可输入：计算 7+8 / 搜索 Agent / 文档 session / 回顾。")
