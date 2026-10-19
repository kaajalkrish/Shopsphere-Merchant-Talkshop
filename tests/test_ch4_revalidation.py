"""
CH-4: Chase benefits are revalidated against the selected card on every change
and right before payment; the checkout has a version; payment happens only for
the exact invoice the customer approved — action PAY + checkout_version +
final_amount + currency. Anything stale → 409 STALE_CHECKOUT, nothing charged.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import AuditEvent, PaymentAuthorization, PaymentInstruction, Product, ProductVariant
from backend.main import app
from backend.talkshop import brain
from tests.conftest import scoped_consent

RUNNING = "SSP002-ORANGE-9"


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


def checkout(client, h, sku=RUNNING):
    line = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h).json()["added_line_id"]
    r = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def done(client, h, co):
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
    for ln in co["lines"]:
        client.delete(f"/api/cart/lines/{ln['line_id']}", headers=h)


def confirm(client, h, co_id, **scope):
    return client.post(f"/api/checkouts/{co_id}/confirm", json={"consent": True, **scope}, headers=h)


def authorizations(co_id):
    with Session(get_engine()) as db:
        return db.query(PaymentAuthorization).filter(PaymentAuthorization.order_id.like(f"{co_id}-%")).all()


# ── Benefit revalidation on a card switch ────────────────────────────────────

def test_switching_card_removes_chase_benefits_with_reasons_and_bumps_the_version(client):
    h = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)
    try:
        assert co["checkout_version"] == 1 and co["invoice"]["checkout_version"] == 1
        applied = {b["offer_id"] for b in co["benefits"]["applied"]}
        assert applied >= {"OFF_RUN15", "OFF_CHASE2X"} and co["benefits"]["removed"] == []   # (+ Visa cashback if unused)
        assert co["invoice"]["discounts"][0]["label"].startswith("Chase card offer")
        citi = next(c for c in co["saved_payment_methods"] if c["issuer"] == "CITI")
        moved = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": citi["payment_method_id"]},
                             headers=h).json()
        assert moved["checkout_version"] == 2 and moved["benefits"]["applied"] == []
        removed = {b["offer_id"]: b for b in moved["benefits"]["removed"]}
        assert {"OFF_RUN15", "OFF_CHASE2X"} <= set(removed)
        assert removed["OFF_RUN15"]["reason"] == "PAYMENT_CARD_NOT_ELIGIBLE"
        assert removed["OFF_RUN15"]["reason_text"] == "Pay with your Chase credit card to get this offer."
        assert removed["OFF_RUN15"]["amount"] == round(moved["subtotal"] * 0.15, 2)
        inv = moved["invoice"]
        assert inv["discounts"] == [] and inv["final_amount"] == moved["total"] and inv["action"] == "PAY"
        assert inv["payment"]["issuer"] == "CITI" and inv["payment"]["network"] == "MASTERCARD"
        assert inv["merchant_id"] == "SHOPSPHERE" and inv["order_ref"] == co["checkout_id"]
        same = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": citi["payment_method_id"]},
                            headers=h).json()
        assert same["checkout_version"] == 2                    # nothing changed → same version
    finally:
        done(client, h, co)


def test_non_chase_customer_gets_no_removed_benefit_list(client):
    h = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    try:
        assert co["benefits"] == {"applied": [], "removed": []}
    finally:
        done(client, h, co)


# ── Scoped consent ───────────────────────────────────────────────────────────

def test_confirm_needs_the_full_consent_scope(client):
    h = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    try:
        for body in ({}, {"action": "PAY"}, {"checkout_version": 1, "final_amount": co["total"], "currency": "USD"}):
            r = confirm(client, h, co["checkout_id"], **body)
            assert r.status_code == 400 and r.json()["detail"]["code"] == "CONSENT_SCOPE_REQUIRED", body
        assert authorizations(co["checkout_id"]) == []
    finally:
        done(client, h, co)


def test_approving_an_old_version_charges_nothing_and_returns_the_new_invoice(client):
    h = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)
    try:
        old = scoped_consent(client, h, co["checkout_id"])
        citi = next(c for c in co["saved_payment_methods"] if c["issuer"] == "CITI")
        client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": citi["payment_method_id"]}, headers=h)
        r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=old, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"
        fresh = r.json()["detail"]["checkout"]
        assert fresh["checkout_version"] == 2 and fresh["total"] > old["final_amount"]
        assert authorizations(co["checkout_id"]) == []
        r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm",
                        json=scoped_consent(client, h, co["checkout_id"]), headers=h)
        assert r.json()["status"] == "authorized" and r.json()["order"]["offer"] is None   # paid with Citi: no Chase offer
    finally:
        for ln in co["lines"]:
            client.delete(f"/api/cart/lines/{ln['line_id']}", headers=h)


def test_a_price_change_before_payment_is_caught_by_the_live_refresh(client):
    h = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    scope = scoped_consent(client, h, co["checkout_id"])
    with Session(get_engine()) as db:
        p = db.query(Product).filter_by(product_id="SSP002").one()
        price, p.price = p.price, p.price + 10
        db.commit()
    try:
        r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scope, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"
        assert r.json()["detail"]["checkout"]["checkout_version"] == scope["checkout_version"] + 1
        assert authorizations(co["checkout_id"]) == []
    finally:
        with Session(get_engine()) as db:
            db.query(Product).filter_by(product_id="SSP002").one().price = price
            db.commit()
        done(client, h, co)


def test_paid_once_even_if_approve_is_sent_twice(client):
    h = login(client, "ravi@shopsphere.demo")
    co = checkout(client, h)
    scope = scoped_consent(client, h, co["checkout_id"])
    first = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scope, headers=h)
    second = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scope, headers=h)
    assert first.json()["status"] == "authorized"
    assert second.status_code == 409 and second.json()["detail"]["code"] == "CHECKOUT_CLOSED"
    auths = authorizations(co["checkout_id"])
    assert len(auths) == 1
    a = auths[0]
    assert (a.checkout_version, a.action, a.consent_status, a.merchant_id, a.currency) == \
        (scope["checkout_version"], "PAY", "approved", "SHOPSPHERE", "USD")
    assert abs(a.max_amount - scope["final_amount"]) < 0.005
    with Session(get_engine()) as db:
        ev = db.query(AuditEvent).filter(AuditEvent.event_type == "CHECKOUT_CONSENT",
                                         AuditEvent.order_id == f"{co['checkout_id']}-1").one()
        meta = json.loads(ev.metadata_json)
    assert meta["checkout_version"] == scope["checkout_version"] and meta["action"] == "PAY"
    trace = {t["check"]: t for t in first.json()["trace"]}
    assert "checkout version" in trace["Consent scope"]["detail"]


def test_vic_instruction_records_the_checkout_version(client):
    h = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)
    try:
        r = client.post("/api/vic/instructions", json={"checkout_id": co["checkout_id"]}, headers=h)
        assert r.status_code == 200, r.text
        with Session(get_engine()) as db:
            instr = db.query(PaymentInstruction).filter_by(instruction_id=r.json()["instruction"]["instruction_id"]).one()
        assert instr.checkout_version == co["checkout_version"]
    finally:
        done(client, h, co)


# ── Talkshop: "Approve and pay" approves the invoice the shopper was shown ────

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


def of(ev, kind):
    return [e for e in ev if e["type"] == kind]


def test_talkshop_refuses_to_pay_for_an_order_that_changed_after_it_was_shown(client, quiet_llm):
    h = login(client, "dana@shopsphere.demo")
    sid = f"v-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    turn(client, h, sid, text="orange")
    co = of(turn(client, h, sid, action={"type": "checkout"}), "checkout_ready")[0]["checkout"]
    # the order changes outside the conversation (another tab): express delivery → new version
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"delivery_method": "express"}, headers=h)
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert not of(ev, "order_confirmed")
    assert "nothing was charged" in of(ev, "message")[-1]["text"]
    fresh = of(ev, "checkout_updated")[-1]["checkout"]
    assert fresh["checkout_version"] == co["checkout_version"] + 1 and fresh["total"] > co["total"]
    assert authorizations(co["checkout_id"]) == []
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"],
                                      "checkout_version": fresh["checkout_version"], "final_amount": fresh["total"],
                                      "currency": "USD"})
    assert of(ev, "order_confirmed")[0]["order"]["total"] == fresh["total"]


def test_talkshop_refuses_a_tapped_card_with_an_old_version(client, quiet_llm):
    h = login(client, "ravi@shopsphere.demo")
    sid = f"w-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    turn(client, h, sid, text="orange")
    co = of(turn(client, h, sid, action={"type": "checkout"}), "checkout_ready")[0]["checkout"]
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"],
                                      "checkout_version": co["checkout_version"] + 5, "final_amount": co["total"]})
    assert not of(ev, "order_confirmed") and authorizations(co["checkout_id"]) == []
    turn(client, h, sid, action={"type": "cancel_checkout"})
