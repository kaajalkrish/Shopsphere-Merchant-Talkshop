"""
"Can I add more items / continue shopping?" during checkout: Talkshop keeps the cart and
the order, asks what to add next (based on what is already in the cart), and never runs a
product search on the sentence itself. Nothing is charged; an open secure-payment session
is released first.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import ProductVariant, ProviderPaymentSession
from backend.main import app
from backend.talkshop import brain

MESSAGE = "I wish to add more items can I continue shopping"


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


@pytest.fixture(autouse=True)
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "search", "args": {"query": message}, "reply": ""}    # the old failure mode

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


def start(client):
    """A signed-in customer with FlexRun 5 (size 9, orange) at the review card, paying with a new card."""
    email = f"more-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "name": "More Shopper", "email": email, "password": "more-pass1"}).json()["access_token"]}
    addr = client.post("/api/me/addresses", headers=h, json={"full_name": "More", "line1": "1 A St", "city": "Austin",
                                                             "state": "TX", "postal_code": "78701"}).json()
    sid = f"more-{uuid.uuid4().hex[:8]}"

    def turn(**body):
        r = client.post("/api/talkshop/turn", json={"session_id": sid, **body}, headers=h)
        assert r.status_code == 200, r.text
        return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]

    turn(action={"type": "select", "product_id": "SSP002"})
    turn(text="9")
    turn(text="orange")
    ev = turn(action={"type": "checkout"})
    if any(e["type"] == "checkout_details_needed" and e["needs"].get("address") for e in ev):
        turn(action={"type": "details_added", "address_id": addr["address_id"]})
    co = next(e for e in turn(action={"type": "details_added", "pay_with": "provider"})
              if e["type"] == "checkout_ready")["checkout"]
    return turn, co


def of(ev, kind):
    return [e for e in ev if e["type"] == kind]


def said(ev):
    return " ".join(e["text"] for e in ev if e["type"] == "message")


def test_add_more_items_at_review_asks_what_to_add_instead_of_searching(client):
    turn, co = start(client)
    ev = turn(text=MESSAGE)
    assert not of(ev, "recommendations")                                    # no random products
    reply = said(ev)
    assert "FlexRun 5 (size 9, orange) stays in your cart and your order is saved" in reply
    assert "What would you like to add" in reply
    chips = of(ev, "suggestions")[0]
    assert "Back to my order" in chips["chips"] and chips["chip_actions"]["Back to my order"] == {"type": "back_to_order"}
    assert next(e for e in ev if e["type"] == "done")["stage"] == "IN_CART"


def test_the_shopper_can_add_another_item_and_check_out_with_both(client):
    turn, co = start(client)
    turn(text=MESSAGE)
    ev = turn(text="show me running shoes")                                  # now a real search
    assert of(ev, "recommendations")
    ev = turn(action={"type": "back_to_order", "label": "Back to my order"})   # even while results are shown
    if of(ev, "checkout_details_needed"):                                      # new card: choose it again
        ev = turn(action={"type": "details_added", "pay_with": "provider"})
    assert of(ev, "checkout_ready")[0]["checkout"]["lines"][0]["name"] == "FlexRun 5"


def test_an_open_secure_payment_is_released_first(client):
    turn, co = start(client)
    hand = of(turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"], "consent": "button"}),
              "provider_handoff")[0]
    ev = turn(text="can I add something else before paying")
    assert not of(ev, "recommendations") and "What would you like to add" in said(ev)
    with Session(get_engine()) as db:
        assert db.query(ProviderPaymentSession).filter_by(session_id=hand["payment_session_id"]).one().status == "CANCELLED"


def test_a_named_product_still_searches(client):
    turn, _ = start(client)
    ev = turn(text="add running shoes too")
    assert of(ev, "recommendations")
