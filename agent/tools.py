"""Readonly tools with strict input validation and one JSON-safe result format.

The model supplies a tool name and a JSON string. It never selects a Python
function or filesystem path directly. New tools join the explicit registry.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import operator
import re
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator


MAX_ARGUMENT_CHARS = 16_384
MAX_RESULT_BYTES = 65_536
MAX_EXPRESSION_CHARS = 512
MAX_AST_NODES = 64
MAX_AST_DEPTH = 16
MAX_ABSOLUTE_VALUE = 10**100
MAX_EXPONENT = 100
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DOCUMENT_IDS = ("runtime", "session", "memory")

_ERROR_MESSAGES = {
    "unknown_tool": "Tool is not registered.",
    "invalid_json": "Tool arguments must be a valid JSON object.",
    "invalid_arguments": "Tool arguments do not match the declared schema.",
    "arguments_too_large": "Tool arguments exceed the size limit.",
    "invalid_expression": "Use only numbers, parentheses and + - * / // % **.",
    "calculation_limit": "Expression exceeds the calculation limits.",
    "arithmetic_error": "Calculation is undefined or outside the finite number range.",
    "invalid_query": "Search query must contain non-whitespace text.",
    "document_not_found": "Document ID is not in the allowlist.",
    "tool_execution_failed": "Tool execution failed. No internal details are exposed.",
    "invalid_tool_result": "Tool did not return a bounded, valid JSON result.",
}


class ToolError(Exception):
    """An expected failure identified by a fixed, safe error code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_MESSAGES else "tool_execution_failed"
        super().__init__(_ERROR_MESSAGES[self.code])


def _error(code: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": _ERROR_MESSAGES[code]}}


def _reject_constant(_: str) -> None:
    # Python's JSON decoder otherwise accepts NaN/Infinity, outside JSON's spec.
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class ToolRegistry:
    """Own the three boundaries: registry lookup, schema check, result parsing."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[dict[str, Any], Draft202012Validator, Callable[..., Any]]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError("Tool name must be a simple identifier")
        if name in self._tools:
            raise ValueError("Tool name is already registered")
        if not isinstance(description, str) or not description.strip() or not callable(handler):
            raise ValueError("Tool needs a description and a callable handler")
        schema = copy.deepcopy(parameters)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Tool schema must declare an object and reject additional properties")
        definition = {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": schema},
        }
        self._tools[name] = (definition, Draft202012Validator(schema), handler)

    def schemas(self) -> list[dict[str, Any]]:
        # Prevent provider/model code from changing validation through an alias.
        return [copy.deepcopy(tool[0]) for tool in self._tools.values()]

    def execute(self, name: str, arguments: str) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self._tools:
            return _error("unknown_tool")
        if not isinstance(arguments, str):
            return _error("invalid_json")
        if len(arguments) > MAX_ARGUMENT_CHARS:
            return _error("arguments_too_large")
        try:
            params = json.loads(arguments, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            # Overflowing exponent notation (1e999) does not use parse_constant.
            json.dumps(params, allow_nan=False)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return _error("invalid_json")
        _, validator, handler = self._tools[name]
        try:
            if not validator.is_valid(params):
                return _error("invalid_arguments")
        except Exception:
            return _error("invalid_arguments")
        try:
            data = handler(**params)
        except ToolError as exc:
            return _error(exc.code)
        except Exception:
            # Exception text can contain URLs, credentials or upstream responses.
            return _error("tool_execution_failed")
        try:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                return _error("invalid_tool_result")
            # A round trip guarantees a plain JSON value, independent of handler.
            parsed = json.loads(encoded)
        except Exception:
            return _error("invalid_tool_result")
        return {"ok": True, "data": parsed}


def _bounded_number(value: Any) -> int | float:
    if type(value) not in (int, float):
        raise ToolError("invalid_expression")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolError("arithmetic_error")
    if abs(value) > MAX_ABSOLUTE_VALUE:
        raise ToolError("calculation_limit")
    return value


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def calculator(expression: str) -> dict[str, Any]:
    """Interpret an arithmetic AST without eval, names, calls or attributes."""
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("invalid_expression")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ToolError("calculation_limit")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ToolError("invalid_expression") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ToolError("calculation_limit")

    def visit(node: ast.AST, depth: int = 0) -> int | float:
        if depth > MAX_AST_DEPTH:
            raise ToolError("calculation_limit")
        if isinstance(node, ast.Constant):
            return _bounded_number(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            number = visit(node.operand, depth + 1)
            return _bounded_number(number if isinstance(node.op, ast.UAdd) else -number)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = visit(node.left, depth + 1)
            right = visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ToolError("calculation_limit")
            try:
                value = _BINARY_OPERATORS[type(node.op)](left, right)
            except (ArithmeticError, ValueError):
                raise ToolError("arithmetic_error") from None
            if isinstance(value, complex):
                raise ToolError("arithmetic_error")
            return _bounded_number(value)
        raise ToolError("invalid_expression")

    return {"expression": expression, "value": visit(tree.body)}


def search(query: str) -> dict[str, Any]:
    """A deterministic local mock, explicitly not an Internet search service."""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("invalid_query")
    if len(query) > 200:
        raise ToolError("invalid_query")
    records = json.loads((DATA_DIR / "search.json").read_text(encoding="utf-8"))
    terms = set(re.findall(r"\w+", query.casefold()))
    ranked = []
    for record in records:
        text = f"{record['title']} {record['snippet']}".casefold()
        score = sum(text.count(term) for term in terms)
        if score:
            ranked.append((score, record))
    ranked.sort(key=lambda item: (-item[0], item[1]["document_id"]))
    return {"source": "mock-local-corpus", "query": query, "results": [item[1] for item in ranked[:3]]}


def read_docs(document_id: str) -> dict[str, Any]:
    """Only these IDs are accepted; model input never becomes a file path."""
    if document_id not in DOCUMENT_IDS:
        raise ToolError("document_not_found")
    documents = json.loads((DATA_DIR / "documents.json").read_text(encoding="utf-8"))
    return documents[document_id]


def make_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "calculator",
        "计算有限实数四则运算、整除、取余和幂；表达式限512字符，不支持变量或函数。",
        {
            "type": "object",
            "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": MAX_EXPRESSION_CHARS}},
            "required": ["expression"],
            "additionalProperties": False,
        },
        calculator,
    )
    registry.register(
        "search",
        "在 mock 本地演示语料中按关键词搜索 Agent、会话、记忆；不联网，不提供实时信息。",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
        search,
    )
    registry.register(
        "read_docs",
        "读取内置只读文档：runtime 主循环、session 会话隔离、memory 上下文与压缩。只接受文档 ID。",
        {
            "type": "object",
            "properties": {"document_id": {"type": "string", "enum": list(DOCUMENT_IDS)}},
            "required": ["document_id"],
            "additionalProperties": False,
        },
        read_docs,
    )
    return registry
