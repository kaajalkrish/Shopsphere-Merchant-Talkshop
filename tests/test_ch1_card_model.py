"""
CH-1: the card model separates the issuer (the bank) from the network (Visa,
Mastercard, Amex, Discover). Additive only: access, offers, checkout and
payment behave exactly as before. All issuers and cards are simulated fixtures.
"""
import json
import secrets
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine, init_db
from backend.db.schema import CardAccount, PaymentMethod
from backend.main import app
from backend.shop import cards
from tests.conftest import add_card, register_member

CARD_FIELDS = {"issuer", "issuer_display", "network", "network_display", "status", "source",
               "eligible_for_standard_checkout"}


@pytest.fixture
def client():
    return TestClient(app)


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def by_last4(cards_list):
    return {c["last4"]: c for c in cards_list}


# ── Normalisation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,code", [("Visa", "VISA"), ("visa", "VISA"), ("Mastercard", "MASTERCARD"),
                                      ("Amex", "AMEX"), ("American Express", "AMEX"), ("Discover", "DISCOVER"),
                                      ("Card", "UNKNOWN"), (None, "UNKNOWN"), ("", "UNKNOWN")])
def test_network_codes(raw, code):
    assert cards.normalize_network(raw) == code


@pytest.mark.parametrize("raw,code", [("Chase", "CHASE"), ("CHASE", "CHASE"), ("Citi", "CITI"),
                                      ("Bank of America", "BOFA"), ("American Express", "AMEX"),
                                      (None, "UNKNOWN"), ("Some Credit Union", "UNKNOWN")])
def test_issuer_codes(raw, code):
    assert cards.normalize_issuer(raw) == code


def test_issuer_and_network_are_independent():
    """A Chase card can be a Mastercard; Amex is both an issuer and a network."""
    assert cards.network_display("MASTERCARD") == "Mastercard" and cards.issuer_display("CHASE") == "Chase"
    assert cards.network_display("AMEX") == "American Express" == cards.issuer_display("AMEX")
    assert cards.issuer_display("UNKNOWN") is None


# ── Seeded personas: issuer and network side by side ─────────────────────────

def test_persona_a_cards_show_chase_issuer_and_their_own_networks(client):
    h = login(client, "kaajal@shopsphere.demo")
    saved = by_last4(client.get("/api/me/payment-methods", headers=h).json())
    visa, mc = saved["4821"], saved["0019"]
    assert CARD_FIELDS <= set(visa)
    assert (visa["issuer"], visa["network"], visa["network_display"]) == ("CHASE", "VISA", "Visa")
    assert (mc["issuer"], mc["network"], mc["network_display"]) == ("CHASE", "MASTERCARD", "Mastercard")
    assert visa["status"] == "active" and visa["eligible_for_standard_checkout"] is True
    assert visa["brand"] == "Visa" and visa["display"] == "Visa •••• 4821"        # unchanged for older callers


@pytest.mark.parametrize("email,last4,network", [("marcus@shopsphere.demo", "5510", "MASTERCARD"),
                                                 ("elena@shopsphere.demo", "4002", "VISA")])
def test_other_personas(client, email, last4, network):
    card = by_last4(client.get("/api/me/payment-methods", headers=login(client, email)).json())[last4]
    assert (card["issuer"], card["network"]) == ("CHASE", network)


def test_member_accounts_carry_codes_next_to_display_names(client):
    accounts = client.get("/api/member", headers=login(client, "kaajal@shopsphere.demo")).json()["member"]["accounts"]
    visa = next(a for a in accounts if a["last4"] == "4821")
    assert (visa["issuer"], visa["network"]) == ("Chase", "Visa")                  # display names, as before
    assert (visa["issuer_code"], visa["network_code"]) == ("CHASE", "VISA")


def test_checkout_snapshot_card_has_issuer_and_network(client):
    h = login(client, "marcus@shopsphere.demo")
    line = client.post("/api/cart/lines", json={"sku": "SSP002-ORANGE-9", "quantity": 1}, headers=h).json()
    co = client.post("/api/checkouts", json={"line_ids": [line["added_line_id"]]}, headers=h).json()
    try:
        assert (co["payment_method"]["issuer"], co["payment_method"]["network"]) == ("CHASE", "MASTERCARD")
        assert all(CARD_FIELDS <= set(c) for c in co["saved_payment_methods"])
    finally:
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
        client.delete(f"/api/cart/lines/{line['added_line_id']}", headers=h)


