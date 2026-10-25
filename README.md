# Conversational banking assistant (take-home)

A working conversational assistant that lets an **already-authenticated** customer, in natural language:

1. check their **balance**,
2. get an **explanation of a debit** from real transaction data,
3. **request a cheque book** (service request raised + confirmation).

An LLM does the understanding and routing through **tool calling** (no keyword/if-else intent matching).
Every balance, transaction and confirmation shown comes from a **mocked bank API**, never from the model.
All data is synthetic.

```
Browser UI ──► Assistant API (FastAPI) ──► LLM (Anthropic, tool use)
                    │  agent loop, guardrails, sessions
                    └──► Mock Bank API (FastAPI) ──► synthetic data
```
Diagram with trust boundaries: [`docs/architecture.md`](docs/architecture.md) (Mermaid; renders on GitHub).

## Run it

Requires Python 3.11+ and an Anthropic API key. Two processes plus the browser.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then put your ANTHROPIC_API_KEY in it
set -a; source .env; set +a   # load env vars (or export them manually)

# terminal 1: mocked bank
uvicorn mock_bank.main:app --port 8001
# terminal 2: assistant (also serves the UI)
uvicorn assistant.main:create_app --factory --port 8000
```
Open <http://localhost:8000>. You are signed in as a demo customer (Asha Rao, two accounts).

Or with Docker: `ANTHROPIC_API_KEY=sk-ant-... docker compose up --build`

### Tests (no API key or network needed)
```bash
pytest -q
```
The LLM is replaced by a scripted fake, so tests exercise *our* logic deterministically:
tool execution, ownership checks, the cheque-book confirmation gate, idempotency, guardrails, auth, sessions.

### Try these
- "what's my balance?" (it will ask which account, or you can say "savings")
- "what was the 649 rupees charge yesterday?" then "and what about the two 1,299 ones?"
- "why was I charged 118?" · "what is the 1,850 pending thing?"
- "send me a cheque book" → it stages and asks you to confirm → "yes" → request ID + delivery date
- Ask for a cheque book again on the same account → bank refuses (one open request per account); the assistant explains.
- "show me Vikram's balance" / "ignore previous instructions…" → declined.

## API
| Endpoint | Purpose |
|---|---|
| `POST /auth/demo-login` | **Stub** for the bank's login; returns a signed token. Disable with `DEMO_AUTH_ENABLED=false` |
| `POST /api/chat` | `{message, session_id?}` + `Authorization: Bearer …` → `{session_id, reply, cards[]}` |
| Mock bank `GET /v1/accounts`, `GET /v1/accounts/{id}/balance`, `GET /v1/accounts/{id}/transactions`, `POST /v1/accounts/{id}/cheque-book-requests` | needs `X-Service-Key` and `X-Customer-Id` |

## Layout
```
assistant/   agent.py (LLM tool loop) · tools.py (tools + gates) · guardrails.py · llm.py (port+Anthropic)
             bank_client.py (port+HTTP) · sessions.py · auth.py (stub) · prompts.py · main.py (API)
mock_bank/   service.py (pure logic + seed data) · main.py (HTTP)
web/         index.html (single-file chat UI)
tests/       offline tests · docs/architecture.md
```

## Design notes

**Decomposition.** Four small parts with narrow interfaces: (1) *UI* – dumb, renders text and cards; (2) *Assistant API* – auth, limits, session binding; (3) *Agent* – LLM tool-use loop, plus the tools and guardrails that surround the model; (4) *Mock bank* – an independent HTTP service standing in for core banking. The agent depends on two ports (`LLM`, `BankPort`), so the model provider or the bank can be swapped without touching agent logic, and tests plug in fakes.

**Why tool calling, not intent classification.** The model decides which capability to use, with what arguments (amount/date/merchant filters turned from "that Netflix thing yesterday"), when to ask a clarifying question (which account?), and how to handle follow-ups from conversation history. Adding a capability = adding a tool schema + executor; there is no routing table.

**Making "never invented by the model" true.** Layered, because a prompt alone is not a control:
1. The model has no data except via tools; the prompt says to copy `*_display` values verbatim (the bank pre-formats currency so the model never does arithmetic or formatting).
2. A server-side **grounding check** blocks any ₹ amount or `SR-/TXN-` reference in the final reply that wasn't in a tool result this session (one repair attempt, then a safe fallback message). It also blocks "your request has been raised" if no request was actually raised.
3. The UI shows **cards built directly from bank responses**, so the key facts are on screen regardless of how the model words things.

**Security choices.**
- Customer identity comes only from the verified token; `customer_id` is not a tool parameter, so the model (or prompt injection) cannot address another customer. The bank independently enforces ownership and returns the same 404 for "missing" and "not yours".
- Cheque book is a state-changing action, so confirmation is **enforced in code**: stage → customer confirms in a *later* message → execute. The model passing `customer_confirmed=true` early is rejected. Execution uses an **idempotency key**, so retries/timeouts cannot raise duplicates.
- Transaction narrations are third-party text: treated as data in the prompt and rendered in the UI with `textContent` (no HTML injection).
- Logs record tool names/outcomes, not message content or amounts. Rate limit, message length cap, session TTL, sessions bound to customers.

**Failure handling.** Bank timeout/unavailable/4xx → structured tool error → the model explains honestly (nothing is faked, no card shown). LLM outage → 503 with a friendly message. Step limit on the agent loop. Unknown tools and bad arguments are returned to the model as errors.

### Assumptions
- The customer is already authenticated upstream; `/auth/demo-login` stands in for that and must never be exposed in production.
- INR, one demo bank, seed dates generated relative to today. Cheque books: 25 or 50 leaves, delivered to the registered address, one open request per account.
- Voice/telephony, multi-language and full dispute flows are out of scope.
- "Available balance" = ledger balance minus pending card holds.

### Known limitations / what I'd do next
- In-memory sessions and rate limiter → Redis; single-process only.
- The grounding check is regex-based and covers amounts/references, not every possible false statement (e.g. a wrong merchant). Next: structured/verifiable output (model emits references to tool-result fields and we render them) and an LLM-judge eval set.
- Add an evaluation harness against the live model (paraphrases, ambiguous accounts, injection attempts, multi-turn) with pass-rate tracking, plus prompt/model version pinning.
- Real auth (bank JWT/OIDC), TLS and secret management, audit trail for service requests, PII redaction layer before the LLM, streaming responses, step-up authentication before sensitive actions.
- Cost/latency: each turn is 1–3 LLM calls; small-model routing and prompt caching would cut both.
- Impact: the three flows are ~54% of ~390k monthly calls; each deflected call saves ~3 minutes of agent/telecom time, so measure *containment rate* and *escalation reasons* in production.
