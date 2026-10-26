"""Claude client wrapper: one forced tool call per request, with token usage.

Every structured reply comes through a single tool, so the result is JSON that
matches the tool's schema. Usage is accumulated so each run can log its cost.
"""

from dataclasses import dataclass
from typing import Any

import anthropic


class LLMError(RuntimeError):
    """The model replied without calling the tool it was asked to use."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_tokens += usage.cache_read_input_tokens or 0
        self.cache_write_tokens += usage.cache_creation_input_tokens or 0

    def summary(self) -> str:
        return (
            f"{self.calls} calls, {self.input_tokens} input tokens, "
            f"{self.output_tokens} output tokens, {self.cache_read_tokens} cache-read, "
            f"{self.cache_write_tokens} cache-write"
        )


class Claude:
    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model
        self.usage = Usage()

    @classmethod
    def from_api_key(cls, api_key: str, model: str) -> "Claude":
        return cls(anthropic.Anthropic(api_key=api_key), model)

    def call_tool(
        self,
        *,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        """Send one request that must answer through `tool`. Returns the tool's input."""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            # Static instructions go in the system block, which is cached between calls.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        self.usage.add(response.usage)
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return dict(block.input)
        raise LLMError(f"model did not call {tool['name']}")
