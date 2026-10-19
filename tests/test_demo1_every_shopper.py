"""
Demo 1, Phase 10: every shopper can buy, and every order can be tracked.
Part 2 retired guest checkout (the marketplace is for eligible cardmembers);
the guest cases now live in test_part2_cardmember_vic.py as denial tests.

The four checkout scenarios (existing customer with a saved card or wallet,
customer without a saved card, new customer, guest), the guest's data
boundary (no account, one-time card), the confirmation email (demo outbox),
and Track Order (Order ID + email), mapped to the 15 cases in the brief.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import EmailOutbox, Order, PaymentMethod, User, Wallet
from backend.main import app
from tests.conftest import pay_with_provider, scoped_consent, add_card

HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"}
CARD = {"number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Test"}
NOT_FOUND = "We could not find an order matching the provided Order ID and email address."
SKU = "SSP002-ORANGE-9"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def restore_stock():
    """These tests buy the same shoe again and again: put its stock back after
    each one so other test modules still find it in stock."""
    from backend.db.schema import ProductVariant
    with Session(get_engine()) as db:
        before = {v.sku: v.stock for v in db.query(ProductVariant).filter(ProductVariant.product_id == "SSP002")}
    yield
    with Session(get_engine()) as db:
        for sku, stock in before.items():
            db.query(ProductVariant).filter_by(sku=sku).update({"stock": stock})
        db.commit()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def customer(client):
    """An eligible cardmember (Part 2): a new account plus a simulated Chase
    credit card, with no saved payment card yet."""
    from backend.db.session_utils import get_session
    from backend.shop.eligibility import link_demo_card
    email = f"c-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"name": "Casey Customer", "email": email, "password": "customer123"})
    assert r.status_code == 200, r.text
    with get_session() as db:
        link_demo_card(db, r.json()["user"]["user_id"], network="Visa", with_payment_method=False)
    return bearer(r.json()["access_token"]), email


def visitor(client):
    r = client.post("/api/auth/visitor")
    return bearer(r.json()["access_token"]), r.json()["user"]["user_id"]


def add_line(client, h, sku=SKU):
    r = client.post("/api/cart/lines", json={"sku": sku, "quantity": 1}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["added_line_id"]


def checkout(client, h, guest=False):
    r = client.post("/api/checkouts", json={"line_ids": [add_line(client, h)], "guest": guest}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, h, co_id):
    r = client.post(f"/api/checkouts/{co_id}/confirm", json=scoped_consent(client, h, co_id), headers=h)
    assert r.status_code == 200, r.text
    return r.json()




_TRACKER: dict = {}


def member_buys(client):
    """Part 2 stand-in for the old guest purchase: an eligible cardmember with a
    saved address and card buys one item. Returns (headers, user_id, email,
    checkout_id, confirm result)."""
    h, email = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    uid = client.get("/api/auth/me", headers=h).json()["user_id"]
    co = checkout(client, h)
    return h, uid, email, co["checkout_id"], confirm(client, h, co["checkout_id"])


def other_member(client):
    h, _ = customer(client)
    return h, client.get("/api/auth/me", headers=h).json()["user_id"]


def track(client, order_id, email, path=""):
    """Track Order is a marketplace service (Part 2): any signed-in cardmember can
    look an order up by Order ID + email."""
    if not _TRACKER:
        _TRACKER.update(customer(client)[0])
    return client.post(f"/api/orders/track{path}", json={"order_id": order_id, "email": email}, headers=_TRACKER)


# ── 1. Existing customer + saved card ────────────────────────────────────────

def test_1_customer_with_saved_card(client):
    h, email = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    co = checkout(client, h)
    assert co["ready"] and co["payment_method"]["display"] == "Visa •••• 4242"   # no card re-entry
    res = confirm(client, h, co["checkout_id"])
    order = res["order"]
    assert res["status"] == "authorized" and order["order_id"].startswith("SS-")
    assert order["payment_method_type"] == "card" and not order["guest"]
    assert res["confirmation_email"] == email
    assert any(o.get("display_id") == order["order_id"] for o in client.get("/api/orders", headers=h).json())


# ── 2. Existing customer + wallet ────────────────────────────────────────────

def test_2_customer_pays_with_wallet(client):
    """CH-2: the wallet is for every customer, and checkout is for credit-card
    holders, so the customer has a credit card on file and chooses the wallet."""
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    before = client.get("/api/wallet", headers=h).json()["balance"]
    co = checkout(client, h)
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "wallet"}, headers=h).json()
    assert co["ready"] and co["pay_with"] == "wallet" and co["payment_method"]["display"] == "ShopSphere Wallet"
    res = confirm(client, h, co["checkout_id"])
    assert res["status"] == "authorized" and res["order"]["payment"]["display"] == "ShopSphere Wallet"
    assert client.get("/api/wallet", headers=h).json()["balance"] == pytest.approx(before - co["total"])


def test_2b_wallet_too_low_is_declined_without_an_order(client):
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    me = client.get("/api/auth/me", headers=h).json()
    with Session(get_engine()) as db:
        db.query(Wallet).filter_by(user_id=me["user_id"]).update({"balance": 5.0})
        db.commit()
    co = checkout(client, h)
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"pay_with": "wallet"}, headers=h).json()
    assert not co["ready"] and any(i["code"] == "INSUFFICIENT_WALLET" for i in co["issues"])


# ── 3. Existing customer without a saved card ────────────────────────────────

def test_3_no_saved_card_uses_secure_payment_and_saves_only_if_asked(client):
    """CH-6: no saved card → a new card on the Simulated Secure Payment Provider's page;
    the card is kept (as a provider token) only when the customer ticks "save" there."""
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    co = checkout(client, h)
    assert any(i["code"] == "NO_PAYMENT_METHOD" for i in co["issues"])        # doesn't fail: asks for a card
    once = pay_with_provider(client, h, co["checkout_id"], "visa", save=False)
    assert once["status"] == "authorized" and once["saved_payment_method"] is None
    assert client.get("/api/me/payment-methods", headers=h).json() == []
    co2 = checkout(client, h)
    kept = pay_with_provider(client, h, co2["checkout_id"], "mastercard", save=True)
    assert kept["status"] == "authorized" and kept["saved_payment_method"]["source"] == "provider_token"
    assert [c["payment_method_id"] for c in client.get("/api/me/payment-methods", headers=h).json()] == \
        [kept["saved_payment_method"]["payment_method_id"]]


# ── 4. New customer (sign up) ────────────────────────────────────────────────

def test_4_new_customer_signs_up_and_buys(client):
    h, email = customer(client)
    co = checkout(client, h)
    assert {i["code"] for i in co["issues"]} >= {"NO_ADDRESS", "NO_PAYMENT_METHOD"}
    addr = client.post("/api/me/addresses", json=HOME, headers=h).json()       # the secure forms attach what they save
    card = add_card(client, h, CARD).json()
    client.patch(f"/api/checkouts/{co['checkout_id']}", headers=h,
                 json={"address_id": addr["address_id"], "payment_method_id": card["payment_method_id"]})
    res = confirm(client, h, co["checkout_id"])
    assert res["status"] == "authorized" and not res["order"]["guest"]
    assert any(o.get("display_id") == res["order"]["order_id"] for o in client.get("/api/orders", headers=h).json())


# ── 5–8. Guest checkout ──────────────────────────────────────────────────────









# ── 9–11. Tracking by Order ID + email ───────────────────────────────────────

def test_9_10_11_tracking_by_order_id_and_email(client):
    _, _, email, _, res = member_buys(client)
    order_id = res["order"]["order_id"]
    r = track(client, order_id, email.upper())                                  # email match ignores case
    assert r.status_code == 200
    view = r.json()
    assert view["order_id"] == order_id and view["shipment"]["status"] == "processing"
    assert view["ship_to"] == {"city": "Austin", "state": "TX"}                 # no street address
    assert "guest_email" not in view and "internal_order_id" not in view
    wrong_email = track(client, order_id, "someone@else.com")
    wrong_id = track(client, "SS-00000", email)
    assert wrong_email.status_code == wrong_id.status_code == 404
    assert wrong_email.json() == wrong_id.json() == {"detail": {"code": "ORDER_NOT_FOUND", "message": NOT_FOUND}}


# ── 12–13. Registered history unchanged; guest orders stay separate ─────────



# ── 14–15. Shipment lifecycle ────────────────────────────────────────────────

def test_14_15_shipment_lifecycle(client):
    _, _, email, _, res = member_buys(client)
    order_id = res["order"]["order_id"]
    view = track(client, order_id, email).json()["shipment"]
    assert view["status"] == "processing" and view["tracking_number"] is None and view["simulated"]
    assert [s["done"] for s in view["steps"]] == [True, True, True, False, False, False, False]
    view = track(client, order_id, email, "/advance").json()["shipment"]        # packed
    assert view["status"] == "packed" and view["tracking_number"] is None
    view = track(client, order_id, email, "/advance").json()["shipment"]        # shipped → tracking ID
    assert view["status"] == "shipped" and view["tracking_number"].startswith("TRK") and len(view["tracking_number"]) == 9
    assert "simulated" in view["carrier"]
    for _ in range(4):
        view = track(client, order_id, email, "/advance").json()["shipment"]
    assert view["status"] == "delivered" and all(s["done"] for s in view["steps"])
    assert track(client, order_id, "x@y.com", "/advance").status_code == 404    # demo control needs ID + email too


# ── Talkshop: the same flows inside the chat ─────────────────────────────────

import json as _json

from backend.talkshop import brain


@pytest.fixture
def quiet_llm(monkeypatch):
    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products), "wants": {}}
    monkeypatch.setattr(brain, "recommend", recommend)


def say(client, h, sid, **kw):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, **kw}, headers=h)
    assert r.status_code == 200, r.text
    return [_json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]


def first(ev, kind):
    return next(e for e in ev if e["type"] == kind)


def to_offer(client, h):
    sid = f"p10-{uuid.uuid4().hex[:8]}"
    say(client, h, sid, text="I need running shoes under $150 for everyday running.")
    say(client, h, sid, action={"type": "select", "product_id": "SSP002"})
    say(client, h, sid, text="9")
    say(client, h, sid, text="orange")
    return sid




def test_talkshop_customer_without_a_credit_card_cannot_pay_even_with_wallet(client, quiet_llm):
    """CH-2: checkout is for credit-card holders. A customer with no credit card is
    asked for one; switching to the wallet doesn't get round that (nothing charged)."""
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    sid = to_offer(client, h)
    ev = say(client, h, sid, action={"type": "checkout"})
    need = first(ev, "checkout_details_needed")
    assert need["needs"]["payment"] and need["wallet"]["balance"] > 0 and not need["guest"]
    assert "credit-card holders" in first(ev, "message")["text"]
    ev = say(client, h, sid, action={"type": "details_added", "pay_with": "wallet", "label": "Pay with wallet"})
    co = first(ev, "checkout_ready")["checkout"]
    assert co["pay_with"] == "wallet" and not co["ready"]
    assert any(i["code"] == "CREDIT_CARD_REQUIRED" for i in co["issues"])
    ev = say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert not [e for e in ev if e["type"] == "order_confirmed"]


