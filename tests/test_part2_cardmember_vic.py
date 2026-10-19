"""
Part 2 (Phases 13–16): cardmember marketplace + VIC-inspired payments.
Covers the brief's mandatory tests: identity/eligibility, shopping, every
VIC-inspired payment control, security and duplicate protection.
Everything Visa/Chase here is a local simulation.
"""
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import (AgentToken, CardAccount, CommerceSignal, GatewayAuthorization, LoyaltyPoints,
                               LoyaltyTransaction, Order, PaymentCredential, PaymentInstruction, PaymentMethod,
                               ProductVariant)
from backend.main import app
from backend.vic.common import now
from tests.conftest import register_member, scoped_consent

HOME = {"full_name": "Test Member", "line1": "1 Main St", "city": "Austin", "state": "tx", "postal_code": "78701"}
SKU = "SSP002-ORANGE-9"          # a running shoe: the 15% member offer applies


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


def db():
    return Session(get_engine())


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def member(client, network="Visa", *, enrolled=True, behaviour="approve"):
    """A fresh eligible cardmember with a Chase card on file and an address."""
    h, uid = register_member(network=network, with_payment_method=True, behaviour=behaviour)
    assert client.post("/api/me/addresses", json=HOME, headers=h).status_code == 200
    if network == "Visa" and enrolled:
        from backend.vic.enrollment import seed_active
        with db() as s:
            acct = s.query(CardAccount).filter_by(user_id=uid).one()
            seed_active(s, uid, acct.account_id)
    return h, uid


def checkout(client, h, sku=SKU, qty=1):
    r = client.post("/api/cart/lines", json={"sku": sku, "quantity": qty}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/checkouts", json={"line_ids": [r.json()["added_line_id"]]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, h, co_id, verified=True):
    r = client.post("/api/vic/instructions", json={"checkout_id": co_id}, headers=h)
    assert r.status_code == 200, r.text
    ins, pk = r.json()["instruction"], r.json()["passkey"]
    a = client.post(f"/api/vic/instructions/{ins['instruction_id']}/authenticate",
                    json={"challenge_id": pk["challenge_id"], "nonce": pk["nonce"], "user_verified": verified}, headers=h)
    return ins["instruction_id"], a


def pay(client, h, co_id, instruction_id=None):
    body = scoped_consent(client, h, co_id)                      # CH-4: approve the exact invoice
    if instruction_id:
        body["instruction_id"] = instruction_id
    return client.post(f"/api/checkouts/{co_id}/confirm", json=body, headers=h)


def code(r):
    return r.json()["detail"]["code"]


# ── Identity and marketplace ────────────────────────────────────────────────

# Browsing the catalog and chatting with Talkshop are open to everyone (Oct 11);
# CH-2: these services need a signed-in ShopSphere customer (any card issuer).
# The cart is open to everyone (visitors too).
PROTECTED = [("get", "/api/offers"), ("get", "/api/rewards"),
             ("get", "/api/me/payment-methods"), ("get", "/api/vic/status"),
             ("post", "/api/checkouts")]


def test_eligible_visa_and_mastercard_members_enter(client):
    for email, network in [("kaajal@shopsphere.demo", "Visa"), ("marcus@shopsphere.demo", "Mastercard")]:
        h = login(client, email)
        m = client.get("/api/member", headers=h).json()
        assert m["eligible"] and m["member"]["primary_card"]["network"] == network
        assert client.get("/api/products", headers=h).status_code == 200
        assert client.get("/api/offers", headers=h).status_code == 200


def test_checking_only_customer_can_shop_but_gets_no_chase_benefits(client):
    """CH-2: Chase status is no longer the gate. Priya (Chase checking only) uses
    the marketplace like any customer; no offers (not a Chase credit card)."""
    h = login(client, "priya@shopsphere.demo")
    m = client.get("/api/member", headers=h).json()
    assert m["eligible"] and m["code"] is None and m["chase_benefits"] is False and m["credit_card_holder"] is False
    for method, url in PROTECTED:
        r = getattr(client, method)(url, headers=h, **({"json": {"line_ids": ["x"]}} if method == "post" else {}))
        assert r.status_code != 403, url
    assert client.get("/api/offers", headers=h).json() == []
    products = client.get("/api/products", headers=h)                 # regular prices, no offers
    assert products.status_code == 200 and all(p["member"]["offers"] == [] for p in products.json())


def test_guest_is_denied_everywhere(client):
    tok = client.post("/api/auth/visitor").json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/member", headers=h).json()["code"] == "LOGIN_REQUIRED"
    for method, url in PROTECTED:
        r = getattr(client, method)(url, headers=h, **({"json": {"line_ids": ["x"]}} if method == "post" else {}))
        assert r.status_code == 403 and code(r) == "LOGIN_REQUIRED", url
    assert client.get("/api/products", headers=h).json()[0]["member"] is None   # browse, no extras
    assert client.get("/api/cart/lines", headers=h).status_code == 200            # the cart is open
    assert client.get("/api/products").status_code == 401             # no session at all


def test_new_signup_without_a_chase_card_is_a_customer_without_offers(client):
    h, _ = register_member(account_type="checking")
    assert client.get("/api/cart/lines", headers=h).status_code == 200
    assert client.get("/api/products?limit=1", headers=h).json()[0]["member"]["offers"] == []


# ── Shopping ────────────────────────────────────────────────────────────────

def test_catalog_is_merchant_restricted_with_member_offers(client):
    h, _ = member(client)
    products = client.get("/api/products?q=running", headers=h).json()
    assert products and all(p["merchant_id"] == "SHOPSPHERE" for p in products)
    running = [p for p in products if p["subcategory"] == "running"][0]
    assert any(o["offer_id"] == "OFF_RUN15" for o in running["member"]["offers"])
    assert running["member"]["points"] >= int(running["price"])


def test_out_of_stock_variant_is_rejected(client):
    h, _ = member(client)
    r = client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-11"}, headers=h)
    assert r.status_code == 409


def test_offer_and_totals_are_backend_authoritative(client):
    h, _ = member(client)
    co = checkout(client, h)
    assert co["offer"]["offer_id"] == "OFF_RUN15"
    assert co["offer_discount"] == round(co["subtotal"] * 0.15, 2)
    expected = round(co["subtotal"] - co["offer_discount"] + co["tax"] + co["shipping"], 2)
    assert co["total"] == expected
    # A client can't send amounts or offers: unknown fields are ignored, totals unchanged.
    r = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"total": 1.0, "offer_discount": 999,
                                                                   "offer_id": "FAKE"}, headers=h)
    assert r.status_code == 200 and r.json()["total"] == co["total"] and r.json()["offer"]["offer_id"] == "OFF_RUN15"


