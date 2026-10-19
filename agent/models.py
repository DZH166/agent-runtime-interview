"""Small transport boundary: the runtime never depends on a vendor SDK."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ModelReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Vendor continuation items may contain opaque encrypted state, never decoded.
    provider_items: list[dict] = field(default_factory=list, repr=False)


class ModelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...
