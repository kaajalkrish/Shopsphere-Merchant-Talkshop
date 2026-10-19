"""
Demo 1, Phase 3 — Talkshop orchestrator. Scripted conversations through
POST /api/talkshop/turn with the LLM replaced by fakes, so these test
Talkshop's own logic: stages, tool calls, events, and the consent gate.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.db.schema import Order, PaymentMethod
from backend.main import app
from backend.talkshop import brain
from backend.talkshop.state import ALLOWED, Stage
from tests.conftest import add_card

REAL_DECIDE = brain.decide          # kept before the autouse fake replaces it
REAL_RECOMMEND = brain.recommend
SPEC_REQUEST = "I need running shoes under $150 for everyday running."
VISA = {"number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123", "cardholder_name": "Test"}
HOME = {"full_name": "Test Shopper", "line1": "1 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"}


@pytest.fixture
def client():
    return TestClient(app)
@pytest.fixture
def auth_headers(member_headers):
    """Part 2: the marketplace is for eligible cardmembers only."""
    return member_headers



@pytest.fixture
def shopper(client, auth_headers):
    client.post("/api/me/addresses", json=HOME, headers=auth_headers)
    add_card(client, auth_headers, VISA)
    return auth_headers


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Deterministic stand-ins for the two LLM calls."""
    calls = {"decide": []}

    async def decide(message, *, stage, allowed, context, history):
        calls["decide"].append({"message": message, "stage": stage, "allowed": allowed})
        m = message.lower()
        if "flat feet" in m:
            return {"action": "answer", "args": {}, "reply": "It has cushioned, breathable support for everyday runs."}
        if "take this" in m:
            return {"action": "select_product", "args": {"product_index": None}, "reply": ""}
        if "gucci" in m:
            return {"action": "search", "args": {"query": "shoes", "category": "shoes", "brand": "Gucci"}, "reply": ""}
        if "running" in m:
            return {"action": "search", "args": {"query": "everyday running", "category": "running_shoes",
                                                 "max_price": 150.0}, "reply": ""}
        return {"action": "answer", "args": {}, "reply": "Happy to help!"}

    async def recommend(request, products):
        return {"intro": "I found three options for you:", "reasons": brain.fallback_reasons(products)}

    monkeypatch.setattr(brain, "decide", decide)
    monkeypatch.setattr(brain, "recommend", recommend)
    return calls


def turn(client, headers, sid, *, text=None, action=None, page=None):
    r = client.post("/api/talkshop/turn", json={"session_id": sid, "text": text, "action": action, "page": page},
                    headers=headers)
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for block in r.text.split("\n\n") for line in block.splitlines()
              if line.startswith("data: ")]
    assert events[-1]["type"] == "done"
    return events


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def stage(events):
    return events[-1]["stage"]


def orders_for(checkout_id):
    with Session(get_engine()) as s:
        return s.query(Order).filter(Order.order_id.like(f"{checkout_id}%")).count()


def _to_review(client, h, sid):
    """Drive the spec conversation up to the review card; returns the checkout."""
    turn(client, h, sid, text=SPEC_REQUEST)
    turn(client, h, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, h, sid, text="Size 8.")
    turn(client, h, sid, text="Black.")
    ev = turn(client, h, sid, text="Yes.")
    return of(ev, "checkout_ready")[0]["checkout"]


# ── The spec's happy path ────────────────────────────────────────────────────

