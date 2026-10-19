"""
CH-3: every offer is a Chase credit-card benefit, decided by the
OfferEligibilityService (backend/shop/entitlements.py) — customer, the SELECTED
payment card and the order must all qualify. Chase cardholders also earn 2×
ShopSphere points when they pay with their Chase card. All rules are mocked.

Personas: F Dana (Citi MC 5678), H Chris (Chase Visa 1234 + Citi MC 5678; not
enrolled in OFF_STYLE3X), B Marcus (Chase MC 5510), C Priya (Chase checking only).
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import MemberOffer, ProductVariant
from backend.main import app
from backend.shop import entitlements
from backend.talkshop import brain
from tests.conftest import add_card, register_member
from tests.conftest import scoped_consent

RUNNING = "SSP002-ORANGE-9"      # $139 running shoe: OFF_RUN15 + OFF_CHASE2X
HOME = {"full_name": "Test Member", "line1": "1 Main St", "city": "Austin", "state": "tx", "postal_code": "78701"}


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


def checkout(client, h, sku=RUNNING):
    r = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/checkouts", json={"line_ids": [r.json()["added_line_id"]]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def patch(client, h, co, **changes):
    r = client.patch(f"/api/checkouts/{co['checkout_id']}", json=changes, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def cancel(client, h, co):
    """Cancel the checkout and take its lines out of the cart (personas are shared across tests)."""
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
    for ln in co["lines"]:
        client.delete(f"/api/cart/lines/{ln['line_id']}", headers=h)


def bonus_ids(co):
    return {b["offer_id"] for b in co["bonus_points"]}


def chase_card_member(network="Mastercard"):
    """A fresh Chase credit-card holder paying with that Chase card (not enrolled in anything)."""
    h, uid = register_member(network=network, with_payment_method=True)
    return h, uid


# ── Scenario 1: no offers for non-Chase cardholders ──────────────────────────

def test_citi_cardholder_gets_no_offers_and_one_x_points(client):
    h = login(client, "dana@shopsphere.demo")
    assert client.get("/api/offers", headers=h).json() == []
    assert client.get("/api/products/SSP002", headers=h).json()["member"]["offers"] == []
    co = checkout(client, h)
    try:
        assert co["offer"] is None and co["bonus_points"] == [] and co["cashback"] == []
        assert co["earn_points"] == int(co["total"])                       # 1 point per $1
    finally:
        cancel(client, h, co)


def test_enrolling_is_refused_for_a_non_chase_customer(client):
    h = login(client, "dana@shopsphere.demo")
    r = client.post("/api/offers/OFF_STYLE3X/enroll", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_CHASE_CARDHOLDER"
    assert client.post("/api/offers/NOPE/enroll", headers=h).status_code == 404


# ── Scenario 2: Chase benefits follow the card the order is paid with ────────

def test_chase_benefits_apply_only_when_paying_with_the_chase_card(client):
    h = login(client, "chris@shopsphere.demo")
    co = checkout(client, h)                                     # default card: Chase Visa 1234
    try:
        assert co["payment_method"]["issuer"] == "CHASE"
        assert co["offer"]["offer_id"] == "OFF_RUN15" and co["offer_discount"] == round(co["subtotal"] * 0.15, 2)
        assert bonus_ids(co) == {"OFF_CHASE2X"}
        boost = next(b["points"] for b in co["bonus_points"])
        assert boost == int(co["subtotal"]) and co["earn_points"] == int(co["total"]) + boost     # 2× points
        citi = next(c for c in co["saved_payment_methods"] if c["issuer"] == "CITI")
        on_citi = patch(client, h, co, payment_method_id=citi["payment_method_id"])
        assert on_citi["offer"] is None and on_citi["offer_discount"] == 0 and on_citi["bonus_points"] == []
        assert on_citi["total"] > co["total"] and on_citi["earn_points"] == int(on_citi["total"])
        on_wallet = patch(client, h, co, pay_with="wallet")
        assert on_wallet["offer"] is None and on_wallet["bonus_points"] == []
        back = patch(client, h, co, payment_method_id=co["payment_method"]["payment_method_id"])
        assert back["offer"]["offer_id"] == "OFF_RUN15" and back["total"] == co["total"]
    finally:
        cancel(client, h, co)


def test_paid_order_records_the_chase_offer_and_double_points(client):
    h, _ = chase_card_member("Mastercard")                      # Chase MC: standard path, no passkey
    client.post("/api/me/addresses", json=HOME, headers=h)
    co = checkout(client, h)
    assert co["ready"] and co["offer"]["offer_id"] == "OFF_RUN15" and bonus_ids(co) == {"OFF_CHASE2X"}
    res = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co['checkout_id']), headers=h).json()
    assert res["status"] == "authorized", res
    order = res["order"]
    assert order["offer"]["offer_id"] == "OFF_RUN15"
    assert order["points"]["bonus"] == 139 and order["points"]["earned"] == int(co["total"]) + 139


def test_a_card_of_unknown_issuer_is_not_a_chase_card(client):
    """A Chase cardholder paying with a provider-saved card of unknown issuer gets no Chase offer."""
    h, uid = register_member()                                  # Chase Visa account, no saved Chase card
    card = add_card(client, h, {"number": "5555 5555 5555 4444"}).json()
    assert card["issuer"] == "UNKNOWN"
    co = checkout(client, h)
    try:
        assert co["offer"] is None and co["bonus_points"] == []
        with Session(get_engine()) as db:
            pay = entitlements.Payment(kind="card", payment_method_id=card["payment_method_id"], path="standard")
            reasons = {d.offer.offer_id: d.reason for d in entitlements.evaluate(db, uid, [], pay)}
        assert reasons["OFF_RUN15"] == "PAYMENT_CARD_NOT_ELIGIBLE"
    finally:
        cancel(client, h, co)


# ── Customer-level rules: Chase holder, enrollment, network, dates ───────────

def test_reasons_for_each_kind_of_customer():
    with Session(get_engine()) as db:
        priya = {d.offer.offer_id: d.reason for d in entitlements.evaluate(db, "USR003")}
        marcus = {d.offer.offer_id: d.reason for d in entitlements.evaluate(db, "USR002")}
        chris = {d.offer.offer_id: d.reason for d in entitlements.evaluate(db, "USR007")}
    assert set(priya.values()) == {"NOT_CHASE_CARDHOLDER"}                     # checking isn't a credit card
    assert marcus["OFF_AGENTPAY10"] == "NETWORK_PATH_REQUIRED" and marcus["OFF_RUN15"] is None   # Chase MC
    assert chris["OFF_STYLE3X"] == "NOT_ENROLLED" and chris["OFF_CHASE2X"] is None


def test_offers_page_shows_state_and_enrollment_unlocks_the_offer(client):
    h, _ = chase_card_member("Mastercard")
    offers = {o["offer_id"]: o for o in client.get("/api/offers", headers=h).json()}
    style = offers["OFF_STYLE3X"]
    assert style["eligible"] is False and style["reason"] == "NOT_ENROLLED" and style["requires_enrollment"]
    assert offers["OFF_RUN15"]["pay_with_chase_card"] and offers["OFF_RUN15"]["exclusive_to"] == "CHASE"
    co = checkout(client, h, "SSP017-RED-M")                          # clothing
    try:
        assert bonus_ids(co) == {"OFF_CHASE2X"}                         # 2× only, not 3× clothing yet
    finally:
        cancel(client, h, co)
    after = {o["offer_id"]: o for o in client.post("/api/offers/OFF_STYLE3X/enroll", headers=h).json()}
    assert after["OFF_STYLE3X"]["eligible"] and after["OFF_STYLE3X"]["enrolled"]
    co = checkout(client, h, "SSP017-RED-M")
    try:
        assert bonus_ids(co) == {"OFF_CHASE2X", "OFF_STYLE3X"}
    finally:
        cancel(client, h, co)


def test_an_expired_or_future_offer_does_not_apply(client):
    h = login(client, "chris@shopsphere.demo")
    with Session(get_engine()) as db:
        db.query(MemberOffer).filter_by(offer_id="OFF_RUN15").update({"ends_at": datetime.utcnow() - timedelta(days=1)})
        db.commit()
    try:
        co = checkout(client, h)
        assert co["offer"] is None
        cancel(client, h, co)
        assert "OFF_RUN15" not in {o["offer_id"] for o in client.get("/api/offers", headers=h).json()}
        with Session(get_engine()) as db:
            db.query(MemberOffer).filter_by(offer_id="OFF_RUN15").update(
                {"ends_at": None, "starts_at": datetime.utcnow() + timedelta(days=1)})
            db.commit()
            assert next(d.reason for d in entitlements.evaluate(db, "USR007")
                        if d.offer.offer_id == "OFF_RUN15") == "OFFER_NOT_STARTED"
    finally:
        with Session(get_engine()) as db:
            db.query(MemberOffer).filter_by(offer_id="OFF_RUN15").update({"ends_at": None, "starts_at": None})
            db.commit()
    co = checkout(client, h)
    assert co["offer"]["offer_id"] == "OFF_RUN15"
    cancel(client, h, co)


def test_every_offer_is_chase_exclusive_and_card_bound():
    with Session(get_engine()) as db:
        rows = db.query(MemberOffer).filter_by(active=True).all()
    assert rows and all(o.requires_issuer == "CHASE" and o.requires_payment_with_issuer for o in rows)
    assert {o.offer_id for o in rows} >= {"OFF_RUN15", "OFF_AUDIO20", "OFF_STYLE3X", "OFF_AGENTPAY10", "OFF_CHASE2X"}


def test_product_badges_say_chase_card(client):
    badges = client.get("/api/products/SSP002", headers=login(client, "chris@shopsphere.demo")).json()["member"]["offers"]
    assert {b["offer_id"] for b in badges} == {"OFF_RUN15", "OFF_CHASE2X"}
    assert all(b["pay_with_chase_card"] for b in badges)


def test_rewards_explain_the_earn_rates(client):
    r = client.get("/api/rewards", headers=login(client, "dana@shopsphere.demo")).json()
    assert "2 per $1" in r["earn_rate"] and "every customer" in r["earn_rate"]


# ── Talkshop: points answered from records, 1× vs 2× ────────────────────────

@pytest.fixture
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}
    monkeypatch.setattr(brain, "decide", decide)


def ask(client, h, text):
    r = client.post("/api/talkshop/turn", json={"session_id": f"p-{uuid.uuid4().hex[:8]}", "text": text}, headers=h)
    events = [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]
    return next(e["text"] for e in events if e["type"] == "message")


def test_talkshop_points_rate_depends_on_chase(client, quiet_llm):
    chase = ask(client, login(client, "chris@shopsphere.demo"), "how many points do I have?")
    assert "2 points per $1 when you pay with your Chase credit card" in chase
    citi = ask(client, login(client, "dana@shopsphere.demo"), "how many points do I have?")
    assert "You earn 1 point per $1" in citi and "Chase credit-card holders earn 2" in citi
