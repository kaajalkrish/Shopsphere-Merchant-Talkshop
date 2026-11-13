"""
Conversational agentic commerce, end to end over the MCP endpoint (the JSON-RPC requests Claude
sends): discovery and questions, cart changes in words, discount discovery without checkout, the
guest's authorized profile (consent, isolation, fallback, an external provider), the single-use
purchase authorization (yes / no / maybe, expired, reused, superseded, tampered, changed), the
customer's own approval for real-world-style credentials, exactly one order, email and tracking,
and no secrets in anything the model or the trace sees.
"""
import json
import re
import time

import pytest

from backend.db.schema import (Checkout, EmailOutbox, Order, PaymentAuthorization, PaymentMethod, ProfileGrant,
                               PurchaseConsent, User)
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth
from backend.observability import protocol_trace as pt
from backend.shop import agent_profile, purchase_consent
from tests.test_mcp_conversational_checkout import guest_buys, mcp_visitor, orders_for, tool
from tests.test_mcp_shop_flow import call, client, rpc, sign_in  # noqa: F401 (fixture)

@pytest.fixture(autouse=True, scope="module")
def _keep_stock():
    """These tests place many real orders; put the catalog's stock back afterwards so later test
    files (which buy fixed variants) see the stock they expect."""
    from backend.db.schema import ProductVariant
    with get_session() as db:
        before = {v.sku: v.stock for v in db.query(ProductVariant)}
    yield
    with get_session() as db:
        for v in db.query(ProductVariant):
            if v.sku in before and v.stock != before[v.sku]:
                v.stock = before[v.sku]
        db.commit()


SECRETS = re.compile(r"\d{12,}|tok_[A-Za-z0-9_-]{6,}|\bcvc\b|\bcvv\b|card_number|eyJ[A-Za-z0-9_-]{10,}\.", re.I)


def no_secrets(*results) -> None:
    """Nothing the model sees holds a card number, CVV, raw token or a JWT other than the opaque
    capability fields (guest_token / authorization) the tools hand out on purpose."""
    for r in results:
        visible = {k: v for k, v in r.items() if k not in ("guest_token", "authorization")} if isinstance(r, dict) else r
        blob = json.dumps(visible)
        assert not SECRETS.search(blob), SECRETS.search(blob).group(0)


def counts() -> tuple[int, int, int]:
    with get_session() as db:
        return db.query(Checkout).count(), db.query(User).count(), db.query(PaymentAuthorization).count()


def start_guest(client, product="SSP001", size="10", color="Black"):
    found = tool(client, "search_shopsphere", query="running shoes", size=size)
    cart = found["visitor_cart"]
    added = tool(client, "add_to_cart", product_id=product, size=size, color=color, visitor_cart=cart)
    co = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[added["line_id"]])
    return cart, added, co


# ── 1. discovery, questions, comparison, variant ───────────────────────────────

def test_1_search_questions_comparison_and_variant_selection(client):
    found = tool(client, "search_shopsphere", query="Show me running shoes under $150 in size 10")
    assert found["found"] and found["size"] == "10"
    assert all(p["price"] <= 150 for p in found["products"]) and all("10" in p["available_sizes"]
                                                                   for p in found["products"])
    cart = found["visitor_cart"]
    first, second = found["products"][0], found["products"][1]
    a = tool(client, "get_shopsphere_product", product_id=first["product_id"], size="10", visitor_cart=cart)
    b = tool(client, "get_shopsphere_product", product_id=second["product_id"], size="10", visitor_cart=cart)
    for info, p in ((a, first), (b, second)):                       # "compare the first two": real facts only
        assert info["price"] == p["price"] and info["reviews"]["review_texts_available"] is False
        assert info["reviews"]["review_count"] == p["review_count"]
    black = next((c["name"] for c in a["all_colors"] if c["name"].lower() == "black" and c["in_stock"]), None)
    colour = black or a["available_colors"][0]                       # "does it come in black?"
    added = tool(client, "add_to_cart", product_id=first["product_id"], size="10", color=colour, visitor_cart=cart)
    assert added["color"] == colour and added["size"] == "10" and added["unit_price"] == first["price"]
    missing = tool(client, "add_to_cart", product_id=first["product_id"], visitor_cart=cart)
    assert "Which size" in missing["error"]                          # ask only for what's missing


