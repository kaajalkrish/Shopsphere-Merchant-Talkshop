"""
Secure Payment Sheet, Phases 4–7: a new card is tokenized by the Simulated Payment
Provider, verified with a generic (simulated) payment verification when the issuer asks
for one, and only then authorized — by PayIt, after GreenLight and PayIt's own checks.

  * the verification screen is generic (any network) and names merchant, amount, card
    and purpose; its one-time challenge is single use and expires
  * cancelled / failed verification and a closed sheet authorize and charge nothing
  * an amount change after verification invalidates it: a new approval, a new session,
    a new verification for the new amount
  * authorization is bound to merchant + payment_ref + amount + currency and idempotent
  * "save" keeps a token + network + last 4 + expiry + display name; unticked keeps nothing
  * Talkshop events, transcript, trace and audit never carry a card number, CVC, the
    challenge or the provider token
"""
import html
import json
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import (AuditEvent, Checkout, Order, PaymentAuthorization, PaymentMethod, Product,
                               ProductVariant, ProviderPaymentSession)
from backend.main import app
from backend.talkshop import brain
from backend.vic.common import now
from tests.conftest import TEST_CARDS, provider_pay, scoped_consent, verification_challenge

KEY = {"X-Gateway-Key": "shopsphere-demo-merchant-key"}
STORE = "http://localhost:5174"
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


@pytest.fixture
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


# ── helpers ──────────────────────────────────────────────────────────────────

def new_customer(client):
    email = f"sheet-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"name": "Sheet Shopper", "email": email, "password": "sheet-pass1"})
    h = {"Authorization": "Bearer " + r.json()["access_token"]}
    addr = client.post("/api/me/addresses", headers=h, json={"full_name": "Sheet", "line1": "1 A St", "city": "Austin",
                                                             "state": "TX", "postal_code": "78701"}).json()
    return h, r.json()["user"]["user_id"], addr["address_id"]


def handoff(client, sku=SKU):
    """A new customer approves the exact invoice → a provider payment session."""
    h, uid, _ = new_customer(client)
    line = client.post("/api/cart/lines", json={"sku": sku}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", headers=h, json=scoped_consent(
        client, h, co["checkout_id"], return_url=f"{STORE}/checkout/{co['checkout_id']}"))
    assert r.json()["status"] == "provider_handoff", r.text
    return h, uid, co["checkout_id"], r.json()["payment_session_id"]


def card(client, sid, which="visa", save=False):
    form = {"cardholder_name": "  Sheet   Shopper ", "card_number": TEST_CARDS[which], "expiry": "07/31",
            "cvc": "1234" if which == "amex" else "123", **({"save": "1"} if save else {})}
    client.post(f"/psp/hosted/{sid}/pay", data=form)
    return client.post(f"/psp/hosted/{sid}/continue").text


def answer(client, sid, page, result):
    return client.post(f"/psp/hosted/{sid}/challenge", data={"result": result, "challenge": verification_challenge(page)}).text


def provider(client, sid):
    return client.get(f"/psp/v1/payment_sessions/{sid}", headers=KEY).json()


def complete(client, h, co_id, sid):
    return client.post(f"/api/checkouts/{co_id}/payment-session/{sid}/complete", headers=h)


def authorizations(co_id):
    with Session(get_engine()) as db:
        return db.query(PaymentAuthorization).filter(PaymentAuthorization.order_id.like(f"{co_id}-%")).count()


def orders(co_id):
    with Session(get_engine()) as db:
        return db.query(Order).filter(Order.order_id.like(f"{co_id}-%"), Order.display_id.isnot(None)).count()


def messages(page):
    return [json.loads(m) for m in re.findall(r"postMessage\((\{.*?\}),", page)]


# ── Phase 4: generic payment verification ────────────────────────────────────

