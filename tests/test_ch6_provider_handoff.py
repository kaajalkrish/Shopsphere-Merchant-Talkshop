"""
CH-6: a new card is given on the Simulated Secure Payment Provider's own page.
ShopSphere and Talkshop never receive a card number or CVV: the hosted page offers
only synthetic outcomes, and ShopSphere fetches the SAFE result (status, opaque
token, network, last 4, authorization reference) from the provider server-to-server.
Sessions are bound to merchant + checkout + payment_ref + version + amount +
currency, live 10 minutes and are used once. Saving the card is a separate,
explicit choice on the provider's page. This is a simulation, not a PCI boundary.
"""
import json
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import Checkout, Order, PaymentMethod, Product, ProductVariant, ProviderPaymentSession
from backend.main import app
from backend.talkshop import brain
from backend.vic.common import now
from tests.conftest import pay_with_provider, provider_pay, scoped_consent

SKU = "SSP001-BLACK-8"
HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "tx", "postal_code": "78701"}


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


def new_customer(client):
    """A brand-new ShopSphere customer with an address and NO saved card."""
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"name": "New Shopper", "email": email, "password": "newshop123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/me/addresses", json=HOME, headers=h)
    return h, r.json()["user"]["user_id"]


def checkout(client, h, sku=SKU):
    line = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h).json()["added_line_id"]
    r = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def handoff(client, h, co_id, **extra):
    client.patch(f"/api/checkouts/{co_id}", json={"pay_with": "provider"}, headers=h)
    r = client.post(f"/api/checkouts/{co_id}/confirm", json=scoped_consent(client, h, co_id, **extra), headers=h)
    assert r.status_code == 200 and r.json()["status"] == "provider_handoff", r.text
    return r.json()


def complete(client, h, co_id, sid):
    return client.post(f"/api/checkouts/{co_id}/payment-session/{sid}/complete", headers=h)


def orders_for(co_id):
    with Session(get_engine()) as db:
        return db.query(Order).filter(Order.order_id.like(f"{co_id}%"), Order.display_id.isnot(None)).count()


# ── The happy paths: any network, saved only on request ─────────────────────

@pytest.mark.parametrize("outcome,network,last4", [("visa", "Visa", "4242"), ("mastercard", "Mastercard", "4444"),
                                                   ("amex", "American Express", "0005"), ("stepup", "Visa", "3184")])
def test_new_card_on_the_provider_page_places_the_order(client, outcome, network, last4):
    h, _ = new_customer(client)
    co = checkout(client, h)
    assert any(i["code"] == "NO_PAYMENT_METHOD" for i in co["issues"])        # no saved card assumed
    res = pay_with_provider(client, h, co["checkout_id"], outcome)
    assert res["status"] == "authorized", res
    assert res["order"]["payment"]["display"] == f"{network} •••• {last4}"
    assert res["saved_payment_method"] is None and client.get("/api/me/payment-methods", headers=h).json() == []
    checks = {t["check"] for t in res["trace"]}
    assert {"Provider authorization", "Order confirmed", "DPAT check 12/12 · Consent record exists"} <= checks


def test_saving_is_a_separate_explicit_choice_and_keeps_only_a_token(client):
    h, uid = new_customer(client)
    res = pay_with_provider(client, h, checkout(client, h)["checkout_id"], "mastercard", save=True)
    saved = res["saved_payment_method"]
    assert saved["source"] == "provider_token" and saved["label"] == "Mastercard •••• 4444" and saved["issuer"] == "UNKNOWN"
    with Session(get_engine()) as db:
        row = db.query(PaymentMethod).filter_by(payment_method_id=saved["payment_method_id"]).one()
    assert row.token_ref.startswith("pm_mock_") and "token_ref" not in saved
    co = checkout(client, h)                                                   # next time: the saved card is there
    assert co["ready"] and co["payment_method"]["payment_method_id"] == saved["payment_method_id"]
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                    headers=h).json()
    assert r["status"] == "authorized" and r["order"]["payment"]["display"] == "Mastercard •••• 4444"


# ── Failures: nothing charged, no order, checkout reopens ───────────────────

@pytest.mark.parametrize("outcome,status,reason", [("decline", "declined", "CARD_DECLINED"),
                                                   ("timeout", "declined", "PAYMENT_TIMEOUT")])
def test_decline_and_timeout_create_no_order(client, outcome, status, reason):
    h, _ = new_customer(client)
    co = checkout(client, h)
    res = pay_with_provider(client, h, co["checkout_id"], outcome)
    assert (res["status"], res["reason"]) == (status, reason) and res["checkout"]["status"] == "open"
    assert orders_for(co["checkout_id"]) == 0


