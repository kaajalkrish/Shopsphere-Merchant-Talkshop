"""
CH-2: ShopSphere is for every credit-card holder; Chase status is an
entitlement, not the gate.

  browse    search, products, Talkshop, cart     everyone (visitors too)
  checkout  review, saved details, orders        signed-in ShopSphere customer (any issuer)
  pay       place the order                      a credit-card holder
  offers    Chase credit-card holders only (decision Q4)

Personas: F Dana (Citi Mastercard 5678), G Ravi (Amex 9012), H Chris (Chase Visa
1234 + Citi Mastercard 5678), C Priya (Chase checking only). All simulated.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import ProductVariant
from backend.main import app
from backend.talkshop import brain
from tests.conftest import scoped_consent

SKU = "SSP002-ORANGE-9"          # a running shoe (a Chase offer applies to Chase credit-card holders)


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
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def checkout(client, h, sku=SKU):
    r = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/checkouts", json={"line_ids": [r.json()["added_line_id"]]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def pay(client, h, co_id):
    return client.post(f"/api/checkouts/{co_id}/confirm", json=scoped_consent(client, h, co_id), headers=h)


def code(r):
    return r.json()["detail"]["code"]


# ── Scenario 1: non-Chase credit-card holders use ShopSphere ──────────────────

@pytest.mark.parametrize("email,issuer,network,last4", [
    ("dana@shopsphere.demo", "CITI", "MASTERCARD", "5678"),
    ("ravi@shopsphere.demo", "AMEX", "AMEX", "9012"),
])
def test_non_chase_cardholder_has_access_but_no_chase_benefits(client, email, issuer, network, last4):
    m = client.get("/api/member", headers=login(client, email)).json()
    assert m["eligible"] and m["access"] == {"allowed": True, "code": None}
    assert m["credit_card_holder"] is True and m["chase_benefits"] is False and m["chase"]["linked"] is False
    card = m["member"]["primary_card"]
    assert (card["issuer_code"], card["network_code"], card["last4"]) == (issuer, network, last4)
    assert m["member"]["status"] == "ShopSphere customer"


@pytest.mark.parametrize("email,issuer_label,network", [
    ("dana@shopsphere.demo", "Simulated Citi issuer", "MASTERCARD"),
    ("ravi@shopsphere.demo", "Simulated American Express issuer", "AMEX"),
])
def test_non_chase_cardholder_buys_with_saved_card_and_no_offer(client, email, issuer_label, network):
    h = login(client, email)
    co = checkout(client, h)
    assert co["ready"], co["issues"]
    assert co["payment_method"]["network"] == network and co["payment_method"]["issuer"] != "CHASE"
    assert co["offer"] is None and co["offer_discount"] == 0          # offers are a Chase benefit
    assert co["payment_path"]["path"] == "standard"                   # never the Visa agent-payment path
    res = pay(client, h, co["checkout_id"])
    assert res.status_code == 200 and res.json()["status"] == "authorized", res.text
    order = res.json()["order"]
    assert order["offer"] is None and order["points"]["earned"] >= int(co["total"])   # points for every customer
    assert any(t["check"] == issuer_label for t in res.json()["trace"])               # not "Simulated Chase issuer"


def test_non_chase_customer_sees_no_offers(client):
    h = login(client, "dana@shopsphere.demo")
    assert client.get("/api/offers", headers=h).json() == []
    product = client.get("/api/products/SSP002", headers=h).json()
    assert product["member"] == {"offers": [], "points": 139}
    assert client.get("/api/rewards", headers=h).status_code == 200                  # points for everyone


# ── Scenario 2: Chase credit-card holder keeps Chase benefits ─────────────────

def test_chase_cardholder_has_access_and_chase_benefits(client):
    h = login(client, "chris@shopsphere.demo")
    m = client.get("/api/member", headers=h).json()
    assert m["eligible"] and m["chase_benefits"] is True and m["member"]["status"] == "Chase credit-card holder"
    assert (m["member"]["primary_card"]["issuer_code"], m["member"]["primary_card"]["last4"]) == ("CHASE", "1234")
    assert {o["offer_id"] for o in client.get("/api/offers", headers=h).json()} >= {"OFF_RUN15", "OFF_STYLE3X"}
    co = checkout(client, h)
    assert co["offer"]["offer_id"] == "OFF_RUN15" and co["payment_path"]["path"] == "vic"
    cards = {c["last4"]: c for c in co["saved_payment_methods"]}
    assert (cards["1234"]["issuer"], cards["5678"]["issuer"]) == ("CHASE", "CITI")
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_existing_chase_personas_keep_their_paths(client):
    for email, path in (("kaajal@shopsphere.demo", "vic"), ("marcus@shopsphere.demo", "standard")):
        h = login(client, email)
        co = checkout(client, h)
        assert co["payment_path"]["path"] == path and co["offer"]["offer_id"] == "OFF_RUN15"
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


# ── Checkout and payment levels ───────────────────────────────────────────────

def test_visitor_needs_an_account_to_check_out_or_pay(client):
    h = {"Authorization": f"Bearer {client.post('/api/auth/visitor').json()['access_token']}"}
    line = client.post("/api/cart/lines", json={"sku": SKU}, headers=h).json()["added_line_id"]
    assert code(client.post("/api/checkouts", json={"line_ids": [line]}, headers=h)) == "LOGIN_REQUIRED"
    assert code(client.post("/api/checkouts/CHK_X/confirm", json={"consent": True}, headers=h)) == "LOGIN_REQUIRED"
    m = client.get("/api/member", headers=h).json()
    assert m["eligible"] is False and m["code"] == "LOGIN_REQUIRED" and m["chase_benefits"] is False


def test_customer_without_a_credit_card_reviews_but_cannot_pay(client):
    """Priya (Chase checking only): access yes, but checkout is for credit-card
    holders — no card and no wallet route around that; nothing is charged."""
    h = login(client, "priya@shopsphere.demo")
    client.post("/api/me/addresses", headers=h, json={"full_name": "Priya", "line1": "5 Elm St", "city": "Austin",
                                                      "state": "TX", "postal_code": "78701"})
    co = checkout(client, h)
    assert not co["ready"] and any(i["code"] == "NO_PAYMENT_METHOD" for i in co["issues"])
    r = pay(client, h, co["checkout_id"])
    assert r.status_code == 409 and code(r) == "NOT_READY"
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "wallet"}, headers=h).json()
    assert any(i["code"] == "CREDIT_CARD_REQUIRED" for i in co["issues"])
    r = pay(client, h, co["checkout_id"])
    assert r.status_code == 403 and code(r) == "CREDIT_CARD_REQUIRED"
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_card_holder_may_pay_from_the_wallet(client):
    h = login(client, "ravi@shopsphere.demo")
    co = checkout(client, h)
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "wallet"}, headers=h).json()
    assert co["ready"], co["issues"]
    res = pay(client, h, co["checkout_id"]).json()
    assert res["status"] == "authorized" and res["order"]["payment"]["display"] == "ShopSphere Wallet"


def test_order_tracking_is_not_behind_any_cardholder_gate(client):
    r = client.post("/api/orders/track", json={"order_id": "SS-00000", "email": "nobody@example.com"})
    assert r.status_code == 404 and code(r) == "ORDER_NOT_FOUND"


# ── Q9 / CH-6: ShopSphere never takes typed card numbers ──────────────────────

def test_typed_card_numbers_are_refused(client):
    h = login(client, "dana@shopsphere.demo")
    r = client.post("/api/me/payment-methods", headers=h, json={
        "number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Dana"})
    assert r.status_code == 410 and code(r) == "PAN_NOT_ACCEPTED"
    assert "4242" not in r.text


# ── Q8: password reset is only for your own, signed-in account ────────────────

def test_password_reset_needs_your_own_signed_in_account(client):
    email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"name": "Reset Me", "email": email, "password": "original123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    body = {"email": email, "new_password": "changed1234"}
    assert client.post("/api/auth/reset-password", json=body).status_code == 401          # not signed in
    other = login(client, "dana@shopsphere.demo")
    assert client.post("/api/auth/reset-password", json=body, headers=other).status_code == 403   # not yours
    assert client.post("/api/auth/reset-password", json={"email": "kaajal@shopsphere.demo",
                                                         "new_password": "hijack1234"}, headers=h).status_code == 403
    assert client.post("/api/auth/reset-password", json=body, headers=h).status_code == 200
    assert client.post("/api/auth/login", json={"email": email, "password": "changed1234"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "kaajal@shopsphere.demo",
                                                "password": "demo1234"}).status_code == 200


# ── Talkshop: no Chase gate in the conversation ───────────────────────────────

@pytest.fixture
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


def turn(client, h, sid, *, text=None, action=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action}, headers=h)
    assert r.status_code == 200, r.text
    return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def test_citi_cardholder_shops_in_talkshop_without_any_chase_gate(client, quiet_llm):
    h = login(client, "dana@shopsphere.demo")
    sid = f"f-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    ev = turn(client, h, sid, text="orange")
    assert [e for e in ev if e["type"] == "cart_updated"]
    ev = turn(client, h, sid, action={"type": "checkout"})
    ready = [e for e in ev if e["type"] == "checkout_ready"]
    assert ready and ready[0]["checkout"]["offer"] is None
    text = " ".join(e.get("text", "") for e in ev if e["type"] == "message")
    assert "Chase" not in text and "cardmember" not in text.lower()
    guards = [e for e in ev if e["type"] == "guardrail"]
    assert not any("Cardmember gate" in g.get("check", "") for g in guards)
    turn(client, h, sid, action={"type": "cancel_checkout"})
