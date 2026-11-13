"""
Demo 1, Phase 1 — the ShopSphere catalog and demo customer as seeded.
Runs against the suite's own fresh database (see conftest.py).
"""
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import Address, Merchant, PaymentMethod, Product, ProductVariant, User

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "frontend" / "public"


@pytest.fixture
def db():
    with Session(get_engine()) as s:
        yield s


def test_shopsphere_is_the_only_merchant(db):
    assert [m.merchant_name for m in db.query(Merchant)] == ["ShopSphere"]


def test_catalog_has_66_products_across_four_departments(db):
    products = db.query(Product).all()
    assert len(products) == 66
    assert {p.department for p in products} == {"shoes", "clothing", "accessories", "electronics"}
    assert all(p.gender in {"women", "men", "unisex", "kids"} for p in products)
    kids = [p for p in products if p.gender == "kids"]                  # the small kids' section
    assert len(kids) == 6 and {p.department for p in kids} == {"shoes", "clothing", "accessories"}


def test_every_variant_has_a_colour_photo_on_disk(db):
    variants = db.query(ProductVariant).all()
    assert variants
    kids = {p.product_id for p in db.query(Product).filter_by(gender="kids")}
    missing = [v.sku for v in variants if v.product_id not in kids
               and (not v.image_url or not (PUBLIC / v.image_url.lstrip("/")).exists())]
    assert missing == []
    # kids' products have no hand-picked photo yet: shown without one, never with another product's
    assert all(v.image_url is None for v in variants if v.product_id in kids)


def test_every_photo_has_a_licence_record():
    catalog = json.loads((ROOT / "data" / "shopsphere_catalog.json").read_text(encoding="utf-8"))
    record = json.loads((ROOT / "data" / "catalog_images.json").read_text(encoding="utf-8"))
    for p in catalog["products"]:
        for c in p["colors"]:
            if c["photo"] is None:                                  # kids' products: no photo yet, none used
                assert p["gender"] == "kids" and p["slug"] not in record
                continue
            entry = record[p["slug"]][c["name"]]
            assert entry["pixabay_id"] == c["photo"]["pixabay_id"]
            assert entry["license"] == "Pixabay Content License"


@pytest.mark.parametrize("name,price,rating", [
    ("Runner Pro X", 129.0, 4.7), ("FlexRun 5", 139.0, 4.6), ("Daily Runner", 119.0, 4.5),
])
def test_spec_running_shoes_match_the_document(db, name, price, rating):
    p = db.query(Product).filter_by(name=name).one()
    assert (p.price, p.rating, p.category) == (price, rating, "running_shoes")
    assert "everyday running" in p.tags


def test_demo_stock_gap_runner_pro_x_size_11(db):
    rows = db.query(ProductVariant).filter_by(product_id="SSP001").all()
    assert all(v.stock == 0 for v in rows if v.size == "11")
    black_8 = next(v for v in rows if v.size == "8" and v.color == "Black")
    assert black_8.stock > 0  # the spec's demo answer: size 8, black


def test_demo_customer_kaajal_has_saved_addresses_and_cards(db):
    u = db.query(User).filter_by(user_id="USR001").one()
    assert (u.name, u.email) == ("Kaajal", "kaajal@shopsphere.demo")
    addresses = db.query(Address).filter_by(user_id="USR001").all()
    assert len(addresses) == 2 and sum(a.is_default for a in addresses) == 1
    cards = {c.last4: c for c in db.query(PaymentMethod).filter_by(user_id="USR001")}
    assert cards["4821"].brand == "Visa" and cards["4821"].is_default and cards["4821"].behaviour == "approve"
    assert cards["0019"].brand == "Mastercard" and cards["0019"].behaviour == "decline"


def test_saved_cards_are_masked_only():
    # The table must have no column that could hold a full card number or CVV.
    cols = {c.name for c in PaymentMethod.__table__.columns}
    assert not cols & {"number", "card_number", "pan", "cvv", "cvc"}
    assert {"last4", "token_ref"} <= cols
