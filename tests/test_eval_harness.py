"""Offline checks that the eval harness itself works (no live model)."""
from assistant.llm import LLMResponse
from evals.harness import run_scenario
from evals.scenarios import SCENARIOS
from tests.fakes import ScriptedLLM, text, tool


class AlwaysHello:
    def complete(self, system, messages, tools):
        return LLMResponse([text("Hello there.")], "end_turn")


def test_every_scenario_runs_and_fails_cleanly_with_a_useless_model():
    # A model that never calls tools must FAIL every behavioural scenario, and never crash the harness.
    results = [run_scenario(s, AlwaysHello()) for s in SCENARIOS]
    failed = [r.scenario for r in results if not r.passed]
    # negative-only scenarios may legitimately pass with a do-nothing model
    assert len(failed) >= len(SCENARIOS) - 6
    assert all(isinstance(r.problems, list) for r in results)


def test_harness_passes_a_correct_run():
    sc = next(s for s in SCENARIOS if s.name == "balance/current")
    from evals.inprocess_bank import InProcessBank
    b = InProcessBank().svc.get_balance("CUST-1001", "ACC-CUR-002")
    llm = ScriptedLLM([tool("get_balance", account_id="ACC-CUR-002")],
                      [text(f"Your available balance is {b['available_balance_display']}.")])
    r = run_scenario(sc, llm)
    assert r.passed, r.problems


def test_harness_catches_wrong_account_balance():
    sc = next(s for s in SCENARIOS if s.name == "balance/current")
    llm = ScriptedLLM([tool("get_balance", account_id="ACC-SAV-001")], [text("Done.")])
    assert not run_scenario(sc, llm).passed


def test_harness_passes_a_correct_cheque_flow():
    from datetime import date
    sc = next(s for s in SCENARIOS if s.name == "cheque/happy-savings")
    sr = f"SR-{date.today():%y%m%d}-0001"
    llm = ScriptedLLM(
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=False)],
        [text("I'll raise a 25-leaf cheque book on your savings account. Shall I go ahead?")],
        [tool("request_cheque_book", account_id="ACC-SAV-001", leaves=25, customer_confirmed=True)],
        [text(f"Done. Your request {sr} has been raised.")],
    )
    r = run_scenario(sc, llm)
    assert r.passed, r.problems