# ── Linking and legacy cards ─────────────────────────────────────────────────

def test_linked_chase_card_records_issuer_and_source(client):
    h, uid = register_member(network="Mastercard", with_payment_method=True)
    card = client.get("/api/me/payment-methods", headers=h).json()[0]
    assert (card["issuer"], card["network"], card["source"]) == ("CHASE", "MASTERCARD", "seeded")


def test_provider_saved_card_has_unknown_issuer_and_its_network(client):
    """CH-6: a card saved on the payment provider's page: the provider gives the network
    and last 4 (and an opaque token); the issuer isn't known to ShopSphere."""
    h, _ = register_member()
    amex = add_card(client, h, {"number": "3782 822463 10005"}).json()
    assert (amex["issuer"], amex["network"], amex["network_display"], amex["source"]) == \
        ("UNKNOWN", "AMEX", "American Express", "provider_token")
    assert amex["issuer_display"] is None and amex["label"] == "American Express •••• 0005"


def test_no_token_or_card_number_in_card_views(client):
    h = login(client, "kaajal@shopsphere.demo")
    body = json.dumps(client.get("/api/me/payment-methods", headers=h).json())
    assert "token_ref" not in body and "tok_" not in body
    with Session(get_engine()) as db:
        tokens = [pm.token_ref for pm in db.query(PaymentMethod).filter_by(user_id="USR001")]
    assert tokens and not any(t in body for t in tokens)


# ── Backfill and migration (databases created before CH-1) ───────────────────

def test_backfill_fills_issuer_from_the_linked_account_else_unknown():
    with Session(get_engine()) as db:
        uid = f"USRBF{secrets.token_hex(3).upper()}"
        linked, loose = f"PM_BF_{secrets.token_hex(3)}", f"PM_BF_{secrets.token_hex(3)}"
        for pm_id, brand in ((linked, "Visa"), (loose, "Mastercard")):
            db.add(PaymentMethod(payment_method_id=pm_id, user_id=uid, brand=brand, last4="1111", exp_month=1,
                                 exp_year=2031, token_ref=f"tok_{secrets.token_urlsafe(18)}"))
        db.flush()
        db.query(PaymentMethod).filter(PaymentMethod.user_id == uid).update(
            {"issuer": None, "status": None, "source": None})
        db.add(CardAccount(account_id=f"ACCT_BF_{secrets.token_hex(3)}", user_id=uid, issuer="Chase",
                           account_type="credit_card", product_name="Chase Visa credit card", network="Visa",
                           last4="1111", payment_method_id=linked))
        db.commit()
        assert cards.backfill(db) >= 2
        a = db.query(PaymentMethod).filter_by(payment_method_id=linked).one()
        b = db.query(PaymentMethod).filter_by(payment_method_id=loose).one()
        assert (a.issuer, a.status, a.source) == ("CHASE", "active", "issuer_link")
        assert (b.issuer, b.status, b.source) == ("UNKNOWN", "active", "manual_legacy")


def test_migration_adds_the_columns_to_an_old_payment_methods_table():
    path = Path(tempfile.mkdtemp(prefix="ch1-mig-")) / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE payment_methods (id INTEGER PRIMARY KEY, payment_method_id VARCHAR(50) UNIQUE NOT NULL,
                   user_id VARCHAR(50) NOT NULL, brand VARCHAR(20) NOT NULL, last4 VARCHAR(4) NOT NULL,
                   exp_month INTEGER NOT NULL, exp_year INTEGER NOT NULL, cardholder_name VARCHAR(100),
                   token_ref VARCHAR(100) NOT NULL, behaviour VARCHAR(10) NOT NULL DEFAULT 'approve',
                   is_default BOOLEAN, created_at DATETIME)""")
    con.commit()
    con.close()
    engine = init_db(path)
    engine.dispose()
    con = sqlite3.connect(path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(payment_methods)")}
    con.close()
    assert {"issuer", "status", "source", "saved"} <= cols
