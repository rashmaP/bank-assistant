"""FastAPI app for the conversational assistant.

Run:  uvicorn assistant.main:create_app --factory --port 8000
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common.logging_setup import accept_request_id, logf, reset_request_id, set_request_id, setup_logging

from .agent import Agent
from .auth import AuthError, issue_token, verify_token
from .bank_client import BankPort, HttpBankClient
from .config import Settings
from .llm import LLM, LLMUnavailable
from .llm_factory import build_llm
from .sessions import RateLimiter, SessionStore

setup_logging()
log = logging.getLogger("assistant.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Demo identities for the auth stub (names only; accounts live in the mocked bank).
DEMO_CUSTOMERS = {"CUST-1001": "Asha Rao", "CUST-1002": "Vikram Menon"}

# The UI is same-origin with no inline script/style, so a strict policy works.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class LoginIn(BaseModel):
    customer_id: str = "CUST-1001"


def add_security_headers(response, path: str) -> None:
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    if path.startswith(("/api", "/auth")):
        h["Cache-Control"] = "no-store"  # never cache account data
    else:
        h["Content-Security-Policy"] = CSP
    # HSTS belongs at the TLS terminator (load balancer / ingress), not here.


def create_app(settings: Settings | None = None, llm: LLM | None = None, bank: BankPort | None = None) -> FastAPI:
    cfg = settings or Settings()
    llm = llm or build_llm(cfg)
    bank = bank or HttpBankClient(cfg.bank_base_url, cfg.bank_service_key)
    agent, store, limiter = Agent(llm, bank, cfg), SessionStore(), RateLimiter(cfg.rate_limit_per_minute)
    app = FastAPI(title="Bank Assistant", version="1.0")

    @app.middleware("http")
    async def observability(request: Request, call_next):
        """Correlation ID, access log (no bodies), security headers, last-resort error handler."""
        rid = accept_request_id(request.headers.get("x-request-id"))
        token = set_request_id(rid)
        t0 = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Unexpected bug: log the traceback server-side, give the client a generic message.
                log.error("unhandled_error", exc_info=True, extra={"fields": {"path": request.url.path}})
                response = JSONResponse({"detail": "Something went wrong. Please try again.", "request_id": rid},
                                        status_code=500)
            response.headers["X-Request-ID"] = rid
            add_security_headers(response, request.url.path)
            logf(log, logging.INFO, "request", method=request.method, path=request.url.path,
                 status=response.status_code, ms=round((time.perf_counter() - t0) * 1000))
            return response
        finally:
            reset_request_id(token)

    def claims_from(authorization: str | None) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token.")
        try:
            return verify_token(cfg.auth_secret, authorization[7:])
        except AuthError:
            raise HTTPException(401, "Invalid or expired token.")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    if cfg.demo_auth_enabled:
        @app.post("/auth/demo-login")
        def demo_login(body: LoginIn):
            """STUB for the bank's real login. Disabled unless DEMO_AUTH_ENABLED=true."""
            name = DEMO_CUSTOMERS.get(body.customer_id)
            if not name:
                raise HTTPException(404, "Unknown demo customer.")
            return {"token": issue_token(cfg.auth_secret, body.customer_id, name, cfg.token_ttl_seconds),
                    "customer_name": name}

    @app.post("/api/chat")
    def chat(body: ChatIn, authorization: str | None = Header(default=None)):
        claims = claims_from(authorization)
        msg = body.message.strip()
        if not msg:
            raise HTTPException(422, "Message is empty.")
        if len(msg) > cfg.max_message_chars:
            raise HTTPException(413, f"Message is too long (max {cfg.max_message_chars} characters).")
        if not limiter.allow(claims["cid"]):
            logf(log, logging.WARNING, "rate_limited")
            raise HTTPException(429, "Too many messages. Please wait a moment.")
        try:
            session = store.get_or_create(body.session_id, claims["cid"])
        except PermissionError:
            logf(log, logging.WARNING, "session_customer_mismatch")
            raise HTTPException(403, "Session does not belong to this customer.")
        with session.lock:  # one in-flight message per session
            try:
                reply = agent.handle(session, msg)
            except LLMUnavailable as e:
                logf(log, logging.ERROR, "llm_unavailable", reason=str(e)[:200])
                raise HTTPException(503, "The assistant is temporarily unavailable. Please try again shortly.")
        return {"session_id": session.id, "reply": reply.text, "cards": reply.cards}

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app
