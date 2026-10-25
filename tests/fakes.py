"""Test doubles. ScriptedLLM plays a model whose tool calls are fixed by the test,
so we test OUR code (tool execution, gates, guardrails) deterministically and offline."""
from itertools import count

from assistant.bank_client import BankError
from assistant.llm import LLMResponse
from mock_bank.service import BankService, ServiceError


class InProcessBank:
    """BankPort adapter straight onto BankService (no HTTP)."""

    def __init__(self, service=None):
        self.svc = service or BankService()

    def _wrap(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ServiceError as e:
            raise BankError(e.code, e.message, e.status)

    def list_accounts(self, cid):
        return self._wrap(self.svc.list_accounts, cid)

    def get_balance(self, cid, acc):
        return self._wrap(self.svc.get_balance, cid, acc)

    def search_transactions(self, cid, acc, **f):
        from decimal import Decimal
        for k in ("min_amount", "max_amount"):
            if f.get(k) is not None:
                f[k] = Decimal(f[k])
        f = {k: v for k, v in f.items() if v is not None}
        return self._wrap(self.svc.search_transactions, cid, acc, **f)

    def request_cheque_book(self, cid, acc, leaves, key):
        return self._wrap(self.svc.request_cheque_book, cid, acc, leaves, key)


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
