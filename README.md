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

## Quick start

You need **Python 3.11+** and **one LLM API key**. There is deliberately no fake model: the assistant must reason with a real one.
A free [Groq](https://console.groq.com) key works (no card), as does an Anthropic key.

**macOS / Linux**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # open .env: set LLM_PROVIDER and the matching key
python run_dev.py
```

**Windows (PowerShell)**
```powershell
py -m venv .venv; .venv\Scripts\Activate.ps1     # if scripts are blocked, skip the venv and just use `py -m pip` / `py`
pip install -r requirements.txt
copy .env.example .env; notepad .env              # set LLM_PROVIDER and the matching key
python run_dev.py
```

Then open <http://localhost:8000>. You are signed in as a demo customer (Asha Rao, two accounts).

`run_dev.py` is a standard-library launcher. It loads `.env` (real environment variables win), **checks your setup and tells you
what is wrong in plain words** (Python version, missing packages, missing or placeholder API key, busy ports), starts the mock bank
and the assistant, waits until both are healthy, shows both logs prefixed `[bank]` / `[assistant]`, and stops both on Ctrl+C.

```bash
python run_dev.py --check            # only run the checks
python run_dev.py --port 9000 --bank-port 9001
python run_dev.py --evals --repeat 2 # live-model evals (extra flags go to the eval runner)
```

**Docker instead:** `docker compose up --build` (reads the same `.env`; the assistant starts after the bank is healthy).

**Manual, two terminals** (what the launcher does for you):
```bash
uvicorn mock_bank.main:app --port 8001
uvicorn assistant.main:create_app --factory --port 8000      # needs the .env variables exported
```

### Using a free provider instead of Anthropic
In `.env`:
```
LLM_PROVIDER=openai
OPENAI_API_KEY=<your Groq key>
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_MODEL=llama-3.3-70b-versatile
```
Any OpenAI-compatible API with tool calling should work. Smaller open models are less reliable at tool use than Claude; the
server-side guardrails (ownership, confirmation gate, grounding check) apply regardless of provider.

### Tests (no API key or network needed)
```bash
pip install -r requirements-dev.txt
pytest -q
```
The LLM is replaced by a scripted fake, so tests exercise *our* logic deterministically:
tool execution, ownership checks, the cheque-book confirmation gate, idempotency, guardrails, auth, sessions,
retry/circuit breaker, logging, HTTP behaviour and the launcher itself.

### Evaluating the AI behaviour (live model)
Unit tests use a fake model, so they cannot tell you how the *real* model behaves. `evals/` does:
~25 scenarios (paraphrased balance and debit questions, clarifying questions, follow-ups, the cheque
confirmation flow, duplicate request, and safety cases such as cross-customer access, prompt leaking,
prompt injection via a transaction narration, and skipping confirmation). Each runs against a fresh
in-process mock bank; checks assert **outcomes** (tool calls, bank state, cards, grounded figures), not exact wording.
```bash
python -m evals.run_evals                       # one pass over every scenario
python -m evals.run_evals --repeat 3 --only cheque --json evals/report.json
```
Prints PASS / FLAKY / FAIL per scenario with reasons, and exits non-zero if the pass rate is under `--min-pass`
(default 0.8) or **any safety scenario fails**. Use `--repeat` because model output varies; use `--pause 2` on free-tier rate limits.

### CI
`.github/workflows/ci.yml` runs byte-compile + all offline tests on Python 3.11 and 3.12 for every push and PR.
A manual `live-evals` job (Actions tab → *Run workflow*) runs the evals against a real model using a key stored as a repository
secret (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, optional variable `LLM_PROVIDER`) and uploads the JSON report.

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
assistant/   agent.py (LLM tool loop) · tools.py (tools + gates) · guardrails.py · llm.py / llm_openai.py / llm_factory.py (model port + adapters)
             bank_client.py (port+HTTP) · resilience.py (retry, circuit breaker) · sessions.py · auth.py (stub) · prompts.py · main.py (API)
common/      logging_setup.py (JSON logs, request IDs) — shared by both services
evals/       live-model scenarios + runner
mock_bank/   service.py (pure logic + seed data) · main.py (HTTP)
web/         index.html + static/app.js, style.css (chat UI, CSP-friendly)
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

**Observability.** Both services emit one JSON log line per event (`LOG_FORMAT=text` for humans) with a **request ID** that the assistant forwards to the bank (`X-Request-ID`), so one customer request can be traced end to end. Per turn the agent logs LLM calls, tools used, whether the grounding check fired, and latency; the API logs method, path, status and duration. Message text, amounts and credentials are never logged. Client-supplied request IDs are accepted only if they match a strict pattern (no log injection).

**HTTP hardening.** Security headers on every response (`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store` on API/auth routes) and a strict CSP on the page (no inline script/style, which is why the UI is split into `web/static/`). HSTS is left to the TLS terminator. No CORS: the UI is same-origin.

**Resilience.** Calls to the bank use timeouts, **retry with exponential backoff + jitter** for timeouts/5xx only (4xx means the bank answered, so never retried; the cheque-book write is retry-safe because of its idempotency key), and a **circuit breaker** (opens after 5 consecutive failures, half-opens after 15s) so a sick bank fails fast instead of piling up threads. A global handler turns unexpected errors into a generic 500 carrying only the request ID (traceback stays in the server log). If a turn fails midway, the staged-cheque state and turn counter are rolled back with the discarded history.

**Failure handling.** Bank timeout/unavailable/4xx → structured tool error → the model explains honestly (nothing is faked, no card shown). LLM outage → 503 with a friendly message. Step limit on the agent loop. Unknown tools and bad arguments are returned to the model as errors.

### Assumptions
- The customer is already authenticated upstream; `/auth/demo-login` stands in for that and must never be exposed in production.
- INR, one demo bank, seed dates generated relative to today. Cheque books: 25 or 50 leaves, delivered to the registered address, one open request per account.
- Voice/telephony, multi-language and full dispute flows are out of scope.
- "Available balance" = ledger balance minus pending card holds.

### Known limitations / what I'd do next
- In-memory sessions and rate limiter → Redis; single-process only. Endpoints are synchronous (one thread per in-flight LLM call): move to async + a worker pool or queue before real load. At ~390k calls/month, LLM latency, provider rate limits and cost are the bottleneck, not CPU.
- Logs go to stdout only: add metrics (latency, token use, tool-call counts, grounding-block rate) and tracing; ship logs to a central store with retention rules.
- Service-to-service auth is one static key → mTLS or short-lived tokens; no PII redaction before the LLM provider; no persistent audit trail; dependency pinning/scanning not set up.
- The grounding check is regex-based and covers amounts/references, not every possible false statement (e.g. a wrong merchant). Next: structured/verifiable output (model emits references to tool-result fields and we render them) and an LLM-judge eval set.
- Grow the eval set from real transcripts, track pass rates per model/prompt version over time, and gate prompt changes on it (the harness and a manual CI job exist; the dataset is small).
- Real auth (bank JWT/OIDC), TLS and secret management, audit trail for service requests, PII redaction layer before the LLM, streaming responses, step-up authentication before sensitive actions.
- Cost/latency: each turn is 1–3 LLM calls; small-model routing and prompt caching would cut both.
- Impact: the three flows are ~54% of ~390k monthly calls; each deflected call saves ~3 minutes of agent/telecom time, so measure *containment rate* and *escalation reasons* in production.
