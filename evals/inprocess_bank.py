"""BankPort adapter straight onto BankService (no HTTP). Used by tests and live evals."""
from decimal import Decimal

from assistant.bank_client import BankError
from mock_bank.service import BankService, ServiceError


class InProcessBank:
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
        for k in ("min_amount", "max_amount"):
            if f.get(k) is not None:
                f[k] = Decimal(f[k])
        f = {k: v for k, v in f.items() if v is not None}
        return self._wrap(self.svc.search_transactions, cid, acc, **f)

    def request_cheque_book(self, cid, acc, leaves, key):
        return self._wrap(self.svc.request_cheque_book, cid, acc, leaves, key)
