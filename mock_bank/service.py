"""Pure-Python mocked core-banking logic (no web framework imports).

All data is synthetic. Dates are generated relative to "today" so questions such as
"what was that debit yesterday?" always have something to find.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fmt_inr(amount: Decimal) -> str:
    """Format with Indian digit grouping, e.g. ₹12,34,567.89."""
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


CUSTOMERS = {"CUST-1001": {"name": "Asha Rao"}, "CUST-1002": {"name": "Vikram Menon"}}

ACCOUNTS = {
    "ACC-SAV-001": dict(customer_id="CUST-1001", type="Savings", masked="XXXXXX4821", opening="40000.00"),
    "ACC-CUR-002": dict(customer_id="CUST-1001", type="Current", masked="XXXXXX9034", opening="25000.00"),
    "ACC-SAV-101": dict(customer_id="CUST-1002", type="Savings", masked="XXXXXX1177", opening="5000.00"),
}

# (days_ago, signed amount, narration, merchant, category, channel, status, note)
_P = "posted"
_SEED: dict[str, list[tuple]] = {
    "ACC-SAV-001": [
        (35, "95000.00", "NEFT CR-ACME TECH PVT LTD-SALARY", "Acme Tech Pvt Ltd", "Salary", "NEFT", _P, ""),
        (33, "-35000.00", "ACH D- HDFC HOME LOAN-EMI 7781", "Home loan EMI", "Loan repayment", "ACH auto-debit", _P, ""),
        (30, "-2200.00", "ATM WDL 000345 KORAMANGALA BLR", "ATM cash withdrawal", "Cash", "ATM", _P, ""),
        (27, "-1499.00", "POS 4821 DECATHLON SPORTS BLR", "Decathlon", "Shopping", "Debit card (POS)", _P, ""),
        (20, "-540.00", "UPI/DR/412398761120/BLUE TOKAI/okaxis", "Blue Tokai Coffee", "Food & dining", "UPI", _P, ""),
        (14, "-899.00", "ECOM 4821 MYNTRA DESIGNS PVT", "Myntra", "Shopping", "Debit card (online)", _P, "Refunded on a later date"),
        (13, "899.00", "REV-ECOM 4821 MYNTRA DESIGNS PVT", "Myntra (refund)", "Refund", "Debit card (online)", _P, "Refund of an earlier Myntra debit"),
        (9, "-118.00", "SMS ALERT CHARGES Q3 + GST", "Bank charges", "Fees", "Bank", _P, "Quarterly SMS alert fee incl. GST"),
        (6, "-349.50", "UPI/DR/412876540031/SWIGGY/swiggy@icici", "Swiggy", "Food & dining", "UPI", _P, ""),
        (5, "-12500.00", "UPI/DR/412345678901/RAHUL SHARMA/okhdfc", "Rahul Sharma (person-to-person)", "Transfer", "UPI", _P, ""),
        (3, "-1299.00", "ECOM 4821 AMZN MKTP IN*2K4TT1", "Amazon", "Shopping", "Debit card (online)", _P, ""),
        (3, "-1299.00", "ECOM 4821 AMZN MKTP IN*7J9QX0", "Amazon", "Shopping", "Debit card (online)", _P, "Same amount as another Amazon debit the same day; different order reference"),
        (2, "-2450.00", "ATM WDL 000781 INDIRANAGAR BLR", "ATM cash withdrawal", "Cash", "ATM", _P, ""),
        (1, "-649.00", "ECOM 4821 NETFLIX.COM 8004 BLR", "Netflix", "Subscriptions", "Debit card (online)", _P, "Recurring monthly subscription"),
        (0, "-1850.00", "POS 4821 BIGBASKET", "BigBasket", "Groceries", "Debit card (POS)", "pending", "Card authorisation hold; usually posts within 1-3 working days"),
    ],
    "ACC-CUR-002": [
        (20, "60000.00", "NEFT CR-FREELANCE PAYMENT-INV 204", "Freelance client payment", "Income", "NEFT", _P, ""),
        (12, "-15000.00", "CHQ PAID 000112 - PRIYA NAIR", "Cheque 000112 (Priya Nair)", "Transfer", "Cheque", _P, ""),
        (8, "-4999.00", "POS 7733 CROMA ELECTRONICS BLR", "Croma", "Shopping", "Debit card (POS)", _P, ""),
        (2, "-2360.00", "NACH D- BESCOM ELECTRICITY", "BESCOM", "Utilities", "NACH auto-debit", _P, ""),
    ],
    "ACC-SAV-101": [
        (10, "30000.00", "NEFT CR-SALARY", "Employer", "Salary", "NEFT", _P, ""),
        (4, "-799.00", "UPI/DR/413000000001/SPOTIFY/spotify@ybl", "Spotify", "Subscriptions", "UPI", _P, ""),
    ],
}

ALLOWED_LEAVES = (25, 50)


@dataclass
class _Txn:
    data: dict
    amount: Decimal


class BankService:
    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self._lock = threading.Lock()
        self._requests: dict[str, dict] = {}  # request_id -> record
        self._idem: dict[str, dict] = {}  # idempotency key -> response
        self._seq = 0
        self._txns: dict[str, list[_Txn]] = {}
        self._build()

    # ------------------------------------------------------------------ data
    def _build(self) -> None:
        n = 40_000_000
        for acc_id, rows in _SEED.items():
            running = Decimal(ACCOUNTS[acc_id]["opening"])
            out: list[_Txn] = []
            for days_ago, amt, narration, merchant, cat, chan, status, note in rows:
                amount = Decimal(amt)
                n += 7
                if status == "posted":
                    running += amount
                d = {
                    "transaction_id": f"TXN-{n}",
                    "date": (self.today - timedelta(days=days_ago)).isoformat(),
                    "direction": "credit" if amount > 0 else "debit",
                    "amount": f"{abs(amount):.2f}",
                    "amount_display": fmt_inr(abs(amount)),
                    "currency": "INR",
                    "narration": narration,
                    "merchant": merchant,
                    "category": cat,
                    "channel": chan,
                    "status": status,
                    "note": note,
                    "balance_after_display": fmt_inr(running) if status == "posted" else None,
                }
                out.append(_Txn(d, amount))
            self._txns[acc_id] = list(reversed(out))  # newest first

    # ------------------------------------------------------------- helpers
    def _account(self, customer_id: str, account_id: str) -> dict:
        acc = ACCOUNTS.get(account_id)
        # Same error for "doesn't exist" and "not yours": don't leak existence.
        if not acc or acc["customer_id"] != customer_id:
            raise ServiceError(404, "ACCOUNT_NOT_FOUND", "Account not found for this customer.")
        return acc

    # ----------------------------------------------------------------- API
    def list_accounts(self, customer_id: str) -> list[dict]:
        if customer_id not in CUSTOMERS:
            raise ServiceError(404, "CUSTOMER_NOT_FOUND", "Customer not found.")
        return [
            {"account_id": k, "type": v["type"], "account_number_masked": v["masked"], "currency": "INR"}
            for k, v in ACCOUNTS.items()
            if v["customer_id"] == customer_id
        ]

    def get_balance(self, customer_id: str, account_id: str) -> dict:
        acc = self._account(customer_id, account_id)
        txns = self._txns[account_id]
        ledger = Decimal(acc["opening"]) + sum((t.amount for t in txns if t.data["status"] == "posted"), Decimal(0))
        holds = sum((-t.amount for t in txns if t.data["status"] == "pending" and t.amount < 0), Decimal(0))
        return {
            "account_id": account_id,
            "account_type": acc["type"],
            "account_number_masked": acc["masked"],
            "currency": "INR",
            "ledger_balance": f"{ledger:.2f}",
            "ledger_balance_display": fmt_inr(ledger),
            "pending_holds_display": fmt_inr(holds),
            "available_balance": f"{ledger - holds:.2f}",
            "available_balance_display": fmt_inr(ledger - holds),
            "as_of": self.today.isoformat(),
        }

    def search_transactions(
        self,
        customer_id: str,
        account_id: str,
        direction: str | None = None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        text: str | None = None,
        limit: int = 10,
    ) -> dict:
        self._account(customer_id, account_id)
        if direction not in (None, "debit", "credit"):
            raise ServiceError(422, "BAD_REQUEST", "direction must be 'debit' or 'credit'.")
        limit = max(1, min(int(limit), 25))
        for label, v in (("date_from", date_from), ("date_to", date_to)):
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise ServiceError(422, "BAD_REQUEST", f"{label} must be YYYY-MM-DD.")
        needle = text.lower().strip() if text else None
        matches = []
        for t in self._txns[account_id]:
            d = t.data
            if direction and d["direction"] != direction:
                continue
            if min_amount is not None and abs(t.amount) < min_amount:
                continue
            if max_amount is not None and abs(t.amount) > max_amount:
                continue
            if date_from and d["date"] < date_from:
                continue
            if date_to and d["date"] > date_to:
                continue
            if needle and needle not in " ".join(
                (d["narration"], d["merchant"], d["category"], d["channel"])
            ).lower():
                continue
            matches.append(d)
        return {
            "account_id": account_id,
            "total_matches": len(matches),
            "returned": min(len(matches), limit),
            "transactions": matches[:limit],
        }

    def request_cheque_book(self, customer_id: str, account_id: str, leaves: int, idempotency_key: str) -> dict:
        acc = self._account(customer_id, account_id)
        if leaves not in ALLOWED_LEAVES:
            raise ServiceError(422, "INVALID_LEAVES", f"Cheque books come in {', '.join(map(str, ALLOWED_LEAVES))} leaves.")
        with self._lock:
            if idempotency_key in self._idem:  # safe retry -> same response
                return self._idem[idempotency_key]
            for r in self._requests.values():
                if r["account_id"] == account_id and r["status"] == "RAISED":
                    raise ServiceError(
                        409,
                        "DUPLICATE_OPEN_REQUEST",
                        f"A cheque book request ({r['request_id']}) is already open for this account.",
                    )
            self._seq += 1
            rec = {
                "request_id": f"SR-{self.today:%y%m%d}-{self._seq:04d}",
                "request_type": "CHEQUE_BOOK",
                "status": "RAISED",
                "account_id": account_id,
                "account_type": acc["type"],
                "account_number_masked": acc["masked"],
                "leaves": leaves,
                "delivery": "Registered address on file (Bengaluru 560034)",
                "expected_delivery_date": (self.today + timedelta(days=7)).isoformat(),
                "raised_on": self.today.isoformat(),
            }
            self._requests[rec["request_id"]] = rec
            self._idem[idempotency_key] = rec
            return rec