def test_talkshop_card_holder_can_pay_with_wallet(client, quiet_llm):
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    sid = to_offer(client, h)
    co = first(say(client, h, sid, action={"type": "checkout"}), "checkout_ready")["checkout"]
    ev = say(client, h, sid, action={"type": "update_checkout", "pay_with": "wallet"})
    assert first(ev, "checkout_updated")["checkout"]["ready"]
    ev = say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert first(ev, "order_confirmed")["order"]["payment"]["display"] == "ShopSphere Wallet"


# ── Phase 11: tracking in Talkshop ───────────────────────────────────────────

from backend.talkshop import state as talk_state


def test_chat_tracks_an_order_with_id_and_email(client, quiet_llm):
    _, _, email, _, res = member_buys(client)
    order_id = res["order"]["order_id"]
    h, vid = other_member(client)                                # another member: needs ID + email
    sid = f"trk-{uuid.uuid4().hex[:8]}"
    ev = say(client, h, sid, text=f"hey i have placed an order with this order id {order_id}, show me the order details and tracking")
    form = first(ev, "track_order_form")
    assert form["order_id"] == order_id and not [e for e in ev if e["type"] == "order_tracking"]
    ev = say(client, h, sid, action={"type": "track_order", "order_id": order_id, "email": "wrong@example.com",
                                     "label": f"Track order {order_id}"})
    assert "could not find an order" in first(ev, "message")["text"] and first(ev, "track_order_form")
    ev = say(client, h, sid, action={"type": "track_order", "order_id": order_id, "email": email,
                                     "label": f"Track order {order_id}"})
    shown = first(ev, "order_tracking")["order"]
    assert shown["order_id"] == order_id and shown["shipment"]["status"] == "processing"
    assert shown["ship_to"] == {"city": "Austin", "state": "TX"}
    s = talk_state.peek(vid, sid)
    assert email not in str(s.history)                           # the email never enters the LLM history


