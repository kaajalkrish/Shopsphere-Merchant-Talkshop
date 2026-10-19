"""
Initialize the SQLite database and seed it: agents, the ShopSphere merchant
and catalog (products + variants from data/shopsphere_catalog.json), and the
demo customer with saved addresses and cards.
Run directly:  python -m backend.db.init_db
"""
import json
import os
import secrets
import zlib
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from backend.auth.security import hash_password
from backend.config.agents import ALL_AGENTS
from backend.db.schema import (
    Base, Address, Agent, CardAccount, LoyaltyPoints, LoyaltyTransaction, Merchant, PaymentMethod, Product,
    ProductVariant, User, Wallet,
)

from backend.db.config import DB_PATH  # noqa: E402  (single source; overridable in tests)
from backend.shop.cards import normalize_issuer  # noqa: E402
DATA_DIR = Path(__file__).parent.parent.parent / "data"
CATALOG_FILE = DATA_DIR / "shopsphere_catalog.json"
# Colour photos live in the frontend's public folder and are served at /catalog/...
CATALOG_IMAGE_DIR = Path(__file__).parent.parent.parent / "frontend" / "public" / "catalog"
DEMO_PASSWORD = "demo1234"
DEMO_USER_ID = "USR001"
DEMO_EMAIL = "kaajal@shopsphere.demo"
STARTING_WALLET_BALANCE = 1000.0


def get_engine(db_path: Path = DB_PATH):
    return create_engine(f"sqlite:///{db_path}", echo=False)


def _ensure_column(engine, table: str, column: str, ddl: str) -> bool:
    """
    Defensive migration: Base.metadata.create_all() never ALTERs an existing
    table, so a dev DB created before a column existed won't have it without this.
    Returns True when the column was just added.
    """
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    columns = {c["name"] for c in inspector.get_columns(table)}
    if column not in columns:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
            conn.commit()
        return True
    return False


