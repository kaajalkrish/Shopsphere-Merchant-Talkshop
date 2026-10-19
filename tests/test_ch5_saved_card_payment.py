"""
CH-5: one generic, network-aware payment route for saved cards. Citi
Mastercard, American Express and Chase Mastercard pay through the saved-card
route (opaque token → mock gateway → simulated <network> network → simulated
<issuer> issuer); only a VIC-capable Visa with an active agent token takes the
VIC-inspired path. All networks and issuers are simulated.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import GatewayAuthorization, Order, ProductVariant
from backend.main import app
from tests.conftest import scoped_consent

SKU = "SSP001-BLACK-8"


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


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "demo1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def checkout(client, h, sku=SKU):
    line = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h).json()["added_line_id"]
    r = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def pay(client, h, co_id):
    return client.post(f"/api/checkouts/{co_id}/confirm", json=scoped_consent(client, h, co_id), headers=h).json()


def scenario(client, h, outcome):
    assert client.post("/api/vic/demo/scenario", json={"outcome": outcome}, headers=h).status_code == 200


def steps(res):
    return {t["check"]: t for t in res["trace"]}


@pytest.mark.parametrize("email,label,network_actor,issuer_actor", [
    ("dana@shopsphere.demo", "Citi Mastercard •••• 5678", "Simulated Mastercard network", "Simulated Citi issuer"),
    ("ravi@shopsphere.demo", "American Express •••• 9012", "Simulated Amex network", "Simulated American Express issuer"),
    ("marcus@shopsphere.demo", "Chase Mastercard •••• 5510", "Simulated Mastercard network", "Simulated Chase issuer"),
])
def test_saved_cards_of_any_issuer_and_network_pay_on_the_saved_card_route(client, email, label, network_actor,
                                                                         issuer_actor):
    h = login(client, email)
    co = checkout(client, h)
    p = co["payment_path"]
    assert (p["path"], p["route"]) == ("standard", "saved_card") and p["card"]["display"] == label
    assert co["payment_method"]["label"] == label
    res = pay(client, h, co["checkout_id"])
    assert res["status"] == "authorized", res
    t = steps(res)
    assert t["Payment route"]["detail"].startswith(f"Saved card token: {label}")
    assert network_actor in t and issuer_actor in t
    assert not any(k.startswith("VIC control") for k in t)                     # never the Visa agent path
    with Session(get_engine()) as db:
        gw = db.query(GatewayAuthorization).filter(GatewayAuthorization.gateway_ref == res["transaction_id"]).one()
    assert gw.instrument_type == "card_on_file"


def test_only_a_visa_with_agent_payments_takes_the_vic_route(client):
    routes = {}
    for email in ("kaajal@shopsphere.demo", "chris@shopsphere.demo", "elena@shopsphere.demo",
                  "marcus@shopsphere.demo", "dana@shopsphere.demo", "ravi@shopsphere.demo"):
        h = login(client, email)
        co = checkout(client, h)
        routes[email.split("@")[0]] = (co["payment_path"]["route"], co["payment_path"].get("network"))
        client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)
    assert routes["kaajal"] == ("vic", "VISA") and routes["chris"] == ("vic", "VISA")
    assert routes["elena"] == ("vic_setup_required", "VISA")
    assert routes["marcus"] == ("saved_card", "MASTERCARD") and routes["dana"] == ("saved_card", "MASTERCARD")
    assert routes["ravi"] == ("saved_card", "AMEX")


def test_kaajal_switching_to_her_mastercard_leaves_the_vic_route(client):
    h = login(client, "kaajal@shopsphere.demo")
    co = checkout(client, h)
    moved = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": "PM_MC_0019"}, headers=h).json()
    assert moved["payment_path"]["route"] == "saved_card" and moved["payment_path"]["network"] == "MASTERCARD"
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


@pytest.mark.parametrize("outcome,status,reason", [("decline", "declined", "CARD_DECLINED"),
                                                   ("timeout", "declined", "PAYMENT_TIMEOUT")])
def test_decline_and_timeout_on_a_non_chase_card_create_no_order(client, outcome, status, reason):
    h = login(client, "dana@shopsphere.demo")
    co = checkout(client, h)
    scenario(client, h, outcome)
    res = pay(client, h, co["checkout_id"])
    assert res["status"] == status and res["reason"] == reason
    assert res["checkout"]["status"] == "open"                                # change card / retry
    with Session(get_engine()) as db:
        assert db.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%"),
                                      Order.display_id.isnot(None)).count() == 0
    retry = pay(client, h, co["checkout_id"])                                 # a new attempt reference
    assert retry["status"] == "authorized" and retry["order"]["payment"]["display"].endswith("5678")
