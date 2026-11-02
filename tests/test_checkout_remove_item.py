"""
Taking an item out of an order at the review step. The item leaves this order only (it
stays in the ShopSphere cart); ShopSphere recalculates the total and bumps the checkout
version, so the shopper approves the new total. The last item can't be removed: the order
is cancelled instead. Works from the website, the Talkshop review card, and typed.
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
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


def customer(client):
    email = f"rm-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "name": "Remove Shopper", "email": email, "password": "remove-pass1"}).json()["access_token"]}
    addr = client.post("/api/me/addresses", headers=h, json={"full_name": "Rm", "line1": "1 A St", "city": "Austin",
                                                             "state": "TX", "postal_code": "78701"}).json()
    return h, addr["address_id"]


# ── website / API ────────────────────────────────────────────────────────────

def two_line_checkout(client):
    h, _ = customer(client)
    a = client.post("/api/cart/lines", json={"sku": "SSP002-ORANGE-9"}, headers=h).json()["added_line_id"]
    b = client.post("/api/cart/lines", json={"sku": "SSP001-WHITE-8"}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [a, b]}, headers=h).json()
    return h, co, a, b


def test_removing_an_item_recalculates_the_order_and_keeps_it_in_the_cart(client):
    h, co, a, b = two_line_checkout(client)
    r = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"remove_line_ids": [b]}, headers=h)
    assert r.status_code == 200, r.text
    after = r.json()
    assert [ln["line_id"] for ln in after["lines"]] == [a]
    assert after["subtotal"] < co["subtotal"] and after["total"] < co["total"]
    assert after["checkout_version"] > co["checkout_version"]                  # the shopper approves the new total
    cart = client.get("/api/cart/lines", headers=h).json()
    assert b in {ln["line_id"] for ln in cart["lines"]}                       # still in the cart for later


def test_an_approval_of_the_old_total_is_refused(client):
    h, co, a, b = two_line_checkout(client)
    old = scoped_consent(client, h, co["checkout_id"])
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"remove_line_ids": [b]}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=old, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"


def test_the_last_item_cannot_be_removed_and_unknown_items_are_refused(client):
    h, co, a, b = two_line_checkout(client)
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"remove_line_ids": [b]}, headers=h)
    r = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"remove_line_ids": [a]}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LAST_ITEM"
    r = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"remove_line_ids": ["CART_NOPE"]}, headers=h)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "UNKNOWN_LINE"


# ── Talkshop ─────────────────────────────────────────────────────────────────

def talkshop_review_with_two_items(client):
    h, addr = customer(client)
    sid = f"rm-{uuid.uuid4().hex[:8]}"

    def turn(**body):
        r = client.post("/api/talkshop/turn", json={"session_id": sid, **body}, headers=h)
        assert r.status_code == 200, r.text
        return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]

    for pid, size, color in (("SSP001", "8", "white"), ("SSP002", "9", "orange")):
        turn(action={"type": "select", "product_id": pid})
        turn(text=size)
        assert any(e["type"] == "cart_updated" for e in turn(text=color))
    ev = turn(action={"type": "checkout"})
    if any(e["type"] == "checkout_details_needed" and e["needs"].get("address") for e in ev):
        turn(action={"type": "details_added", "address_id": addr})
    ev = turn(action={"type": "details_added", "pay_with": "provider"})
    co = next(e for e in ev if e["type"] == "checkout_ready")["checkout"]
    assert len(co["lines"]) == 2
    return turn, co


def said(ev):
    return " ".join(e["text"] for e in ev if e["type"] == "message")


def test_the_review_card_remove_button_takes_the_item_out(client):
    turn, co = talkshop_review_with_two_items(client)
    runner = next(ln for ln in co["lines"] if ln["name"] == "Runner Pro X")
    ev = turn(action={"type": "update_checkout", "remove_line_ids": [runner["line_id"]]})
    updated = next(e for e in ev if e["type"] == "checkout_updated")["checkout"]
    assert [ln["name"] for ln in updated["lines"]] == ["FlexRun 5"]
    assert "Removed Runner Pro X from this order" in said(ev) and f"${updated['total']:.2f}" in said(ev)
    ev = turn(action={"type": "back_to_order"})                               # the next review leaves it out too
    if any(e["type"] == "checkout_details_needed" for e in ev):
        ev = turn(action={"type": "details_added", "pay_with": "provider"})
    assert [ln["name"] for ln in next(e for e in ev if e["type"] == "checkout_ready")["checkout"]["lines"]] == ["FlexRun 5"]


@pytest.mark.parametrize("text,left", [("remove the runner pro x", "FlexRun 5"), ("only the flexrun please", "FlexRun 5"),
                                       ("I don't want the flexrun", "Runner Pro X")])
def test_typed_removal_at_the_review_card(client, text, left):
    turn, co = talkshop_review_with_two_items(client)
    ev = turn(text=text)
    updated = next(e for e in ev if e["type"] == "checkout_updated")["checkout"]
    assert [ln["name"] for ln in updated["lines"]] == [left]
