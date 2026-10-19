"""
Shared pytest fixtures. auth_headers registers a fresh, isolated test user
(own $1000 wallet) for every test that requests it — most endpoints now
require a real logged-in user. Deliberately function-scoped, not session-
scoped: tests that execute real purchases deduct from the wallet, and a
shared session-scoped user would eventually hit INSUFFICIENT_BALANCE
partway through the suite depending on run order.
"""
import re
import os
import tempfile
import uuid
from pathlib import Path

# Point the whole suite at its own throwaway database BEFORE any backend
# module is imported (they read COMMERCE_DB_PATH at import time), then seed
# it — so tests run against a fresh ShopSphere catalog and never touch the
# app's real backend/db/commerce.db.
_TEST_DB = Path(tempfile.mkdtemp(prefix="talkshop-tests-")) / "test.db"
os.environ["COMMERCE_DB_PATH"] = str(_TEST_DB)

from backend.db import init_db  # noqa: E402

init_db.run(_TEST_DB)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def auth_headers() -> dict:
    from backend.main import app
    client = TestClient(app)
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post("/api/auth/register", json={
        "name": "Pytest User", "email": email, "password": "pytest1234",
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def register_member(network: str = "Visa", account_type: str = "credit_card", **card) -> tuple[dict, str]:
    """A fresh user plus a simulated Chase account (Part 2). By default an
    eligible Chase credit card *without* a saved payment card, so tests that
    add their own cards start from an empty wallet as before."""
    from backend.db.session_utils import get_session
    from backend.main import app
    from backend.shop.eligibility import link_demo_card
    client = TestClient(app)
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post("/api/auth/register", json={"name": "Pytest Member", "email": email, "password": "pytest1234"})
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["user"]["user_id"]
    card.setdefault("with_payment_method", False)
    with get_session() as db:
        link_demo_card(db, user_id, network=network, account_type=account_type, **card)
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}, user_id


class _Resp:
    """Looks enough like a TestClient response for the older tests' call sites."""
    def __init__(self, status_code: int, body: dict):
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self) -> dict:
        return self._body


def add_card(client, headers: dict, card: dict) -> _Resp:
    """CH-6 test stand-in for "the customer saved a card on the payment provider's page":
    ShopSphere never receives card numbers any more, so tests put the provider's result
    (an opaque token + network + last 4) on file directly. The test card's number never
    reaches the app — it is only read here, in the test, to pick the network and last 4."""
    import re
    import secrets as _secrets
    from backend.db.session_utils import get_session
    from backend.shop import profile
    digits = re.sub(r"\D", "", card.get("number", "4242424242424242"))
    network = ("VISA" if digits.startswith("4") else "MASTERCARD" if re.match(r"5[1-5]|2[2-7]", digits)
               else "AMEX" if re.match(r"3[47]", digits) else "DISCOVER" if digits.startswith("6") else "UNKNOWN")
    user_id = client.get("/api/auth/me", headers=headers).json()["user_id"]
    exp_year = card.get("exp_year", 2030)
    with get_session() as db:
        pm = profile.save_provider_card(db, user_id, network=network, last4=digits[-4:],
                                        payment_token=f"pm_test_{_secrets.token_urlsafe(12)}",
                                        exp_month=card.get("exp_month", 12),
                                        exp_year=exp_year + 2000 if exp_year < 100 else exp_year,
                                        cardholder_name=card.get("cardholder_name"),
                                        make_default=bool(card.get("make_default")))
        return _Resp(200, profile.card_dict(pm))


# The simulated provider's published TEST cards (the provider refuses any other number).
TEST_CARDS = {"visa": "4242 4242 4242 4242", "mastercard": "5555 5555 5555 4444", "amex": "3782 822463 10005",
              "discover": "6011 1111 1111 1117", "decline": "4000 0000 0000 0002", "timeout": "4000 0000 0000 0119",
              "stepup": "4000 0027 6000 3184", "stepup_fail": "4000 0027 6000 3184",
              "frictionless": "4000 0000 0000 3055", "tokenize_fail": "4000 0000 0000 0101"}


def provider_pay(client, session_id: str, outcome: str = "visa", save: bool = False, verify: str = "ok"):
    """Fill in the provider's own card form (what the shopper does inside the embedded page),
    then answer the simulated payment verification if the card needs one.
    The test card number goes ONLY to the provider page — never to a ShopSphere endpoint.
    verify: "ok" | "fail" (once) | "fail_all" (until VERIFICATION_FAILED) | "cancel" | "skip"."""
    form = {"cardholder_name": "Test Shopper", "card_number": TEST_CARDS[outcome], "expiry": "12/30",
            "cvc": "1234" if outcome == "amex" else "123", **({"save": "1"} if save else {})}
    r = client.post(f"/psp/hosted/{session_id}/pay", data=form)        # tokenize (nothing charged yet)
    if outcome == "tokenize_fail":
        return r
    r = client.post(f"/psp/hosted/{session_id}/continue")               # verification, or frictionless
    if outcome == "stepup_fail":
        verify = "fail_all"
    answers = {"ok": ["ok"], "fail": ["fail"], "fail_all": ["fail"] * 3, "cancel": ["cancel"], "skip": []}[verify]
    for answer in answers:
        challenge = verification_challenge(r.text)
        if not challenge:
            break
        r = client.post(f"/psp/hosted/{session_id}/challenge", data={"result": answer, "challenge": challenge})
    return r


def verification_challenge(page: str):
    """The one-time challenge embedded in the provider's verification screen (None if not shown)."""
    m = re.search(r'name="challenge" value="([^"]+)"', page)
    return m.group(1) if m else None


def pay_with_provider(client, headers: dict, checkout_id: str, outcome: str = "visa", save: bool = False) -> dict:
    """CH-6: pay with a new card the way a shopper does — choose the provider, approve the
    exact invoice, enter a test card on the provider's own page (/psp/hosted/…),
    then let ShopSphere verify the result server-to-server. Returns the completion result."""
    r = client.patch(f"/api/checkouts/{checkout_id}", json={"pay_with": "provider"}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/checkouts/{checkout_id}/confirm", json=scoped_consent(client, headers, checkout_id),
                    headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "provider_handoff", r.text
    sid = r.json()["payment_session_id"]
    assert provider_pay(client, sid, outcome, save).status_code == 200
    return client.post(f"/api/checkouts/{checkout_id}/payment-session/{sid}/complete", headers=headers).json()


def scoped_consent(client, headers: dict, checkout_id: str, **extra) -> dict:
    """CH-4: the confirm body a shopper sends after looking at the final invoice:
    action PAY + the checkout version, final amount and currency they were shown."""
    co = client.get(f"/api/checkouts/{checkout_id}", headers=headers).json()
    return {"consent": True, "action": "PAY", "checkout_version": co.get("checkout_version"),
            "final_amount": co.get("total"), "currency": co.get("currency"), **extra}


@pytest.fixture
def member_headers() -> dict:
    """A Chase Visa credit-card holder (Chase benefits), no saved payment card."""
    return register_member()[0]