@pytest.mark.parametrize("which,label", [("visa", "Visa •••• 4242"), ("mastercard", "Mastercard •••• 4444")])
def test_the_verification_screen_is_generic_and_names_exactly_what_is_approved(client, which, label):
    h, _, co_id, sid = handoff(client)
    total = client.get(f"/api/checkouts/{co_id}", headers=h).json()["total"]
    page = html.unescape(card(client, sid, which))
    assert provider(client, sid)["status"] == "VERIFICATION_REQUIRED"
    for text in ("Simulated payment verification", "Verify your payment", "ShopSphere", f"USD {total:,.2f}", label,
                 "One-time purchase", "Verify payment", "Cancel", "Simulate failed verification",
                 "This verification approves this exact ShopSphere purchase only. If the merchant, amount or order "
                 "changes, you will be asked again."):
        assert text in page, text
    if which == "mastercard":
        assert "Visa" not in page.split("Real card issuers")[0]                # not every card is a Visa
    step = [m for m in messages(page) if m["type"] == "shopsphere-provider-step"][-1]
    assert step == {"type": "shopsphere-provider-step", "step": "verify", "network": which.upper(),
                    "last4": label[-4:], "session": sid}
    challenge = verification_challenge(page)
    assert challenge not in json.dumps(messages(page)) and challenge not in json.dumps(provider(client, sid))
    with Session(get_engine()) as db:                                         # only the hash is kept
        row = db.query(ProviderPaymentSession).filter_by(session_id=sid).one()
        assert row.challenge_hash and challenge not in row.challenge_hash


def test_the_challenge_is_single_use_and_a_stale_one_is_refused(client):
    _, _, _, sid = handoff(client)
    page = card(client, sid)
    old = verification_challenge(page)
    page2 = client.get(f"/psp/hosted/{sid}").text                            # a new view issues a new challenge
    again = client.post(f"/psp/hosted/{sid}/challenge", data={"result": "ok", "challenge": old}).text
    assert "no longer valid" in again and provider(client, sid)["status"] == "VERIFICATION_REQUIRED"
    forged = client.post(f"/psp/hosted/{sid}/challenge", data={"result": "ok", "challenge": "guess"}).text
    assert "no longer valid" in forged and provider(client, sid)["status"] == "VERIFICATION_REQUIRED"
    done = answer(client, sid, forged, "ok")
    assert provider(client, sid)["status"] == "VERIFIED" and "Payment verified" in done
    assert page2 != page


def test_an_expired_verification_creates_a_new_request(client):
    _, _, _, sid = handoff(client)
    page = card(client, sid)
    with Session(get_engine()) as db:
        db.query(ProviderPaymentSession).filter_by(session_id=sid).update(
            {"challenge_expires_at": now() - timedelta(seconds=1)})
        db.commit()
    page = answer(client, sid, page, "ok")
    assert "verification expired" in page and provider(client, sid)["status"] == "VERIFICATION_REQUIRED"
    answer(client, sid, page, "ok")                                          # the new request works
    assert provider(client, sid)["status"] == "VERIFIED"


def test_a_failed_verification_can_be_retried(client):
    h, _, co_id, sid = handoff(client)
    page = answer(client, sid, card(client, sid), "fail")
    assert "Verification failed" in page and "2 attempts left" in page
    assert provider(client, sid)["status"] == "VERIFICATION_REQUIRED" and authorizations(co_id) == 0
    answer(client, sid, page, "ok")
    assert complete(client, h, co_id, sid).json()["status"] == "authorized" and authorizations(co_id) == 1


def test_three_failed_verifications_authorize_and_charge_nothing(client):
    h, _, co_id, sid = handoff(client)
    page = card(client, sid)
    for _ in range(3):
        page = answer(client, sid, page, "fail")
    assert provider(client, sid)["status"] == "VERIFICATION_FAILED"
    res = complete(client, h, co_id, sid).json()
    assert (res["status"], res["reason"]) == ("blocked", "VERIFICATION_FAILED") and res["checkout"]["status"] == "open"
    assert authorizations(co_id) == 0 and orders(co_id) == 0


