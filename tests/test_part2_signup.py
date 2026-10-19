"""
Cardmember sign-up (Part 2): name + email + last four digits must match the
simulated Chase cardholder directory; otherwise nothing is created.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import CardAccount, IssuerCardholder, PaymentMethod, User
from backend.main import app
from backend.shop.cardholder_verification import NO_MATCH


@pytest.fixture
def client():
    return TestClient(app)


def cardholder(*cards, name="Robin Example"):
    """A fresh simulated Chase cardholder with the given (account_type, network, last4) accounts."""
    email = f"robin-{uuid.uuid4().hex[:8]}@cardmember.demo"
    with Session(get_engine()) as db:
        for kind, network, last4 in cards:
            db.add(IssuerCardholder(record_id=f"CH_T_{uuid.uuid4().hex[:10]}", full_name=name, email=email,
                                    account_type=kind, network=network, last4=last4))
        db.commit()
    return name, email


def signup(client, name, email, last4, password="member1234"):
    return client.post("/api/auth/register-cardmember",
                       json={"full_name": name, "email": email, "password": password, "last4": last4})


def users_with(email):
    with Session(get_engine()) as db:
        return db.query(User).filter_by(email=email).count()


def test_matching_details_create_an_eligible_member(client):
    name, email = cardholder(("credit_card", "Visa", "4417"))
    r = signup(client, "  robin   EXAMPLE ", email.upper(), "4417")       # case and spacing don't matter
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    m = client.get("/api/member", headers=h).json()
    assert m["eligible"] and m["member"]["primary_card"]["last4"] == "4417"
    assert m["member"]["agent_pay"][0]["status"] == "NOT_ENROLLED"          # sets up agent pay later, like Elena
    assert client.get("/api/products", headers=h).status_code == 200
    with Session(get_engine()) as db:
        pm = db.query(PaymentMethod).filter_by(user_id=r.json()["user"]["user_id"]).one()
        assert pm.last4 == "4417" and "4417" not in pm.token_ref


@pytest.mark.parametrize("field", ["name", "email", "last4"])
def test_any_mismatch_is_refused_with_one_generic_message(client, field):
    name, email = cardholder(("credit_card", "Mastercard", "5562"))
    details = {"name": name, "email": email, "last4": "5562"}
    details[field] = {"name": "Someone Else", "email": f"other-{uuid.uuid4().hex[:6]}@cardmember.demo",
                      "last4": "5563"}[field]
    r = signup(client, details["name"], details["email"], details["last4"])
    assert r.status_code == 400 and r.json()["detail"] == {"code": "NO_MATCH", "message": NO_MATCH}
    assert users_with(details["email"].lower()) == 0                        # nothing created


def test_checking_account_only_is_not_eligible(client):
    name, email = cardholder(("checking", None, "9901"))
    r = signup(client, name, email, "9901")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_ELIGIBLE"
    assert users_with(email) == 0


def test_visa_and_mastercard_holder_gets_both_cards(client):
    name, email = cardholder(("credit_card", "Visa", "3001"), ("credit_card", "Mastercard", "3002"))
    r = signup(client, name, email, "3002")                                 # either card's digits work
    assert r.status_code == 200, r.text
    with Session(get_engine()) as db:
        accts = db.query(CardAccount).filter_by(user_id=r.json()["user"]["user_id"]).all()
        assert sorted((a.network, a.last4) for a in accts) == [("Mastercard", "3002"), ("Visa", "3001")]
        assert all(a.marketplace_eligible for a in accts)


def test_a_linked_card_cannot_be_claimed_again(client):
    name, email = cardholder(("credit_card", "Visa", "6006"))
    assert signup(client, name, email, "6006").status_code == 200
    again = signup(client, name, email, "6006")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "EMAIL_EXISTS"


def test_guessing_is_locked_after_five_failures(client):
    name, email = cardholder(("credit_card", "Visa", "7007"))
    for guess in ["0001", "0002", "0003", "0004", "0005"]:
        assert signup(client, name, email, guess).status_code == 400
    r = signup(client, name, email, "7007")                                 # even the right digits now wait
    assert r.status_code == 429 and r.json()["detail"]["code"] == "TOO_MANY_ATTEMPTS"


def test_only_four_digits_are_accepted(client):
    name, email = cardholder(("credit_card", "Visa", "8008"))
    assert signup(client, name, email, "80a8").json()["detail"]["code"] == "INVALID_LAST4"
    assert signup(client, name, email, "4242424242424242").status_code == 422   # a full card number is refused


def test_existing_account_can_verify_its_card(client):
    email = f"older-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"name": "Casey Older", "email": email, "password": "older1234"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    before = client.get("/api/member", headers=h).json()     # CH-2: a customer already; no Chase benefits yet
    assert before["eligible"] and before["code"] is None and before["chase_benefits"] is False
    with Session(get_engine()) as db:
        db.add(IssuerCardholder(record_id=f"CH_T_{uuid.uuid4().hex[:10]}", full_name="Casey Older", email=email,
                                account_type="credit_card", network="Mastercard", last4="2468"))
        db.commit()
    bad = client.post("/api/member/verify-card", json={"last4": "2469"}, headers=h)
    assert bad.status_code == 400 and bad.json()["detail"]["message"] == NO_MATCH
    ok = client.post("/api/member/verify-card", json={"last4": "2468"}, headers=h)
    assert ok.status_code == 200 and ok.json()["eligible"] and ok.json()["chase_benefits"] is True
    assert client.get("/api/products", headers=h).status_code == 200


def test_seeded_demo_cardholders_exist():
    with Session(get_engine()) as db:
        alex = db.query(IssuerCardholder).filter_by(record_id="CH_ALEX_VISA").one()
        assert alex.claimed_by is None and alex.last4 == "1188"
        assert db.query(IssuerCardholder).filter_by(record_id="CH_A_VISA").one().claimed_by == "USR001"