def test_chat_tracking_with_the_email_typed_in(client, quiet_llm):
    _, _, email, _, res = member_buys(client)
    order_id = res["order"]["order_id"]
    h, vid = other_member(client)
    sid = f"trk-{uuid.uuid4().hex[:8]}"
    ev = say(client, h, sid, text=f"where is my order {order_id}? my email is {email}")
    assert first(ev, "order_tracking")["order"]["order_id"] == order_id
    assert email not in str(talk_state.peek(vid, sid).history)


def test_chat_shows_a_customers_own_order_without_email(client, quiet_llm):
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    order_id = confirm(client, h, checkout(client, h)["checkout_id"])["order"]["order_id"]
    ev = say(client, h, f"trk-{uuid.uuid4().hex[:8]}", text=f"track my order {order_id}")
    assert first(ev, "order_tracking")["order"]["order_id"] == order_id
    other, _ = customer(client)                                  # someone else's order still needs the email
    ev = say(client, other, f"trk-{uuid.uuid4().hex[:8]}", text=f"track order {order_id}")
    assert first(ev, "track_order_form") and not [e for e in ev if e["type"] == "order_tracking"]




# ── Phase 11: real email delivery (fake SMTP server) ─────────────────────────

class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("mail server unreachable")
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    from backend.shop import mailer, notifications
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USERNAME": "shop@example.com",
                 "SMTP_PASSWORD": "app-password", "SMTP_SECURITY": "starttls"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifications, "_dispatch", lambda job: job())          # send inline in tests
    FakeSMTP.sent, FakeSMTP.fail = [], False
    return FakeSMTP