# ── 2. cart changes in words ───────────────────────────────────────────────────

def test_2_cart_changes_through_natural_language(client):
    found = tool(client, "search_shopsphere", query="running shoes", size="10")
    cart = found["visitor_cart"]
    a = tool(client, "add_to_cart", product_id="SSP001", size="10", color="Black", visitor_cart=cart)
    b = tool(client, "add_to_cart", product_id="SSP049", color="White", visitor_cart=cart)
    two = tool(client, "update_cart_item", line_id=a["line_id"], quantity=2, visitor_cart=cart)   # "make it two"
    line = next(i for i in two["items"] if i["product_id"] == "SSP001")
    assert line["quantity"] == 2 and two["subtotal"] == round(2 * a["unit_price"] + b["unit_price"], 2)
    info = tool(client, "get_shopsphere_product", product_id="SSP001", size="10")
    other = next(c["name"] for c in info["all_colors"] if c["in_stock"] and c["name"] != "Black")
    swapped = tool(client, "update_cart_item", line_id=a["line_id"], color=other, visitor_cart=cart)  # "change to …"
    line = next(i for i in swapped["items"] if i["product_id"] == "SSP001")
    assert line["color"] == other and line["quantity"] == 2 and swapped["line_id"] == line["line_id"]
    gone = tool(client, "update_cart_item", line_id=b["line_id"], quantity=0, visitor_cart=cart)      # "remove it"
    assert [i["product_id"] for i in gone["items"]] == ["SSP001"]
    bad = tool(client, "update_cart_item", line_id="CART_NOPE", quantity=1, visitor_cart=cart)
    assert "isn't in this cart" in bad["error"]
    # "buy just the first item": only that line goes to checkout
    keep = tool(client, "add_to_cart", product_id="SSP049", color="White", visitor_cart=cart)
    co = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[line["line_id"]])
    assert [i["name"] for i in co["items"]] == [line["name"]] and keep["line_id"]


# ── 3 + 4. discounts without checkout; Chase-only eligibility told honestly ─────

def test_3_discount_questions_need_no_signup_checkout_or_personal_data(client):
    found = tool(client, "search_shopsphere", query="running shoes", size="10")
    cart = found["visitor_cart"]
    tool(client, "add_to_cart", product_id="SSP001", size="10", color="Black", visitor_cart=cart)
    before = counts()
    offers = tool(client, "get_available_offers", product_id="SSP001", visitor_cart=cart)   # HTTP 200, no 401
    preview = tool(client, "preview_cart_discounts", visitor_cart=cart)
    store = tool(client, "get_available_offers")
    assert counts() == before                                         # no checkout, no user, no payment
    run15 = next(o for o in offers["offers"] if o["offer_id"] == "OFF_RUN15")
    price = found["products"][0]["price"] if found["products"][0]["product_id"] == "SSP001" else \
        tool(client, "get_shopsphere_product", product_id="SSP001")["price"]
    assert run15["type"] == "conditional" and run15["status"] == "not_verified"
    assert run15["estimate"]["amount"] == round(price * 0.15, 2) and run15["guest_checkout_eligible"] is False
    assert any("Chase credit card" in c for c in run15["conditions"])
    assert offers["eligibility_verified"] is False and "verified" in offers["next_step"]
    assert not any(o["offer_id"] == "OFF_AUDIO20" for o in offers["offers"])          # not about shoes
    assert preview["subtotal"] == price and preview["best_discount_estimate"]["offer_id"] == "OFF_RUN15"
    assert preview["best_discount_estimate"]["verified"] is False and "Guest checkout" in preview["guest_checkout_note"]
    assert {o["offer_id"] for o in store["offers"]} >= {"OFF_RUN15", "OFF_AUDIO20", "OFF_CHASE2X"}
    assert all(o["estimate"] is None for o in store["offers"])
    no_secrets(offers, preview, store)


