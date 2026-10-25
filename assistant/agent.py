"""The conversational agent: an LLM tool-use loop with server-side guardrails."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .bank_client import BankPort
from .config import Settings
from .guardrails import find_ungrounded
from .llm import LLM
from .prompts import build_system_prompt
from .sessions import Session
from .tools import TOOLS, ToolExecutor

log = logging.getLogger("assistant.agent")

FALLBACK = ("I'm sorry, I couldn't confirm that information reliably just now, so I'd rather not guess. "
            "Please try asking again, or check the banking app or call the bank.")
STEP_LIMIT_MSG = "I'm having trouble completing that. Please try again, or use the banking app or call the bank."


@dataclass
class AgentReply:
    text: str
    cards: list[dict] = field(default_factory=list)


class Agent:
    def __init__(self, llm: LLM, bank: BankPort, settings: Settings | None = None, today: date | None = None):
        self.llm, self.bank, self.cfg, self._today = llm, bank, settings or Settings(), today

    def handle(self, session: Session, user_text: str) -> AgentReply:
        session.turn_no += 1
        system = build_system_prompt(self._today)
        executor = ToolExecutor(self.bank, session)
        turn: list[dict] = [{"role": "user", "content": user_text}]
        cards: list[dict] = []
        text = STEP_LIMIT_MSG

        for _ in range(self.cfg.max_agent_steps):
            resp = self.llm.complete(system, session.messages() + turn, TOOLS)
            turn.append({"role": "assistant", "content": resp.blocks})
            calls = [b for b in resp.blocks if b["type"] == "tool_use"]
            if not calls:
                text = "\n".join(b["text"] for b in resp.blocks if b["type"] == "text").strip()
                text = self._ground(session, system, turn, text)
                turn[-1] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
                break
            results = []
            for c in calls:
                out = executor.execute(c["name"], c["input"])
                if out.card:
                    cards.append(out.card)
                results.append({"type": "tool_result", "tool_use_id": c["id"],
                                "content": _dump(out.result), "is_error": out.is_error})
            turn.append({"role": "user", "content": results})
        else:
            turn.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            cards = []

        session.turns.append(turn)
        del session.turns[: -self.cfg.max_history_turns]  # trim on turn boundaries only
        return AgentReply(text=text, cards=cards)

    def _ground(self, session: Session, system: str, turn: list[dict], text: str) -> str:
        problems = find_ungrounded(text, session.grounded_text, session.cheque_requests_raised)
        if not problems:
            return text
        log.warning("ungrounded reply blocked: %s", problems)
        note = ("Your draft contained values not found in tool results (" + "; ".join(problems) + "). "
                "Rewrite it using only values returned by tools, or call a tool to get them.")
        retry = self.llm.complete(system, session.messages() + turn + [{"role": "user", "content": note}], TOOLS)
        if any(b["type"] == "tool_use" for b in retry.blocks):
            return FALLBACK  # keep it simple: no tool calls inside the repair attempt
        text2 = "\n".join(b["text"] for b in retry.blocks if b["type"] == "text").strip()
        if text2 and not find_ungrounded(text2, session.grounded_text, session.cheque_requests_raised):
            return text2
        return FALLBACK


def _dump(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