def test_confirmation_email_is_really_sent(client, smtp, monkeypatch):
    from backend.shop import mailer
    monkeypatch.setattr(mailer, "deliverable", lambda a: True)               # test addresses use example.com
    _, _, email, _, res = member_buys(client)
    order_id = res["order"]["order_id"]
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == email and order_id in msg["Subject"] and "shop@example.com" in msg["From"]
    body = msg.get_content()
    assert order_id in body and "/track?order=" + order_id in body and "4242424242424242" not in body
    assert track(client, order_id, email, "/email").json()["delivery"] == "sent"


def test_email_failure_never_undoes_the_order(client, smtp, monkeypatch):
    from backend.shop import mailer
    monkeypatch.setattr(mailer, "deliverable", lambda a: True)
    smtp.fail = True
    _, _, email, _, res = member_buys(client)
    assert res["status"] == "authorized"
    mail = track(client, res["order"]["order_id"], email, "/email").json()
    assert mail["delivery"] == "failed"


# ── Automatic payment after the review countdown ─────────────────────────────

def test_automatic_payment_is_refused_and_only_a_tap_pays(client, quiet_llm):
    """CH-8: no automatic or countdown consent. A go-ahead claiming one charges nothing;
    the shopper's own tap pays, and the consent record says so."""
    import json as _j
    from backend.db.schema import AuditEvent
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    sid = to_offer(client, h)
    co = first(say(client, h, sid, action={"type": "checkout"}), "checkout_ready")["checkout"]
    ev = say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"], "consent": "auto_countdown"})
    assert not [e for e in ev if e["type"] == "order_confirmed"] and "Nothing was charged" in first(ev, "message")["text"]
    assert first(say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]}), "order_confirmed")
    with Session(get_engine()) as db:
        ev = (db.query(AuditEvent).filter(AuditEvent.event_type == "CHECKOUT_CONSENT",
                                          AuditEvent.order_id.like(f"{co['checkout_id']}-%")).one())
    assert _j.loads(ev.metadata_json)["mode"] == "button"


