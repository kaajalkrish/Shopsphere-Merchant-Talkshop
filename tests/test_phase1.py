"""
Phase 1 — Basic smoke tests.
Covers: DB init, seeding, API endpoints, Pydantic models, agent config.
Run: pytest tests/test_phase1.py -v
"""
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_db(tmp_path_factory):
    """Isolated SQLite DB for tests — never touches the real commerce.db."""
    db_path = tmp_path_factory.mktemp("db") / "test_commerce.db"
    from backend.db.init_db import init_db, get_engine, seed_agents, seed_demo_user, seed_merchants, seed_products
    engine = init_db(db_path)
    with Session(engine) as session:
        seed_agents(session)
        seed_demo_user(session)
        seed_merchants(session)
        seed_products(session)
    return db_path


@pytest.fixture(scope="module")
def client(tmp_db, monkeypatch_session):
    """FastAPI test client pointed at the temp DB."""
    from backend.db import init_db as init_module
    monkeypatch_session.setattr(init_module, "DB_PATH", tmp_db)
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def monkeypatch_session():
    """Module-scoped monkeypatch (pytest monkeypatch is function-scoped by default)."""
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


# ── 1. Agent config ───────────────────────────────────────────────────────────

def test_agent_roster_has_six_agents():
    from backend.config.agents import ALL_AGENTS
    assert len(ALL_AGENTS) == 6


def test_agent_names_are_correct():
    from backend.config.agents import ALL_AGENTS
    names = [a.display_name for a in ALL_AGENTS]
    assert names == ["VibeCheck", "SneakPeek", "CartUp", "GreenLight", "PayIt", "TrackIt"]


def test_agent_ids_are_unique():
    from backend.config.agents import ALL_AGENTS
    ids = [a.agent_id for a in ALL_AGENTS]
    assert len(ids) == len(set(ids))


# ── 2. Database init & seeding ────────────────────────────────────────────────

def test_db_creates_all_tables(tmp_db):
    from sqlalchemy import create_engine, inspect
    engine = create_engine(f"sqlite:///{tmp_db}")
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    expected = {
        "users", "agents", "merchants", "products",
        "orders", "payment_authorizations", "delegated_tokens", "audit_events"
    }
    assert expected.issubset(set(tables))


def test_db_seeds_shopsphere_as_only_merchant(tmp_db):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from backend.db.schema import Merchant
    engine = create_engine(f"sqlite:///{tmp_db}")
    with Session(engine) as s:
        count = s.query(Merchant).count()
    assert count == 1


def test_db_seeds_66_products(tmp_db):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from backend.db.schema import Product
    engine = create_engine(f"sqlite:///{tmp_db}")
    with Session(engine) as s:
        count = s.query(Product).count()
    assert count == 66


def test_db_seeds_six_agents(tmp_db):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from backend.db.schema import Agent
    engine = create_engine(f"sqlite:///{tmp_db}")
    with Session(engine) as s:
        count = s.query(Agent).count()
    assert count == 6


# ── 3. API endpoints ──────────────────────────────────────────────────────────

def test_health_endpoint(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_merchants_returns_shopsphere(client):
    r = client.get("/api/merchants")
    assert r.status_code == 200
    data = r.json()
    assert [m["merchant_id"] for m in data] == ["SHOPSPHERE"]


def test_products_returns_66(client, member_headers):
    r = client.get("/api/products", headers=member_headers)
    assert r.status_code == 200
    assert len(r.json()) == 66


def test_products_filter_by_category(client, member_headers):
    r = client.get("/api/products?category=running_shoes", headers=member_headers)
    assert r.status_code == 200
    products = r.json()
    assert len(products) == 9                       # 7 adult + 2 kids' (Junior Runner, Trail Scout)
    assert all(p["category"] == "running_shoes" for p in products)


def test_products_filter_by_merchant(client, member_headers):
    r = client.get("/api/products?merchant_id=SHOPSPHERE", headers=member_headers)
    assert r.status_code == 200
    products = r.json()
    assert len(products) == 66
    assert all(p["merchant_id"] == "SHOPSPHERE" for p in products)


def test_product_get_by_id(client, member_headers):
    r = client.get("/api/products/SSP001", headers=member_headers)
    assert r.status_code == 200
    p = r.json()
    assert p["product_id"] == "SSP001"
    assert p["name"] == "Runner Pro X"
    assert p["price"] == 129.00


def test_product_not_found_returns_404(client, member_headers):
    r = client.get("/api/products/DOES_NOT_EXIST", headers=member_headers)
    assert r.status_code == 404


def test_merchant_not_found_returns_404(client):
    r = client.get("/api/merchants/DOES_NOT_EXIST")
    assert r.status_code == 404


# ── 4. Pydantic models ────────────────────────────────────────────────────────

def test_shopping_intent_requires_category():
    from pydantic import ValidationError
    from backend.models.intent import ShoppingIntent
    with pytest.raises(ValidationError):
        ShoppingIntent()  # no category


def test_shopping_intent_valid():
    from backend.models.intent import ShoppingIntent
    intent = ShoppingIntent(
        category="running_shoes",
        size="10",
        max_price=100.0,
        preferences=["lightweight"],
    )
    assert intent.category == "running_shoes"
    assert intent.currency == "USD"


def test_normalized_product_available_flag():
    from backend.models.product import NormalizedProduct
    p = NormalizedProduct(
        merchant_id="SHOPSPHERE",
        merchant_name="ShopSphere",
        product_id="SSP001",
        title="Nike Pegasus",
        category="running_shoes",
        price=109.0,
        inventory=5,
    )
    assert p.available is True


def test_dpat_token_defaults_single_use():
    from datetime import datetime, timedelta
    from backend.models.payment import DPATToken
    token = DPATToken(
        token_id="DPAT_TEST",
        authorization_id="AUTH001",
        customer_id="USR001",
        agent_id="GREENLIGHT_001",
        merchant_id="SHOPSPHERE",
        order_id="ORD001",
        max_amount=117.99,
        checkout_hash="abc123",
        expires_at=datetime.utcnow() + timedelta(minutes=15),
    )
    assert token.single_use is True
    assert token.status == "active"
