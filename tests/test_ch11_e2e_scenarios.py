"""
CH-11: end-to-end proof of the three scenarios and the negative fixtures in the
plan (§4). The whole-database card-number scan runs last, in tests/test_zz_leak_scan.py.
All simulated.

  Scenario 1  non-Chase cardholder  → ShopSphere → no Chase offers → saved card → order
  Scenario 2  Chase cardholder      → Chase offers (card-bound) → revalidated → approve → order
  Scenario 3  new ShopSphere user   → sign-up → cart → invoice → approve → provider page → order
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import Order, PaymentAuthorization, ProductVariant
from backend.main import app
from tests.conftest import pay_with_provider, scoped_consent

SHOE = "SSP002-ORANGE-9"
HOME = {"full_name": "E2E Shopper", "line1": "7 Test Way", "city": "Austin", "state": "tx", "postal_code": "78701"}


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


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def checkout(client, h, sku=SHOE):
    line = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h).json()["added_line_id"]
    r = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def pay(client, h, co_id, **extra):
    return client.post(f"/api/checkouts/{co_id}/confirm", json=scoped_consent(client, h, co_id, **extra), headers=h)


def orders_for(co_id):
    with Session(get_engine()) as db:
        return db.query(Order).filter(Order.order_id.like(f"{co_id}%"), Order.display_id.isnot(None)).count()


# ── The three scenarios ─────────────────────────────────────────────────────

def test_scenario_1_generic_cardholder(client):
    h = login(client, "dana@shopsphere.demo")
    me = client.get("/api/member", headers=h).json()
    assert me["eligible"] and me["credit_card_holder"] and not me["chase_benefits"]
    assert client.get("/api/offers", headers=h).json() == []
    co = checkout(client, h)
    assert co["offer"] is None and co["payment_path"]["route"] == "saved_card"
    assert co["invoice"]["payment"]["display"] == "Citi Mastercard •••• 5678"
    res = pay(client, h, co["checkout_id"]).json()
    assert res["status"] == "authorized" and res["order"]["payment"]["display"] == "Mastercard •••• 5678"
    assert res["order"]["points"]["earned"] == int(co["total"])                 # 1× points


def test_scenario_2_chase_cardholder_with_revalidation(client):
    h = login(client, "marcus@shopsphere.demo")                                 # Chase MC: no passkey step
    co = checkout(client, h)
    assert co["offer"]["offer_id"] == "OFF_RUN15"
    assert {b["offer_id"] for b in co["benefits"]["applied"]} >= {"OFF_RUN15", "OFF_CHASE2X"}
    on_wallet = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "wallet"}, headers=h).json()
    assert on_wallet["offer"] is None and on_wallet["checkout_version"] == co["checkout_version"] + 1
    back = client.patch(f"/api/checkouts/{co['checkout_id']}",
                        json={"payment_method_id": co["payment_method"]["payment_method_id"]}, headers=h).json()
    assert back["offer"]["offer_id"] == "OFF_RUN15" and back["total"] == co["total"]
    res = pay(client, h, co["checkout_id"]).json()
    assert res["status"] == "authorized" and res["order"]["offer"]["offer_id"] == "OFF_RUN15"
    assert res["order"]["points"]["bonus"] >= int(co["subtotal"])               # 2× with the Chase card


def test_scenario_3_new_user_signup_and_provider_payment(client):
    vh = {"Authorization": "Bearer " + client.post("/api/auth/visitor").json()["access_token"]}
    client.post("/api/cart/lines", json={"sku": SHOE}, headers=vh)
    r = client.post("/api/auth/register", json={"name": "E2E New", "email": f"e2e-{uuid.uuid4().hex[:8]}@example.com",
                                                "password": "e2enew123"}, headers=vh)
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/me/addresses", json=HOME, headers=h)
    lines = client.get("/api/cart/lines", headers=h).json()["lines"]
    co = client.post("/api/checkouts", json={"line_ids": [lines[0]["line_id"]]}, headers=h).json()
    assert any(i["code"] == "NO_PAYMENT_METHOD" for i in co["issues"])        # no saved payment assumed
    res = pay_with_provider(client, h, co["checkout_id"], "visa", save=True)
    assert res["status"] == "authorized" and res["saved_payment_method"]["source"] == "provider_token"
    assert {t["check"] for t in res["trace"]} >= {"Consent scope", "Provider authorization", "Order confirmed"}


# ── Negative fixtures (plan §4) ─────────────────────────────────────────────

def test_chase_offer_with_an_ineligible_card_and_a_card_switch(client):
    h = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)
    citi = next(c for c in co["saved_payment_methods"] if c["issuer"] == "CITI")
    moved = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": citi["payment_method_id"]},
                         headers=h).json()
    reasons = {b["offer_id"]: b["reason"] for b in moved["benefits"]["removed"]}
    assert reasons["OFF_RUN15"] == "PAYMENT_CARD_NOT_ELIGIBLE" and moved["offer"] is None
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


@pytest.mark.parametrize("outcome,reason", [("decline", "CARD_DECLINED"), ("timeout", "PAYMENT_TIMEOUT")])
def test_declines_and_timeouts_create_no_order(client, outcome, reason):
    h = login(client, "ravi@shopsphere.demo")
    co = checkout(client, h)
    client.post("/api/vic/demo/scenario", json={"outcome": outcome}, headers=h)
    res = pay(client, h, co["checkout_id"]).json()
    assert res["reason"] == reason and orders_for(co["checkout_id"]) == 0
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_cancel_stale_and_duplicate(client):
    h = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    stale = pay(client, h, co["checkout_id"], checkout_version=co["checkout_version"] + 1)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_CHECKOUT"
    first, second = pay(client, h, co["checkout_id"]), pay(client, h, co["checkout_id"])
    assert first.json()["status"] == "authorized" and second.status_code == 409
    with Session(get_engine()) as db:
        assert db.query(PaymentAuthorization).filter(
            PaymentAuthorization.order_id.like(f"{co['checkout_id']}-%")).count() == 1
    co2 = checkout(client, h)
    assert client.post(f"/api/checkouts/{co2['checkout_id']}/cancel", headers=h).json()["status"] == "cancelled"
    assert orders_for(co2["checkout_id"]) == 0


def test_expired_session_and_save_unchecked(client):
    email = f"neg-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "name": "Neg", "email": email, "password": "negative1"}).json()["access_token"]}
    client.post("/api/me/addresses", json=HOME, headers=h)
    res = pay_with_provider(client, h, checkout(client, h)["checkout_id"], "mastercard", save=False)
    assert res["status"] == "authorized" and client.get("/api/me/payment-methods", headers=h).json() == []
    bad = client.post(f"/api/checkouts/{checkout(client, h)['checkout_id']}/payment-session/pps_nope/complete", headers=h)
    assert bad.status_code == 409


def test_non_visa_card_never_takes_the_vic_path(client):
    h = login(client, "kaajal@shopsphere.demo")
    co = checkout(client, h)
    moved = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": "PM_MC_0019"}, headers=h).json()
    assert moved["payment_path"]["route"] == "saved_card"
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
