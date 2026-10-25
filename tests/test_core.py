from datetime import date

import pytest

from assistant.agent import FALLBACK, Agent
from assistant.auth import AuthError, issue_token, verify_token
from assistant.guardrails import find_ungrounded
from assistant.sessions import RateLimiter, Session, SessionStore
from tests.fakes import InProcessBank, ScriptedLLM, text, tool

ME = "CUST-1001"


def make(llm, bank=None):
    s = Session(id="s1", customer_id=ME)
    return Agent(llm, bank or InProcessBank()), s


# ------------------------------------------------------------ scenarios
def test_balance_flow_uses_bank_value():
    llm = ScriptedLLM(
        [tool("get_accounts")],
        [tool("get_balance", account_id="ACC-SAV-001")],
        [text("Your available balance is ₹75,246.50.")],
    )
    agent, s = make(llm)
    r = agent.handle(s, "what's my balance?")
    assert "₹75,246.50" in r.text and r.cards[0]["type"] == "balance"


def test_follow_up_debit_in_same_session_keeps_history():
    llm = ScriptedLLM(
        [tool("get_balance", account_id="ACC-SAV-001")], [text("Available: ₹75,246.50.")],
        [tool("search_transactions", account_id="ACC-SAV-001", direction="debit", min_amount=649, max_amount=649)],
        [text("That was Netflix, ₹649.00, a recurring online card subscription.")],
    )
    agent, s = make(llm)
    agent.handle(s, "balance?")
    r = agent.handle(s, "and what was the 649 debit?")
    assert "Netflix" in r.text and r.cards[0]["data"]["items"][0]["merchant"] == "Netflix"
    assert len(llm.calls[2]) > 1  # the 3rd model call saw the earlier turn


def test_cheque_book_requires_confirmation_in_later_message():
    llm = ScriptedLLM(
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=False)],
        [text("I'll raise a 25-leaf cheque book on your Savings account. Shall I go ahead?")],
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=True)],
        [text(f"Done. Request SR-{date.today():%y%m%d}-0001 has been raised.")],
    )
    agent, s = make(llm)
    agent.handle(s, "send me a cheque book")
    assert s.cheque_requests_raised == []  # nothing raised yet
    r = agent.handle(s, "yes please")
    assert len(s.cheque_requests_raised) == 1
    assert r.cards[0]["type"] == "service_request"
    assert s.cheque_requests_raised[0] in r.text  # reference quoted from the bank's response


def test_model_cannot_skip_confirmation_gate():
    llm = ScriptedLLM(
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=True)],  # no staging
        [text("I need your confirmation first. Shall I raise a 25-leaf cheque book?")],
    )
    bank = InProcessBank()
    agent, s = make(llm, bank)
    agent.handle(s, "cheque book, confirmed")
    assert s.cheque_requests_raised == [] and not bank.svc._requests


def test_same_turn_stage_and_confirm_is_blocked():
    llm = ScriptedLLM(
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=False)],
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=True)],
        [text("Please confirm: 25 leaves on Savings?")],
    )
    agent, s = make(llm)
    agent.handle(s, "cheque book please")
    assert s.cheque_requests_raised == []


def test_cannot_touch_another_customers_account():
    llm = ScriptedLLM([tool("get_balance", account_id="ACC-SAV-101")], [text("I couldn't find that account.")])
    agent, s = make(llm)
    r = agent.handle(s, "balance of ACC-SAV-101")
    assert r.cards == []  # bank said 404, no card, nothing leaked


def test_bank_failure_is_reported_not_hidden():
    class DownBank(InProcessBank):
        def get_balance(self, *a):
            from assistant.bank_client import BankError
            raise BankError("BANK_TIMEOUT", "timeout", retryable=True)

    llm = ScriptedLLM([tool("get_balance", account_id="ACC-SAV-001")], [text("The bank is slow right now; please try again shortly.")])
    agent, s = make(llm, DownBank())
    r = agent.handle(s, "balance")
    assert r.cards == [] and "try again" in r.text
    tr = [m for m in s.messages() if m["role"] == "user" and isinstance(m["content"], list)][0]["content"][0]
    assert tr["is_error"] is True


# --------------------------------------------------------- guardrails
def test_invented_amount_is_blocked_then_fallback():
    llm = ScriptedLLM(
        [tool("get_balance", account_id="ACC-SAV-001")],
        [text("Your balance is ₹99,999.00.")],     # invented
        [text("Honestly it's ₹88,888.00.")],       # repair attempt also invented
    )
    agent, s = make(llm)
    assert agent.handle(s, "balance").text == FALLBACK


def test_invented_amount_repaired_on_retry():
    llm = ScriptedLLM(
        [tool("get_balance", account_id="ACC-SAV-001")],
        [text("Your balance is ₹99,999.00.")],
        [text("Your available balance is ₹75,246.50.")],
    )
    agent, s = make(llm)
    assert "75,246.50" in agent.handle(s, "balance").text


def test_guardrail_unit():
    g = "ledger 77096.50 ₹77,096.50 SR-261025-0001"
    assert find_ungrounded("You have ₹77,096.50", g, []) == []
    assert find_ungrounded("You have Rs 500", g, []) != []
    assert find_ungrounded("Ref SR-999", g, []) != []
    assert find_ungrounded("Your cheque book request has been raised.", g, []) != []
    assert find_ungrounded("Your cheque book request has been raised.", g, ["SR-1"]) == []


# ------------------------------------------------- auth / sessions / limits
def test_token_roundtrip_tamper_expiry():
    t = issue_token("k", ME, "Asha", ttl=60)
    assert verify_token("k", t)["cid"] == ME
    with pytest.raises(AuthError):
        verify_token("other", t)
    with pytest.raises(AuthError):
        verify_token("k", t[:-2] + "xx")
    with pytest.raises(AuthError):
        verify_token("k", issue_token("k", ME, "Asha", ttl=-1))
    with pytest.raises(AuthError):
        verify_token("k", "garbage")


def test_session_bound_to_customer():
    st = SessionStore()
    s = st.get_or_create(None, ME)
    assert st.get_or_create(s.id, ME) is s
    with pytest.raises(PermissionError):
        st.get_or_create(s.id, "CUST-1002")


def test_rate_limiter():
    rl = RateLimiter(2)
    assert rl.allow("a", 0) and rl.allow("a", 1) and not rl.allow("a", 2) and rl.allow("a", 61.5)