def test_spec_conversation_end_to_end(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, action={"type": "greet"}, page={"type": "home"})
    assert of(ev, "message")[0]["text"].startswith("Hi ") and of(ev, "suggestions")

    ev = turn(client, shopper, sid, text=SPEC_REQUEST)
    recs = of(ev, "recommendations")[0]["products"]
    assert [p["name"] for p in recs] == ["Runner Pro X", "FlexRun 5", "Daily Runner"]
    assert all(p["reason"] for p in recs) and stage(ev) == "RECOMMENDED"
    assert not of(ev, "ask_option")                       # no size/colour questions before searching

    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    ask = of(ev, "ask_option")[0]
    assert ask["option"] == "size" and {"value": "11", "available": False} in ask["choices"]
    assert stage(ev) == "ASK_SIZE"

    ev = turn(client, shopper, sid, text="Size 8.")
    assert "Size 8 is available" in of(ev, "message")[0]["text"]
    assert of(ev, "ask_option")[0]["option"] == "color" and stage(ev) == "ASK_COLOR"

    ev = turn(client, shopper, sid, text="Black.")
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP001-BLACK-8"
    cart = of(ev, "cart_updated")[0]["cart"]
    assert any(l["sku"] == "SSP001-BLACK-8" for l in cart["lines"])
    assert of(ev, "offer_checkout") and stage(ev) == "OFFER_CHECKOUT"

    ev = turn(client, shopper, sid, text="Yes.")
    co = of(ev, "checkout_ready")[0]["checkout"]
    # Part 2: member pricing (15% running-shoe offer before tax)
    assert (co["subtotal"], co["tax"], co["total"]) == (129.0, 10.64, 139.64)  # CH-3: card typed in (issuer unknown) → no Chase offer; Demo 1 spec amounts
    assert stage(ev) == "AWAITING_CONSENT"

    ev = turn(client, shopper, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    states = [e["state"] for e in of(ev, "payment_status")]
    assert states == ["processing", "authorizing", "authorized"]
    order = of(ev, "order_confirmed")[0]["order"]
    assert order["order_id"].startswith("SS-") and order["total"] == 139.64
    assert stage(ev) == "ORDER_CONFIRMED"

    agents = [e["agent"] for e in of(ev, "status")]       # presenter trace
    assert agents[:2] == ["GreenLight", "PayIt"] and "TrackIt" in agents


def test_answer_both_options_at_once(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, text="the second one")     # FlexRun 5
    ev = turn(client, shopper, sid, text="9 in orange")
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP002-ORANGE-9"


# ── The consent gate ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed", ["go ahead", "GO AHEAD!", "yes", "pay now", "place order", "confirm"])
def test_typing_never_pays(client, shopper, typed, fake_llm):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    ev = turn(client, shopper, sid, text=typed)
    assert not of(ev, "payment_status") and not of(ev, "order_confirmed")
    assert "tap Approve and pay" in of(ev, "message")[0]["text"] and of(ev, "checkout_ready")
    assert stage(ev) == "AWAITING_CONSENT" and orders_for(co["checkout_id"]) == 0
    assert all(c["message"] != typed for c in fake_llm["decide"])   # never even reached the LLM


def test_go_ahead_for_a_different_checkout_is_refused(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    ev = turn(client, shopper, sid, action={"type": "go_ahead", "checkout_id": "CHK_SOMEONE_ELSE"})
    assert not of(ev, "payment_status") and orders_for(co["checkout_id"]) == 0


def test_llm_cannot_choose_payment(monkeypatch):
    """Even if the model answers with a payment action, the real decide() rejects it."""
    import asyncio

    class FakeLLM:
        async def ainvoke(self, _):
            return type("R", (), {"content": '{"action": "go_ahead", "args": {}, "reply": ""}'})()

    monkeypatch.setattr(brain, "_llm", lambda *a, **k: FakeLLM())
    allowed = sorted(ALLOWED[Stage.AWAITING_CONSENT])
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(REAL_DECIDE("pay it", stage="AWAITING_CONSENT", allowed=allowed,
                                                  context={}, history=[]))
    finally:
        loop.close()
    assert "go_ahead" not in allowed and out["action"] in allowed and out["action"] != "go_ahead"


# ── Alternative paths ────────────────────────────────────────────────────────

def test_out_of_stock_size_stays_on_size_question(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    ev = turn(client, shopper, sid, text="size 11")
    assert "out of stock" in of(ev, "message")[0]["text"] and stage(ev) == "ASK_SIZE"


def test_declined_card_then_switch_and_pay(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    with Session(get_engine()) as s:
        s.query(PaymentMethod).filter_by(payment_method_id=co["payment_method"]["payment_method_id"]) \
            .update({"behaviour": "decline"})
        s.commit()
    ev = turn(client, shopper, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert of(ev, "payment_status")[-1]["state"] == "declined"
    assert "No order was created" in of(ev, "message")[-1]["text"]
    assert stage(ev) == "AWAITING_CONSENT" and orders_for(co["checkout_id"]) == 0
    good = add_card(client, shopper, {**VISA, "number": "5555 5555 5555 4444"}).json()
    ev = turn(client, shopper, sid, action={"type": "update_checkout", "payment_method_id": good["payment_method_id"]})
    assert of(ev, "checkout_updated")[0]["checkout"]["payment_method"]["display"] == "Mastercard •••• 4444"
    ev = turn(client, shopper, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert of(ev, "order_confirmed")


def test_express_delivery_from_review_card(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    ev = turn(client, shopper, sid, action={"type": "update_checkout", "delivery_method": "express"})
    assert of(ev, "checkout_updated")[0]["checkout"]["total"] == round(co["total"] + 9.99, 2)


def test_cancel_at_review(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    ev = turn(client, shopper, sid, action={"type": "cancel_checkout"})
    assert "nothing was charged" in of(ev, "message")[0]["text"] and stage(ev) == "IN_CART"
    assert orders_for(co["checkout_id"]) == 0


def test_keep_shopping_instead_of_checkout(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, shopper, sid, text="8")
    turn(client, shopper, sid, text="white")
    ev = turn(client, shopper, sid, text="no thanks")
    assert "saved in your ShopSphere cart" in of(ev, "message")[0]["text"] and stage(ev) == "IN_CART"


def test_question_mid_flow_keeps_the_stage(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, shopper, sid, text="8")
    ev = turn(client, shopper, sid, text="Is it good for flat feet?")
    assert "cushioned" in of(ev, "message")[0]["text"] and stage(ev) == "ASK_COLOR"


def test_brand_shopsphere_does_not_carry(client, shopper):
    ev = turn(client, shopper, f"t-{uuid.uuid4().hex[:8]}", text="Show me Gucci shoes")
    assert "doesn't carry Gucci" in of(ev, "message")[0]["text"] and of(ev, "recommendations")


def test_ask_about_from_product_page_skips_search(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, action={"type": "ask_about", "product_id": "SSP002"},
              page={"type": "product", "product_id": "SSP002"})
    assert of(ev, "product_selected")[0]["from_page"] and of(ev, "ask_option")[0]["option"] == "size"


def test_single_option_product_skips_questions(client, shopper):
    ev = turn(client, shopper, f"t-{uuid.uuid4().hex[:8]}", action={"type": "select", "product_id": "SSP049"})
    assert not of(ev, "ask_option")                       # AirPods: one size, one colour
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP049-WHITE" and of(ev, "offer_checkout")


def test_checkout_covers_only_this_conversations_items(client, shopper):
    other = client.post("/api/cart/lines", json={"sku": "SSP059-BLACK"}, headers=shopper).json()["added_line_id"]
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    assert [l["sku"] for l in co["lines"]] == ["SSP001-BLACK-8"]
    assert any(l["line_id"] == other for l in client.get("/api/cart/lines", headers=shopper).json()["lines"])


def test_session_endpoint_rebuilds_the_panel(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    snap = client.get(f"/api/talkshop/sessions/{sid}", headers=shopper).json()
    assert snap["stage"] == "RECOMMENDED" and any(e["type"] == "recommendations" for e in snap["transcript"])
    assert not any(e["type"] == "status" for e in snap["transcript"])
    client.delete(f"/api/talkshop/sessions/{sid}", headers=shopper)
    assert client.get(f"/api/talkshop/sessions/{sid}", headers=shopper).json()["transcript"] == []


def test_talkshop_requires_login(client):
    assert client.post("/api/talkshop/turn", json={"session_id": "abcd", "text": "hi"}).status_code == 401


def test_pasted_photo_becomes_a_search(client, shopper, monkeypatch):
    async def describe(image, note=""):
        return {"query": "leather crossbody bag", "category": "bags", "color": "Red"}
    monkeypatch.setattr(brain, "describe_image", describe)
    r = client.post("/api/talkshop/turn", json={"session_id": f"t-{uuid.uuid4().hex[:8]}", "image_base64": "data:image/jpeg;base64,AAAA"},
                    headers=shopper)
    events = [json.loads(l[6:]) for b in r.text.split("\n\n") for l in b.splitlines() if l.startswith("data: ")]
    recs = of(events, "recommendations")[0]["products"]
    assert recs[0]["name"] == "Leather Crossbody Bag"


def test_unreadable_photo_asks_for_words(client, shopper, monkeypatch):
    async def describe(image, note=""):
        return None
    monkeypatch.setattr(brain, "describe_image", describe)
    r = client.post("/api/talkshop/turn", json={"session_id": f"t-{uuid.uuid4().hex[:8]}", "image_base64": "AAAA"}, headers=shopper)
    assert "couldn't make out a product" in r.text


def test_item_already_in_cart_buys_only_what_the_chat_added(client, shopper):
    """Cart already holds 1× Runner Pro X 8 black; Talkshop adds another.
    The chat's order is for 1 pair, and the other pair stays in the cart."""
    client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-8"}, headers=shopper)
    sid = f"t-{uuid.uuid4().hex[:8]}"
    co = _to_review(client, shopper, sid)
    assert co["lines"][0]["quantity"] == 1 and co["total"] == 139.64  # CH-3: card typed in (issuer unknown) → no Chase offer; Demo 1 spec amounts
    ev = turn(client, shopper, sid, action={"type": "go_ahead", "checkout_id": co["checkout_id"]})
    assert of(ev, "order_confirmed")[0]["order"]["total"] == 139.64
    left = [l for l in client.get("/api/cart/lines", headers=shopper).json()["lines"] if l["sku"] == "SSP001-BLACK-8"]
    assert len(left) == 1 and left[0]["quantity"] == 1


def test_keep_shopping_then_a_second_item_checks_out_both(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, shopper, sid, text="8")
    turn(client, shopper, sid, text="black")
    turn(client, shopper, sid, action={"type": "keep_shopping", "label": "Keep shopping"})
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP049"})
    ev = turn(client, shopper, sid, action={"type": "checkout", "label": "Checkout now"})
    co = of(ev, "checkout_ready")[0]["checkout"]
    assert sorted(l["sku"] for l in co["lines"]) == ["SSP001-BLACK-8", "SSP049-WHITE"]


def test_items_added_as_a_visitor_survive_sign_in_when_the_cart_merges(client):
    """The account's cart already holds the AirPods the visitor adds: sign-in
    merges the two lines, and the chat's checkout still includes them."""
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    client.post("/api/auth/register", json={"name": "Merge Shopper", "email": email, "password": "pytest1234"})
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": "pytest1234"}
                                                  ).json()["access_token"]}
    client.post("/api/me/addresses", json=HOME, headers=h)
    add_card(client, h, VISA)
    client.post("/api/cart/lines", json={"sku": "SSP049-WHITE"}, headers=h)

    vh = {"Authorization": "Bearer " + client.post("/api/auth/visitor").json()["access_token"]}
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, vh, sid, action={"type": "select", "product_id": "SSP049"})
    turn(client, vh, sid, action={"type": "keep_shopping", "label": "Keep shopping"})
    turn(client, vh, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, vh, sid, text="8")
    turn(client, vh, sid, text="black")
    r = client.post("/api/auth/login", json={"email": email, "password": "pytest1234"}, headers=vh)
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    ev = turn(client, h, sid, action={"type": "checkout", "label": "Checkout now"})
    co = (of(ev, "checkout_ready") or [{}])[0].get("checkout") or of(ev, "checkout_details_needed")[0]
    lines = co["lines"] if "lines" in co else client.get(f"/api/checkouts/{co['checkout_id']}", headers=h).json()["lines"]
    assert {l["sku"]: l["quantity"] for l in lines} == {"SSP001-BLACK-8": 1, "SSP049-WHITE": 1}


def test_cart_already_holds_all_stock_is_not_a_dead_end(client, shopper):
    from backend.db.schema import ProductVariant
    with Session(get_engine()) as s:
        stock = s.query(ProductVariant).filter_by(sku="SSP001-BLACK-8").one().stock
    client.post("/api/cart/lines", json={"sku": "SSP001-BLACK-8", "quantity": stock}, headers=shopper)
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    turn(client, shopper, sid, text="8")
    ev = turn(client, shopper, sid, text="black")
    assert "already holds every pair" in " ".join(m["text"] for m in of(ev, "message"))
    assert of(ev, "ask_option")[-1]["option"] == "color" and stage(ev) == "ASK_COLOR"



def test_greeting_fits_the_page(client, shopper):
    ev = turn(client, shopper, f"t-{uuid.uuid4().hex[:8]}", action={"type": "greet"}, page={"type": "category", "department": "women"})
    assert "women's styles" in of(ev, "message")[0]["text"] and "A summer dress" in of(ev, "suggestions")[0]["chips"]
    ev = turn(client, shopper, f"t-{uuid.uuid4().hex[:8]}", action={"type": "greet"}, page={"type": "product", "product_id": "SSP002"})
    sug = of(ev, "suggestions")[0]
    assert "FlexRun 5" in of(ev, "message")[0]["text"]
    chip = sug["chips"][0]
    assert sug["chip_actions"][chip] == {"type": "ask_about", "product_id": "SSP002"}


def test_this_one_on_a_product_page_selects_it(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, text="I'll take this one", page={"type": "product", "product_id": "SSP002"})
    assert of(ev, "product_selected")[0]["product"]["product_id"] == "SSP002" and stage(ev) == "ASK_SIZE"


# ── Size/colour named in the request are remembered ──────────────────────────

def test_size_and_colour_from_the_request_are_not_asked_again(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, text="wait I need teal colour nike shoes of size 10")
    pegasus = next(p for p in of(ev, "recommendations")[0]["products"] if p["product_id"] == "SSP004")
    teal = next(c for c in pegasus["colors"] if c["name"] == "Teal")
    assert pegasus["image_url"] == teal["image_url"]          # the card shows the colour asked for
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP004"})
    assert not of(ev, "ask_option")
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP004-TEAL-10"
    assert stage(ev) == "OFFER_CHECKOUT"


def test_size_from_the_request_then_asks_only_colour(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text="I need running shoes under $150, size 8")
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    assert "I've set size 8, as you asked." in of(ev, "message")[0]["text"]
    assert [a["option"] for a in of(ev, "ask_option")] == ["color"] and stage(ev) == "ASK_COLOR"
    ev = turn(client, shopper, sid, text="Black")
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP001-BLACK-8"


def test_unavailable_requested_size_is_explained_and_asked(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text="I need running shoes under $150 in size 11")
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    assert "Size 11 isn't available in this one." in of(ev, "message")[0]["text"]
    assert of(ev, "ask_option")[0]["option"] == "size" and stage(ev) == "ASK_SIZE"


def test_a_different_kind_of_product_forgets_the_size(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text="teal nike shoes of size 10")
    turn(client, shopper, sid, text="show me wallets")
    turn(client, shopper, sid, text=SPEC_REQUEST)              # running shoes again, no size this time
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    assert of(ev, "ask_option")[0]["option"] == "size"


@pytest.mark.parametrize("text,size", [
    ("wait I need teal colour nike shoes of size 10", "10"), ("running shoes size 8.5", "8.5"),
    ("a tee in size medium", "m"), ("uk 9 sneakers", "9"), ("what size is it", None),
    ("black shoes for 2 people", None), ("running shoes under $150", None),
])
def test_size_named_in_a_request(text, size):
    from backend.talkshop.parse import asked_size
    assert asked_size(text) == size


def test_new_request_mid_question_searches_instead_of_answering(client, shopper):
    """The reported case: on Runner Pro X's size question, "…nike shoes of size 10"
    is a new search, not size 10 for Runner Pro X."""
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    ev = turn(client, shopper, sid, text="wait I need teal colour nike shoes of size 10")
    assert not of(ev, "variant_confirmed") and stage(ev) == "RECOMMENDED"
    assert "SSP004" in [p["product_id"] for p in of(ev, "recommendations")[0]["products"]]
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP004"})
    assert not of(ev, "ask_option") and of(ev, "variant_confirmed")[0]["sku"] == "SSP004-TEAL-10"


@pytest.mark.parametrize("text", ["size 10", "10 in black", "Black.", "the second one", "I want size 9"])
def test_plain_answers_are_not_new_requests(text):
    from backend.talkshop.parse import is_new_request
    assert not is_new_request(text)


def test_typo_in_colour_is_understood(client, shopper):
    """The reported case: "tale" means Teal (here with the LLM faked out, so the parser's fallback)."""
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text="no can I get nike tale colour size 10 shoes")
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP004"})
    assert not of(ev, "ask_option") and of(ev, "variant_confirmed")[0]["sku"] == "SSP004-TEAL-10"


def test_colour_the_llm_understood_is_used(client, shopper, monkeypatch):
    async def recommend(request, products):
        return {"intro": "Here you go:", "reasons": brain.fallback_reasons(products),
                "wants": {"color": "Teal"} if "sea green" in request else {}}
    monkeypatch.setattr(brain, "recommend", recommend)
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text="I want nike shoes in a sea green shade, size 10")
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP004"})
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP004-TEAL-10"


def test_llm_cannot_invent_a_colour(monkeypatch):
    """recommend() keeps only colours these products really come in."""
    import asyncio

    class FakeLLM:
        def __init__(self, color):
            self.color = color

        async def ainvoke(self, _):
            return type("R", (), {"content": json.dumps({"intro": "Hi", "reasons": {},
                                                         "wants": {"color": self.color, "size": "10"}})})()

    products = [{"product_id": "P1", "name": "Shoe", "brand": "B", "rating": 4.0, "review_count": 1, "tags": ["x"],
                 "description": "d", "sizes": ["10"], "colors": [{"name": "Teal"}, {"name": "Coral"}]}]
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(brain, "_llm", lambda *a, **k: FakeLLM("teal"))
        assert loop.run_until_complete(REAL_RECOMMEND("tale shoes", products))["wants"] == {"color": "Teal", "size": "10"}
        monkeypatch.setattr(brain, "_llm", lambda *a, **k: FakeLLM("Purple"))
        assert loop.run_until_complete(REAL_RECOMMEND("purple shoes", products))["wants"] == {"size": "10"}
    finally:
        loop.close()


@pytest.mark.parametrize("text,colour", [
    ("no can I get nike tale colour size 10 shoes", "Teal"), ("teel", "Teal"), ("blak please", "Black"),
    ("burgandy", "Burgundy"), ("black", "Black"), ("while you are at it", None), ("a great deal", None),
    ("tall shoes", None), ("what about the goal", None),
])
def test_colour_typos(text, colour):
    from backend.talkshop.parse import pick_color
    assert pick_color(text, ["Coral", "Teal", "Black", "White", "Navy", "Gold", "Grey", "Burgundy"]) == colour


def test_reported_typo_request_mid_size_question(client, shopper):
    """On Runner Pro X's size question: "no can I get nike tale colour size 10 shoes"."""
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    ev = turn(client, shopper, sid, text="no can I get nike tale colour size 10 shoes")
    assert not of(ev, "variant_confirmed") and stage(ev) == "RECOMMENDED"
    ev = turn(client, shopper, sid, action={"type": "select", "product_id": "SSP004"})
    assert of(ev, "variant_confirmed")[0]["sku"] == "SSP004-TEAL-10"


def test_unclear_product_mention_mid_question_goes_to_the_llm(client, shopper, fake_llm):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)
    turn(client, shopper, sid, action={"type": "select", "product_id": "SSP001"})
    ev = turn(client, shopper, sid, text="size 10 shoes please")
    assert fake_llm["decide"][-1]["message"] == "size 10 shoes please" and not of(ev, "variant_confirmed")


# ── Things ShopSphere doesn't sell ───────────────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("no laptop tomatoes grocery", "groceries"), ("I need tomatoes", "groceries"), ("I want a sofa", "furniture"),
])
def test_not_sold_gets_a_plain_answer_and_no_cards(client, shopper, fake_llm, text, label):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    turn(client, shopper, sid, text=SPEC_REQUEST)                    # mid-conversation, cards on screen
    ev = turn(client, shopper, sid, text=text)
    assert not of(ev, "recommendations")
    msg = of(ev, "message")[0]["text"]
    assert "doesn't sell" in msg and label in msg and "clothing, shoes, accessories and electronics" in msg
    assert not any(c["message"] == text for c in fake_llm["decide"])   # answered without searching or the LLM


def test_mixed_request_searches_what_is_sold_and_says_what_isnt(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, text="show me a laptop and some tomatoes")
    recs = of(ev, "recommendations")[0]
    assert all(p["category"] == "laptops" for p in recs["products"])
    assert "doesn't sell groceries or food like tomatoes" in recs["intro"]


def test_not_a_laptop_means_not_a_laptop(client, shopper):
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, text="I don't want a laptop, show me headphones")
    assert all(p["category"] != "laptops" for p in of(ev, "recommendations")[0]["products"])


