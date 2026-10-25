# Architecture

```mermaid
flowchart LR
    subgraph Browser["Customer browser (untrusted)"]
        UI["Web chat UI<br/>web/index.html"]
    end

    subgraph Assistant["Assistant service :8000 (trusted, ours)"]
        API["FastAPI /api/chat<br/>token check, rate limit,<br/>length cap, session binding"]
        AG["Agent loop<br/>(tool-use, max 6 steps)"]
        TX["Tool executor<br/>customer_id from session,<br/>cheque-book confirmation gate,<br/>idempotency key"]
        GR["Grounding guardrail<br/>amounts / refs must exist<br/>in tool results"]
        SS[("Session store<br/>in-memory")]
        API --> AG
        AG --> TX
        AG --> GR
        AG <--> SS
        TX <--> SS
    end

    subgraph Bank["Mock bank :8001 (trusted, stands in for core banking)"]
        BAPI["FastAPI /v1/*<br/>service key + X-Customer-Id,<br/>ownership check on every call"]
        BSV["BankService<br/>synthetic accounts, txns,<br/>service requests"]
        BAPI --> BSV
    end

    LLM[["LLM provider<br/>(Anthropic API)<br/>external, untrusted output"]]
    IDP[["Bank login / IdP<br/>(stubbed: /auth/demo-login)"]]

    UI -- "HTTPS + Bearer token<br/>{message, session_id}" --> API
    UI -. "login" .-> IDP
    IDP -. "signed token (customer id)" .-> UI
    AG -- "system prompt + history +<br/>tool results (no customer id)" --> LLM
    LLM -- "text / tool calls" --> AG
    TX -- "HTTP + service key +<br/>X-Customer-Id" --> BAPI
    API -- "reply + cards<br/>(cards built from bank data)" --> UI
```

## Trust boundaries

| Boundary | What crosses | Control |
|---|---|---|
| Browser → Assistant | user text, token | Signed, expiring token; the customer identity is taken **only** from it; length cap, rate limit, sessions bound to customer |
| Assistant → LLM | prompt, history, tool results | Only data needed for the answer is sent; no credentials; the model never sees or sets `customer_id` |
| LLM → Assistant | text, tool calls | Treated as **untrusted input**: arguments validated, state-changing action gated server-side, final text checked for ungrounded figures |
| Assistant → Bank | tool calls | Service key + customer header; bank re-checks account ownership; idempotency key on writes |
| Bank data → LLM | narrations, merchants | Third-party text = data; prompt tells the model never to follow instructions inside it |

## Flow: "What was the 649 debit?"
1. UI sends text + token. API verifies token → `customer_id`, loads/creates the session.
2. Agent calls the LLM with the system prompt, history and tool schemas.
3. LLM decides (reasoning, no keyword rules) to call `get_accounts` then `search_transactions(direction=debit, min=max=649)`.
4. Executor calls the bank on behalf of the session's customer; results go back to the LLM and into the session's "grounded" record.
5. LLM writes the explanation. Guardrail checks every ₹ amount / reference exists in tool results. API returns reply plus a **card rendered from bank data**.

## Flow: cheque book
`request_cheque_book(confirmed=false)` stages → LLM asks the customer to confirm → customer replies in a **later** message → `request_cheque_book(confirmed=true)` executes with an idempotency key → bank returns `SR-…` → confirmation text and card both come from that response.
