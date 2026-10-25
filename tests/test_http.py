"""HTTP-level tests. Need fastapi/httpx installed (pip install -r requirements-dev.txt)."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from assistant.bank_client import HttpBankClient  # noqa: E402
from assistant.config import Settings  # noqa: E402
from assistant.main import create_app  # noqa: E402
from mock_bank.main import app as bank_app  # noqa: E402
from tests.fakes import ScriptedLLM, text, tool  # noqa: E402


def client_with(llm):
    bank_http = TestClient(bank_app, base_url="http://bank")
    bank = HttpBankClient("http://bank", "dev-service-key", client=bank_http)
    return TestClient(create_app(Settings(anthropic_api_key="x"), llm=llm, bank=bank))


def login(c, cid="CUST-1001"):
    return {"Authorization": "Bearer " + c.post("/auth/demo-login", json={"customer_id": cid}).json()["token"]}


def test_bank_rejects_missing_service_key():
    assert TestClient(bank_app).get("/v1/accounts", headers={"X-Customer-Id": "CUST-1001"}).status_code == 401


def test_chat_requires_auth():
    assert client_with(ScriptedLLM()).post("/api/chat", json={"message": "hi"}).status_code == 401


def test_end_to_end_balance_over_http():
    llm = ScriptedLLM([tool("get_accounts")], [tool("get_balance", account_id="ACC-SAV-001")],
                      [text("Your available balance is shown above.")])
    c = client_with(llm)
    r = c.post("/api/chat", json={"message": "balance?"}, headers=login(c))
    assert r.status_code == 200 and r.json()["cards"][0]["type"] == "balance"


def test_session_hijack_blocked():
    c = client_with(ScriptedLLM([text("Hello!")]))
    sid = c.post("/api/chat", json={"message": "hi"}, headers=login(c)).json()["session_id"]
    r = c.post("/api/chat", json={"message": "hi", "session_id": sid}, headers=login(c, "CUST-1002"))
    assert r.status_code == 403


def test_overlong_message_rejected():
    c = client_with(ScriptedLLM())
    assert c.post("/api/chat", json={"message": "x" * 1500}, headers=login(c)).status_code == 413