def test_failed_payment_verification_is_not_charged(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"])["payment_session_id"]
    provider_pay(client, sid, "stepup_fail")
    res = complete(client, h, co["checkout_id"], sid).json()
    assert (res["status"], res["reason"]) == ("blocked", "VERIFICATION_FAILED") and orders_for(co["checkout_id"]) == 0


def test_unfinished_session_is_pending_then_an_expired_one_is_refused(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"])["payment_session_id"]
    assert complete(client, h, co["checkout_id"], sid).json()["status"] == "pending"
    with Session(get_engine()) as db:
        db.query(ProviderPaymentSession).filter_by(session_id=sid).update({"expires_at": now() - timedelta(minutes=1)})
        db.commit()
    res = complete(client, h, co["checkout_id"], sid).json()
    assert (res["status"], res["reason"]) == ("blocked", "SESSION_EXPIRED") and res["checkout"]["status"] == "open"
    page = client.get(f"/psp/hosted/{sid}")
    assert "expired" in page.text and orders_for(co["checkout_id"]) == 0


def test_a_session_is_bound_to_its_order_and_used_once(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"])["payment_session_id"]
    other = checkout(client, h, "SSP004-CORAL-9")
    r = complete(client, h, other["checkout_id"], sid)                          # not this order's session
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SESSION_MISMATCH"
    assert complete(client, h, co["checkout_id"], "pps_forged").status_code == 409
    provider_pay(client, sid, "visa")
    first = complete(client, h, co["checkout_id"], sid).json()
    again = complete(client, h, co["checkout_id"], sid).json()
    assert first["status"] == again["status"] == "authorized" and again["duplicate"] is True
    assert first["order"]["order_id"] == again["order"]["order_id"] and orders_for(co["checkout_id"]) == 1
    page = provider_pay(client, sid, "visa")     # can't be paid again
    assert "this payment is complete" in page.text.lower()


def test_approve_twice_while_on_the_provider_page_does_not_start_a_second_payment(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    first = handoff(client, h, co["checkout_id"])
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co["checkout_id"]),
                    headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PAYMENT_IN_PROGRESS"
    assert r.json()["detail"]["payment_session_id"] == first["payment_session_id"]


def test_a_price_change_while_on_the_provider_page_releases_the_payment(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"])["payment_session_id"]
    provider_pay(client, sid, "visa")
    with Session(get_engine()) as db:
        p = db.query(Product).filter_by(product_id="SSP001").one()
        price, p.price = p.price, p.price + 5
        db.commit()
    try:
        r = complete(client, h, co["checkout_id"], sid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"
        with Session(get_engine()) as db:
            assert db.query(ProviderPaymentSession).filter_by(session_id=sid).one().status == "CANCELLED"
            assert db.query(Checkout).filter_by(checkout_id=co["checkout_id"]).one().status == "open"
        assert orders_for(co["checkout_id"]) == 0
    finally:
        with Session(get_engine()) as db:
            db.query(Product).filter_by(product_id="SSP001").one().price = price
            db.commit()


# ── Security ────────────────────────────────────────────────────────────────

def test_the_card_form_belongs_to_the_provider_and_only_shopsphere_may_embed_it(client):
    h, _ = new_customer(client)
    sid = handoff(client, h, checkout(client, h)["checkout_id"])["payment_session_id"]
    r = client.get(f"/psp/hosted/{sid}")
    page = r.text.lower()
    assert "simulated secure payment provider" in page and "test cards only" in page
    assert 'name="card_number"' in page and 'action="/psp/hosted/' in page        # posts to the PROVIDER, not the store
    csp = r.headers["content-security-policy"]
    assert csp.startswith("frame-ancestors") and "http://localhost:5173" in csp and "*" not in csp


@pytest.mark.parametrize("number,expiry,cvc,error", [
    ("4111 1111 1111 1111", "12/30", "123", "only accepts the test card numbers"),   # a real-looking card: refused
    ("4242 4242 4242 4242", "01/20", "123", "expired"),
    ("4242 4242 4242 4242", "13/30", "123", "MM/YY"),
    ("3782 822463 10005", "12/30", "123", "security code"),                         # Amex needs 4 digits
])
def test_the_provider_refuses_bad_or_real_cards_and_never_echoes_them(client, number, expiry, cvc, error):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"])["payment_session_id"]
    r = client.post(f"/psp/hosted/{sid}/pay", data={"cardholder_name": "Test", "card_number": number,
                                                   "expiry": expiry, "cvc": cvc})
    assert error in r.text
    assert not re.search(r'name="card_number"[^>]*value=', r.text)           # the number is never filled back in
    assert number.replace(" ", "") not in r.text and 'name="cvc"' in r.text and f'value="{cvc}"' not in r.text
    with Session(get_engine()) as db:
        assert db.query(ProviderPaymentSession).filter_by(session_id=sid).one().status == "PENDING"
    assert complete(client, h, co["checkout_id"], sid).json()["status"] == "pending"


def test_the_embedding_store_page_is_told_when_the_payment_is_done(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    url = f"http://localhost:5174/checkout/{co['checkout_id']}"
    sid = handoff(client, h, co["checkout_id"], return_url=url)["payment_session_id"]
    page = provider_pay(client, sid, "visa").text
    assert "shopsphere-provider-result" in page and '"http://localhost:5174");' in page   # only to the store's origin
    assert "4242 4242" not in page and "4242424242424242" not in page                  # session id + status only
    with Session(get_engine()) as db:
        row = db.query(ProviderPaymentSession).filter_by(session_id=sid).one()
    assert (row.status, row.network, row.last4) == ("VERIFIED", "VISA", "4242")     # PayIt authorizes later


def test_merchant_api_needs_the_merchant_key(client):
    h, _ = new_customer(client)
    sid = handoff(client, h, checkout(client, h)["checkout_id"])["payment_session_id"]
    assert client.get(f"/psp/v1/payment_sessions/{sid}").status_code == 401
    assert client.post(f"/psp/v1/payment_sessions/{sid}/authorize",
                       json={"merchant_id": "SHOPSPHERE", "payment_ref": "x", "amount": 1, "currency": "USD"}).status_code == 401


def test_only_shopsphere_return_urls_are_used(client):
    h, _ = new_customer(client)
    co = checkout(client, h)
    sid = handoff(client, h, co["checkout_id"], return_url="https://evil.example/steal")["payment_session_id"]
    with Session(get_engine()) as db:
        assert db.query(ProviderPaymentSession).filter_by(session_id=sid).one().return_url is None
    good = checkout(client, h, "SSP004-CORAL-9")
    url = f"http://localhost:5173/checkout/{good['checkout_id']}"
    sid2 = handoff(client, h, good["checkout_id"], return_url=url)["payment_session_id"]
    page = provider_pay(client, sid2, "visa").text
    assert f"{url}?provider_session={sid2}" in page


def test_no_card_number_or_cvv_anywhere_after_a_provider_payment(client):
    h, uid = new_customer(client)
    res = pay_with_provider(client, h, checkout(client, h)["checkout_id"], "visa", save=True)
    blob = json.dumps(res)
    engine = get_engine()
    import sqlalchemy
    with Session(engine) as db:
        for table in ("provider_payment_sessions", "payment_methods", "checkouts", "orders", "audit_events"):
            for row in db.execute(sqlalchemy.text(f"SELECT * FROM {table}")).fetchall():
                blob += json.dumps([str(v) for v in row])
    for raw in ("4242424242424242", "4242 4242 4242 4242", "5555555555554444", "378282246310005"):
        assert raw not in blob
    cols = {c["name"] for c in sa_inspect(engine).get_columns("provider_payment_sessions")}
    assert not cols & {"pan", "card_number", "number", "cvv", "cvc", "expiry"}


# ── Chase benefits and a new card ───────────────────────────────────────────

def test_chase_benefits_do_not_apply_to_a_new_card_through_the_provider(client):
    r = client.post("/api/auth/login", json={"email": "chris@shopsphere.demo", "password": "demo1234"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    co = checkout(client, h, "SSP002-ORANGE-9")
    moved = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h).json()
    try:
        assert moved["offer"] is None and moved["payment_path"]["route"] == "provider_handoff"
        assert any(b["offer_id"] == "OFF_RUN15" and b["reason"] == "PAYMENT_CARD_NOT_ELIGIBLE"
                   for b in moved["benefits"]["removed"])
        assert moved["invoice"]["payment"]["type"] == "provider"
    finally:
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


# ── Talkshop: the chat orchestrates, the provider's page takes the card ─────

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


def test_talkshop_hands_off_to_the_provider_and_confirms_after(client, quiet_llm):
    h, _ = new_customer(client)
    sid = f"p-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text="I need running shoes under $150 for everyday running.")
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    turn(client, h, sid, text="orange")
    ev = turn(client, h, sid, action={"type": "checkout"})
    assert of(ev, "checkout_details_needed")[0]["needs"]["payment"]
    assert "Simulated Secure Payment Provider" in of(ev, "message")[-1]["text"]
    co = of(turn(client, h, sid, action={"type": "details_added", "pay_with": "provider",
                                         "label": "Pay with a new card"}), "checkout_ready")[0]["checkout"]
    assert co["pay_with"] == "provider" and co["ready"]
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    hand = of(ev, "provider_handoff")[0]
    assert "/psp/hosted/" in hand["hosted_url"] and not of(ev, "order_confirmed")
    ps = hand["payment_session_id"]
    ev = turn(client, h, sid, action={"type": "provider_complete", "checkout_id": co["checkout_id"],
                                      "payment_session_id": ps})
    assert not of(ev, "order_confirmed") and "still waiting" in of(ev, "message")[-1]["text"]
    provider_pay(client, ps, "amex", save=True)
    ev = turn(client, h, sid, action={"type": "provider_complete", "checkout_id": co["checkout_id"],
                                      "payment_session_id": ps})
    order = of(ev, "order_confirmed")[0]["order"]
    assert order["payment"]["display"] == "American Express •••• 0005"
    assert "saved for next time" in of(ev, "message")[-1]["text"]
    transcript = json.dumps(client.get(f"/api/talkshop/sessions/{sid}", headers=h).json())
    assert "378282246310005" not in transcript and "pm_mock_" not in transcript