def test_4_chase_offer_verified_only_for_an_eligible_cardholder_and_recalculated_at_checkout(client):
    chris = sign_in(client, "chris@shopsphere.demo")                  # Chase Visa •••• 1234 + a Citi card
    dana = sign_in(client, "dana@shopsphere.demo")                    # Citi only
    yes = tool(client, "get_available_offers", chris, product_id="SSP001")
    no = tool(client, "get_available_offers", dana, product_id="SSP001")
    run15 = next(o for o in yes["offers"] if o["offer_id"] == "OFF_RUN15")
    assert run15["type"] == "verified" and "1234" in run15["pay_with"] and yes["eligibility_verified"]
    nope = next(o for o in no["offers"] if o["offer_id"] == "OFF_RUN15")
    assert nope["type"] == "not_eligible" and nope["reason"] == "NOT_CHASE_CARDHOLDER" and nope["estimate"]["amount"] == 0
    # the estimate is what checkout really gives with that card, and switching card removes it
    call(client, "add_to_cart", chris, product_id="SSP001", size="10", color="Black")
    est = tool(client, "preview_cart_discounts", chris)["best_discount_estimate"]
    co = call(client, "checkout", chris, payment_method_id="PM_CHASE_VISA_1234")
    assert co["offer_discount"] == est["amount"] and est["verified"]
    citi = tool(client, "review_order", chris, checkout_id=co["checkout_id"], payment_method_id="PM_H_CITI_MC_5678")
    assert not citi["discounts"] and citi["total"] > co["total"]


# ── 5 + 6. guest without OAuth; consent before the profile is used ─────────────

def test_5_6_guest_needs_no_oauth_and_the_profile_waits_for_consent(client):
    cart, added, co = start_guest(client)
    vid = mcp_visitor({"co": co})
    assert co["ship_to"] is None and co["saved_profile"]["consented"] is False   # nothing applied yet
    with get_session() as db:                                         # no sandbox token, no grant yet
        assert not db.query(PaymentMethod).filter_by(user_id=vid).count()
        assert not db.query(ProfileGrant).filter_by(subject=vid).count()
    early = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="4242")
    assert "authorized profile first" in early["error"]
    used = tool(client, "use_guest_profile", guest_token=co["guest_token"], use_shipping=True)
    assert used["address_used"]["label"] == "Home" and "San Francisco" in used["ship_to"]
    assert used["profile_provider"] == {"provider_id": "demo", "name": "Demo agent profile (fictional, sandbox)",
                                        "simulated": True}
    with get_session() as db:
        g = db.query(ProfileGrant).filter_by(subject=vid).one()
        assert set(g.scopes.split(",")) == {"contact", "shipping", "payment_instruments"}
    # the next checkout in the same chat doesn't ask again
    again = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[added["line_id"]])
    assert again["saved_profile"]["consented"] and "without asking again" in again["next_step"]
    no_secrets(co, used, again)


# ── 7. one guest session never sees another's profile choices or tokens ────────

