"""
CH-9: Visa agent payments (VIC-inspired, simulated) are a Visa-NETWORK capability:
the card must be on the Visa network, flagged agent-pay capable by its (simulated)
issuer, and have an active agent token. It is not a Chase feature and not an
access check. Mastercard / Amex / Discover and provider-entered new cards never
reach the VIC path. In the fixtures only Chase Visa cards are capable (decision Q6).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import CardAccount, ProductVariant
from backend.main import app
from backend.vic.enrollment import seed_active
from tests.conftest import provider_pay, register_member, scoped_consent

SKU = "SSP001-BLACK-8"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def restore_stock():
    with Session(get_engine()) as db:
        before = {v.sku: v.stock for v in db.query(ProductVariant)}
    yield
    with Session(get_engine()) as db:
        for v in db.query(ProductVariant):
            v.stock = before.get(v.sku, v.stock)
        db.commit()


def route(client, h):
    line = client.post("/api/cart/lines", json={"sku": SKU, "quantity": 1}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
    return co["payment_path"]["route"]


def holder(issuer, network, capable=None, enrolled=False):
    h, uid = register_member(issuer=issuer, network=network, with_payment_method=True, agent_pay_capable=capable)
    if enrolled:
        with Session(get_engine()) as db:
            seed_active(db, uid, db.query(CardAccount).filter_by(user_id=uid).one().account_id)
    return h, uid


def test_a_non_chase_visa_that_supports_agent_payments_can_use_vic(client):
    """Decoupled from Chase: a (simulated) Citi Visa flagged capable takes the VIC path once enrolled."""
    h, _ = holder("Citi", "Visa", capable=True)
    assert route(client, h) == "vic_setup_required"
    h, _ = holder("Citi", "Visa", capable=True, enrolled=True)
    assert route(client, h) == "vic"


def test_a_visa_without_the_capability_uses_the_saved_card_route(client):
    h, _ = holder("Citi", "Visa")                                       # not flagged (fixture default)
    assert route(client, h) == "saved_card"
    r = client.get("/api/vic/status", headers=h).json()
    assert r["cards"] == []                                             # nothing to enroll


@pytest.mark.parametrize("network", ["Mastercard", "Amex", "Discover"])
def test_non_visa_networks_never_reach_vic_even_if_flagged(client, network):
    h, uid = holder("Chase", network, capable=True)                   # a wrong flag can't route a non-Visa card
    assert route(client, h) == "saved_card"
    with Session(get_engine()) as db:
        acct = db.query(CardAccount).filter_by(user_id=uid).one().account_id
    r = client.post("/api/vic/enrollment/start", json={"card_account_id": acct}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "NOT_VISA_ELIGIBLE"


def test_chase_visa_personas_keep_their_vic_behaviour(client):
    def login(email):
        r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert route(client, login("kaajal@shopsphere.demo")) == "vic"
    assert route(client, login("chris@shopsphere.demo")) == "vic"
    assert route(client, login("elena@shopsphere.demo")) == "vic_setup_required"
    accounts = client.get("/api/member", headers=login("kaajal@shopsphere.demo")).json()["member"]["accounts"]
    caps = {a["last4"]: a["agent_pay_capable"] for a in accounts}
    assert caps == {"4821": True, "0019": False}


def test_a_new_visa_through_the_provider_is_not_vic(client):
    email = f"pv-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "name": "Pv", "email": email, "password": "provider1"}).json()["access_token"]}
    client.post("/api/me/addresses", headers=h, json={"full_name": "Pv", "line1": "1 A St", "city": "Austin",
                                                      "state": "TX", "postal_code": "78701"})
    line = client.post("/api/cart/lines", json={"sku": SKU}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                    headers=h).json()
    assert r["payment_path"] == "provider_handoff"
    sid = r["payment_session_id"]
    provider_pay(client, sid, "visa", save=True)
    done = client.post(f"/api/checkouts/{co['checkout_id']}/payment-session/{sid}/complete", headers=h).json()
    assert done["status"] == "authorized" and not any(t["check"].startswith("VIC control") for t in done["trace"])
    assert route(client, h) == "saved_card"                             # the saved provider Visa: no agent token
