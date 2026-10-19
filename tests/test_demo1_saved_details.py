"""
Deleting saved addresses and cards (checkout page). Only the customer's own
items can be deleted; the next saved one becomes the default; open checkouts
switch to it (or ask again when none are left); past orders are unaffected.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from tests.conftest import scoped_consent, add_card

HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"}
WORK = {**HOME, "line1": "500 Congress Ave", "label": "Work"}
VISA = {"number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Test"}
MASTER = {**VISA, "number": "5555 5555 5555 4444"}


@pytest.fixture
def client():
    return TestClient(app)
@pytest.fixture
def auth_headers(member_headers):
    """Part 2: the marketplace is for eligible cardmembers only."""
    return member_headers



def add(client, h, path, body):
    # CH-6: cards come from the (simulated) payment provider, never as card numbers
    r = add_card(client, h, body) if path == "/api/me/payment-methods" else client.post(path, json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def open_checkout(client, h):
    line = client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-8", "quantity": 1}, headers=h).json()["added_line_id"]
    return client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()


def test_delete_default_address_promotes_the_next_and_moves_open_checkouts(client, auth_headers):
    home = add(client, auth_headers, "/api/me/addresses", HOME)          # first → default
    work = add(client, auth_headers, "/api/me/addresses", WORK)
    co = open_checkout(client, auth_headers)
    assert co["address"]["address_id"] == home["address_id"]

    r = client.delete(f"/api/me/addresses/{home['address_id']}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["default_address_id"] == work["address_id"]
    saved = client.get("/api/me/addresses", headers=auth_headers).json()
    assert [a["address_id"] for a in saved] == [work["address_id"]] and saved[0]["is_default"]
    co = client.get(f"/api/checkouts/{co['checkout_id']}", headers=auth_headers).json()
    assert co["address"]["address_id"] == work["address_id"]


def test_deleting_the_last_card_brings_the_card_form_back(client, auth_headers):
    add(client, auth_headers, "/api/me/addresses", HOME)
    card = add(client, auth_headers, "/api/me/payment-methods", VISA)
    co = open_checkout(client, auth_headers)
    assert co["ready"]
    r = client.delete(f"/api/me/payment-methods/{card['payment_method_id']}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["default_payment_method_id"] is None
    co = client.get(f"/api/checkouts/{co['checkout_id']}", headers=auth_headers).json()
    assert co["payment_method"] is None and not co["ready"]
    assert client.get("/api/me/payment-methods", headers=auth_headers).json() == []


def test_deleting_a_non_default_card_keeps_the_default(client, auth_headers):
    visa = add(client, auth_headers, "/api/me/payment-methods", VISA)
    master = add(client, auth_headers, "/api/me/payment-methods", MASTER)   # make_default false → Visa stays default
    r = client.delete(f"/api/me/payment-methods/{master['payment_method_id']}", headers=auth_headers)
    assert r.json()["default_payment_method_id"] == visa["payment_method_id"]


def test_cannot_delete_someone_elses_details(client, auth_headers):
    from tests.conftest import register_member
    oh, _ = register_member()                     # another eligible cardmember
    theirs = add(client, oh, "/api/me/addresses", HOME)
    their_card = add(client, oh, "/api/me/payment-methods", VISA)
    r = client.delete(f"/api/me/addresses/{theirs['address_id']}", headers=auth_headers)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "UNKNOWN_ADDRESS"
    r = client.delete(f"/api/me/payment-methods/{their_card['payment_method_id']}", headers=auth_headers)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "UNKNOWN_CARD"
    assert len(client.get("/api/me/addresses", headers=oh).json()) == 1          # still there


def test_past_orders_keep_their_address_and_card(client, auth_headers):
    home = add(client, auth_headers, "/api/me/addresses", HOME)
    card = add(client, auth_headers, "/api/me/payment-methods", VISA)
    co = open_checkout(client, auth_headers)
    res = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, auth_headers, co['checkout_id']), headers=auth_headers).json()
    order_id = res["order"]["order_id"]
    client.delete(f"/api/me/addresses/{home['address_id']}", headers=auth_headers)
    client.delete(f"/api/me/payment-methods/{card['payment_method_id']}", headers=auth_headers)
    order = client.get(f"/api/orders/{order_id}", headers=auth_headers).json()
    assert "1 Main St" in str(order) and "4242" in str(order)


def test_visitors_cannot_delete(client):
    v = client.post("/api/auth/visitor").json()
    vh = {"Authorization": f"Bearer {v['access_token']}"}
    assert client.delete("/api/me/addresses/ADDR_X", headers=vh).status_code == 403
    assert client.delete("/api/me/payment-methods/PM_X", headers=vh).status_code == 403