def test_7_profile_data_is_isolated_between_guest_sessions(client):
    _, _, a = start_guest(client)
    work = tool(client, "use_guest_profile", guest_token=a["guest_token"], address_id="work", remember=True)
    assert work["address_used"]["label"] == "Work" and work["remembered"] == {"address_id": "work"}
    _, _, b = start_guest(client)
    assert b["saved_profile"]["consented"] is False                   # A's consent isn't B's
    home = tool(client, "use_guest_profile", guest_token=b["guest_token"])
    assert home["address_used"]["label"] == "Home"                    # A's remembered choice stays with A
    a_cards = {p["payment_method_id"] for p in work["payment_methods"]}
    b_cards = {p["payment_method_id"] for p in home["payment_methods"]}
    assert a_cards.isdisjoint(b_cards)                                # tokens per guest
    stolen = tool(client, "review_order", guest_token=b["guest_token"], payment_method_id=sorted(a_cards)[0])
    assert "isn't one of this guest's options" in stolen["error"]
    review_a = tool(client, "review_order", guest_token=a["guest_token"], payment_method_id="4242")
    cross = tool(client, "place_order", guest_token=b["guest_token"], authorization=review_a["authorization"],
                 customer_reply="yes")
    assert "isn't valid for this order" in cross["error"]
    bad = tool(client, "use_guest_profile", guest_token=b["guest_token"], address_id="someone-elses")
    assert "isn't in the shopper's profile" in bad["error"]


# ── 8. no provider: the safe fallback; an external provider through the same interface ──

def test_8_fallback_when_no_profile_provider_is_available(client, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_PROVIDER", "external")          # selected, but nothing registered
    _, _, co = start_guest(client)
    assert co["saved_profile"]["available"] is False and "none is configured" in co["saved_profile"]["reason"]
    assert "secure payment page" in co["next_step"]
    out = tool(client, "use_guest_profile", guest_token=co["guest_token"])
    assert "update_guest_checkout" in out["error"]
    tool(client, "update_guest_checkout", guest_token=co["guest_token"], full_name="Pat Guest",
         email="pat@guestmail.net", line1="500 Congress Ave", city="Austin", state="TX", postal_code="78701")
    review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="provider")
    assert review["ready"] and "provider's page" in review["approval_method"]
    page = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                customer_reply="Yes, proceed")
    assert page["status"] == "verification_required" and not orders_for(co["checkout_id"])
    monkeypatch.setenv("AGENT_PROFILE_PROVIDER", "off")
    assert tool(client, "guest_checkout", visitor_cart=start_guest(client)[0])["saved_profile"]["available"] is False


class FakeWallet:
    """Stands in for a real provider client in tests only (no such provider is configured by default)."""
    name = "Test wallet provider"

    def preview(self, subject):
        return {"areas": ["Portland, OR"], "payment_networks": ["Visa"]}

    def contact(self, subject):
        return {"full_name": "Sam Rivera", "email": "sam@wallet.example"}

    def addresses(self, subject):
        return [{"address_id": "a1", "label": "Home", "full_name": "Sam Rivera", "line1": "1 Pine St",
                 "line2": None, "city": "Portland", "state": "OR", "postal_code": "97204"}]

    def instruments(self, subject):
        return [{"reference": f"wallet-ref-{subject}", "network": "Visa", "last4": "1111"}]


def test_8b_an_external_provider_plugs_into_the_same_interface_and_needs_customer_approval(client, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_PROVIDER", "external")
    agent_profile.register_external_client(FakeWallet())
    try:
        _, _, co = start_guest(client)
        assert co["saved_profile"]["provider"] == "Test wallet provider" and co["saved_profile"]["simulated"] is False
        used = tool(client, "use_guest_profile", guest_token=co["guest_token"])
        assert "Portland" in used["ship_to"] and used["payment_methods"][0]["display"] == "Visa •••• 1111"
        assert "wallet-ref" not in json.dumps(used)                   # the provider reference never comes back
        review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="1111")
        step = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                    customer_reply="yes please")
        assert step["status"] == "approval_required"                  # not a sandbox card: the customer approves
        path = "/" + step["approval_page"].split("/", 3)[3]
        assert client.post(path + "/approve").status_code == 200
        placed = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                      customer_reply="done, approved")
        assert placed["status"] == "authorized" and len(orders_for(co["checkout_id"])) == 1
        with get_session() as db:                                     # only a keyed hash of the reference is kept
            pm = db.query(PaymentMethod).filter_by(user_id=mcp_visitor({"co": co}), source="agent_provider").one()
            assert pm.token_ref.startswith("tok_ext_") and "wallet-ref" not in pm.token_ref
        ops = [e["operation"] for e in pt.events(pt.trace_id_for(mcp_visitor({"co": co})))]
        assert "Customer approved the order" in ops
    finally:
        agent_profile.register_external_client(None)


