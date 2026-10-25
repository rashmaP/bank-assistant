"""FastAPI app for the conversational assistant.

Run:  uvicorn assistant.main:create_app --factory --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import Agent
from .auth import AuthError, issue_token, verify_token
from .bank_client import BankPort, HttpBankClient
from .config import Settings
from .llm import LLM, AnthropicLLM, LLMUnavailable
from .sessions import RateLimiter, SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Demo identities for the auth stub (names only; accounts live in the mocked bank).
DEMO_CUSTOMERS = {"CUST-1001": "Asha Rao", "CUST-1002": "Vikram Menon"}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class LoginIn(BaseModel):
    customer_id: str = "CUST-1001"


def create_app(settings: Settings | None = None, llm: LLM | None = None, bank: BankPort | None = None) -> FastAPI:
    cfg = settings or Settings()
    llm = llm or AnthropicLLM(cfg.anthropic_api_key, cfg.anthropic_model)
    bank = bank or HttpBankClient(cfg.bank_base_url, cfg.bank_service_key)
    agent, store, limiter = Agent(llm, bank, cfg), SessionStore(), RateLimiter(cfg.rate_limit_per_minute)
    app = FastAPI(title="Bank Assistant", version="1.0")

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
            raise HTTPException(429, "Too many messages. Please wait a moment.")
        try:
            session = store.get_or_create(body.session_id, claims["cid"])
        except PermissionError:
            raise HTTPException(403, "Session does not belong to this customer.")
        with session.lock:  # one in-flight message per session
            try:
                reply = agent.handle(session, msg)
            except LLMUnavailable:
                raise HTTPException(503, "The assistant is temporarily unavailable. Please try again shortly.")
        return {"session_id": session.id, "reply": reply.text, "cards": reply.cards}

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app
