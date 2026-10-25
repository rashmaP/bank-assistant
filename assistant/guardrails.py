"""Defence-in-depth grounding check on the model's final text.

Primary control = the model only gets numbers from tools and is told to copy them.
This check is the safety net: any rupee amount or SR-/TXN- reference in the reply that
never appeared in a tool result this session blocks the reply.
It is deliberately heuristic (regex) and cannot catch every kind of wrong statement;
the review discussion notes what a production version would add.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONEY = re.compile(r"(?:₹|Rs\.?\s?|INR\s?)\s?(\d[\d,]*(?:\.\d+)?)", re.I)
_REF = re.compile(r"\b(?:SR|TXN)-[A-Z0-9-]+\b")
_RAISED_CLAIM = re.compile(
    r"(cheque book|request)[^.?!]{0,60}\b(has been|have been|was|is now|is)\s+(raised|submitted|placed|registered|created)",
    re.I,
)


def _to_decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[Decimal]:
    return {d for m in _NUM.findall(text) if (d := _to_decimal(m)) is not None}


def find_ungrounded(reply: str, grounded_text: str, cheque_requests_raised: list[str]) -> list[str]:
    problems: list[str] = []
    known = numbers_in(grounded_text)
    for m in _MONEY.findall(reply):
        d = _to_decimal(m)
        if d is not None and d not in known:
            problems.append(f"amount {m}")
    for ref in _REF.findall(reply):
        if ref not in grounded_text:
            problems.append(f"reference {ref}")
    if _RAISED_CLAIM.search(reply) and not cheque_requests_raised:
        problems.append("claims a request was raised, but none was")
    return problems