def test_8c_demo_trace_never_claims_an_external_wallet_was_contacted(client):
    run = guest_buys_reply(client)
    rows = pt.events(run["placed"]["trace_url"].rsplit("/", 1)[1])
    prof = [e for e in rows if e["operation"].startswith("Profile provider response")]
    assert prof and all(e["source"] == "Demo agent profile (fictional, sandbox)" and e["layer"] == "simulated"
                        and "no external wallet contacted" in e["response"] for e in prof)
    assert not any("Claude wallet" in json.dumps(e) for e in rows)


# ── 9-11. sandbox card choice; yes, no, maybe, a question ──────────────────────

def guest_buys_reply(client, card="4444", reply="Yes, proceed") -> dict:
    _, _, co = start_guest(client)
    used = tool(client, "use_guest_profile", guest_token=co["guest_token"])
    review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id=card)
    placed = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                  customer_reply=reply)
    return {"co": co, "used": used, "review": review, "placed": placed}


def test_9_10_choose_a_sandbox_card_by_its_last_digits_and_say_yes(client):
    run = guest_buys_reply(client, card="4444")
    review, placed = run["review"], run["placed"]
    assert review["pay_with"] == "Mastercard •••• 4444 (sandbox)" and review["merchant"] == "ShopSphere"
    assert "Sandbox card" in review["approval_method"]
    for k in ("items", "subtotal", "shipping", "tax", "total", "estimated_delivery", "ship_to", "pay_with"):
        assert review[k] not in (None, "", []), k                     # the complete summary
    assert placed["status"] == "authorized" and placed["total"] == review["total"]
    with get_session() as db:
        row = db.query(PurchaseConsent).filter_by(checkout_id=run["co"]["checkout_id"]).one()
        assert row.status == "used" and row.level == "sandbox_chat" and json.loads(row.evidence)["reply"] == "yes"
    no_secrets(run["used"], review, placed)


@pytest.mark.parametrize("reply", ["no", "No thanks", "maybe", "hmm, not sure", "What's the return policy?",
                                   "yes but use express", "", "ok wait"])
def test_11_anything_but_a_clear_yes_places_nothing(client, reply):
    _, _, co = start_guest(client)
    tool(client, "use_guest_profile", guest_token=co["guest_token"])
    review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="4242")
    out = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
               customer_reply=reply)
    assert "isn't a clear yes" in out["error"] and not orders_for(co["checkout_id"])
    # the authorization survives a question: a clear yes afterwards still works, once
    ok = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
              customer_reply="Yes, go ahead")
    assert ok["status"] == "authorized" and len(orders_for(co["checkout_id"])) == 1


def test_11b_reply_classifier():
    c = purchase_consent.classify_reply
    for y in ("Yes", "yes, proceed", "Yep go ahead", "Sure, place the order", "I authorize it", "OK do it", "approved"):
        assert c(y) == "yes", y
    for n in ("No", "nope", "don't", "cancel it", "stop", "not now", "No, maybe later"):
        assert c(n) in ("no", "unclear"), n
    for u in ("maybe", "Is it waterproof?", "yes?", "hmm", "", "yes but change the card", "x" * 400):
        assert c(u) == "unclear", u


# ── 12 + 13. authorizations that must not work ─────────────────────────────────

