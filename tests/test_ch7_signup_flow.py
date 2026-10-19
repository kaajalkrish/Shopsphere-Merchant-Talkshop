"""
CH-7 (Scenario 3): a brand-new ShopSphere user who owns a credit card but has no
profile and no saved card. They browse and fill a cart as a visitor, sign up at
checkout (website or inline in Talkshop), keep their cart and conversation, and
pay with a new card on the Simulated Secure Payment Provider's page. Sign-up
never asks for card details and never implies Chase status.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import AuditEvent, ProductVariant
from backend.main import app
from backend.talkshop import brain
from tests.conftest import pay_with_provider, provider_pay

HOME = {"full_name": "Nova Lane", "line1": "9 Harbor Rd", "city": "Austin", "state": "tx", "postal_code": "78702"}


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


@pytest.fixture
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def turn(client, h, sid, *, text=None, action=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action}, headers=h)
    assert r.status_code == 200, r.text
    return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def of(ev, kind):
    return [e for e in ev if e["type"] == kind]


def test_scenario_3_new_user_signs_up_in_talkshop_and_pays_through_the_provider(client, quiet_llm):
    vh = bearer(client.post("/api/auth/visitor").json()["access_token"])
    sid = f"n-{uuid.uuid4().hex[:8]}"
    turn(client, vh, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, vh, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, vh, sid, text="9")
    assert of(turn(client, vh, sid, text="orange"), "cart_updated")              # visitor keeps a cart
    ev = turn(client, vh, sid, action={"type": "checkout"})
    assert of(ev, "login_required")[0]["reason"] == "checkout"                   # sign up at checkout

    password = f"S3cret-{uuid.uuid4().hex[:6]}"
    email = f"Nova.{uuid.uuid4().hex[:6]}@Example.com"
    r = client.post("/api/auth/register", json={"name": " Nova  Lane ", "email": email, "password": password}, headers=vh)
    assert r.status_code == 200 and len(r.json()["merged_lines"]) == 1            # the cart came along
    h = bearer(r.json()["access_token"])
    me = client.get("/api/member", headers=h).json()
    assert me["eligible"] and not me["credit_card_holder"] and not me["chase_benefits"]   # no card, no Chase implied
    assert r.json()["user"]["email"] == email.lower() and r.json()["user"]["name"] == "Nova Lane"

    ev = turn(client, h, sid, action={"type": "checkout"})                       # the conversation carries on
    assert of(ev, "checkout_details_needed")[0]["needs"] == {"guest": False, "address": True, "payment": True}
    addr = client.post("/api/me/addresses", json=HOME, headers=h).json()
    ev = turn(client, h, sid, action={"type": "details_added", "address_id": addr["address_id"]})
    assert of(ev, "checkout_details_needed")[0]["needs"]["payment"]               # no saved payment assumed
    co = of(turn(client, h, sid, action={"type": "details_added", "pay_with": "provider"}), "checkout_ready")[0]["checkout"]
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    hand = of(ev, "provider_handoff")[0]
    provider_pay(client, hand['payment_session_id'], "visa")   # save NOT ticked
    ev = turn(client, h, sid, action={"type": "provider_complete", "checkout_id": co["checkout_id"],
                                      "payment_session_id": hand["payment_session_id"]})
    order = of(ev, "order_confirmed")[0]["order"]
    assert order["payment"]["display"] == "Visa •••• 4242" and order["total"] == co["total"]
    assert client.get("/api/me/payment-methods", headers=h).json() == []           # nothing saved without consent
    transcript = json.dumps(client.get(f"/api/talkshop/sessions/{sid}", headers=h).json())
    assert password not in transcript


def test_scenario_3_on_the_website_with_save_for_next_time(client):
    email = f"web-{uuid.uuid4().hex[:8]}@example.com"
    h = bearer(client.post("/api/auth/register", json={"name": "Web Shopper", "email": email,
                                                       "password": "webshop123"}).json()["access_token"])
    client.post("/api/me/addresses", json=HOME, headers=h)
    line = client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-8"}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    res = pay_with_provider(client, h, co["checkout_id"], "amex", save=True)
    assert res["status"] == "authorized" and res["saved_payment_method"]["label"] == "American Express •••• 0005"
    me = client.get("/api/member", headers=h).json()
    assert me["credit_card_holder"] and not me["chase_benefits"]


def test_signup_is_audited_without_card_details(client):
    email = f"aud-{uuid.uuid4().hex[:8]}@example.com"
    uid = client.post("/api/auth/register", json={"name": "Audit Me", "email": email,
                                                  "password": "audit1234"}).json()["user"]["user_id"]
    with Session(get_engine()) as db:
        ev = db.query(AuditEvent).filter_by(user_id=uid, event_type="CUSTOMER_SIGNUP").one()
    assert json.loads(ev.metadata_json) == {"chase_linked": False, "card_details_collected": False}


def test_email_is_case_insensitive_and_duplicates_are_throttled(client):
    email = f"Case.{uuid.uuid4().hex[:8]}@Example.com"
    assert client.post("/api/auth/register", json={"name": "Case", "email": email, "password": "casecase1"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": email.lower(), "password": "casecase1"}).status_code == 200
    codes = [client.post("/api/auth/register", json={"name": "Again", "email": email.upper(),
                                                     "password": "casecase1"}).status_code for _ in range(6)]
    assert codes[:5] == [409] * 5 and codes[5] == 429
