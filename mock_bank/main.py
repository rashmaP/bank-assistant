"""HTTP face of the mocked bank. Run: uvicorn mock_bank.main:app --port 8001

Trust model (simulated): only a caller holding the service key may call this API, and it
must say *which* customer it acts for (X-Customer-Id), as an API gateway would after
validating the customer's session. Every endpoint enforces account ownership.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .service import BankService, ServiceError

SERVICE_KEY = os.environ.get("BANK_SERVICE_KEY", "dev-service-key")

app = FastAPI(title="Mock Bank API", version="1.0")
bank = BankService()


@app.exception_handler(ServiceError)
async def _svc_err(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})


def caller(x_service_key: str = Header(default=""), x_customer_id: str = Header(default="")) -> str:
    if x_service_key != SERVICE_KEY:
        raise ServiceError(401, "UNAUTHORIZED", "Invalid service credentials.")
    if not x_customer_id:
        raise ServiceError(400, "BAD_REQUEST", "X-Customer-Id header required.")
    return x_customer_id


def _dec(v: str | None, name: str) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        raise ServiceError(422, "BAD_REQUEST", f"{name} must be a number.")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/v1/accounts")
def accounts(cid: str = Depends(caller)):
    return {"accounts": bank.list_accounts(cid)}


@app.get("/v1/accounts/{account_id}/balance")
def balance(account_id: str, cid: str = Depends(caller)):
    return bank.get_balance(cid, account_id)


@app.get("/v1/accounts/{account_id}/transactions")
def transactions(
    account_id: str,
    direction: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    text: str | None = None,
    limit: int = 10,
    cid: str = Depends(caller),
):
    return bank.search_transactions(
        cid, account_id, direction, _dec(min_amount, "min_amount"), _dec(max_amount, "max_amount"),
        date_from, date_to, text, limit,
    )


class ChequeBookIn(BaseModel):
    leaves: int = 25


@app.post("/v1/accounts/{account_id}/cheque-book-requests", status_code=201)
def cheque_book(account_id: str, body: ChequeBookIn, idempotency_key: str = Header(default=""),
                cid: str = Depends(caller)):
    if not idempotency_key:
        raise ServiceError(400, "BAD_REQUEST", "Idempotency-Key header required.")
    return bank.request_cheque_book(cid, account_id, body.leaves, idempotency_key)
