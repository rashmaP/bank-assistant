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


# ------------------------------------------- observability / security / resilience
import httpx  # noqa: E402



def test_security_headers_and_request_id_on_api_and_page():
    c = client_with(ScriptedLLM())
    r = c.post("/auth/demo-login", json={"customer_id": "CUST-1001"})
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert len(r.headers["x-request-id"]) >= 8
    page = c.get("/")
    assert page.status_code == 200 and "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_client_supplied_request_id_is_echoed_but_dirty_ones_are_replaced():
    c = client_with(ScriptedLLM())
    assert c.get("/healthz", headers={"X-Request-ID": "trace-abc-123456"}).headers["x-request-id"] == "trace-abc-123456"
    bad = c.get("/healthz", headers={"X-Request-ID": "bad id with spaces"}).headers["x-request-id"]
    assert bad != "bad id with spaces"


def test_static_assets_served():
    c = client_with(ScriptedLLM())
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/style.css").status_code == 200


def test_unexpected_error_returns_generic_500_without_leaking():
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("secret internal detail")

    c = client_with(Boom())
    r = c.post("/api/chat", json={"message": "hi"}, headers=login(c))
    assert r.status_code == 500
    body = r.json()
    assert "secret internal detail" not in r.text and body["request_id"] == r.headers["x-request-id"]


def test_llm_outage_is_503():
    from assistant.llm import LLMUnavailable

    class Down:
        def complete(self, *a, **k):
            raise LLMUnavailable("401 bad key")

    c = client_with(Down())
    assert c.post("/api/chat", json={"message": "hi"}, headers=login(c)).status_code == 503


def _bank_client(handler, attempts=3):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://bank")
    return HttpBankClient("http://bank", "k", client=http, attempts=attempts, sleep=lambda s: None)


def test_bank_client_retries_503_then_succeeds_and_forwards_request_id():
    seen = []

    def handler(req):
        seen.append(req.headers.get("x-request-id"))
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"code": "X", "message": "down"}})
        return httpx.Response(200, json={"accounts": [{"account_id": "A"}]})

    assert _bank_client(handler).list_accounts("CUST-1001") == [{"account_id": "A"}]
    assert len(seen) == 2 and seen[0] is not None


def test_bank_client_retries_timeouts():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        if n["c"] < 3:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"accounts": []})

    assert _bank_client(handler).list_accounts("CUST-1001") == [] and n["c"] == 3


def test_bank_client_does_not_retry_404():
    from assistant.bank_client import BankError
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(404, json={"error": {"code": "ACCOUNT_NOT_FOUND", "message": "no"}})

    with pytest.raises(BankError) as e:
        _bank_client(handler).get_balance("CUST-1001", "ACC-X")
    assert n["c"] == 1 and e.value.code == "ACCOUNT_NOT_FOUND"
