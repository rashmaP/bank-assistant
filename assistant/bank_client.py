"""Port + HTTP adapter for the (mocked) bank.

The agent only knows `BankPort`. Swapping the mock for a real core-banking gateway means
writing another adapter; nothing in the agent changes.
"""
from __future__ import annotations

from typing import Protocol


class BankError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


class BankPort(Protocol):
    def list_accounts(self, customer_id: str) -> list[dict]: ...
    def get_balance(self, customer_id: str, account_id: str) -> dict: ...
    def search_transactions(self, customer_id: str, account_id: str, **filters) -> dict: ...
    def request_cheque_book(self, customer_id: str, account_id: str, leaves: int, idempotency_key: str) -> dict: ...


class HttpBankClient:
    def __init__(self, base_url: str, service_key: str, client=None, timeout: float = 5.0):
        import httpx  # imported lazily so the core logic has no hard dependency

        self._httpx = httpx
        self._http = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._key = service_key

    def _call(self, method: str, path: str, customer_id: str, **kw) -> dict:
        headers = {"X-Service-Key": self._key, "X-Customer-Id": customer_id, **kw.pop("headers", {})}
        try:
            r = self._http.request(method, path, headers=headers, **kw)
        except self._httpx.TimeoutException as e:
            raise BankError("BANK_TIMEOUT", "The banking service did not respond in time.", retryable=True) from e
        except self._httpx.HTTPError as e:
            raise BankError("BANK_UNAVAILABLE", "The banking service is unavailable.", retryable=True) from e
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                code, msg = err["code"], err["message"]
            except Exception:
                code, msg = "BANK_ERROR", "The banking service returned an error."
            raise BankError(code, msg, r.status_code, retryable=r.status_code >= 500)
        return r.json()

    def list_accounts(self, customer_id):
        return self._call("GET", "/v1/accounts", customer_id)["accounts"]

    def get_balance(self, customer_id, account_id):
        return self._call("GET", f"/v1/accounts/{account_id}/balance", customer_id)

    def search_transactions(self, customer_id, account_id, **filters):
        params = {k: v for k, v in filters.items() if v is not None}
        return self._call("GET", f"/v1/accounts/{account_id}/transactions", customer_id, params=params)

    def request_cheque_book(self, customer_id, account_id, leaves, idempotency_key):
        return self._call(
            "POST", f"/v1/accounts/{account_id}/cheque-book-requests", customer_id,
            json={"leaves": leaves}, headers={"Idempotency-Key": idempotency_key},
        )