def test_once_per_member_offer_is_not_redeemed_twice(client):
    h, uid = member(client)
    co = checkout(client, h, "SSP047-BLACK")         # headphones ≥ $150 → $20 off, once per member
    if not co["offer"] or co["offer"]["offer_id"] != "OFF_AUDIO20":
        pytest.skip("catalog headphones SKU changed")
    iid, a = approve(client, h, co["checkout_id"])
    assert pay(client, h, co["checkout_id"], iid).json()["status"] == "authorized"
    again = checkout(client, h, "SSP047-BLACK")
    assert not again["offer"] or again["offer"]["offer_id"] != "OFF_AUDIO20"


# ── VIC-inspired payment ────────────────────────────────────────────────────

def test_enrollment_succeeds_for_seeded_visa_persona(client):
    h = login(client, "elena@shopsphere.demo")
    st = client.get("/api/vic/status", headers=h).json()["cards"][0]
    assert st["status"] in ("NOT_ENROLLED", "ACTIVE")
    if st["status"] == "ACTIVE":
        client.post("/api/vic/enrollment/revoke", json={"card_account_id": "ACCT_E_VISA"}, headers=h)
    r = client.post("/api/vic/enrollment/start", json={"card_account_id": "ACCT_E_VISA"}, headers=h).json()
    assert r["status"] == "PENDING_AUTHENTICATION" and "Simulated" in r["passkey"]["label"]
    pk = r["passkey"]
    done = client.post("/api/vic/enrollment/complete", json={"card_account_id": "ACCT_E_VISA", **{
        "challenge_id": pk["challenge_id"], "nonce": pk["nonce"], "user_verified": True}}, headers=h).json()
    assert done["cards"][0]["status"] == "ACTIVE"
    with db() as s:
        tok = s.query(AgentToken).filter_by(user_id="USR004", status="ACTIVE").one()
        assert "4002" not in tok.agent_token_ref and len(tok.agent_token_ref) > 30


