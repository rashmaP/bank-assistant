"""LLM port + Anthropic adapter. Blocks are plain dicts so the agent is SDK-agnostic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResponse:
    blocks: list[dict]  # {"type":"text","text":..} | {"type":"tool_use","id","name","input"}
    stop_reason: str


class LLM(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class LLMUnavailable(Exception):
    pass


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, max_tokens: int = 1024):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example).")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=30.0, max_retries=2)
        self._model, self._max_tokens = model, max_tokens

    def complete(self, system, messages, tools) -> LLMResponse:
        try:
            r = self._client.messages.create(
                model=self._model, max_tokens=self._max_tokens, temperature=0,
                system=system, messages=messages, tools=tools,
            )
        except self._anthropic.APIError as e:
            raise LLMUnavailable(str(e)) from e
        blocks: list[dict] = []
        for b in r.content:
            if b.type == "text" and b.text:
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return LLMResponse(blocks=blocks, stop_reason=r.stop_reason)
