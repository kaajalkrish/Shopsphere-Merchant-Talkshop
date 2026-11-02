"""
The cardholder's simulated passkey approval is the same for EVERY saved card, not only
the Visa agent-payment (VIC-inspired) path: Mastercard, Amex, Discover and a Visa without
agent payments confirm the exact order with a passkey before the saved card token is used.
It is network-neutral (no VIC agent token, no instruction), single use, bound to the
checkout version and total, and the challenge never appears in Talkshop events.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import PaymentInstruction, PaymentMethod, ProductVariant
from backend.main import app
from backend.talkshop import brain
from tests.conftest import add_card, card_approval, register_member, scoped_consent

HOME = {"full_name": "Pass Key", "line1": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"}
CARDS = {"Visa": "4242 4242 4242 4242", "Mastercard": "5555 5555 5555 4444", "Amex": "3782 822463 10005",
         "Discover": "6011 1111 1111 1117"}


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


def shopper(client, network="Mastercard"):
    h, _ = register_member()
    assert client.post("/api/me/addresses", json=HOME, headers=h).status_code == 200
    card = add_card(client, h, {"number": CARDS[network], "make_default": True}).json()
    return h, card["payment_method_id"]


def checkout(client, h, pm_id, sku="SSP002-ORANGE-9"):
    line = client.post("/api/cart/lines", json={"sku": sku}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": pm_id}, headers=h).json()
    assert co["ready"] and co["payment_path"]["route"] == "saved_card", co.get("issues")
    return co


def confirm(client, h, co_id, **body):
    return client.post(f"/api/checkouts/{co_id}/confirm", headers=h, json=body)


def code(r):
    return r.json()["detail"]["code"]


# ── every saved card gets the same step ──────────────────────────────────────

@pytest.mark.parametrize("network", list(CARDS))
def test_every_network_needs_the_cardholder_passkey_then_pays(client, network):
    h, pm = shopper(client, network)
    co = checkout(client, h, pm)
    body = scoped_consent(client, h, co["checkout_id"], approval_id=None)
    r = confirm(client, h, co["checkout_id"], **body)
    assert r.status_code == 409 and code(r) == "APPROVAL_REQUIRED"            # a tap alone doesn't pay
    start = client.post(f"/api/checkouts/{co['checkout_id']}/approval", headers=h).json()
    assert start["approval"]["amount"] == co["total"] and start["approval"]["network"] == network.upper()
    assert "not a real passkey" in start["passkey"]["label"] and "Visa" not in start["passkey"]["label"]
    r = confirm(client, h, co["checkout_id"], **scoped_consent(client, h, co["checkout_id"]))
    assert r.status_code == 200 and r.json()["status"] == "authorized", r.text
    trace = {t["check"]: t for t in r.json()["trace"]}
    assert trace["Cardholder approval"]["result"] == "pass" and "Payment instruction" not in trace
    with Session(get_engine()) as db:                                        # no VIC objects for this card
        assert not db.query(PaymentInstruction).filter_by(checkout_id=co["checkout_id"]).count()


def test_a_failed_passkey_approves_nothing(client):
    h, pm = shopper(client)
    co = checkout(client, h, pm)
    aid = card_approval(client, h, co["checkout_id"], verified=False)
    r = confirm(client, h, co["checkout_id"], **scoped_consent(client, h, co["checkout_id"], approval_id=aid))
    assert r.status_code == 409 and code(r) == "APPROVAL_NOT_VERIFIED"


def test_the_approval_covers_this_exact_order_only(client):
    h, pm = shopper(client)
    co = checkout(client, h, pm)
    aid = card_approval(client, h, co["checkout_id"])
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"delivery_method": "express"}, headers=h)
    r = confirm(client, h, co["checkout_id"], **scoped_consent(client, h, co["checkout_id"], approval_id=aid))
    assert r.status_code == 409 and code(r) == "APPROVAL_STALE"               # total changed → approve again
    other = checkout(client, h, pm, sku="SSP001-WHITE-8")
    r = confirm(client, h, other["checkout_id"], **scoped_consent(client, h, other["checkout_id"], approval_id=aid))
    assert r.status_code == 404 and code(r) == "UNKNOWN_APPROVAL"             # not another order's approval


def test_the_approval_is_used_once(client):
    h, pm = shopper(client)
    co = checkout(client, h, pm)
    with Session(get_engine()) as db:                                        # this attempt is declined
        db.query(PaymentMethod).filter_by(payment_method_id=pm).update({"behaviour": "decline"})
        db.commit()
    aid = card_approval(client, h, co["checkout_id"])
    r = confirm(client, h, co["checkout_id"], **scoped_consent(client, h, co["checkout_id"], approval_id=aid))
    assert r.json()["status"] == "declined"
    r = confirm(client, h, co["checkout_id"], **scoped_consent(client, h, co["checkout_id"], approval_id=aid))
    assert r.status_code == 409 and code(r) == "APPROVAL_USED"                # a retry needs a new passkey tap


def test_other_ways_to_pay_keep_their_own_approval(client):
    h, _ = register_member(network="Visa", with_payment_method=True)          # Visa agent-pay capable card
    client.post("/api/me/addresses", json=HOME, headers=h)
    line = client.post("/api/cart/lines", json={"sku": "SSP002-ORANGE-9"}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    assert co["payment_path"]["path"] in ("vic", "vic_setup_required")
    r = client.post(f"/api/checkouts/{co['checkout_id']}/approval", headers=h)
    assert r.status_code == 409 and code(r) == "APPROVAL_NOT_NEEDED"          # VIC keeps its instruction
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/approval", headers=h)
    assert r.status_code == 409 and code(r) == "APPROVAL_NOT_NEEDED"          # new card: provider's own check


# ── Talkshop ─────────────────────────────────────────────────────────────────

def test_talkshop_asks_every_cardholder_for_the_passkey_and_never_sees_it(client):
    h, pm = shopper(client, "Amex")
    sid = f"pk-{uuid.uuid4().hex[:8]}"

    def turn(**body):
        r = client.post("/api/talkshop/turn", json={"session_id": sid, **body}, headers=h)
        assert r.status_code == 200, r.text
        return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]

    turn(action={"type": "select", "product_id": "SSP002"})
    turn(text="9")
    turn(text="orange")
    ev = turn(action={"type": "checkout"})
    co = next(e for e in ev if e["type"] == "checkout_ready")["checkout"]
    assert "confirm with a simulated passkey" in " ".join(e["text"] for e in ev if e["type"] == "message")
    ev = turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    req = next(e for e in ev if e["type"] == "approval_required")
    assert req["kind"] == "card" and "American Express" in (req["payment"] or "") and not any(e["type"] == "order_confirmed" for e in ev)
    aid = card_approval(client, h, co["checkout_id"])                        # the panel, directly with ShopSphere
    ev2 = turn(action={"type": "go_ahead", "checkout_id": co["checkout_id"], "approval_id": aid})
    assert next(e for e in ev2 if e["type"] == "order_confirmed")["order"]["payment_path"] == "standard"
    blob = json.dumps(ev + ev2)
    assert "nonce" not in blob                                               # the challenge stays in the browser