def test_missing_enrollment_blocks_vic_payment(client):
    h, _ = member(client, enrolled=False)
    co = checkout(client, h)
    assert co["payment_path"]["path"] == "vic_setup_required"
    r = client.post("/api/vic/instructions", json={"checkout_id": co["checkout_id"]}, headers=h)
    assert r.status_code == 409 and code(r) == "AGENT_PAY_NOT_ENROLLED"
    r = pay(client, h, co["checkout_id"])
    assert r.status_code == 409 and code(r) == "AGENT_PAY_NOT_ENROLLED"


def test_failed_authentication_blocks_payment(client):
    h, _ = member(client)
    co = checkout(client, h)
    iid, a = approve(client, h, co["checkout_id"], verified=False)
    assert a.status_code == 401 and code(a) == "AUTHENTICATION_FAILED"
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "blocked" and r["reason"] == "INSTRUCTION_NOT_AUTHENTICATED"
    assert not r.get("order")


def test_vic_payment_without_instruction_is_refused(client):
    h, _ = member(client)
    co = checkout(client, h)
    r = pay(client, h, co["checkout_id"])
    assert r.status_code == 409 and code(r) == "INSTRUCTION_REQUIRED"
    assert client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json={"consent": False},
                       headers=h).status_code == 400


def _tamper(iid, **fields):
    with db() as s:
        s.query(PaymentInstruction).filter_by(instruction_id=iid).update(fields)
        s.commit()


def test_merchant_mismatch_blocks_payment(client):
    h, _ = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    _tamper(iid, merchant_id="OTHER_STORE")
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "blocked" and r["reason"] == "MERCHANT_MISMATCH"


def test_amount_mismatch_blocks_payment(client):
    h, _ = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    _tamper(iid, amount=co["total"] - 5)
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "blocked" and r["reason"] == "AMOUNT_MISMATCH"


def test_checkout_change_invalidates_instruction(client):
    h, _ = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"delivery_method": "express"}, headers=h)
    with db() as s:
        instr = s.query(PaymentInstruction).filter_by(instruction_id=iid).one()
        assert instr.status == "CANCELLED" and instr.status_reason == "CHECKOUT_CHANGED"
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "blocked" and not r.get("order")


def test_expired_instruction_blocks_payment(client):
    h, _ = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    _tamper(iid, expires_at=now() - timedelta(seconds=1))
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "blocked" and r["reason"] == "INSTRUCTION_EXPIRED"


def test_revoked_token_blocks_payment(client):
    h, uid = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    with db() as s:
        acct = s.query(CardAccount).filter_by(user_id=uid).one().account_id
    assert client.post("/api/vic/enrollment/revoke", json={"card_account_id": acct}, headers=h).status_code == 200
    r = pay(client, h, co["checkout_id"])
    assert code(r) == "AGENT_PAY_NOT_ENROLLED"       # the card is no longer on the agent-pay path
    with db() as s:
        assert s.query(PaymentInstruction).filter_by(instruction_id=iid).one().status == "CANCELLED"


def test_replayed_or_altered_credential_is_refused():
    """The simulated network accepts a purchase credential once, only for what it was issued for."""
    from backend.vic import credentials
    from backend.vic.common import VicError
    h, uid = member(TestClient(app))
    client = TestClient(app)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    with db() as s:
        instr = s.query(PaymentInstruction).filter_by(instruction_id=iid).one()
        cred = credentials.issue(s, instr)
        s.commit()
        ref = cred.credential_ref
        base = dict(merchant_id="SHOPSPHERE", amount=co["total"], currency="USD", checkout_id=co["checkout_id"],
                    user_id=uid)
        for bad, expected in [({"merchant_id": "OTHER"}, "CREDENTIAL_MERCHANT_MISMATCH"),
                              ({"amount": co["total"] + 1}, "CREDENTIAL_AMOUNT_MISMATCH"),
                              ({"user_id": "USR002"}, "CREDENTIAL_BINDING_MISMATCH")]:
            with pytest.raises(VicError) as e:
                credentials.redeem(s, ref, **{**base, **bad})
            assert e.value.code == expected
        credentials.redeem(s, ref, **base)
        s.commit()
        with pytest.raises(VicError) as e:
            credentials.redeem(s, ref, **base)
        assert e.value.code == "CREDENTIAL_REPLAYED"


