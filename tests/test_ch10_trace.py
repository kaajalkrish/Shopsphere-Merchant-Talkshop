"""
CH-10: the presenter trace tells the business story honestly — customer access,
cardholder and Chase benefits, payment route and who owns the payment page,
benefit eligibility, scoped consent, authorization and order — with every
protocol label qualified (ACP-style, AP2-style, UCP-inspired, VIC-inspired …)
and no secrets (tokens, card numbers, emails) in it.
"""
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import ProductVariant
from backend.main import app
from backend.talkshop import brain

PROTOCOL_NAMES = re.compile(r"\b(ACP|AP2|UCP|VIC|MCP|A2A)\b")


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


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def turn(client, h, sid, *, text=None, action=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action}, headers=h)
    return [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def buy(client, h):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    events = []
    for kw in ({"text": "I need running shoes under $150 for everyday running."},
               {"action": {"type": "select", "product_id": "SSP002"}}, {"text": "9"}, {"text": "orange"},
               {"action": {"type": "checkout"}}):
        events += turn(client, h, sid, **kw)
    co = next(e for e in events if e["type"] == "checkout_ready")["checkout"]
    events += turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    return [e for e in events if e["type"] == "guardrail"], events


def assert_honest(guards):
    for g in guards:
        for name in PROTOCOL_NAMES.findall(g.get("protocol") or ""):
            assert re.search(rf"{name}-(style|inspired)", g["protocol"]), g["protocol"]   # never a bare compliance claim
    blob = json.dumps(guards)
    assert not re.search(r"tok_[A-Za-z0-9_-]{8}|pm_mock_[A-Za-z0-9_-]{4}|vat_[A-Za-z0-9_-]{12}", blob)
    assert not re.search(r"\b\d{13,19}\b", blob) and "@" not in blob


def test_scenario_1_trace_tells_the_citi_cardholder_story(client):
    guards, events = buy(client, login(client, "dana@shopsphere.demo"))
    by = {g["check"]: g for g in guards}
    assert by["Customer access"]["result"] == "pass" and "card issuer is never an access rule" in by["Customer access"]["detail"]
    assert "Citi Mastercard •••• 5678" in by["Cardholder & benefits"]["detail"]
    assert "no Chase benefits on this order" in by["Cardholder & benefits"]["detail"]
    assert "Citi Mastercard •••• 5678 → simulated Mastercard network" in by["Payment route"]["detail"]
    assert "No Chase benefit on this order" in by["Benefit eligibility"]["detail"]
    assert "checkout version" in by["Consent scope"]["detail"]
    assert {"Simulated Mastercard network", "Simulated Citi issuer", "Order confirmed"} <= set(by)
    assert any(e["type"] == "order_confirmed" for e in events)
    assert_honest(guards)


def test_scenario_2_trace_shows_chase_benefits_on_the_visa_path(client):
    guards, events = buy(client, login(client, "chris@shopsphere.demo"))
    by = {g["check"]: g for g in guards}
    assert "Chase Visa •••• 1234" in by["Cardholder & benefits"]["detail"]
    assert "Chase benefits: 15% off running shoes" in by["Cardholder & benefits"]["detail"]
    assert any(e["type"] == "approval_required" for e in events)          # Visa path: the passkey comes next
    assert_honest(guards)


def test_info_view_text_is_qualified():
    src = open("frontend/src/components/talkshop/TalkshopPanel.tsx", encoding="utf-8").read()
    assert "Simulated Secure Payment Provider" in src and "not a PCI boundary" in src
    assert "Chase status is an entitlement, not the gate" in src
