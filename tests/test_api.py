from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import (
    agreements_by_id,
    app,
    members_by_id,
    members_by_phone,
    plans_by_id,
    runs_by_id,
    state_lock,
)


@pytest.fixture(autouse=True)
def reset_state():
    with state_lock:
        members_by_id.clear()
        members_by_phone.clear()
        plans_by_id.clear()
        agreements_by_id.clear()
        runs_by_id.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_member(client, country="SA", phone="0551234567", name="Noura"):
    response = client.post("/members", json={"name": name, "country": country, "phone": phone})
    assert response.status_code == 201
    return response.json()


def add_plan(client, plan_id="plan-sa", country="SA", monthly=29900, joining_fee=0):
    response = client.post("/plans", json={
        "id": plan_id,
        "country": country,
        "monthly": monthly,
        "joining_fee": joining_fee,
    })
    assert response.status_code == 201
    return response.json()


def add_agreement(client, member, plan, start_date="2026-09-20", due_day=1):
    response = client.post("/agreements", json={
        "member_id": member["id"],
        "plan_id": plan["id"],
        "start_date": start_date,
        "due_day": due_day,
    })
    assert response.status_code == 201
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_phone_spellings_resolve_to_same_member(client):
    member = add_member(client)
    assert member["phone"] == "+966551234567"
    for params in (
        {"phone": "966551234567"},
        {"phone": "+966551234567"},
        {"phone": "055 123 4567", "country": "SA"},
        {"phone": "(055)-123-4567", "country": "SA"},
    ):
        response = client.get("/members/lookup", params=params)
        assert response.status_code == 200
        assert response.json() == member