def init_db(db_path: Path = DB_PATH):
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    _ensure_column(engine, "users", "password_hash", "password_hash VARCHAR(255) NOT NULL DEFAULT ''")
    _ensure_column(engine, "users", "is_guest", "is_guest BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(engine, "orders", "tracking_number", "tracking_number VARCHAR(50)")
    _ensure_column(engine, "products", "image_url", "image_url VARCHAR(500)")
    # Demo 1: ShopSphere catalog + order detail columns on pre-existing tables
    for col, ddl in [
        ("slug", "slug VARCHAR(120)"), ("department", "department VARCHAR(30)"),
        ("subcategory", "subcategory VARCHAR(50)"), ("gender", "gender VARCHAR(10)"),
        ("description", "description TEXT"), ("tags", "tags TEXT"),
        ("is_new", "is_new BOOLEAN DEFAULT 0"), ("option_label", "option_label VARCHAR(30) DEFAULT 'Size'"),
    ]:
        _ensure_column(engine, "products", col, ddl)
    for col, ddl in [
        ("display_id", "display_id VARCHAR(20)"), ("checkout_id", "checkout_id VARCHAR(50)"),
        ("subtotal", "subtotal FLOAT"), ("tax", "tax FLOAT"), ("shipping", "shipping FLOAT"),
        ("delivery_method", "delivery_method VARCHAR(20)"), ("delivery_date", "delivery_date DATETIME"),
        ("ship_to_json", "ship_to_json TEXT"), ("payment_brand", "payment_brand VARCHAR(20)"),
        ("payment_last4", "payment_last4 VARCHAR(4)"), ("payment_status", "payment_status VARCHAR(20)"),
    ]:
        _ensure_column(engine, "orders", col, ddl)
    _ensure_column(engine, "cart_items", "sku", "sku VARCHAR(60)")
    # Phase 10: guest checkout, wallet at checkout, one-time cards, shipment tracking
    for col, ddl in [("pay_with", "pay_with VARCHAR(10) NOT NULL DEFAULT 'card'"),
                     ("guest_email", "guest_email VARCHAR(255)"), ("guest_name", "guest_name VARCHAR(100)")]:
        _ensure_column(engine, "checkouts", col, ddl)
    for col, ddl in [("payment_method_type", "payment_method_type VARCHAR(10)"),
                     ("guest_email", "guest_email VARCHAR(255)"), ("guest_name", "guest_name VARCHAR(100)"),
                     ("shipment_status", "shipment_status VARCHAR(30)"), ("shipment_events_json", "shipment_events_json TEXT")]:
        _ensure_column(engine, "orders", col, ddl)
    _ensure_column(engine, "payment_methods", "saved", "saved BOOLEAN NOT NULL DEFAULT 1")
    # Phase 11: real email delivery status
    _ensure_column(engine, "email_outbox", "delivery", "delivery VARCHAR(20) NOT NULL DEFAULT 'outbox'")
    _ensure_column(engine, "email_outbox", "delivery_error", "delivery_error VARCHAR(200)")
    # Phase 12: loyalty points on checkouts and orders
    _ensure_column(engine, "checkouts", "points_used", "points_used INTEGER NOT NULL DEFAULT 0")
    _ensure_column(engine, "checkouts", "points_discount", "points_discount FLOAT NOT NULL DEFAULT 0")
    for col in ("points_used", "points_earned"):
        _ensure_column(engine, "orders", col, f"{col} INTEGER")
    _ensure_column(engine, "orders", "points_discount", "points_discount FLOAT")
    # Part 2 (Phase 14): member offers on checkouts and orders
    _ensure_column(engine, "checkouts", "offer_id", "offer_id VARCHAR(40)")
    _ensure_column(engine, "checkouts", "offer_discount", "offer_discount FLOAT NOT NULL DEFAULT 0")
    for col, ddl in [("offer_id", "offer_id VARCHAR(40)"), ("offer_discount", "offer_discount FLOAT"),
                     ("bonus_points", "bonus_points INTEGER"), ("cashback", "cashback FLOAT"),
                     ("payment_path", "payment_path VARCHAR(20)")]:
        _ensure_column(engine, "orders", col, ddl)
    # CH-4: checkout version + benefits; consent scope on authorizations and instructions
    _ensure_column(engine, "checkouts", "version", "version INTEGER NOT NULL DEFAULT 1")
    _ensure_column(engine, "checkouts", "benefits_json", "benefits_json TEXT")
    for col, ddl in [("checkout_version", "checkout_version INTEGER"), ("action", "action VARCHAR(10)"),
                     ("consent_status", "consent_status VARCHAR(20)")]:
        _ensure_column(engine, "payment_authorizations", col, ddl)
    _ensure_column(engine, "payment_instructions", "checkout_version", "checkout_version INTEGER")
    # Secure Payment Sheet, Phase 3: tokenization on the provider's sessions
    _ensure_column(engine, "provider_payment_sessions", "verification_required", "verification_required BOOLEAN")
    _ensure_column(engine, "provider_payment_sessions", "tokenized_at", "tokenized_at DATETIME")
    # Phases 4–6: verification challenge + saved-card display metadata
    for col, ddl in [("challenge_hash", "challenge_hash VARCHAR(64)"), ("challenge_expires_at", "challenge_expires_at DATETIME"),
                     ("verification_attempts", "verification_attempts INTEGER NOT NULL DEFAULT 0"),
                     ("verified_at", "verified_at DATETIME"), ("cardholder_display", "cardholder_display VARCHAR(80)"),
                     ("card_exp_month", "card_exp_month INTEGER"), ("card_exp_year", "card_exp_year INTEGER")]:
        _ensure_column(engine, "provider_payment_sessions", col, ddl)
    # CH-6: provider handoff on checkouts
    _ensure_column(engine, "checkouts", "provider_session_id", "provider_session_id VARCHAR(64)")
    _ensure_column(engine, "checkouts", "pending_payment_ref", "pending_payment_ref VARCHAR(60)")
    # CH-3: offer eligibility rules (Chase-exclusive, card-bound, enrollment, dates)
    for col, ddl in [("requires_issuer", "requires_issuer VARCHAR(30)"),
                     ("requires_payment_with_issuer", "requires_payment_with_issuer BOOLEAN NOT NULL DEFAULT 0"),
                     ("requires_network", "requires_network VARCHAR(20)"),
                     ("requires_payment_path", "requires_payment_path VARCHAR(20)"),
                     ("requires_enrollment", "requires_enrollment BOOLEAN NOT NULL DEFAULT 0"),
                     ("starts_at", "starts_at DATETIME"), ("ends_at", "ends_at DATETIME")]:
        _ensure_column(engine, "member_offers", col, ddl)
    # CH-9: agent payments are a Visa capability flag on the card, not "is a Chase card"
    if _ensure_column(engine, "card_accounts", "agent_pay_capable", "agent_pay_capable BOOLEAN NOT NULL DEFAULT 0"):
        with engine.connect() as conn:          # older demo DBs: the Chase Visa cards that already supported it
            conn.execute(text("UPDATE card_accounts SET agent_pay_capable = 1 WHERE issuer = 'Chase' AND "
                              "network = 'Visa' AND account_type = 'credit_card'"))
            conn.commit()
    # CH-1: issuer is separate from the network on every payment method
    for col, ddl in [("issuer", "issuer VARCHAR(30)"), ("status", "status VARCHAR(20) DEFAULT 'active'"),
                     ("source", "source VARCHAR(20)")]:
        _ensure_column(engine, "payment_methods", col, ddl)
    return engine


def seed_agents(session: Session):
    for cfg in ALL_AGENTS:
        exists = session.query(Agent).filter_by(agent_id=cfg.agent_id).first()
        if not exists:
            session.add(Agent(
                agent_id=cfg.agent_id,
                agent_name=cfg.display_name,
                agent_type=cfg.role,
                trust_status=cfg.trust_status,
            ))
    session.commit()


def seed_demo_user(session: Session):
    """The Demo 1 customer, Kaajal, already logged in to ShopSphere in the spec."""
    exists = session.query(User).filter_by(user_id=DEMO_USER_ID).first()
    if not exists:
        session.add(User(
            user_id=DEMO_USER_ID,
            name="Kaajal",
            email=DEMO_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            status="active",
        ))
    else:
        if exists.email == "demo@talkshop.io":
            # Pre-Demo-1 database: rename the old demo account to Kaajal
            exists.name, exists.email = "Kaajal", DEMO_EMAIL
        if not exists.password_hash:
            # Pre-auth seed data — backfill so the demo account can actually log in
            exists.password_hash = hash_password(DEMO_PASSWORD)
    session.commit()


def seed_demo_profile(session: Session):
    """Kaajal's saved checkout details: 2 addresses and 2 cards (masked only).
    The Mastercard is set to decline so the demo can show a refused payment."""
    if not session.query(Address).filter_by(user_id=DEMO_USER_ID).first():
        session.add_all([
            Address(address_id="ADDR_HOME_USR001", user_id=DEMO_USER_ID, label="Home", full_name="Kaajal",
                    line1="48 Willow Creek Lane", line2="Apt 4B", city="Austin", state="TX",
                    postal_code="78704", country="US", is_default=True),
            Address(address_id="ADDR_WORK_USR001", user_id=DEMO_USER_ID, label="Work", full_name="Kaajal",
                    line1="210 Lavaca Street", line2="Suite 900", city="Austin", state="TX",
                    postal_code="78701", country="US", is_default=False),
        ])
    if not session.query(PaymentMethod).filter_by(user_id=DEMO_USER_ID).first():
        session.add_all([
            PaymentMethod(payment_method_id="PM_VISA_4821", user_id=DEMO_USER_ID, brand="Visa", last4="4821",
                          issuer="CHASE", source="seeded", exp_month=8, exp_year=2029, cardholder_name="Kaajal",
                          token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour="approve", is_default=True),
            PaymentMethod(payment_method_id="PM_MC_0019", user_id=DEMO_USER_ID, brand="Mastercard", last4="0019",
                          issuer="CHASE", source="seeded", exp_month=11, exp_year=2028, cardholder_name="Kaajal",
                          token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour="decline", is_default=False),
        ])
    session.commit()


def seed_demo_wallet(session: Session):
    exists = session.query(Wallet).filter_by(user_id=DEMO_USER_ID).first()
    if not exists:
        session.add(Wallet(user_id=DEMO_USER_ID, balance=STARTING_WALLET_BALANCE))
    session.commit()


def seed_demo_loyalty(session: Session):
    exists = session.query(LoyaltyPoints).filter_by(user_id=DEMO_USER_ID).first()
    if not exists:
        session.add(LoyaltyPoints(user_id=DEMO_USER_ID, balance=0, lifetime_points=0))
    session.commit()


# Part 2 (Phase 13): demo personas. Every identity, account and card here is a
# simulated fixture — no real Chase or Visa data, masked card metadata only.
PERSONAS = [
    # user_id, name, email, accounts [(account_id, type, network, last4, payment_method_id, behaviour
    #                                  [, issuer, product name])], points. Issuer defaults to Chase.
    ("USR001", "Kaajal", DEMO_EMAIL, [
        ("ACCT_A_VISA", "credit_card", "Visa", "4821", "PM_VISA_4821", "approve"),
        ("ACCT_A_MC", "credit_card", "Mastercard", "0019", "PM_MC_0019", "decline"),
    ], 12500),
    ("USR002", "Marcus", "marcus@shopsphere.demo", [
        ("ACCT_B_MC", "credit_card", "Mastercard", "5510", "PM_MC_5510", "approve"),
    ], 4200),
    ("USR003", "Priya", "priya@shopsphere.demo", [
        ("ACCT_C_CHK", "checking", None, "7702", None, None),
    ], 0),
    ("USR004", "Elena", "elena@shopsphere.demo", [
        ("ACCT_E_VISA", "credit_card", "Visa", "4002", "PM_VISA_4002", "approve"),
    ], 800),
    # CH-2: general credit-card holders. ShopSphere is for every credit-card holder;
    # Chase status only adds Chase benefits. (All simulated fixtures.)
    ("USR005", "Dana Brooks", "dana@shopsphere.demo", [            # F · Scenario 1 (CARD_002)
        ("ACCT_F_CITI_MC", "credit_card", "Mastercard", "5678", "PM_CITI_MC_5678", "approve",
         "Citi", "Citi Mastercard credit card"),
    ], 1500),
    ("USR006", "Ravi Menon", "ravi@shopsphere.demo", [             # G · Scenario 1 (CARD_003)
        ("ACCT_G_AMEX", "credit_card", "Amex", "9012", "PM_AMEX_9012", "approve",
         "American Express", "American Express Card"),
    ], 600),
    ("USR007", "Chris Park", "chris@shopsphere.demo", [             # H · Scenario 2 (CARD_001)
        ("ACCT_H_VISA", "credit_card", "Visa", "1234", "PM_CHASE_VISA_1234", "approve"),
        ("ACCT_H_CITI_MC", "credit_card", "Mastercard", "5678", "PM_H_CITI_MC_5678", "approve",
         "Citi", "Citi Mastercard credit card"),                    # to show a Chase benefit removed on a card switch
    ], 9000),
]
PERSONA_ADDRESSES = {
    "USR002": ("ADDR_HOME_USR002", "Marcus", "915 Larkspur Avenue", None, "Denver", "CO", "80203"),
    "USR004": ("ADDR_HOME_USR004", "Elena", "77 Harbor View Road", "Unit 12", "Seattle", "WA", "98109"),
    "USR005": ("ADDR_HOME_USR005", "Dana Brooks", "402 Maple Grove Drive", None, "Chicago", "IL", "60614"),
    "USR006": ("ADDR_HOME_USR006", "Ravi Menon", "18 Orchard Street", "Apt 3C", "Jersey City", "NJ", "07302"),
    "USR007": ("ADDR_HOME_USR007", "Chris Park", "250 Bayshore Boulevard", None, "San Diego", "CA", "92101"),
}


def seed_personas(session: Session):
    """Personas A–H (D, the guest, is simply not signed in). Inserts only
    what's missing, so an existing demo database keeps its history."""
    for user_id, name, email, accts, points in PERSONAS:
        if not session.query(User).filter_by(user_id=user_id).first():
            session.add(User(user_id=user_id, name=name, email=email,
                             password_hash=hash_password(DEMO_PASSWORD), status="active"))
            session.add(Wallet(user_id=user_id, balance=STARTING_WALLET_BALANCE))
        if user_id in PERSONA_ADDRESSES and not session.query(Address).filter_by(user_id=user_id).first():
            aid, full, l1, l2, city, state, zipc = PERSONA_ADDRESSES[user_id]
            session.add(Address(address_id=aid, user_id=user_id, label="Home", full_name=full, line1=l1,
                                line2=l2, city=city, state=state, postal_code=zipc, country="US", is_default=True))
        # Older databases seeded readable processor tokens (tok_demo_visa_4821):
        # replace them with random ones so no token is derived from card digits.
        for pm in session.query(PaymentMethod).filter(PaymentMethod.user_id == user_id,
                                                       PaymentMethod.token_ref.like("tok_demo_%")):
            pm.token_ref = f"tok_{secrets.token_urlsafe(18)}"
        first_seed = not session.query(CardAccount).filter_by(user_id=user_id).first()
        for i, (account_id, kind, network, last4, pm_id, behaviour, *extra) in enumerate(accts):
            issuer = extra[0] if extra else "Chase"
            if session.query(CardAccount).filter_by(account_id=account_id).first():
                continue
            if pm_id and not session.query(PaymentMethod).filter_by(payment_method_id=pm_id).first():
                session.add(PaymentMethod(payment_method_id=pm_id, user_id=user_id, brand=network, last4=last4,
                                          issuer=normalize_issuer(issuer), source="seeded", exp_month=9,
                                          exp_year=2030, cardholder_name=name,
                                          token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour=behaviour,
                                          is_default=i == 0))
            is_card = kind == "credit_card"
            product = extra[1] if len(extra) > 1 else (f"{issuer} {network} credit card" if is_card
                                                       else f"{issuer} checking account")
            session.add(CardAccount(
                account_id=account_id, user_id=user_id, issuer=issuer, account_type=kind,
                product_name=product,
                # legacy column: "eligible Chase credit card" (the source of Chase benefits), never access
                network=network, last4=last4, status="active",
                marketplace_eligible=is_card and normalize_issuer(issuer) == "CHASE",
                # CH-9 (decision Q6): in the fixtures only Chase Visa cards support agent payments
                agent_pay_capable=is_card and network == "Visa" and normalize_issuer(issuer) == "CHASE",
                payment_method_id=pm_id, credit_limit=8000.0 if is_card else None,
                available_credit=8000.0 if is_card else None))
        if first_seed and points:
            lp = session.query(LoyaltyPoints).filter_by(user_id=user_id).first()
            if not lp:
                lp = LoyaltyPoints(user_id=user_id, balance=0, lifetime_points=0)
                session.add(lp)
            lp.balance = (lp.balance or 0) + points
            lp.lifetime_points = (lp.lifetime_points or 0) + points
            session.add(LoyaltyTransaction(transaction_id=f"LTX_WELCOME_{user_id}", user_id=user_id,
                                           points_earned=points, reason="Welcome bonus (demo)"))
    session.commit()


def seed_agent_tokens(session: Session):
    """Personas A and H start enrolled (ACTIVE simulated agent token); Persona E doesn't."""
    from backend.vic.enrollment import seed_active
    for user_id, account_id in (("USR001", "ACCT_A_VISA"), ("USR007", "ACCT_H_VISA")):
        if session.query(CardAccount).filter_by(account_id=account_id).first():
            seed_active(session, user_id, account_id)


def seed_issuer_directory(session: Session):
    """The simulated Chase cardholder directory used by cardmember sign-up."""
    from backend.shop.cardholder_verification import seed
    seed(session)


def seed_card_metadata(session: Session):
    """CH-1: payment methods saved before issuer/status/source existed get them filled in."""
    from backend.shop.cards import backfill
    backfill(session)


def seed_offers(session: Session):
    from backend.shop.offers import seed
    seed(session)


def _load_catalog() -> dict:
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _slugify(value: str) -> str:
    return "-".join(value.lower().split())


def catalog_image_url(product_slug: str, color: str) -> str | None:
    """Public URL of a colour photo, or None until that photo has been added."""
    filename = f"{_slugify(color)}.webp"
    if (CATALOG_IMAGE_DIR / product_slug / filename).exists():
        return f"/catalog/{product_slug}/{filename}"
    return None


def _variant_stock(sku: str) -> int:
    # Deterministic so every reset gives the same demo stock: 3–20 units.
    return 3 + zlib.crc32(sku.encode()) % 18


def _is_out_of_stock(rules: list[dict], size: str | None, color: str) -> bool:
    return any(
        (r.get("size") in (None, size)) and (r.get("color") in (None, color))
        for r in rules
    )


def seed_merchants(session: Session):
    """ShopSphere is the only merchant in Demo 1."""
    m = _load_catalog()["merchant"]
    if not session.query(Merchant).filter_by(merchant_id=m["merchant_id"]).first():
        session.add(Merchant(**m))
    session.commit()


def seed_products(session: Session):
    """Products and their variants (SKUs) from data/shopsphere_catalog.json.
    Inserts what's missing and refreshes photo links, so re-running after new
    photos are added picks them up without a reset."""
    catalog = _load_catalog()
    merchant_id = catalog["merchant"]["merchant_id"]
    for p in catalog["products"]:
        sizes = catalog["size_ranges"][p["sizes"]]
        variants = []
        for c in p["colors"]:
            for size in sizes:
                sku = "-".join(x for x in (p["product_id"], c["name"].upper().replace(" ", ""), size) if x)
                stock = 0 if _is_out_of_stock(p["out_of_stock"], size, c["name"]) else _variant_stock(sku)
                variants.append(dict(sku=sku, size=size, color=c["name"], color_hex=c["hex"], stock=stock,
                                     image_url=catalog_image_url(p["slug"], c["name"])))
        default = variants[0]

        product = session.query(Product).filter_by(product_id=p["product_id"]).first()
        if not product:
            product = Product(
                product_id=p["product_id"], merchant_id=merchant_id, name=p["name"], brand=p["brand"],
                category=p["category"], price=p["price"], currency="USD",
                delivery_days=p["delivery_days"], rating=p["rating"], review_count=p["review_count"],
                slug=p["slug"], department=p["department"], subcategory=p["subcategory"], gender=p["gender"],
                description=p["description"], tags=",".join(p["tags"]), is_new=p["is_new"],
                option_label=p["option_label"],
                # Summary fields for pre-variant code paths
                size=None, color=default["color"], inventory=sum(v["stock"] for v in variants),
                image_url=default["image_url"],
            )
            session.add(product)
            for v in variants:
                session.add(ProductVariant(product_id=p["product_id"], **v))
        else:
            product.image_url = default["image_url"]
            for v in variants:
                row = session.query(ProductVariant).filter_by(sku=v["sku"]).first()
                if row:
                    row.image_url = v["image_url"]
                else:
                    session.add(ProductVariant(product_id=p["product_id"], **v))
    session.commit()


def run(db_path: Path = DB_PATH):
    engine = init_db(db_path)
    with Session(engine) as session:
        seed_agents(session)
        seed_demo_user(session)
        seed_demo_profile(session)
        seed_demo_wallet(session)
        seed_demo_loyalty(session)
        seed_personas(session)
        seed_offers(session)
        seed_agent_tokens(session)
        seed_issuer_directory(session)
        seed_merchants(session)
        seed_products(session)
        seed_card_metadata(session)
    print(f"Database initialized at {db_path}")


if __name__ == "__main__":
    run()
