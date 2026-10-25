"""Adapter for any OpenAI-compatible chat API (Groq, Google Gemini, GitHub Models, OpenRouter...).

The agent speaks Anthropic-style blocks internally; this translates to/from the
OpenAI chat-completions format so the rest of the code does not change.
"""
from __future__ import annotations

import json

from .llm import LLMResponse, LLMUnavailable


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if m["role"] == "user":
            if isinstance(c, str):
                out.append({"role": "user", "content": c})
                continue
            for b in c:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
        else:
            texts = [b["text"] for b in c if b["type"] == "text"]
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                for b in c if b["type"] == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]


def from_openai_message(choice: dict) -> LLMResponse:
    msg = choice["message"]
    blocks: list[dict] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
    return LLMResponse(blocks=blocks, stop_reason=choice.get("finish_reason") or "stop")


class OpenAICompatLLM:
    def __init__(self, api_key: str, model: str, base_url: str, max_tokens: int = 1024, client=None):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY (your provider's key) is not set.")
        if client is None:
            import httpx
            client = httpx.Client(timeout=30.0)
        self._http, self._key, self._model = client, api_key, model
        self._url, self._max_tokens = base_url.rstrip("/") + "/chat/completions", max_tokens

    def complete(self, system, messages, tools) -> LLMResponse:
        body = {"model": self._model, "max_tokens": self._max_tokens, "temperature": 0,
                "messages": to_openai_messages(system, messages), "tools": to_openai_tools(tools)}
        try:
            r = self._http.post(self._url, json=body, headers={"Authorization": f"Bearer {self._key}"})
        except Exception as e:  # network / timeout
            raise LLMUnavailable(str(e)) from e
        if r.status_code >= 400:
            raise LLMUnavailable(f"{r.status_code}: {r.text[:300]}")
        try:
            return from_openai_message(r.json()["choices"][0])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMUnavailable("malformed response from provider") from e
