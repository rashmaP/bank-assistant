from decimal import Decimal

import pytest

from mock_bank.service import BankService, ServiceError, fmt_inr

SAV, CUR, ME = "ACC-SAV-001", "ACC-CUR-002", "CUST-1001"


def test_fmt_inr_indian_grouping():
    assert fmt_inr(Decimal("1234567.89")) == "₹12,34,567.89"
    assert fmt_inr(Decimal("999")) == "₹999.00"
    assert fmt_inr(Decimal("100000")) == "₹1,00,000.00"


def test_balance_excludes_pending_from_available():
    b = BankService().get_balance(ME, SAV)
    assert b["ledger_balance"] == "77096.50"
    assert b["available_balance"] == "75246.50"  # minus the pending 1,850.00 hold


def test_ownership_enforced_and_non_leaky():
    svc = BankService()
    with pytest.raises(ServiceError) as e1:
        svc.get_balance("CUST-1002", SAV)  # someone else's account
    with pytest.raises(ServiceError) as e2:
        svc.get_balance(ME, "ACC-DOES-NOT-EXIST")
    assert e1.value.status == e2.value.status == 404 and e1.value.code == e2.value.code


def test_transaction_filters():
    svc = BankService()
    r = svc.search_transactions(ME, SAV, direction="debit", min_amount=Decimal("1299"), max_amount=Decimal("1299"))
    assert r["total_matches"] == 2  # the two same-day Amazon orders
    r = svc.search_transactions(ME, SAV, text="netflix")
    assert r["transactions"][0]["amount"] == "649.00"
    assert svc.search_transactions(ME, SAV, direction="debit", limit=3)["returned"] == 3


def test_bad_filter_values():
    with pytest.raises(ServiceError):
        BankService().search_transactions(ME, SAV, date_from="yesterday")


def test_cheque_book_idempotent_and_duplicate_blocked():
    svc = BankService()
    a = svc.request_cheque_book(ME, SAV, 25, "k1")
    assert svc.request_cheque_book(ME, SAV, 25, "k1") == a  # retry -> same record
    with pytest.raises(ServiceError) as e:
        svc.request_cheque_book(ME, SAV, 25, "k2")  # second open request on same account
    assert e.value.code == "DUPLICATE_OPEN_REQUEST"
    assert svc.request_cheque_book(ME, CUR, 50, "k3")["request_id"] != a["request_id"]


def test_cheque_book_rejects_odd_leaf_count():
    with pytest.raises(ServiceError):
        BankService().request_cheque_book(ME, SAV, 7, "k")