def test_12_forged_expired_reused_superseded_tampered_and_declined_authorizations_are_refused(client):
    _, _, co = start_guest(client)
    g = co["guest_token"]
    tool(client, "use_guest_profile", guest_token=g)
    first = tool(client, "review_order", guest_token=g, payment_method_id="4242")
    assert "isn't valid" in tool(client, "place_order", guest_token=g, authorization="forged",
                                 customer_reply="yes")["error"]
    second = tool(client, "review_order", guest_token=g, payment_method_id="4242")      # a newer summary
    assert "newer summary replaced" in tool(client, "place_order", guest_token=g, authorization=first["authorization"],
                                            customer_reply="yes")["error"]
    claims = mcp_auth.read_purchase_authorization(second["authorization"])
    tampered = mcp_auth._issue("purchase_auth", 900, **{**{k: v for k, v in claims.items()
                                                           if k not in ("typ", "iat", "exp")}, "amt": 1})
    assert "changed after the shopper authorized" in tool(client, "place_order", guest_token=g, authorization=tampered,
                                                          customer_reply="yes")["error"]
    with get_session() as db:                                          # 15 minutes pass
        db.query(PurchaseConsent).filter_by(consent_id=claims["jti"]).update({"expires_at": time.time() - 1})
        db.commit()
    assert "expired" in tool(client, "place_order", guest_token=g, authorization=second["authorization"],
                             customer_reply="yes")["error"]
    third = tool(client, "review_order", guest_token=g, payment_method_id="4242")
    tool(client, "decline_order", guest_token=g, reason="changed my mind")
    assert "declined" in tool(client, "place_order", guest_token=g, authorization=third["authorization"],
                              customer_reply="yes")["error"]
    assert not orders_for(co["checkout_id"])
    # reuse: a consumed authorization can't be claimed again
    fourth = tool(client, "review_order", guest_token=g, payment_method_id="4242")
    with get_session() as db:
        row = purchase_consent.find(db, mcp_auth.read_purchase_authorization(fourth["authorization"])["jti"])
        purchase_consent.consume(db, row)
        with pytest.raises(purchase_consent.ConsentError) as e:
            purchase_consent.consume(db, row)
        assert e.value.code == "AUTH_USED"
    assert "already used" in tool(client, "place_order", guest_token=g, authorization=fourth["authorization"],
                                  customer_reply="yes")["error"]
    assert not orders_for(co["checkout_id"])


def test_13_a_new_card_or_amount_needs_a_fresh_authorization(client):
    _, _, co = start_guest(client)
    g = co["guest_token"]
    tool(client, "use_guest_profile", guest_token=g)
    visa = tool(client, "review_order", guest_token=g, payment_method_id="4242")
    tool(client, "update_guest_checkout", guest_token=g, delivery="express")     # the total changes
    assert "changed after" in tool(client, "place_order", guest_token=g, authorization=visa["authorization"],
                                   customer_reply="yes")["error"]
    mc = tool(client, "review_order", guest_token=g, payment_method_id="4444")   # another card
    assert mc["total"] > visa["total"] and mc["authorization"] != visa["authorization"]
    placed = tool(client, "place_order", guest_token=g, authorization=mc["authorization"], customer_reply="yes")
    assert placed["status"] == "authorized" and "4444" in placed["paid_with"]


# ── 14 + 15. one order; email; tracking ────────────────────────────────────────

