"""
Demo 1, Phase 2 — ShopSphere merchant services as plain APIs, no LLM anywhere.
Covers the spec's happy path (search → variant → cart → checkout → GO AHEAD →
authorized → order) and the plan's alternative paths (§7).
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import CartItem, Checkout, Order, PaymentMethod, ProductVariant
from backend.main import app
from tests.conftest import scoped_consent, add_card

VISA = {"number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Test Shopper"}
HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "tx", "postal_code": "78701"}


@pytest.fixture
def client():
    return TestClient(app)
@pytest.fixture
def auth_headers(member_headers):
    """Part 2: the marketplace is for eligible cardmembers only."""
    return member_headers



@pytest.fixture
def shopper(client, auth_headers):
    """A logged-in customer with a saved address and Visa card."""
    assert client.post("/api/me/addresses", json=HOME, headers=auth_headers).status_code == 200
    assert add_card(client, auth_headers, VISA).status_code == 200
    return auth_headers


def _stock(sku):
    with Session(get_engine()) as s:
        return s.query(ProductVariant).filter_by(sku=sku).one().stock


def _set_stock(sku, n):
    with Session(get_engine()) as s:
        s.query(ProductVariant).filter_by(sku=sku).update({"stock": n})
        s.commit()


def _add(client, headers, sku, qty=1):
    r = client.post("/api/cart/lines", json={"sku": sku, "quantity": qty}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["added_line_id"]


def _checkout(client, headers, *line_ids):
    r = client.post("/api/checkouts", json={"line_ids": list(line_ids)}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Catalog / search / inventory ────────────────────────────────────────────

def test_spec_search_returns_the_three_documented_shoes_first(client, auth_headers):
    r = client.get("/api/products", headers=auth_headers, params={"q": "I need running shoes under $150 for everyday running",
                                            "category": "running_shoes", "max_price": 150, "limit": 3})
    assert [p["name"] for p in r.json()] == ["Runner Pro X", "FlexRun 5", "Daily Runner"]


def test_search_filters(client, auth_headers):
    women = client.get("/api/products", headers=auth_headers, params={"gender": "women"}).json()
    assert women and all(p["gender"] in ("women", "unisex") for p in women)
    cheap = client.get("/api/products", headers=auth_headers, params={"department": "shoes", "sort": "price_asc"}).json()
    assert [p["price"] for p in cheap] == sorted(p["price"] for p in cheap)
    size11 = {p["product_id"] for p in client.get("/api/products", headers=auth_headers, params={"size": "11", "category": "running_shoes"}).json()}
    assert "SSP001" not in size11            # Runner Pro X is sold out in 11
    assert client.get("/api/products", headers=auth_headers, params={"sort": "nope"}).status_code == 422


def test_product_detail_and_availability(client, auth_headers):
    d = client.get("/api/products/SSP001", headers=auth_headers).json()
    assert d["name"] == "Runner Pro X" and d["variants"] and {"sku", "stock", "image_url"} <= d["variants"][0].keys()
    a = client.get("/api/products/SSP001/availability", headers=auth_headers, params={"size": "11"}).json()
    assert all(not c["available"] for c in a["colors"])
    a = client.get("/api/products/SSP001/availability", headers=auth_headers, params={"size": "8", "color": "black"}).json()
    assert a["selected"]["sku"] == "SSP001-BLACK-8" and a["selected"]["available"]
    assert client.get("/api/products/NOPE/availability", headers=auth_headers).status_code == 404


# ── Cart ────────────────────────────────────────────────────────────────────

def test_cart_lines_merge_update_remove(client, auth_headers):
    line = _add(client, auth_headers, "SSP002-ORANGE-9")
    again = client.post("/api/cart/lines", json={"sku": "SSP002-ORANGE-9", "quantity": 1}, headers=auth_headers).json()
    assert again["added_line_id"] == line and again["item_count"] == 2
    cart = client.patch(f"/api/cart/lines/{line}", json={"quantity": 3}, headers=auth_headers).json()
    assert cart["subtotal"] == round(139.0 * 3, 2)
    cart = client.delete(f"/api/cart/lines/{line}", headers=auth_headers).json()
    assert cart["lines"] == [] and cart["item_count"] == 0


def test_cart_rejects_unknown_sku_and_out_of_stock(client, auth_headers):
    assert client.post("/api/cart/lines", json={"sku": "NOPE"}, headers=auth_headers).status_code == 404
    r = client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-11"}, headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OUT_OF_STOCK"


def test_cart_requires_login(client):
    assert client.get("/api/cart/lines").status_code == 401


# ── Profile ─────────────────────────────────────────────────────────────────

def test_saved_card_is_masked_and_never_a_card_number(client, auth_headers):
    refused = client.post("/api/me/payment-methods", json=VISA, headers=auth_headers)   # CH-6: never a PAN
    assert refused.status_code == 410 and refused.json()["detail"]["code"] == "PAN_NOT_ACCEPTED"
    card = add_card(client, auth_headers, VISA).json()                               # (a provider-saved card)
    assert card["display"] == "Visa •••• 4242" and "number" not in card and "cvc" not in card
    with Session(get_engine()) as s:
        row = s.query(PaymentMethod).filter_by(payment_method_id=card["payment_method_id"]).one()
        stored = " ".join(str(getattr(row, c.name)) for c in PaymentMethod.__table__.columns)
    assert "4242424242424242" not in stored.replace(" ", "") and " 123 " not in f" {stored} "


def test_address_validation(client, auth_headers):
    r = client.post("/api/me/addresses", json={**HOME, "postal_code": "ABC"}, headers=auth_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_POSTAL_CODE"


# ── Checkout ────────────────────────────────────────────────────────────────

def test_checkout_snapshot_matches_spec_amounts(client, shopper):
    co = _checkout(client, shopper, _add(client, shopper, "SSP001-BLACK-8"))
    # CH-3: the 15% running-shoe offer needs the order PAID with an eligible Chase card. This card was
    # typed in (issuer unknown), so no offer: the Demo 1 spec amounts ($139.64). See test_ch3_offers.py.
    assert (co["subtotal"], co["tax"], co["shipping"], co["total"]) == (129.0, 10.64, 0.0, 139.64)
    assert co["offer"] is None and co["offer_discount"] == 0
    assert co["ready"] and co["payment_method"]["display"] == "Visa •••• 4242"
    assert {o["method"] for o in co["delivery"]["options"]} == {"standard", "express"}
    express = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"delivery_method": "express"}, headers=shopper).json()
    assert express["shipping"] == 9.99 and express["total"] == round(139.64 + 9.99, 2)
    assert express["delivery"]["date"] < co["delivery"]["date"]
    assert express["checkout_hash"] != co["checkout_hash"]


def test_checkout_quantity_change_recalculates(client, shopper):
    line = _add(client, shopper, "SSP003-BLUE-9")
    co = _checkout(client, shopper, line)
    co2 = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"quantities": {line: 2}}, headers=shopper).json()
    after_offer = 238.0 - co2["offer_discount"]
    assert co2["subtotal"] == 238.0 and co2["tax"] == round(after_offer * 0.0825, 2)


def test_new_customer_without_saved_details_is_not_ready(client, auth_headers):
    co = _checkout(client, auth_headers, _add(client, auth_headers, "SSP009-BLACK-9"))
    assert not co["ready"] and {"NO_ADDRESS", "NO_PAYMENT_METHOD"} <= {i["code"] for i in co["issues"]}
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, auth_headers, co['checkout_id']), headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_READY"


def test_cannot_check_out_someone_elses_cart_line(client, shopper, auth_headers):
    line = _add(client, shopper, "SSP010-RED-9")
    from tests.conftest import register_member
    other, _ = register_member()
    r = client.post("/api/checkouts", json={"line_ids": [line]}, headers=other)
    assert r.status_code == 404


# ── Confirm & pay (the GO AHEAD gate) ───────────────────────────────────────

def test_happy_path_go_ahead_creates_order(client, shopper):
    sku = "SSP001-BLACK-8"
    before = _stock(sku)
    line = _add(client, shopper, sku)
    co = _checkout(client, shopper, line)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "authorized"
    order = out["order"]
    assert re.fullmatch(r"SS-\d{5}", order["order_id"])
    assert order["total"] == 139.64 and order["payment"]["display"] == "Visa •••• 4242"  # CH-3: card typed in (issuer unknown) → no Chase offer; Demo 1 spec amounts
    assert order["lines"][0]["sku"] == sku and order["ship_to"]["city"] == "Austin" and order["delivery_date"]
    assert _stock(sku) == before - 1                                  # stock committed
    assert client.get("/api/cart/lines", headers=shopper).json()["lines"] == []   # bought line removed
    assert client.get(f"/api/checkouts/{co['checkout_id']}", headers=shopper).json()["status"] == "paid"
    listed = client.get("/api/orders", headers=shopper).json()
    assert any(o.get("display_id") == order["order_id"] for o in listed)
    assert client.get(f"/api/orders/{order['order_id']}", headers=shopper).json()["display_id"] == order["order_id"]


def test_confirm_without_consent_never_charges(client, shopper):
    co = _checkout(client, shopper, _add(client, shopper, "SSP004-CORAL-9"))
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json={}, headers=shopper)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "CONSENT_REQUIRED"
    with Session(get_engine()) as s:
        assert s.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%")).count() == 0


def test_client_cannot_change_the_amount(client, shopper):
    """CH-4: a confirm that names a different amount is refused (nothing charged); a
    client-sent total is never used; the server's invoice total is what's charged."""
    co = _checkout(client, shopper, _add(client, shopper, "SSP005-GREY-7"))
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm",
                    json=scoped_consent(client, shopper, co["checkout_id"], final_amount=0.01, total=0.01), headers=shopper)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_CHECKOUT"
    with Session(get_engine()) as s:
        assert s.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%")).count() == 0
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm",
                    json=scoped_consent(client, shopper, co["checkout_id"], total=0.01, amount=0.01), headers=shopper)
    assert r.json()["status"] == "authorized" and r.json()["order"]["total"] == co["total"]


def test_declined_card_creates_no_order_and_can_retry(client, shopper):
    co = _checkout(client, shopper, _add(client, shopper, "SSP006-GREEN-9"))
    with Session(get_engine()) as s:                     # make this shopper's card a decline card
        card = s.query(PaymentMethod).filter_by(payment_method_id=co["payment_method"]["payment_method_id"]).one()
        card.behaviour = "decline"
        s.commit()
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper).json()
    assert r["status"] == "declined" and r["reason"] == "CARD_DECLINED"
    assert r["checkout"]["status"] == "open"
    with Session(get_engine()) as s:
        assert s.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%")).count() == 0
    good = add_card(client, shopper, {**VISA, "number": "5555 5555 5555 4444"}).json()
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": good["payment_method_id"]}, headers=shopper)
    retry = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper).json()
    assert retry["status"] == "authorized" and retry["order"]["payment"]["display"] == "Mastercard •••• 4444"


def test_demo_mastercard_0019_declines_for_kaajal(client):
    token = client.post("/api/auth/login", json={"email": "kaajal@shopsphere.demo", "password": "demo1234"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    co = _checkout(client, h, _add(client, h, "SSP059-BLACK"))
    client.patch(f"/api/checkouts/{co['checkout_id']}", json={"payment_method_id": "PM_MC_0019"}, headers=h)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, h, co['checkout_id']), headers=h).json()
    assert r["status"] == "declined" and r["checkout"]["payment_method"]["display"] == "Mastercard •••• 0019"
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=h)


def test_cancel_at_review_charges_nothing(client, shopper):
    line = _add(client, shopper, "SSP011-WHITE-7")
    co = _checkout(client, shopper, line)
    assert client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=shopper).json()["status"] == "cancelled"
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CHECKOUT_CLOSED"
    assert any(l["line_id"] == line for l in client.get("/api/cart/lines", headers=shopper).json()["lines"])


def test_sold_out_just_before_payment(client, shopper):
    sku = "SSP013-NAVY-9"
    co = _checkout(client, shopper, _add(client, shopper, sku))
    saved = _stock(sku)
    _set_stock(sku, 0)
    try:
        r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper)
        assert r.status_code == 409
        assert any(i["code"] == "OUT_OF_STOCK" for i in r.json()["detail"]["issues"])
        with Session(get_engine()) as s:
            assert s.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%")).count() == 0
    finally:
        _set_stock(sku, saved)


def test_paid_checkout_cannot_be_changed_or_paid_twice(client, shopper):
    co = _checkout(client, shopper, _add(client, shopper, "SSP046-BLACK"))
    client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper)
    assert client.patch(f"/api/checkouts/{co['checkout_id']}", json={"delivery_method": "express"},
                        headers=shopper).status_code == 409
    again = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper)
    assert again.status_code == 409


def test_over_limit_purchase_is_blocked_by_policy(client, shopper):
    line = _add(client, shopper, "SSP054-SILVER-256GB", qty=3)          # 3 × $1,099 > $2,500 limit
    co = _checkout(client, shopper, line)
    r = client.post(f"/api/checkouts/{co['checkout_id']}/confirm", json=scoped_consent(client, shopper, co['checkout_id']), headers=shopper).json()
    assert r["status"] == "blocked" and r["reason"] == "POLICY_DENIED"
    with Session(get_engine()) as s:
        assert s.query(Order).filter(Order.order_id.like(f"{co['checkout_id']}%")).count() == 0
        assert s.query(Checkout).filter_by(checkout_id=co["checkout_id"]).one().status == "open"
    client.post(f"/api/checkouts/{co['checkout_id']}/cancel", headers=shopper)
    client.delete(f"/api/cart/lines/{line}", headers=shopper)