def test_made_up_addresses_are_not_mailed(client, smtp):
    """kaajal@shopsphere.demo, example.com… would only bounce: they stay in the outbox."""
    from backend.shop.mailer import deliverable
    assert not deliverable("kaajal@shopsphere.demo") and not deliverable("a@example.com") and deliverable("a@gmail.com")
    _, _, email, _, res = member_buys(client)                                   # member email is @example.com
    assert smtp.sent == [] and track(client, res["order"]["order_id"], email, "/email").json()["delivery"] == "outbox"


# ── Logged-in customers track their own orders without ID or email ───────────

def _buy(client, h, sku):
    line = add_line(client, h, sku)
    co = client.post("/api/checkouts", json={"line_ids": [line]}, headers=h).json()
    return confirm(client, h, co["checkout_id"])["order"]["order_id"]


def test_logged_in_customer_asks_by_product_name(client, quiet_llm):
    h, _ = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    shoes = _buy(client, h, "SSP004-TEAL-10")          # Nike Pegasus 41
    wallet = _buy(client, h, "SSP046-BLACK")            # newer order
    ev = say(client, h, f"own-{uuid.uuid4().hex[:6]}", text="hey i placed an order of nike shoes, where is it?")
    assert first(ev, "order_tracking")["order"]["order_id"] == shoes            # matched by product, not newest
    assert not [e for e in ev if e["type"] in ("track_order_form", "recommendations")]
    ev = say(client, h, f"own-{uuid.uuid4().hex[:6]}", text="where is my order?")
    assert first(ev, "order_tracking")["order"]["order_id"] == wallet           # newest when nothing named
    assert f"Track {shoes}" in first(ev, "suggestions")["chips"]                # the others offered as buttons


def test_logged_in_customer_with_no_orders(client, quiet_llm):
    h, _ = customer(client)
    ev = say(client, h, f"own-{uuid.uuid4().hex[:6]}", text="where is my order?")
    assert "haven't placed any orders" in first(ev, "message")["text"]




# ── Phase 12: loyalty points ─────────────────────────────────────────────────

from backend.db.schema import LoyaltyPoints


def _points(client, h):
    return client.get("/api/loyalty", headers=h).json()["balance"]


def _ready_customer(client):
    h, email = customer(client)
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, CARD)
    return h, email


def _give_points(client, h, n):
    me = client.get("/api/auth/me", headers=h).json()
    with Session(get_engine()) as db:
        lp = db.query(LoyaltyPoints).filter_by(user_id=me["user_id"]).first()
        if lp:
            lp.balance = n
        else:
            db.add(LoyaltyPoints(user_id=me["user_id"], balance=n, lifetime_points=n))
        db.commit()


def test_customers_earn_points(client):
    h, _ = _ready_customer(client)
    co = checkout(client, h)
    assert co["points"]["balance"] == 0 and co["earn_points"] == int(co["total"])
    res = confirm(client, h, co["checkout_id"])
    assert res["order"]["points"]["earned"] == int(co["total"]) == _points(client, h)


def test_redeem_points_discount_tax_and_balance(client):
    h, _ = _ready_customer(client)
    _give_points(client, h, 2500)                                   # $25.00 worth
    co = checkout(client, h)                                       # FlexRun 5, $139.00
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"points_used": 2500}, headers=h).json()
    assert co["points"]["used"] == 2500 and co["points_discount"] == 25.0
    off = co["offer_discount"]                                     # Part 2 member offer, before points and tax
    assert co["tax"] == round((139.0 - off - 25.0) * 0.0825, 2) and co["total"] == round(139.0 - off - 25.0 + co["tax"], 2)
    res = confirm(client, h, co["checkout_id"])                    # payment checks pass on the discounted total
    assert res["status"] == "authorized" and res["order"]["total"] == co["total"]
    assert res["order"]["points"] == {"used": 2500, "discount": 25.0, "earned": int(co["total"]), "bonus": 0}
    assert _points(client, h) == 2500 - 2500 + int(co["total"])