def test_cancelling_verification_goes_back_to_review_and_charges_nothing(client):
    h, _, co_id, sid = handoff(client)
    page = html.unescape(answer(client, sid, card(client, sid), "cancel"))
    assert "Payment cancelled. Nothing was charged." in page
    assert any(m.get("status") == "CANCELLED" for m in messages(page))
    res = complete(client, h, co_id, sid).json()
    assert res["status"] == "cancelled" and res["checkout"]["status"] == "open"
    assert authorizations(co_id) == 0 and orders(co_id) == 0


def test_a_frictionless_card_is_ready_without_a_verification_step(client):
    h, _, co_id, sid = handoff(client)
    page = card(client, sid, "frictionless")
    assert "Verify your payment" not in page and provider(client, sid)["status"] == "VERIFIED"
    assert any(m.get("status") == "VERIFIED" for m in messages(page))
    res = complete(client, h, co_id, sid).json()
    assert res["status"] == "authorized"
    events = [t.get("event") for t in res["trace"]]
    assert "CARD_TOKENIZED" in events and "ISSUER_VERIFICATION_REQUIRED" not in events and "USER_VERIFIED" not in events


# ── Phase 5: PayIt authorizes, bound and once ────────────────────────────────

def test_verified_is_not_authorized_until_payit_asks(client):
    h, _, co_id, sid = handoff(client)
    answer(client, sid, card(client, sid), "ok")
    res = provider(client, sid)
    assert res["status"] == "VERIFIED" and res["authorization_reference"] is None and authorizations(co_id) == 0
    done = complete(client, h, co_id, sid).json()
    assert done["status"] == "authorized" and provider(client, sid)["status"] == "CAPTURED"
    assert done["card"] == "Visa •••• 4242"


def test_the_merchant_cannot_authorize_before_verification_or_for_another_amount(client):
    h, _, co_id, sid = handoff(client)
    with Session(get_engine()) as db:
        co = db.query(Checkout).filter_by(checkout_id=co_id).one()
        body = {"merchant_id": "SHOPSPHERE", "payment_ref": co.pending_payment_ref, "amount": co.total, "currency": "USD"}
    card(client, sid)                                                         # tokenized, verification pending
    r = client.post(f"/psp/v1/payment_sessions/{sid}/authorize", json=body, headers=KEY)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CREDENTIAL_NOT_READY"
    answer(client, sid, client.get(f"/psp/hosted/{sid}").text, "ok")
    for wrong in ({"amount": body["amount"] + 1}, {"payment_ref": "other"}, {"merchant_id": "EVIL"}, {"currency": "EUR"}):
        r = client.post(f"/psp/v1/payment_sessions/{sid}/authorize", json={**body, **wrong}, headers=KEY)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "SESSION_BINDING_MISMATCH", wrong
    first = client.post(f"/psp/v1/payment_sessions/{sid}/authorize", json=body, headers=KEY).json()
    again = client.post(f"/psp/v1/payment_sessions/{sid}/authorize", json=body, headers=KEY).json()
    assert first["status"] == again["status"] == "CAPTURED" and again["duplicate"] is True
    assert first["authorization_reference"] == again["authorization_reference"]          # authorized once


def test_insufficient_funds_is_declined_and_another_method_can_be_used(client):
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "decline")
    with Session(get_engine()) as db:
        db.query(ProviderPaymentSession).filter_by(session_id=sid).update({"last4": "9995"})   # the 9995 test card
        db.commit()
    res = complete(client, h, co_id, sid).json()
    assert (res["status"], res["reason"]) == ("declined", "INSUFFICIENT_FUNDS") and "insufficient funds" in res["message"]
    assert res["checkout"]["status"] == "open" and orders(co_id) == 0
    r = client.post(f"/api/checkouts/{co_id}/confirm", headers=h, json=scoped_consent(client, h, co_id))
    sid2 = r.json()["payment_session_id"]                                     # a new session for another card
    assert sid2 != sid
    provider_pay(client, sid2, "mastercard")
    assert complete(client, h, co_id, sid2).json()["status"] == "authorized" and orders(co_id) == 1