def test_expired_credential_is_refused():
    from backend.vic import credentials
    from backend.vic.common import VicError
    client = TestClient(app)
    h, uid = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    with db() as s:
        instr = s.query(PaymentInstruction).filter_by(instruction_id=iid).one()
        cred = credentials.issue(s, instr)
        cred.expires_at = now() - timedelta(seconds=1)
        s.commit()
        with pytest.raises(VicError) as e:
            credentials.redeem(s, cred.credential_ref, merchant_id="SHOPSPHERE", amount=co["total"], currency="USD",
                               checkout_id=co["checkout_id"], user_id=uid)
        assert e.value.code == "CREDENTIAL_EXPIRED"


def test_successful_vic_payment_records_signal_and_lifecycle(client):
    h, uid = member(client)
    co = checkout(client, h)
    iid, a = approve(client, h, co["checkout_id"])
    assert a.json()["instruction"]["status"] == "AUTHENTICATED"
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "authorized" and r["payment_path"] == "vic"
    assert r["order"]["payment_path"] == "vic" and r["order"]["cashback"] == 10.0
    det = client.get(f"/api/vic/instructions/{iid}", headers=h).json()
    assert [e["state"] for e in det["lifecycle"]] == [
        "CARD_LINKED", "AGENT_TOKEN_ACTIVE", "CHECKOUT_CREATED", "INSTRUCTION_AUTHENTICATED", "CREDENTIAL_ISSUED",
        "PAYMENT_AUTHORIZED", "SIGNAL_RECORDED", "ORDER_CONFIRMED"]
    assert det["signals"][0]["outcome"] == "APPROVED" and det["signals"][0]["order_id"] == r["order"]["order_id"]
    assert det["instruction"]["status"] == "CONSUMED"
    checks = [t["check"] for t in r["trace"]]
    assert sum(c.startswith("DPAT check") for c in checks) == 12
    assert sum(c.startswith("VIC control") for c in checks) == 12
    for actor in ("Mock gateway / PSP", "Simulated Visa network", "Simulated Chase issuer"):
        assert actor in checks
    # Replaying the same instruction can't pay again
    assert pay(client, h, co["checkout_id"], iid).json().get("status") != "authorized"


def test_duplicate_authorization_does_not_double_charge(client):
    h, uid = member(client)
    with db() as s:
        before = s.query(CardAccount).filter_by(user_id=uid).one().available_credit
    client.post("/api/vic/demo/scenario", json={"outcome": "duplicate"}, headers=h)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "authorized"
    assert any(t["check"] == "Duplicate submission" and "same reference" in t["detail"] for t in r["trace"])
    with db() as s:
        assert s.query(GatewayAuthorization).filter(GatewayAuthorization.idempotency_key.like(f"{iid}%")).count() == 1
        after = s.query(CardAccount).filter_by(user_id=uid).one().available_credit
        assert round(before - after, 2) == r["order"]["total"]
        assert s.query(Order).filter(Order.checkout_id == co["checkout_id"], Order.status == "confirmed").count() == 1


def test_gateway_decline_creates_no_order_and_keeps_points(client):
    h, uid = member(client)
    with db() as s:
        lp = s.query(LoyaltyPoints).filter_by(user_id=uid).first()
        if not lp:
            s.add(LoyaltyPoints(user_id=uid, balance=1000, lifetime_points=1000))
        else:
            lp.balance = 1000
        s.commit()
    co = checkout(client, h)
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"points_used": 500}, headers=h)
    client.post("/api/vic/demo/scenario", json={"outcome": "decline"}, headers=h)
    iid, _ = approve(client, h, co["checkout_id"])
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "declined" and r["reason"] == "CARD_DECLINED"
    with db() as s:
        assert s.query(LoyaltyPoints).filter_by(user_id=uid).one().balance == 1000
        assert not s.query(Order).filter(Order.checkout_id == co["checkout_id"], Order.status == "confirmed").first()
        sig = s.query(CommerceSignal).filter_by(instruction_id=iid).one()
        assert sig.outcome == "DECLINED"


def test_gateway_timeout_creates_no_order(client):
    h, uid = member(client)
    client.post("/api/vic/demo/scenario", json={"outcome": "timeout"}, headers=h)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "declined" and r["reason"] == "PAYMENT_TIMEOUT"
    with db() as s:
        assert s.query(PaymentCredential).filter_by(instruction_id=iid).one().status == "REVOKED"