def test_points_are_capped_and_kept_on_decline(client):
    h, _ = _ready_customer(client)
    _give_points(client, h, 99999)
    co = checkout(client, h)
    co = client.patch(f"/api/checkouts/{co['checkout_id']}", json={"points_used": 99999}, headers=h).json()
    cap = round((139.0 - co["offer_discount"] - 1.0) * 100)      # $1 of the products stays payable
    assert co["points"]["used"] == cap and co["points_discount"] == cap / 100
    me = client.get("/api/auth/me", headers=h).json()
    with Session(get_engine()) as db:                               # the card will be declined
        db.query(PaymentMethod).filter_by(user_id=me["user_id"]).update({"behaviour": "decline"})
        db.commit()
    assert confirm(client, h, co["checkout_id"])["status"] == "declined"
    assert _points(client, h) == 99999                             # nothing spent without an authorized payment




def test_talkshop_points_question_and_review_toggle(client, quiet_llm):
    h, _ = _ready_customer(client)
    _give_points(client, h, 1415)
    ev = say(client, h, f"pts-{uuid.uuid4().hex[:6]}", text="how many points do I have?")
    assert "1,415 ShopSphere points, worth $14.15" in first(ev, "message")["text"]
    sid = to_offer(client, h)
    co = first(say(client, h, sid, action={"type": "checkout"}), "checkout_ready")["checkout"]
    ev = say(client, h, sid, action={"type": "update_checkout", "points_used": 1415})
    upd = first(ev, "checkout_updated")["checkout"]
    assert upd["points_discount"] == 14.15 and upd["total"] < co["total"]
    vh, _ = visitor(client)                                      # visitors can chat; points come with an account
    ev = say(client, vh, f"pts-{uuid.uuid4().hex[:6]}", text="my loyalty points?")
    assert "points are for every ShopSphere customer" in first(ev, "message")["text"]


# ── Live guardrail trace (info view) ─────────────────────────────────────────

def _guards(ev):
    return [e for e in ev if e["type"] == "guardrail"]


def test_trace_covers_every_step_of_a_purchase(client, quiet_llm):
    h, _ = _ready_customer(client)
    sid = to_offer(client, h)
    co = first(say(client, h, sid, action={"type": "checkout"}), "checkout_ready")["checkout"]
    ev = say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    g = _guards(ev)
    payit = [x for x in g if x["agent"] == "PayIt" and x["check"].startswith("DPAT check ")]
    assert len(payit) == 12 and all(x["result"] == "pass" for x in payit)
    names = [(x["agent"], x["check"]) for x in g]
    for want in [("GreenLight", "Consent gate"), ("GreenLight", "Spending policy"), ("GreenLight", "DPAT issued"),
                 ("PayIt", "Gateway request sent"), ("Mock gateway / PSP", "Mock gateway / PSP"),
                 ("Simulated Visa network", "Simulated Visa network"), ("Simulated card issuer", "Simulated card issuer"),
                 ("TrackIt", "Order confirmed"), ("TrackIt", "Confirmation email")]:
        assert want in names, want
    assert "Approve & Pay tapped" in next(x for x in g if x["check"] == "Consent gate")["detail"]
    assert all(x["protocol"] for x in g)                                  # every check is tagged with its protocol


def test_trace_shows_a_decline_and_no_secrets(client, quiet_llm):
    h, email = _ready_customer(client)
    me = client.get("/api/auth/me", headers=h).json()
    with Session(get_engine()) as db:
        db.query(PaymentMethod).filter_by(user_id=me["user_id"]).update({"behaviour": "decline"})
        db.commit()
    sid = to_offer(client, h)
    co = first(say(client, h, sid, action={"type": "checkout"}), "checkout_ready")["checkout"]
    ev = say(client, h, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    issuer = next(x for x in _guards(ev) if x["check"] == "Simulated card issuer")
    assert issuer["result"] == "blocked" and "declined" in issuer["detail"] and issuer["layer"] == "simulated"
    assert not [e for e in ev if e["type"] == "order_confirmed"]
    ev2 = say(client, h, sid, text=f"my card is 4242 4242 4242 4242 and email {email}")
    blob = str(_guards(ev) + _guards(ev2))
    assert "4242424242424242" not in blob and "4242 4242 4242 4242" not in blob and email not in blob
    assert any(x["check"] == "Sensitive-data mask" and x["result"] == "blocked" for x in _guards(ev2))


def test_guest_checkout_is_retired_in_part2(client):
    """Part 2: a visitor can't create a checkout, guest or not."""
    h, _ = visitor(client)
    r = client.post("/api/checkouts", json={"line_ids": ["x"], "guest": True}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LOGIN_REQUIRED"

