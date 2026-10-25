"""Offline tests: logging, retry/backoff, circuit breaker, turn rollback on failure."""
import json
import logging

import pytest

from assistant.agent import Agent
from assistant.bank_client import BankError
from assistant.llm import LLMUnavailable
from assistant.resilience import CircuitBreaker, call_with_retry
from assistant.sessions import Session
from common.logging_setup import (_Formatter, accept_request_id, logf, reset_request_id,
                                  set_request_id)
from tests.fakes import InProcessBank, ScriptedLLM, tool


# ------------------------------------------------------------------ logging
def test_request_id_accepts_clean_rejects_dirty():
    assert accept_request_id("abc12345-XYZ") == "abc12345-XYZ"
    for bad in (None, "", "short", "has space in it!!", "a" * 65, "line\nbreak12345"):
        out = accept_request_id(bad)
        assert out != bad and len(out) == 16  # minted fresh -> no log injection


def _record(**fields):
    r = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    r.fields = fields
    return r


def test_json_log_line_has_request_id_and_fields():
    tok = set_request_id("req-0000001")
    try:
        line = _Formatter(True).format(_record(tool="get_balance", ms=12))
    finally:
        reset_request_id(tok)
    d = json.loads(line)
    assert d["request_id"] == "req-0000001" and d["tool"] == "get_balance" and d["msg"] == "hello"
    assert d["level"] == "INFO" and "ts" in d


def test_text_log_line_readable():
    assert "tool=x" in _Formatter(False).format(_record(tool="x"))


def test_logf_attaches_fields():
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.fields)

    lg = logging.getLogger("t.logf")
    lg.addHandler(H())
    lg.setLevel(logging.INFO)
    logf(lg, logging.INFO, "evt", a=1)
    assert seen == [{"a": 1}]


# ------------------------------------------------------------ retry/backoff
def _flaky(fail_times, err):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise err
        return "ok"
    return fn, calls


RETRYABLE = BankError("BANK_TIMEOUT", "t", retryable=True)


def test_retries_then_succeeds_with_exponential_backoff():
    fn, calls = _flaky(2, RETRYABLE)
    sleeps = []
    assert call_with_retry(fn, attempts=3, base_delay=1.0, sleep=sleeps.append, jitter=lambda: 0.5) == "ok"
    assert calls["n"] == 3 and sleeps == [1.0, 2.0]  # 1*2^0*1.0, 1*2^1*1.0


def test_gives_up_after_attempts_and_raises_last_error():
    fn, calls = _flaky(99, RETRYABLE)
    with pytest.raises(BankError):
        call_with_retry(fn, attempts=3, sleep=lambda s: None)
    assert calls["n"] == 3


def test_does_not_retry_client_errors():
    fn, calls = _flaky(99, BankError("ACCOUNT_NOT_FOUND", "nope", 404, retryable=False))
    with pytest.raises(BankError):
        call_with_retry(fn, attempts=3, sleep=lambda s: None)
    assert calls["n"] == 1


# ----------------------------------------------------------- circuit breaker
def test_breaker_opens_then_half_opens_then_closes():
    now = [0.0]
    br = CircuitBreaker(threshold=3, cooldown=10, clock=lambda: now[0])
    fn, calls = _flaky(99, RETRYABLE)
    for _ in range(3):
        with pytest.raises(BankError):
            call_with_retry(fn, attempts=1, sleep=lambda s: None, breaker=br)
    assert br.is_open
    before = calls["n"]
    with pytest.raises(BankError) as e:  # fails fast: the downstream is not called
        call_with_retry(fn, attempts=1, sleep=lambda s: None, breaker=br)
    assert calls["n"] == before and e.value.code == "BANK_UNAVAILABLE"
    now[0] = 11  # cooldown over -> one trial allowed; success closes it
    assert call_with_retry(lambda: "ok", attempts=1, breaker=br) == "ok"
    assert not br.is_open


def test_client_error_counts_as_healthy_for_breaker():
    br = CircuitBreaker(threshold=2, cooldown=10)
    with pytest.raises(BankError):
        call_with_retry(_flaky(99, RETRYABLE)[0], attempts=1, sleep=lambda s: None, breaker=br)
    with pytest.raises(BankError):
        call_with_retry(_flaky(99, BankError("X", "x", 404))[0], attempts=1, breaker=br)
    assert not br.is_open  # the 404 reset the failure streak


# ------------------------------------------------- turn rollback on failure
class FailAfterFirstCall:
    """First call stages a cheque book via a tool; the next call (the provider) blows up."""

    def __init__(self):
        self.n = 0

    def complete(self, system, messages, tools):
        from assistant.llm import LLMResponse
        self.n += 1
        if self.n == 1:
            return LLMResponse([tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=False)], "tool_use")
        raise LLMUnavailable("provider down")


def test_failed_turn_rolls_back_staged_state_and_history():
    s = Session(id="s", customer_id="CUST-1001")
    agent = Agent(FailAfterFirstCall(), InProcessBank())
    with pytest.raises(LLMUnavailable):
        agent.handle(s, "cheque book please")
    assert s.pending_cheque is None and s.turn_no == 0 and s.turns == []


def test_openai_adapter_rejects_malformed_provider_json():
    from assistant.llm_openai import OpenAICompatLLM

    class R:
        status_code = 200
        text = "{}"

        def json(self):
            return {"unexpected": True}

    class C:
        def post(self, *a, **k):
            return R()

    with pytest.raises(LLMUnavailable):
        OpenAICompatLLM("k", "m", "http://x/v1", client=C()).complete("s", [{"role": "user", "content": "hi"}], [])
