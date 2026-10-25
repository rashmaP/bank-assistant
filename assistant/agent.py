"""The conversational agent: an LLM tool-use loop with server-side guardrails."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date

from common.logging_setup import logf

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
        # If anything fails mid-turn the turn is discarded, so also roll back the state it touched.
        pending_before, turn_before = session.pending_cheque, session.turn_no
        session.turn_no += 1
        t0 = time.perf_counter()
        try:
            reply, stats = self._run(session, user_text)
        except Exception:
            session.pending_cheque, session.turn_no = pending_before, turn_before
            raise
        logf(log, logging.INFO, "turn", session=session.id[:8], turn=session.turn_no, cards=len(reply.cards),
             ms=round((time.perf_counter() - t0) * 1000), **stats)
        return reply

    def _run(self, session: Session, user_text: str) -> tuple[AgentReply, dict]:
        system = build_system_prompt(self._today)
        executor = ToolExecutor(self.bank, session)
        turn: list[dict] = [{"role": "user", "content": user_text}]
        cards: list[dict] = []
        text = STEP_LIMIT_MSG
        stats = {"llm_calls": 0, "tools": [], "grounding_blocked": False, "step_limit": False}

        for _ in range(self.cfg.max_agent_steps):
            resp = self.llm.complete(system, session.messages() + turn, TOOLS)
            stats["llm_calls"] += 1
            turn.append({"role": "assistant", "content": resp.blocks})
            calls = [b for b in resp.blocks if b["type"] == "tool_use"]
            if not calls:
                text = "\n".join(b["text"] for b in resp.blocks if b["type"] == "text").strip()
                text, blocked, extra_calls = self._ground(session, system, turn, text)
                stats["grounding_blocked"], stats["llm_calls"] = blocked, stats["llm_calls"] + extra_calls
                turn[-1] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
                break
            results = []
            for c in calls:
                out = executor.execute(c["name"], c["input"])
                stats["tools"].append(c["name"] + ("" if not out.is_error else ":error"))
                if out.card:
                    cards.append(out.card)
                results.append({"type": "tool_result", "tool_use_id": c["id"],
                                "content": json.dumps(out.result, ensure_ascii=False), "is_error": out.is_error})
            turn.append({"role": "user", "content": results})
        else:
            stats["step_limit"] = True
            turn.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            cards = []

        session.turns.append(turn)
        del session.turns[: -self.cfg.max_history_turns]  # trim on turn boundaries only
        return AgentReply(text=text, cards=cards), stats

    def _ground(self, session: Session, system: str, turn: list[dict], text: str) -> tuple[str, bool, int]:
        """Returns (text, was_blocked, extra_llm_calls)."""
        problems = find_ungrounded(text, session.grounded_text, session.cheque_requests_raised)
        if not problems:
            return text, False, 0
        logf(log, logging.WARNING, "ungrounded_reply_blocked", problems=problems)
        note = ("Your draft contained values not found in tool results (" + "; ".join(problems) + "). "
                "Rewrite it using only values returned by tools, or call a tool to get them.")
        retry = self.llm.complete(system, session.messages() + turn + [{"role": "user", "content": note}], TOOLS)
        if any(b["type"] == "tool_use" for b in retry.blocks):
            return FALLBACK, True, 1  # keep it simple: no tool calls inside the repair attempt
        text2 = "\n".join(b["text"] for b in retry.blocks if b["type"] == "text").strip()
        if text2 and not find_ungrounded(text2, session.grounded_text, session.cheque_requests_raised):
            return text2, True, 1
        return FALLBACK, True, 1
