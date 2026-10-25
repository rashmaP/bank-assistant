"""Scenario runner for evaluating the assistant against a REAL model.

Each run gets a fresh in-process mock bank and session, so scenarios are independent.
Checks look at observable outcomes (tool calls, bank state, cards, reply text) rather than
exact wording, because the model's phrasing varies.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from assistant.agent import FALLBACK, Agent
from assistant.config import Settings
from assistant.guardrails import find_ungrounded
from assistant.llm import LLMUnavailable
from assistant.sessions import Session
from evals.inprocess_bank import InProcessBank

ME = "CUST-1001"


@dataclass
class TurnResult:
    user: str
    reply: str
    cards: list[dict]
    tool_calls: list[tuple[str, dict]]
    requests_after: int  # number of cheque-book requests in the bank after this turn


@dataclass
class Ctx:
    bank: InProcessBank
    session: Session
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def last(self) -> TurnResult:
        return self.turns[-1]


@dataclass
class Scenario:
    name: str
    turns: list[str]
    check: Callable[[Ctx], list[str]]
    setup: Callable[[InProcessBank], None] | None = None
    tags: tuple[str, ...] = ()


@dataclass
class RunResult:
    scenario: str
    passed: bool
    problems: list[str]
    seconds: float


def _tool_calls(turn_msgs: list[dict]) -> list[tuple[str, dict]]:
    return [(b["name"], b["input"]) for m in turn_msgs if m["role"] == "assistant" and isinstance(m["content"], list)
            for b in m["content"] if b["type"] == "tool_use"]


def run_scenario(sc: Scenario, llm, retries: int = 2, retry_wait: float = 5.0, pause: float = 0.0) -> RunResult:
    t0 = time.time()
    bank = InProcessBank()
    if sc.setup:
        sc.setup(bank)
    session = Session(id="eval", customer_id=ME)
    ctx = Ctx(bank, session)
    agent = Agent(llm, bank, Settings(), today=bank.svc.today)
    problems: list[str] = []
    try:
        for user in sc.turns:
            for attempt in range(retries + 1):
                try:
                    reply = agent.handle(session, user)
                    break
                except LLMUnavailable as e:  # rate limits / transient provider errors
                    if attempt == retries:
                        raise
                    time.sleep(retry_wait)
            ctx.turns.append(TurnResult(user, reply.text, reply.cards, _tool_calls(session.turns[-1]),
                                        len(bank.svc._requests)))
            if pause:
                time.sleep(pause)
        # Universal checks on every reply
        for t in ctx.turns:
            if not t.reply.strip():
                problems.append("empty reply")
            if t.reply == FALLBACK:
                problems.append("guardrail fallback fired (model produced ungrounded figures)")
            bad = find_ungrounded(t.reply, session.grounded_text, session.cheque_requests_raised)
            if bad:
                problems.append(f"ungrounded values in reply: {bad}")
        problems += sc.check(ctx)
    except Exception as e:  # provider down, bug, etc. counts as a failure with a reason
        problems.append(f"{type(e).__name__}: {e}")
    return RunResult(sc.name, not problems, problems, time.time() - t0)
