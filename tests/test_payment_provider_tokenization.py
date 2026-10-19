"""
Secure Payment Sheet, Phase 3: the Simulated Payment Provider TOKENIZES the card
first (PENDING → TOKENIZED). Nothing is charged at this step. The merchant can then
read only a safe result: payment_session_id, an opaque token, the network, the last
4 and the status. The store page in the browser hears even less: the step, network
and last 4 (never the token). A card that can't be secured stops before any payment
attempt. Test cards only; the provider never keeps the card number or CVC.
"""
import html
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import Order, ProductVariant, ProviderPaymentSession
from backend.main import app
from tests.conftest import TEST_CARDS, scoped_consent, verification_challenge

KEY = {"X-Gateway-Key": "shopsphere-demo-merchant-key"}
STORE = "http://localhost:5174"


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


def new_session(client):
    """A new customer, an order, "Continue to Secure Payment" → a provider payment session."""
    email = f"tok-{uuid.uuid4().hex[:8]}@example.com"
    h = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "name": "Tok Shopper", "email": email, "password": "tokenize1"}).json()["access_token"]}
    client.post("/api/me/addresses", headers=h, json={"full_name": "Tok", "line1": "1 A St", "city": "Austin",
                                                      "state": "TX", "postal_code": "78701"})
    line = client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-8"}, headers=h).json()["added_line_id"]
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "provider"}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", headers=h, json=scoped_consent(
        client, h, co["checkout_id"], return_url=f"{STORE}/checkout/{co['checkout_id']}"))
    assert r.json()["status"] == "provider_handoff", r.text
    return h, co["checkout_id"], r.json()["payment_session_id"]


def submit(client, sid, card="visa", **extra):
    form = {"cardholder_name": "Tok Shopper", "card_number": TEST_CARDS[card], "expiry": "12/30",
            "cvc": "1234" if card == "amex" else "123", **extra}
    return client.post(f"/psp/hosted/{sid}/pay", data=form)


def provider_result(client, sid):
    return client.get(f"/psp/v1/payment_sessions/{sid}", headers=KEY).json()


def complete(client, h, co_id, sid):
    return client.post(f"/api/checkouts/{co_id}/payment-session/{sid}/complete", headers=h).json()


def messages(page: str) -> list[dict]:
    return [json.loads(m) for m in re.findall(r"postMessage\((\{.*?\}),", page)]


def test_the_card_form_reports_only_its_step_and_secures_on_submit(client):
    _, _, sid = new_session(client)
    page = client.get(f"/psp/hosted/{sid}").text
    assert {"type": "shopsphere-provider-step", "step": "card", "session": sid} in messages(page)
    assert "Continue securely" in page and "Securing your payment method" in page
    assert '"step": "securing"' in page                                   # told on submit, before the server answers


def test_tokenization_returns_only_a_safe_result(client):
    h, co_id, sid = new_session(client)
    page = submit(client, sid, "visa").text                               # follows the redirect to "Card secured"
    res = provider_result(client, sid)
    assert res["status"] == "TOKENIZED" and res["payment_token"].startswith("pm_mock_")
    assert (res["network"], res["last4"], res["verification_required"]) == ("VISA", "4242", True)
    assert res["authorization_reference"] is None                       # nothing authorized, nothing charged
    blob = json.dumps(res)
    assert "4242424242424242" not in blob and not {"pan", "card_number", "number", "cvc", "cvv"} & set(res)
    # the store page hears "Visa •••• 4242" — never the token, never the card number
    assert "Card secured" in page and "Visa •••• 4242" in page
    step = next(m for m in messages(page) if m["type"] == "shopsphere-provider-step")
    assert step == {"type": "shopsphere-provider-step", "step": "tokenized", "network": "VISA", "last4": "4242",
                    "session": sid}
    assert res["payment_token"] not in page and "4242 4242 4242 4242" not in page.split("tests")[0]
    assert complete(client, h, co_id, sid)["status"] == "pending"       # tokenized ≠ paid


def test_a_frictionless_card_needs_no_verification(client):
    _, _, sid = new_session(client)
    submit(client, sid, "frictionless")
    assert provider_result(client, sid)["verification_required"] is False


def test_a_card_that_cannot_be_secured_stops_before_any_payment_attempt(client):
    h, co_id, sid = new_session(client)
    page = html.unescape(submit(client, sid, "tokenize_fail").text)
    assert "couldn't be secured" in page and "no payment was attempted" in page
    res = provider_result(client, sid)
    assert res["status"] == "PENDING" and res["payment_token"] is None
    assert complete(client, h, co_id, sid)["status"] == "pending"
    with Session(get_engine()) as db:
        assert db.query(Order).filter(Order.order_id.like(f"{co_id}%"), Order.display_id.isnot(None)).count() == 0
    submit(client, sid, "mastercard")                                     # another card on the same sheet
    assert provider_result(client, sid)["status"] == "TOKENIZED"


def test_after_tokenization_the_payment_is_verified_and_the_order_is_placed(client):
    h, co_id, sid = new_session(client)
    submit(client, sid, "amex")
    page = client.post(f"/psp/hosted/{sid}/continue").text
    assert "Verify your payment" in page and provider_result(client, sid)["status"] == "VERIFICATION_REQUIRED"
    page = client.post(f"/psp/hosted/{sid}/challenge",
                       data={"result": "ok", "challenge": verification_challenge(page)}).text
    assert any(m["type"] == "shopsphere-provider-result" and m["status"] == "VERIFIED" for m in messages(page))
    assert provider_result(client, sid)["authorization_reference"] is None   # verified ≠ authorized
    done = complete(client, h, co_id, sid)
    assert done["status"] == "authorized" and done["order"]["payment"]["display"] == "American Express •••• 0005"


def test_resubmitting_after_tokenization_does_not_tokenize_twice(client):
    _, _, sid = new_session(client)
    submit(client, sid, "visa")
    token = provider_result(client, sid)["payment_token"]
    again = submit(client, sid, "mastercard")                             # double submit / back button
    assert "Card secured" in again.text and "Visa •••• 4242" in again.text
    assert provider_result(client, sid)["payment_token"] == token


def test_closing_a_tokenized_session_cancels_it(client):
    h, co_id, sid = new_session(client)
    submit(client, sid, "visa")
    client.post(f"/api/checkouts/{co_id}/cancel", headers=h)
    with Session(get_engine()) as db:
        assert db.query(ProviderPaymentSession).filter_by(session_id=sid).one().status == "CANCELLED"