def test_mastercard_member_uses_standard_path(client):
    h, uid = member(client, "Mastercard")
    co = checkout(client, h)
    assert co["payment_path"]["path"] == "standard" and co["cashback"] == []
    assert client.post("/api/vic/instructions", json={"checkout_id": co["checkout_id"]}, headers=h).status_code == 400
    r = pay(client, h, co["checkout_id"]).json()
    assert r["status"] == "authorized" and r["order"]["payment_path"] == "standard"
    checks = [t["check"] for t in r["trace"]]
    assert "Simulated Mastercard network" in checks and not any(c.startswith("VIC control") for c in checks)
    with db() as s:
        assert not s.query(PaymentInstruction).filter_by(user_id=uid).first()
        assert s.query(GatewayAuthorization).filter_by(instrument_type="card_on_file").count() >= 1


def test_seeded_decline_card_is_declined_by_simulated_issuer(client):
    h, _ = member(client, "Mastercard", behaviour="decline")
    co = checkout(client, h)
    r = pay(client, h, co["checkout_id"]).json()
    assert r["status"] == "declined" and r["reason"] == "CARD_DECLINED" and not r.get("order")


# ── Security ────────────────────────────────────────────────────────────────

PAN = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def test_no_pan_cvv_or_secrets_in_responses_or_trace(client):
    h, uid = member(client)
    co = checkout(client, h)
    iid, _ = approve(client, h, co["checkout_id"])
    r = pay(client, h, co["checkout_id"], iid).json()
    blob = json.dumps(r) + json.dumps(client.get("/api/member", headers=h).json()) + \
        json.dumps(client.get(f"/api/vic/instructions/{iid}", headers=h).json())
    assert not PAN.search(blob.replace(".", ""))
    with db() as s:
        secrets_ = [c.credential_ref for c in s.query(PaymentCredential).filter_by(instruction_id=iid)]
        secrets_ += [t.agent_token_ref for t in s.query(AgentToken).filter_by(user_id=uid)]
        secrets_ += [p.token_ref for p in s.query(PaymentMethod).filter_by(user_id=uid)]
        secrets_ += [c.cryptogram_hash for c in s.query(PaymentCredential).filter_by(instruction_id=iid)]
    assert secrets_ and all(sec not in blob for sec in secrets_)
    assert "cvc" not in blob.lower() and "cvv" not in blob.lower()


def test_tokens_are_random_not_derived_from_card_digits():
    with db() as s:
        for pm in s.query(PaymentMethod):
            assert not pm.token_ref.startswith("tok_demo") and len(pm.token_ref) >= 24
        refs = [t.agent_token_ref for t in s.query(AgentToken)]
        assert len(refs) == len(set(refs))
        for t in s.query(AgentToken):
            card = s.query(PaymentMethod).filter_by(payment_method_id=t.payment_method_id).one()
            assert card.last4 not in t.agent_token_ref


def test_no_double_redemption_and_no_duplicate_orders(client):
    from backend.shop import orders as order_service
    from backend.db.schema import Checkout
    h, uid = member(client)
    with db() as s:
        lp = s.query(LoyaltyPoints).filter_by(user_id=uid).first() or LoyaltyPoints(user_id=uid, balance=0, lifetime_points=0)
        lp.balance = 2000
        s.add(lp)
        s.commit()
    co = checkout(client, h)
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"points_used": 1000}, headers=h)
    iid, _ = approve(client, h, co["checkout_id"])
    r = pay(client, h, co["checkout_id"], iid).json()
    assert r["status"] == "authorized" and r["order"]["points"]["used"] == 1000
    with db() as s:
        order = s.query(Order).filter_by(checkout_id=co["checkout_id"], status="confirmed").one()
        chk = s.query(Checkout).filter_by(checkout_id=co["checkout_id"]).one()
        balance = s.query(LoyaltyPoints).filter_by(user_id=uid).one().balance
        order_service.finalize(s, chk, order.order_id, "vic")              # a retry: no-op
        s.commit()
        assert s.query(LoyaltyPoints).filter_by(user_id=uid).one().balance == balance
        assert s.query(LoyaltyTransaction).filter_by(transaction_id=f"RDM-{order.order_id}"[:50]).count() == 1
    r2 = pay(client, h, co["checkout_id"], iid)
    assert r2.status_code == 409 and code(r2) == "CHECKOUT_CLOSED"