def test_a_timeout_says_nothing_was_charged_and_a_retry_does_not_double_charge(client):
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "timeout")
    res = complete(client, h, co_id, sid).json()
    assert (res["status"], res["reason"]) == ("declined", "PAYMENT_TIMEOUT") and "Nothing was charged" in res["message"]
    assert provider(client, sid)["status"] == "TIMEOUT" and orders(co_id) == 0
    assert complete(client, h, co_id, sid).status_code == 409                  # the old session can't be reused


def test_duplicate_completion_creates_one_authorization_and_one_order(client):
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "visa")
    first = complete(client, h, co_id, sid).json()
    again = complete(client, h, co_id, sid).json()
    assert first["order"]["order_id"] == again["order"]["order_id"] and again["duplicate"] is True
    assert authorizations(co_id) == 1 and orders(co_id) == 1


def test_an_amount_change_after_verification_needs_a_new_approval_and_verification(client):
    h, _, co_id, sid = handoff(client)
    answer(client, sid, card(client, sid), "ok")
    with Session(get_engine()) as db:
        p = db.query(Product).filter_by(product_id="SSP001").one()
        price, p.price = p.price, p.price + 7
        db.commit()
    try:
        r = complete(client, h, co_id, sid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"
        fresh = r.json()["detail"]["checkout"]
        assert provider(client, sid)["status"] == "CANCELLED" and authorizations(co_id) == 0   # old verification void
        r = client.post(f"/api/checkouts/{co_id}/confirm", headers=h, json=scoped_consent(client, h, co_id))
        sid2 = r.json()["payment_session_id"]
        assert sid2 != sid and provider(client, sid2)["amount"] == fresh["total"]
        page = html.unescape(card(client, sid2))
        assert f"USD {fresh['total']:,.2f}" in page and "Verify your payment" in page          # asked again
        answer(client, sid2, page, "ok")
        done = complete(client, h, co_id, sid2).json()
        assert done["status"] == "authorized" and done["order"]["total"] == fresh["total"]
    finally:
        with Session(get_engine()) as db:
            db.query(Product).filter_by(product_id="SSP001").one().price = price
            db.commit()


# ── Phase 6: save card ───────────────────────────────────────────────────────

def test_save_ticked_keeps_token_network_last4_expiry_and_name_only(client):
    h, uid, co_id, sid = handoff(client)
    answer(client, sid, card(client, sid, "mastercard", save=True), "ok")
    res = provider(client, sid)
    assert (res["cardholder_display"], res["card_exp_month"], res["card_exp_year"]) == ("Sheet Shopper", 7, 2031)
    saved = complete(client, h, co_id, sid).json()["saved_payment_method"]
    assert saved["label"] == "Mastercard •••• 4444"
    with Session(get_engine()) as db:
        pm = db.query(PaymentMethod).filter_by(user_id=uid).one()
        assert (pm.exp_month, pm.exp_year, pm.cardholder_name, pm.source) == (7, 2031, "Sheet Shopper", "provider_token")
        assert pm.token_ref.startswith("pm_mock_")
        stored = json.dumps({c.name: str(getattr(pm, c.name)) for c in pm.__table__.columns})
    assert "5555555555554444" not in stored and "555555" not in stored


def test_save_unticked_by_default_keeps_nothing(client):
    h, uid, co_id, sid = handoff(client)
    page = client.get(f"/psp/hosted/{sid}").text
    assert 'name="save" value="1">' in page and "checked" not in page.split('name="save"')[1][:20]
    answer(client, sid, card(client, sid, "visa"), "ok")
    res = provider(client, sid)
    assert res["save_requested"] is False and res["cardholder_display"] is None and res["card_exp_month"] is None
    with Session(get_engine()) as db:
        row = db.query(ProviderPaymentSession).filter_by(session_id=sid).one()
        assert row.cardholder_display is None and row.card_exp_year is None
    assert complete(client, h, co_id, sid).json()["saved_payment_method"] is None
    with Session(get_engine()) as db:
        assert db.query(PaymentMethod).filter_by(user_id=uid).count() == 0


# ── Phase 7: closing the sheet, trace, redaction ─────────────────────────────

def test_closing_the_sheet_releases_the_session_and_goes_back_to_review(client):
    h, uid, co_id, sid = handoff(client)
    card(client, sid)                                                         # mid-way: verification showing
    r = client.post(f"/api/checkouts/{co_id}/payment-session/{sid}/abandon", headers=h).json()
    assert r["status"] == "cancelled" and r["checkout"]["status"] == "open"
    assert provider(client, sid)["status"] == "CANCELLED" and authorizations(co_id) == 0 and orders(co_id) == 0
    with Session(get_engine()) as db:
        meta = [json.loads(e.metadata_json) for e in db.query(AuditEvent).filter_by(
            event_type="APPROVAL_CANCELLED", order_id=co_id)]
    assert meta and meta[-1]["reason"] == "PAYMENT_SHEET_CLOSED" and meta[-1]["charged"] is False
    assert complete(client, h, co_id, sid).status_code == 409                  # that session is gone


def test_closing_after_the_order_is_placed_changes_nothing(client):
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "visa")
    assert complete(client, h, co_id, sid).json()["status"] == "authorized"
    r = client.post(f"/api/checkouts/{co_id}/payment-session/{sid}/abandon", headers=h).json()
    assert r["status"] == "noop" and r["checkout"]["status"] == "paid" and provider(client, sid)["status"] == "CAPTURED"