def test_phone_country_rules(client):
    add_member(client, country="BH", phone="31234567")
    response = client.get("/members/lookup", params={"phone": "31234567"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PHONE_NEEDS_COUNTRY"
    response = client.get("/members/lookup", params={"phone": "+97331234567", "country": "SA"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PHONE_COUNTRY_MISMATCH"


def test_invalid_phone_forms(client):
    for phone in ("00966551234567", "+966 551 234 567 ext2", "0212345678", "055123456"):
        response = client.post("/members", json={"name": "A", "country": "SA", "phone": phone})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PHONE"


def test_duplicate_phone_returns_existing_member_id(client):
    member = add_member(client)
    response = client.post("/members", json={
        "name": "Another",
        "country": "SA",
        "phone": "+966551234567",
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PHONE_TAKEN"
    assert response.json()["error"]["existing_member_id"] == member["id"]


def test_concurrent_duplicate_phone_writes_are_serialized(client):
    payloads = [
        {"name": f"Member {index}", "country": "SA", "phone": "0551234567"}
        for index in range(12)
    ]
    with ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(lambda payload: client.post("/members", json=payload), payloads))
    assert sum(response.status_code == 201 for response in responses) == 1
    assert sum(response.status_code == 409 for response in responses) == 11


def test_bahrain_phone_and_minor_unit_vat(client):
    member = add_member(client, country="BH", phone="31234567")
    assert member["phone"] == "+97331234567"
    lookup = client.get("/members/lookup", params={"phone": "+97331234567"})
    assert lookup.status_code == 200
    plan = add_plan(client, plan_id="plan-bh", country="BH", monthly=5791)
    agreement = add_agreement(client, member, plan, start_date="2026-10-01", due_day=1)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2026-10-01", "to": "2026-10-01"},
    )
    first = response.json()[0]
    assert first["net"] == 5791
    assert first["vat"] == 579
    assert first["total"] == 6370
    assert first["currency"] == "BHD"


def test_plans_and_agreement_validation(client):
    member = add_member(client)
    add_plan(client)
    duplicate = client.post("/plans", json={
        "id": "plan-sa",
        "country": "SA",
        "monthly": 100,
        "joining_fee": 0,
    })
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PLAN_EXISTS"
    mismatch_plan = add_plan(client, plan_id="plan-ae", country="AE")
    response = client.post("/agreements", json={
        "member_id": member["id"],
        "plan_id": mismatch_plan["id"],
        "start_date": "2026-09-20",
        "due_day": 1,
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLAN_COUNTRY_MISMATCH"


def test_sa_proration_worked_example_and_monthly_invoice(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2026-09-01", "to": "2026-12-31"},
    )
    assert response.status_code == 200
    invoices = response.json()
    assert invoices[0] == {
        "invoice_no": 1,
        "due_date": "2026-09-20",
        "kind": "FIRST",
        "proration": {"days": 11, "cycle_days": 30},
        "net": 10963,
        "vat": 1644,
        "total": 12607,
        "currency": "SAR",
    }
    assert invoices[1]["invoice_no"] == 2
    assert invoices[1]["due_date"] == "2026-10-01"
    assert invoices[1]["net"] == 29900
    assert invoices[1]["vat"] == 4485
    assert invoices[1]["total"] == 34385


def test_ae_proration_with_joining_fee_and_two_september_invoices(client):
    member = add_member(client, country="AE", phone="0501234567")
    plan = add_plan(client, plan_id="plan-ae", country="AE", monthly=24900, joining_fee=9900)
    agreement = add_agreement(client, member, plan, start_date="2026-09-05", due_day=20)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2026-09-01", "to": "2026-09-30"},
    )
    invoices = response.json()
    assert len(invoices) == 2
    assert invoices[0]["net"] == 21948
    assert invoices[0]["vat"] == 1097
    assert invoices[0]["total"] == 23045
    assert invoices[1]["invoice_no"] == 2
    assert invoices[1]["due_date"] == "2026-09-20"


def test_due_day_anchor_clamps_in_leap_year_without_drifting(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan, start_date="2024-01-31", due_day=31)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2024-02-01", "to": "2024-04-30"},
    )
    invoices = response.json()
    assert [invoice["due_date"] for invoice in invoices] == [
        "2024-02-29",
        "2024-03-31",
        "2024-04-30",
    ]


def test_invoice_number_does_not_restart_at_range_start(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2026-11-01", "to": "2026-11-30"},
    )
    assert [invoice["invoice_no"] for invoice in response.json()] == [3]


def test_invoice_range_validation_and_unknown_agreement(client):
    response = client.get("/agreements/nope/invoices", params={"from": "2026-01-01", "to": "2026-01-02"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    response = client.get(
        f"/agreements/{agreement['id']}/invoices",
        params={"from": "2026-01-02", "to": "2026-01-01"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RANGE"


def test_reconciliation_matched_declined_and_missing(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    response = client.post("/reconciliation/runs", json={
        "month": "2026-10",
        "rows": [
            {
                "gateway_txn_id": "T-1",
                "agreement_id": agreement["id"],
                "amount": 34385,
                "currency": "SAR",
                "status": "SETTLED",
                "attempted_on": "2026-10-01",
            },
            {
                "gateway_txn_id": "T-2",
                "agreement_id": agreement["id"],
                "amount": 34385,
                "currency": "SAR",
                "status": "DECLINED",
                "attempted_on": "2026-10-02",
            },
            {
                "gateway_txn_id": "T-3",
                "agreement_id": "unknown",
                "amount": 100,
                "currency": "SAR",
                "status": "SETTLED",
                "attempted_on": "2026-10-03",
            },
        ],
    })
    assert response.status_code == 201
    body = response.json()
    assert [row["verdict"] for row in body["rows"]] == ["MATCHED", "DECLINED", "MISSING_FROM_LEDGER"]
    assert body["summary"] == {
        "MATCHED": 1,
        "DUE_DAY_DRIFT": 0,
        "DOUBLE_CHARGE_RISK": 0,
        "MISSING_FROM_LEDGER": 1,
        "DECLINED": 1,
    }
    assert body["unmatched_invoices"] == []
    fetched = client.get(f"/reconciliation/runs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_reconciliation_double_charge_and_drift_are_order_independent(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    rows = [
        {
            "gateway_txn_id": "T-A",
            "agreement_id": agreement["id"],
            "amount": 34384,
            "currency": "SAR",
            "status": "SETTLED",
            "attempted_on": "2026-10-02",
        },
        {
            "gateway_txn_id": "T-B",
            "agreement_id": agreement["id"],
            "amount": 34385,
            "currency": "SAR",
            "status": "SETTLED",
            "attempted_on": "2026-10-01",
        },
    ]
    response = client.post("/reconciliation/runs", json={"month": "2026-10", "rows": rows})
    body = response.json()
    assert [row["verdict"] for row in body["rows"]] == ["DOUBLE_CHARGE_RISK", "DOUBLE_CHARGE_RISK"]
    assert body["summary"]["DOUBLE_CHARGE_RISK"] == 2
    assert body["unmatched_invoices"] == []


def test_reconciliation_validation_reports_each_row_rule(client):
    member = add_member(client)
    plan = add_plan(client)
    agreement = add_agreement(client, member, plan)
    row = {
        "gateway_txn_id": "DUP",
        "agreement_id": agreement["id"],
        "amount": 1,
        "currency": "BHD",
        "status": "SETTLED",
        "attempted_on": "2026-11-01",
    }
    response = client.post("/reconciliation/runs", json={"month": "2026-10", "rows": [row, row]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ROWS"
    errors = response.json()["error"]["errors"]
    assert Counter(error["code"] for error in errors) == {
        "DUPLICATE_TXN": 2,
        "ROW_OUTSIDE_MONTH": 2,
        "CURRENCY_MISMATCH": 2,
    }

