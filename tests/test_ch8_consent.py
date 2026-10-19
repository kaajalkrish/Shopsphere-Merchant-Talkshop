"""
CH-8: one explicit, scoped approval for every way to pay. No automatic / countdown
consent; GreenLight states the benefits and payment readiness it validated; the
final invoice carries everything the approval covers; cancelling an approval or
an order is recorded (APPROVAL_CANCELLED) and charges nothing.
"""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser
from backend.db.init_db import get_engine
from backend.db.schema import AuditEvent, ProductVariant, ProviderPaymentSession
from backend.main import app
from backend.shop import checkout as checkout_service
from backend.shop import payment as payment_service
from backend.talkshop import brain
from tests.conftest import scoped_consent

SKU = "SSP002-ORANGE-9"


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
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]


def checkout(client, h, sku=SKU):
    line = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h).json()["added_line_id"]
    return client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()


def audits(event, order_id):
    with Session(get_engine()) as db:
        return [json.loads(e.metadata_json) for e in db.query(AuditEvent).filter_by(event_type=event, order_id=order_id)]


def test_the_final_invoice_has_everything_the_approval_covers(client):
    h, _ = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)
    try:
        inv = co["invoice"]
        assert {"merchant_id", "merchant_name", "order_ref", "items", "subtotal", "discounts", "eligible_benefits", "tax",
                "shipping", "final_amount", "currency", "checkout_version", "action", "payment"} <= set(inv)
        assert inv["merchant_name"] == "ShopSphere" and inv["action"] == "PAY" and inv["final_amount"] == co["total"]
        assert inv["items"][0]["quantity"] >= 1 and inv["discounts"][0]["amount"] == co["offer_discount"]
        assert any(b["offer_id"] == "OFF_CHASE2X" for b in inv["eligible_benefits"])
        assert inv["payment"]["display"] == "Chase Visa •••• 1234"
    finally:
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_no_automatic_consent_mode_is_accepted(client):
    h, user = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    try:
        r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm",
                        json=scoped_consent(client, h, co["checkout_id"], consent_mode="auto_countdown"), headers=h)
        assert r.status_code == 422                                          # not even a valid request
        cu = CurrentUser(user_id=user["user_id"], name=user["name"], email=user["email"])
        loop = asyncio.new_event_loop()          # private loop: leaves the main thread's loop alone
        try:
            with pytest.raises(checkout_service.CheckoutError) as exc:
                loop.run_until_complete(payment_service.confirm(
                    cu, co["checkout_id"], True, "auto_countdown", action="PAY",
                    checkout_version=co["checkout_version"], final_amount=co["total"], currency="USD"))
        finally:
            loop.close()
        assert exc.value.code == "EXPLICIT_APPROVAL_REQUIRED"
        assert audits("CHECKOUT_CONSENT", f"{co['checkout_id']}-1") == []
    finally:
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_greenlight_states_the_benefits_and_readiness_it_validated(client):
    h, _ = login(client, "marcus@shopsphere.demo")                          # Chase Mastercard → saved card route
    co = checkout(client, h)
    res = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                      headers=h).json()
    assert res["status"] == "authorized"
    t = {x["check"]: x for x in res["trace"]}
    assert t["Benefit eligibility"]["agent"] == "GreenLight" and "15% off running shoes" in t["Benefit eligibility"]["detail"]
    assert "Route saved_card ready" in t["Payment readiness"]["detail"]
    order = [x["check"] for x in res["trace"]]
    assert order.index("Benefit eligibility") < order.index("DPAT issued")   # validated before any token exists


def test_cancelling_before_paying_is_recorded_and_charges_nothing(client):
    h, _ = login(client, "ravi@shopsphere.demo")
    co = checkout(client, h)
    snap = client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h).json()
    assert snap["status"] == "cancelled"
    rec = audits("APPROVAL_CANCELLED", co["checkout_id"])
    assert rec and rec[0]["charged"] is False and rec[0]["stage"] == "open"
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                    headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CHECKOUT_CLOSED"


def test_cancelling_while_on_the_provider_page_releases_the_session(client):
    email = f"cx-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={"name": "Cx", "email": email,
                                                                             "password": "cancel123"}).json()["access_token"]}
    client.post("/api/me/addresses", headers=h, json={"full_name": "Cx", "line1": "1 A St", "city": "Austin",
                                                      "state": "TX", "postal_code": "78701"})
    co = checkout(client, h)
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h)
    sid = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                      headers=h).json()["payment_session_id"]
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
    with Session(get_engine()) as db:
        assert db.query(ProviderPaymentSession).filter_by(session_id=sid).one().status == "CANCELLED"
    assert audits("APPROVAL_CANCELLED", co["checkout_id"])[0]["stage"] == "awaiting_payment"
    assert "cancelled" in client.get(f"/psp/hosted/{sid}").text                # the provider page won't take it now


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
    return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def test_talkshop_passkey_cancel_is_recorded(client, quiet_llm):
    h, _ = login(client, "kaajal@shopsphere.demo")                             # Visa agent-payment path
    sid = f"c-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    turn(client, h, sid, text="orange")
    co = next(e for e in turn(client, h, sid, action={"type": "checkout"}) if e["type"] == "checkout_ready")["checkout"]
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert any(e["type"] == "approval_required" for e in ev)
    turn(client, h, sid, action={"type": "approval_cancelled"})
    assert audits("APPROVAL_CANCELLED", co["checkout_id"])[0]["reason"] == "PASSKEY_CANCELLED"
    turn(client, h, sid, action={"type": "cancel_checkout"})