def test_the_trace_names_each_step_in_order(client):
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "visa")
    trace = complete(client, h, co_id, sid).json()["trace"]
    events = [t["event"] for t in trace if t.get("event")]
    assert events == ["CARD_TOKENIZED", "ISSUER_VERIFICATION_REQUIRED", "USER_VERIFIED", "PAYMENT_CREDENTIAL_READY",
                      "GREENLIGHT_CONSENT_VALIDATED", "DPAT_CHECKS_PASSED", "PAYMENT_AUTHORIZED", "ORDER_CONFIRMED"]


def _talkshop_turn(client, h, sid, **body):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, **body}, headers=h)
    assert r.status_code == 200, r.text
    return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def _of(ev, kind):
    return [e for e in ev if e["type"] == kind]


def _talkshop_to_handoff(client, quiet_llm_unused=None):
    h, uid, addr = new_customer(client)
    sid = f"sheet-{uuid.uuid4().hex[:8]}"
    turn = lambda **b: _talkshop_turn(client, h, sid, **b)                    # noqa: E731
    turn(action={"type": "select", "product_id": "SSP002"})
    turn(text="9")
    assert _of(turn(text="orange"), "cart_updated")
    ev = turn(action={"type": "checkout"})
    need = _of(ev, "checkout_details_needed")
    if need and need[0]["needs"].get("address"):
        ev = turn(action={"type": "details_added", "address_id": addr})
    co = _of(turn(action={"type": "details_added", "pay_with": "provider"}), "checkout_ready")[0]["checkout"]
    ev = turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"], "consent": "button"})
    hand = _of(ev, "provider_handoff")[0]
    return h, uid, sid, turn, co, hand, ev