def test_llm_says_results_do_not_fit(client, shopper, monkeypatch):
    """Not on the blocker list: the LLM's relevance check stops unrelated cards."""
    async def recommend(request, products):
        return {"intro": "x", "reasons": {}, "wants": {}, "fits": False, "asked_for": "a garden hose"}
    monkeypatch.setattr(brain, "recommend", recommend)
    sid = f"t-{uuid.uuid4().hex[:8]}"
    ev = turn(client, shopper, sid, text="I need a garden hose for my shoes area")
    assert not of(ev, "recommendations") and "doesn't sell a garden hose" in of(ev, "message")[0]["text"]


def test_recommend_reads_fits_safely(monkeypatch):
    """Only an explicit false hides results; a garbled answer shows them."""
    import asyncio

    class FakeLLM:
        def __init__(self, body):
            self.body = body

        async def ainvoke(self, _):
            return type("R", (), {"content": self.body})()

    products = [{"product_id": "P1", "name": "Shoe", "brand": "B", "rating": 4.0, "review_count": 1, "tags": ["x"],
                 "description": "d", "sizes": [], "colors": [], "department": "shoes", "category": "shoes"}]
    loop = asyncio.new_event_loop()
    try:
        for body, fits in [('{"intro": "Hi", "reasons": {}, "fits": false, "asked_for": "tomatoes"}', False),
                           ('{"intro": "Hi", "reasons": {}}', True), ("not json", True)]:
            monkeypatch.setattr(brain, "_llm", lambda *a, **k: FakeLLM(body))
            assert loop.run_until_complete(REAL_RECOMMEND("x", products))["fits"] is fits
    finally:
        loop.close()


@pytest.mark.parametrize("text,blocked", [
    ("no laptop tomatoes grocery", True), ("no tomatoes, just sneakers", False), ("apple watch", False),
    ("orange running shoes", False), ("a gift under $50", False), ("I want a sofa", True),
])
def test_not_sold_detection(text, blocked):
    from backend.talkshop.parse import not_sold
    assert bool(not_sold(text)) is blocked
