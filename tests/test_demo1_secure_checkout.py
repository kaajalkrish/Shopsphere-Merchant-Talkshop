"""
Demo 1, Phase 9 — secure checkout details. When a customer has no saved
address and/or card, Talkshop shows ShopSphere's secure forms instead of the
review card. The forms post straight to ShopSphere; Talkshop receives only ids,
then resumes checkout with merchant-calculated totals. Raw card data never
reaches events, the session, or the LLM.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.payment import mock_processor
from backend.shop import orders as order_service
from backend.talkshop import brain
from backend.talkshop import state as talk_state
from tests.conftest import add_card

SPEC_REQUEST = "I need running shoes under $150 for everyday running."
CARD = "4242 4242 4242 4242"
RAW = ("4242424242424242", CARD)
VISA = {"number": CARD, "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Test"}
HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"}


@pytest.fixture
def client():
    return TestClient(app)
@pytest.fixture
def auth_headers(member_headers):
    """Part 2: the marketplace is for eligible cardmembers only."""
    return member_headers



@pytest.fixture(autouse=True)
def llm(monkeypatch):
    """Fake LLM that records everything it is shown."""
    seen = []

    async def decide(message, *, stage, allowed, context, history):
        seen.append(json.dumps({"message": message, "history": history, "context": context}))
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        seen.append(request)
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}

    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)
    return seen


def turn(client, headers, sid, *, text=None, action=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action}, headers=headers)
    assert r.status_code == 200, r.text
    return [json.loads(line[6:]) for block in r.text.split("\n\n") for line in block.splitlines()
            if line.startswith("data: ")]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def stage(events):
    return events[-1]["stage"]


def to_checkout(client, headers):
    sid = f"s-{uuid.uuid4().hex[:8]}"
    turn(client, headers, sid, text=SPEC_REQUEST)
    turn(client, headers, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, headers, sid, text="Size 8.")
    turn(client, headers, sid, text="Black.")
    return sid, turn(client, headers, sid, text="Yes.")


def session_of(client, headers, sid):
    me = client.get("/api/auth/me", headers=headers).json()
    return talk_state.peek(me["user_id"], sid)


def assert_no_raw_card(*blobs):
    text = json.dumps(blobs, default=str)
    assert not any(raw in text for raw in RAW)


# ── Missing details → secure forms, then automatic resume ───────────────────

def test_new_customer_gets_secure_forms_not_the_review(client, auth_headers):
    sid, ev = to_checkout(client, auth_headers)
    assert of(ev, "checkout_details_needed")[0]["needs"] == {"guest": False, "address": True, "payment": True}
    assert not of(ev, "checkout_ready") and stage(ev) == "CHECKOUT_DETAILS"
    co_id = of(ev, "checkout_details_needed")[0]["checkout_id"]
    ev = turn(client, auth_headers, sid, action={"type": "go_ahead", "checkout_id": co_id})
    assert not of(ev, "payment_status") and "no order waiting" in of(ev, "message")[0]["text"]


def test_address_then_card_resume_to_review_and_pay(client, auth_headers, llm):
    sid, _ = to_checkout(client, auth_headers)
    addr = client.post("/api/me/addresses", json=HOME, headers=auth_headers).json()
    ev = turn(client, auth_headers, sid, action={"type": "details_added", "address_id": addr["address_id"],
                                                 "label": "Shipping address added"})
    assert of(ev, "checkout_details_needed")[0]["needs"] == {"guest": False, "address": False, "payment": True}
    assert "Address saved" in of(ev, "message")[0]["text"] and stage(ev) == "CHECKOUT_DETAILS"

    card = add_card(client, auth_headers, VISA).json()
    assert not {"number", "cvc", "token_ref"} & set(card) and card["display"] == "Visa •••• 4242"
    ev = turn(client, auth_headers, sid, action={"type": "details_added", "payment_method_id": card["payment_method_id"],
                                                 "label": f"Card added · {card['display']}"})
    co = of(ev, "checkout_ready")[0]["checkout"]
    assert co["address"]["city"] == "Austin" and co["payment_method"]["display"] == "Visa •••• 4242"
    assert (co["subtotal"], co["tax"], co["total"]) == (129.0, 10.64, 139.64) and stage(ev) == "AWAITING_CONSENT"  # CH-3: card typed in (issuer unknown) → no Chase offer; Demo 1 spec amounts

    ev = turn(client, auth_headers, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert of(ev, "order_confirmed")[0]["order"]["total"] == 139.64
    s = session_of(client, auth_headers, sid)
    assert_no_raw_card(s.transcript, s.history, llm)


def test_only_the_card_missing(client, auth_headers):
    client.post("/api/me/addresses", json=HOME, headers=auth_headers)
    _, ev = to_checkout(client, auth_headers)
    assert of(ev, "checkout_details_needed")[0]["needs"] == {"guest": False, "address": False, "payment": True}
    text = of(ev, "message")[0]["text"]           # CH-2: no card numbers taken in ShopSphere
    assert "credit-card holders" in text and "Simulated Secure Payment Provider" in text
    assert "never see the card number" in text                  # CH-6: the card goes to the provider's page


def test_both_saved_goes_straight_to_review(client, auth_headers):
    client.post("/api/me/addresses", json=HOME, headers=auth_headers)
    add_card(client, auth_headers, VISA)
    _, ev = to_checkout(client, auth_headers)
    assert of(ev, "checkout_ready") and not of(ev, "checkout_details_needed") and stage(ev) == "AWAITING_CONSENT"


def test_someone_elses_address_is_refused(client, auth_headers):
    from tests.conftest import register_member
    oh, _ = register_member()                     # another eligible cardmember
    theirs = client.post("/api/me/addresses", json=HOME, headers=oh).json()
    sid, _ = to_checkout(client, auth_headers)
    ev = turn(client, auth_headers, sid, action={"type": "details_added", "address_id": theirs["address_id"]})
    assert "isn't saved on your account" in of(ev, "message")[0]["text"]
    assert of(ev, "checkout_details_needed")[0]["needs"]["address"] and stage(ev) == "CHECKOUT_DETAILS"


def test_not_now_cancels_from_the_secure_forms(client, auth_headers):
    sid, _ = to_checkout(client, auth_headers)
    ev = turn(client, auth_headers, sid, action={"type": "cancel_checkout", "label": "Not now"})
    assert stage(ev) == "IN_CART" and "nothing was charged" in of(ev, "message")[0]["text"]


# ── Card and address data never enter the conversation ──────────────────────

def test_card_typed_in_chat_is_masked_and_never_reaches_the_llm(client, auth_headers, llm):
    sid, _ = to_checkout(client, auth_headers)
    ev = turn(client, auth_headers, sid, text=f"my card is {CARD} exp 12/30 cvc 123")
    shown = of(ev, "user_message")[0]["text"]
    assert "•••• 4242" in shown and "cvc •••" in shown and "12/30" not in shown
    assert "didn't keep that" in of(ev, "message")[0]["text"]
    assert of(ev, "checkout_details_needed")                       # points back to the secure form
    s = session_of(client, auth_headers, sid)
    assert_no_raw_card(ev, s.transcript, s.history, llm)
    assert not any("cvc 123" in x for x in llm)


def test_card_typed_outside_checkout_is_masked_too(client, auth_headers, llm):
    sid = f"s-{uuid.uuid4().hex[:8]}"
    ev = turn(client, auth_headers, sid, text=f"can I pay with {CARD}?")
    assert_no_raw_card(ev, llm)
    assert not llm                                                 # answered without the LLM


def test_address_typed_in_chat_is_withheld(client, auth_headers, llm):
    sid, _ = to_checkout(client, auth_headers)
    ev = turn(client, auth_headers, sid, text="12 Oak Street, Austin TX 78704")
    assert of(ev, "user_message")[0]["text"] == "📍 (address hidden)"
    assert of(ev, "checkout_details_needed")[0]["needs"]["address"]
    s = session_of(client, auth_headers, sid)
    assert "Oak" not in json.dumps([s.transcript, s.history, llm])


@pytest.mark.parametrize("text,masked", [
    (f"my card is {CARD}", True), ("4242424242424242", True), ("cvv: 4321", True),
    ("running shoes under $150 size 10", False), ("order SS-12345", False), ("call 512 555 0199", False),
])
def test_payment_data_detection(text, masked):
    from backend.talkshop.parse import redact_payment_data
    out, found = redact_payment_data(text)
    assert found == masked and (out != text) == masked


# ── Server-side validation and failure paths ─────────────────────────────────

@pytest.mark.parametrize("change,code", [
    ({"state": "Texas"}, "INVALID_STATE"), ({"postal_code": "787"}, "INVALID_POSTAL_CODE"),
    ({"city": ""}, "MISSING_FIELD"),
])
def test_address_is_validated_by_shopsphere(client, auth_headers, change, code):
    r = client.post("/api/me/addresses", json={**HOME, **change}, headers=auth_headers)
    assert r.status_code >= 400 and r.json()["detail"]["code"] == code


@pytest.mark.parametrize("change", [{}, {"number": "4242 4242 4242 4241"}, {"exp_year": 2020}, {"cvc": "12"}])
def test_shopsphere_never_takes_card_numbers(client, auth_headers, change):
    """CH-6: any card number sent to ShopSphere is refused unread (the card is given on
    the payment provider's own page), and nothing of it comes back."""
    r = client.post("/api/me/payment-methods", json={**VISA, **change}, headers=auth_headers)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "PAN_NOT_ACCEPTED"
    assert_no_raw_card(r.json())


def test_order_failure_after_authorization_releases_the_payment(client, auth_headers, monkeypatch):
    client.post("/api/me/addresses", json=HOME, headers=auth_headers)
    add_card(client, auth_headers, VISA)
    sid, ev = to_checkout(client, auth_headers)
    co_id = of(ev, "checkout_ready")[0]["checkout"]["checkout_id"]
    real = order_service.finalize

    def boom(*a, **k):
        raise RuntimeError("order store unavailable")

    monkeypatch.setattr(order_service, "finalize", boom)
    from backend.db.schema import GatewayAuthorization
    from backend.db.session_utils import get_session
    with get_session() as db:
        voided = db.query(GatewayAuthorization).filter_by(status="voided").count()
    ev = turn(client, auth_headers, sid, action={"type": "go_ahead", "checkout_id": co_id})
    assert of(ev, "payment_status")[-1]["state"] == "failed" and not of(ev, "order_confirmed")
    assert "haven't been charged" in of(ev, "message")[-1]["text"] and stage(ev) == "AWAITING_CONSENT"
    with get_session() as db:                 # the gateway authorization was voided (issuer hold released)
        assert db.query(GatewayAuthorization).filter_by(status="voided").count() == voided + 1
    assert client.get("/api/orders", headers=auth_headers).json() in ([], {"orders": []})

    monkeypatch.setattr(order_service, "finalize", real)           # the shopper can simply try again
    ev = turn(client, auth_headers, sid, action={"type": "go_ahead", "checkout_id": co_id})
    assert of(ev, "order_confirmed")


def test_card_masked_by_the_browser_still_gets_the_warning(client, auth_headers, llm):
    """frontend/src/talkshop/redact.ts masks before sending; the server still treats it as card data."""
    sid = f"s-{uuid.uuid4().hex[:8]}"
    ev = turn(client, auth_headers, sid, text="my card is •••• 4242 cvc •••")
    assert "didn't keep that" in of(ev, "message")[0]["text"] and not llm