def test_talkshop_full_flow_never_sees_card_data_or_secrets(client, quiet_llm):
    h, uid, sid, turn, co, hand, ev0 = _talkshop_to_handoff(client)
    psid = hand["payment_session_id"]
    page = card(client, psid, "visa", save=True)
    challenge = verification_challenge(page)
    answer(client, psid, page, "ok")
    token = provider(client, psid)["payment_token"]
    ev = turn(action={"type": "provider_complete", "checkout_id": co["checkout_id"], "payment_session_id": psid,
                      "label": "Payment verified with the payment provider"})
    says = " ".join(e.get("text", "") for e in ev if e["type"] == "message")
    assert f"✓ Payment verified · Visa •••• 4242 · ${co['total']:.2f} authorized. Creating your order…" in says
    order = _of(ev, "order_confirmed")[0]["order"]
    assert order["payment"]["display"] == "Visa •••• 4242"
    transcript = json.dumps(client.get(f"/api/talkshop/sessions/{sid}", headers=h).json())
    with Session(get_engine()) as db:
        audit = json.dumps([e.metadata_json for e in db.query(AuditEvent).filter_by(user_id=uid)])
    for blob in (json.dumps(ev0), json.dumps(ev), transcript, audit):
        for secret in (TEST_CARDS["visa"].replace(" ", ""), TEST_CARDS["visa"], challenge, token):
            assert secret not in blob
        assert not re.search(r'"(cvc|cvv|card_number|pan)"\s*:', blob)


def test_talkshop_closing_the_sheet_returns_to_review(client, quiet_llm):
    h, uid, sid, turn, co, hand, _ = _talkshop_to_handoff(client)
    psid = hand["payment_session_id"]
    card(client, psid)
    ev = turn(action={"type": "provider_abandon", "checkout_id": co["checkout_id"], "payment_session_id": psid,
                      "label": "Closed secure payment"})
    assert _of(ev, "checkout_ready") and _of(ev, "payment_status")[0]["state"] == "cancelled"
    assert any("Nothing was charged" in e.get("text", "") for e in ev if e["type"] == "message")
    assert provider(client, psid)["status"] == "CANCELLED" and authorizations(co["checkout_id"]) == 0
    ev = turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"], "consent": "button"})   # approve again
    assert _of(ev, "provider_handoff")[0]["payment_session_id"] != psid


def test_talkshop_cancelled_verification_and_decline_offer_the_review_again(client, quiet_llm):
    h, uid, sid, turn, co, hand, _ = _talkshop_to_handoff(client)
    psid = hand["payment_session_id"]
    answer(client, psid, card(client, psid), "cancel")
    ev = turn(action={"type": "provider_complete", "checkout_id": co["checkout_id"], "payment_session_id": psid})
    assert _of(ev, "checkout_ready") and not _of(ev, "order_confirmed")
    assert any("cancelled the payment verification" in e.get("text", "") for e in ev if e["type"] == "message")
    psid2 = _of(turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"], "consent": "button"}),
                "provider_handoff")[0]["payment_session_id"]
    provider_pay(client, psid2, "decline")
    ev = turn(action={"type": "provider_complete", "checkout_id": co["checkout_id"], "payment_session_id": psid2})
    assert _of(ev, "payment_status")[-1]["state"] == "declined" and _of(ev, "checkout_ready")   # pick another way
    assert authorizations(co["checkout_id"]) == 1 and orders(co["checkout_id"]) == 0          # declined, not charged


def test_saved_mastercard_and_visa_agent_paths_are_unchanged(client):
    """Regression: a saved Mastercard still pays on its token (no sheet, no Visa/VIC path)."""
    h, _, co_id, sid = handoff(client)
    provider_pay(client, sid, "mastercard", save=True)
    assert complete(client, h, co_id, sid).json()["status"] == "authorized"
    line = client.post("/api/cart/lines", json={"sku": "SSP004-CORAL-9"}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    assert co["payment_path"]["path"] not in ("vic", "vic_setup_required", "provider_handoff")
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", headers=h, json=scoped_consent(client, h, co["checkout_id"]))
    assert r.json()["status"] == "authorized" and r.json()["order"]["payment"]["display"] == "Mastercard •••• 4444"
