"""Tools exposed to the model + the executor that runs them.

Security-relevant properties:
  * `customer_id` is NOT a tool parameter. It comes from the verified session.
  * The bank re-checks account ownership on every call (defence in depth).
  * Cheque book is a two-step, server-enforced flow: stage -> customer confirms in a later
    message -> execute. The model cannot skip the gate by passing a flag.
  * Execution uses an idempotency key, so retries can never raise a duplicate request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from common.logging_setup import logf

from .bank_client import BankError, BankPort
from .sessions import Session

log = logging.getLogger("assistant.tools")

TOOLS: list[dict] = [
    {
        "name": "get_accounts",
        "description": "List the authenticated customer's accounts (account_id, type, masked number). Call this first when you need an account_id.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_balance",
        "description": "Get the current ledger and available balance of one of the customer's accounts.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string", "description": "From get_accounts."}},
            "required": ["account_id"],
        },
    },
    {
        "name": "search_transactions",
        "description": "Search the customer's recent transactions on one account (newest first). Use filters to narrow down a transaction the customer is asking about. Includes merchant, channel, category, status and notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["debit", "credit"]},
                "min_amount": {"type": "number", "description": "Absolute amount, inclusive."},
                "max_amount": {"type": "number", "description": "Absolute amount, inclusive."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
                "text": {"type": "string", "description": "Case-insensitive match on narration, merchant, category or channel."},
                "limit": {"type": "integer", "description": "Default 10, max 25."},
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "request_cheque_book",
        "description": "Raise a cheque book request. Two-step: call with customer_confirmed=false to stage it (nothing is raised), tell the customer the details and ask for confirmation, then, only after they confirm in a later message, call again with customer_confirmed=true and the same account_id and leaves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "leaves": {"type": "integer", "enum": [25, 50], "description": "Default 25."},
                "customer_confirmed": {"type": "boolean"},
            },
            "required": ["account_id", "customer_confirmed"],
        },
    },
]


@dataclass
class ToolOutcome:
    result: dict
    is_error: bool = False
    card: dict | None = None  # deterministic UI card built from bank data (not from model text)


class ToolExecutor:
    def __init__(self, bank: BankPort, session: Session):
        self.bank, self.s = bank, session

    def execute(self, name: str, args: dict) -> ToolOutcome:
        fn = {
            "get_accounts": self._accounts,
            "get_balance": self._balance,
            "search_transactions": self._txns,
            "request_cheque_book": self._cheque,
        }.get(name)
        if fn is None:
            return ToolOutcome({"error": {"code": "UNKNOWN_TOOL", "message": f"No tool named {name}."}}, True)
        try:
            out = fn(args or {})
        except BankError as e:
            logf(log, logging.WARNING, "tool_error", tool=name, code=e.code, status=e.status)
            return ToolOutcome({"error": {"code": e.code, "message": e.message, "retryable": e.retryable}}, True)
        except (KeyError, TypeError, ValueError) as e:
            return ToolOutcome({"error": {"code": "BAD_ARGUMENTS", "message": f"Invalid arguments: {e}"}}, True)
        logf(log, logging.INFO, "tool_call", tool=name, ok=not out.is_error, session=self.s.id[:8])  # no PII in logs
        if not out.is_error:
            self.s.grounded_text += "\n" + json.dumps(out.result, ensure_ascii=False)
        return out

    # ---------------------------------------------------------------- tools
    def _accounts(self, a: dict) -> ToolOutcome:
        return ToolOutcome({"accounts": self.bank.list_accounts(self.s.customer_id)})

    def _balance(self, a: dict) -> ToolOutcome:
        b = self.bank.get_balance(self.s.customer_id, a["account_id"])
        card = {"type": "balance", "data": {k: b[k] for k in (
            "account_type", "account_number_masked", "ledger_balance_display",
            "available_balance_display", "pending_holds_display", "as_of")}}
        return ToolOutcome(b, card=card)

    def _txns(self, a: dict) -> ToolOutcome:
        filt = {k: a.get(k) for k in ("direction", "date_from", "date_to", "text", "limit")}
        for k in ("min_amount", "max_amount"):
            filt[k] = None if a.get(k) is None else str(a[k])
        r = self.bank.search_transactions(self.s.customer_id, a["account_id"], **filt)
        items = [
            {k: t[k] for k in ("transaction_id", "date", "direction", "amount_display", "merchant", "channel", "status")}
            for t in r["transactions"][:5]
        ]
        card = {"type": "transactions", "data": {"total_matches": r["total_matches"], "items": items}} if items else None
        return ToolOutcome(r, card=card)

    def _cheque(self, a: dict) -> ToolOutcome:
        s = self.s
        account_id = a["account_id"]
        leaves = int(a.get("leaves") or 25)
        if a.get("customer_confirmed") not in (True, "true", "True"):
            s.pending_cheque = {"account_id": account_id, "leaves": leaves, "turn": s.turn_no, "nonce": uuid.uuid4().hex}
            # Validate the account now so we don't ask the customer to confirm something impossible.
            self.bank.get_balance(s.customer_id, account_id)
            return ToolOutcome({
                "status": "AWAITING_CUSTOMER_CONFIRMATION",
                "account_id": account_id,
                "leaves": leaves,
                "delivery": "customer's registered address on file",
                "instruction": "Nothing has been raised yet. Ask the customer to confirm. Do not say the request is raised.",
            })
        p = s.pending_cheque
        if not p or p["account_id"] != account_id or p["leaves"] != leaves or s.turn_no <= p["turn"]:
            return ToolOutcome({"error": {
                "code": "CONFIRMATION_REQUIRED",
                "message": "The customer has not confirmed this exact request in a later message. Stage it, ask them to confirm, and wait for their reply.",
            }}, True)
        key = hashlib.sha256(f"{s.id}:{p['nonce']}".encode()).hexdigest()
        rec = self.bank.request_cheque_book(s.customer_id, account_id, leaves, key)
        s.pending_cheque = None
        s.cheque_requests_raised.append(rec["request_id"])
        card = {"type": "service_request", "data": {k: rec[k] for k in (
            "request_id", "status", "account_type", "account_number_masked", "leaves",
            "delivery", "expected_delivery_date")}}
        return ToolOutcome(rec, card=card)
