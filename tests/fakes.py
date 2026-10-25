"""Test doubles. ScriptedLLM plays a model whose tool calls are fixed by the test,
so we test OUR code (tool execution, gates, guardrails) deterministically and offline."""
from itertools import count

from assistant.llm import LLMResponse
from evals.inprocess_bank import InProcessBank  # noqa: F401 (re-exported for tests)


_ids = count(1)


def tool(name, **inp):
    return {"type": "tool_use", "id": f"tu_{next(_ids)}", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


class ScriptedLLM:
    """Each call pops the next scripted list of blocks."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def complete(self, system, messages, tools):
        self.calls.append(messages)
        blocks = self.steps.pop(0)
        return LLMResponse(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn")