def test_14_15_exactly_one_order_then_email_and_guest_tracking(client):
    run = guest_buys_reply(client, card="4242")
    co, placed, review = run["co"], run["placed"], run["review"]
    again = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                 customer_reply="yes")
    assert again["duplicate"] and again["order_id"] == placed["order_id"]
    (order,) = orders_for(co["checkout_id"])
    with get_session() as db:
        assert db.query(PaymentAuthorization).filter(
            PaymentAuthorization.order_id.like(f"{co['checkout_id']}-%")).count() == 1
        mail = db.query(EmailOutbox).filter_by(order_id=order.order_id).one()
        assert mail.to_email == "jordan.lee@guest.shopsphere.demo"
    assert order.user_id.startswith("VIS") and order.guest_email                # a guest order, no account
    assert placed["confirmation_sent_to"].startswith("j***@") and placed["email_delivery"] == "outbox"
    assert "not delivered" in placed["email_note"]                     # never claims delivery without a provider
    tracked = tool(client, "track_order", order_id=placed["order_id"], email="jordan.lee@guest.shopsphere.demo")
    assert tracked["found"] and tracked["order"]["order_id"] == placed["order_id"]
    assert tracked["order"]["ship_to"] == {"city": "San Francisco", "state": "CA"}
    wrong = tool(client, "track_order", order_id=placed["order_id"], email="someone@else.example")
    assert wrong["found"] is False
    ops = [e["operation"] for e in pt.events(placed["trace_url"].rsplit("/", 1)[1])]
    assert {"Order confirmation email", "Order tracking lookup", "Customer purchase authorization"} <= set(ops)


# ── 16. nothing secret in model-visible results or the trace ───────────────────

def test_16_no_card_numbers_tokens_or_secrets_reach_the_model_or_the_trace(client):
    run = guest_buys_reply(client)
    no_secrets(run["co"], run["used"], run["review"], run["placed"])
    rows = pt.events(run["placed"]["trace_url"].rsplit("/", 1)[1])
    blob = json.dumps(rows)
    assert not SECRETS.search(blob) and "jordan.lee@" not in blob and "500 Market" not in blob
    assert run["co"]["guest_token"] not in blob and run["review"]["authorization"] not in blob


# ── customer step-up page ──────────────────────────────────────────────────────

def test_customer_approval_page_decline_and_changed_order(client):
    token = sign_in(client, "chris@shopsphere.demo")
    call(client, "add_to_cart", token, product_id="SSP001", size="10", color="Black")
    co = call(client, "checkout", token)
    review = tool(client, "review_order", token, checkout_id=co["checkout_id"])
    step = tool(client, "place_order", token, checkout_id=co["checkout_id"], authorization=review["authorization"],
                customer_reply="yes")
    path = "/" + step["approval_page"].split("/", 3)[3]
    assert client.get("/approve/not-a-token").status_code == 404
    tool(client, "review_order", token, checkout_id=co["checkout_id"], delivery="express")   # order changes
    page = client.get(path)
    assert "newer summary" in page.text and "Approve with passkey" not in page.text
    fresh = tool(client, "review_order", token, checkout_id=co["checkout_id"])
    step2 = tool(client, "place_order", token, checkout_id=co["checkout_id"], authorization=fresh["authorization"],
                 customer_reply="yes")
    path2 = "/" + step2["approval_page"].split("/", 3)[3]
    assert client.post(path2 + "/decline").status_code == 200
    assert "declined" in tool(client, "place_order", token, checkout_id=co["checkout_id"],
                              authorization=fresh["authorization"], customer_reply="yes")["error"]
    assert not orders_for(co["checkout_id"])


# ── tool surface ───────────────────────────────────────────────────────────────

def test_new_tools_are_model_visible_read_only_where_they_should_be(client):
    _, msg = rpc(client, "tools/list")
    tools = {t["name"]: t for t in msg["result"]["tools"]}
    for name in ("get_available_offers", "preview_cart_discounts", "update_cart_item"):
        assert name in tools and tools[name]["annotations"]["readOnlyHint"] is True
    assert "customer_reply" in tools["place_order"]["inputSchema"]["required"]
    assert "customer_confirmed" not in tools["place_order"]["inputSchema"]["properties"]
    assert tools["card_place_guest_order"]["annotations"]["destructiveHint"] is True
    vis = (tools["card_place_guest_order"].get("_meta") or {}).get("ui", {}).get("visibility")
    assert vis == ["app"]                                             # the model can't press the card's button
    assert Order and ProfileGrant                                     # (imported for the schema)
