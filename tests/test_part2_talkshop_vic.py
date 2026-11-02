"""
Part 2 (Phase 18): Talkshop AI on the Visa agent-payment path.
Talkshop never handles the passkey: it asks the panel for approval
(approval_required), and pays only with the member's AUTHENTICATED
instruction id. Persona E-style members are asked to set up agent payments.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.talkshop import brain
from tests.conftest import card_passkey
from tests.test_part2_cardmember_vic import approve, member, restore_stock  # noqa: F401  (autouse fixture)

SPEC_REQUEST = "I need running shoes under $150 for everyday running."


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def quiet_llm(monkeypatch):
    async def decide(message, *, stage, allowed, context, history):
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)


def turn(client, h, sid, *, text=None, action=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action}, headers=h)
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for block in r.text.split("\n\n") for line in block.splitlines()
              if line.startswith("data: ")]
    return card_passkey(client, h, sid, events)


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def to_review(client, h):
    sid = f"v-{uuid.uuid4().hex[:8]}"
    turn(client, h, sid, text=SPEC_REQUEST)
    turn(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    turn(client, h, sid, text="9")
    turn(client, h, sid, text="orange")
    ev = turn(client, h, sid, action={"type": "checkout"})
    return sid, of(ev, "checkout_ready")[0]["checkout"]


def test_visa_go_ahead_asks_for_passkey_approval_then_pays_with_the_instruction(client):
    h, _ = member(client)
    sid, co = to_review(client, h)
    assert co["payment_path"]["path"] == "vic" and co["offer"]["offer_id"] == "OFF_RUN15"
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    req = of(ev, "approval_required")
    assert req and req[0]["amount"] == co["total"] and not of(ev, "payment_status")    # nothing charged yet
    assert "nonce" not in json.dumps(ev) and "challenge" not in json.dumps(ev)           # passkey never via Talkshop
    iid, a = approve(client, h, co["checkout_id"])                                       # the panel's passkey step
    assert a.status_code == 200
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"], "instruction_id": iid})
    order = of(ev, "order_confirmed")[0]["order"]
    assert order["payment_path"] == "vic" and order["cashback"] == 10.0
    guards = [e for e in ev if e["type"] == "guardrail"]
    assert any(g["check"] == "Commerce signal recorded" and g["layer"] == "simulated" for g in guards)
    assert any(g["check"].startswith("VIC control 12/12") for g in guards)


def test_typed_go_ahead_starts_the_approval_but_never_pays(client):
    h, _ = member(client)
    sid, co = to_review(client, h)
    ev = turn(client, h, sid, text="go ahead")
    assert of(ev, "approval_required") and not of(ev, "payment_status") and not of(ev, "order_confirmed")
    assert of(ev, "approval_required")[0]["consent_mode"] == "typed"


def test_cancelled_approval_charges_nothing(client):
    h, _ = member(client)
    sid, co = to_review(client, h)
    turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    ev = turn(client, h, sid, action={"type": "approval_cancelled"})
    assert "Nothing was approved or charged" in of(ev, "message")[0]["text"] and not of(ev, "payment_status")


def test_unenrolled_visa_member_is_asked_to_set_up_agent_payments(client):
    h, _ = member(client, enrolled=False)
    sid, co = to_review(client, h)
    assert co["payment_path"]["path"] == "vic_setup_required"
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert of(ev, "agent_pay_setup") and not of(ev, "payment_status")
    acct = of(ev, "agent_pay_setup")[0]["card_account_id"]
    st = client.post("/api/vic/enrollment/start", json={"card_account_id": acct}, headers=h).json()["passkey"]
    client.post("/api/vic/enrollment/complete", json={"card_account_id": acct, "challenge_id": st["challenge_id"],
                                                       "nonce": st["nonce"], "user_verified": True}, headers=h)
    ev = turn(client, h, sid, action={"type": "agent_pay_ready"})
    assert of(ev, "checkout_updated")[0]["checkout"]["payment_path"]["path"] == "vic"


def test_mastercard_member_pays_on_the_standard_path_with_the_cardholder_passkey(client):
    """Same passkey step as the Visa member, but no VIC: no agent token, no instruction."""
    h, _ = member(client, "Mastercard")
    sid, co = to_review(client, h)
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    req = of(ev, "approval_required")[0]
    assert req["kind"] == "card" and req["payment"] and "Mastercard" in req["payment"]
    order = of(ev, "order_confirmed")[0]["order"]
    assert order["payment_path"] == "standard" and not order["cashback"]
    guards = {g["check"]: g for g in of(ev, "guardrail")}
    assert guards["Cardholder approval"]["result"] == "pass" and "Payment instruction" not in guards


def test_progress_steps_are_customer_friendly(client):
    h, _ = member(client)
    sid, co = to_review(client, h)
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert ev[-1]["step"] == "Review"
    iid, _ = approve(client, h, co["checkout_id"])
    ev = turn(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"], "instruction_id": iid})
    assert ev[-1]["step"] == "Done" and any(e.get("step") == "Approve" for e in ev if e["type"] == "stage")
