"""
Conversational agentic checkout in Claude, end to end over the MCP endpoint (the same JSON-RPC
requests Claude sends), plus the agent communication trace.

  A  existing customer: signs in, buys with their saved address + card by saying yes
  B  guest: no account, authorized (fictional) profile for shipping, sandbox card, says yes
  C  product questions before buying: real catalog facts only, the chosen product kept
  D  the shopper declines: no payment, no order
  E  guest regression: a guest is never routed into the account sign-in
  F  the trace shows the real steps of that same transaction, with no secrets in it
"""
import json
import re

from backend.db.schema import AuditEvent, Order, PaymentAuthorization, PaymentMethod, ProtocolEvent
from backend.db.session_utils import get_session
from backend.observability import protocol_trace as pt
from backend.shop import guest_wallet
from tests.test_mcp_shop_flow import call, client, rpc, sign_in  # noqa: F401 (fixture)


def tool(client, name: str, token: str | None = None, **args) -> dict:
    """The tool's JSON, or {"error": text}; the HTTP status must be 200 (never a sign-in 401)."""
    r, msg = rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    assert r.status_code == 200, f"{name}: HTTP {r.status_code} {r.text[:200]}"
    res = msg["result"]
    text = res["content"][0]["text"] if res.get("content") else "{}"
    return {"error": text} if res.get("isError") else json.loads(text)


def orders_for(checkout_id: str) -> list[Order]:
    with get_session() as db:
        return db.query(Order).filter(Order.checkout_id == checkout_id, Order.display_id.isnot(None)).all()


def pick_running_shoe(client) -> tuple[str, dict, dict]:
    """ "I am looking for running shoes, size 10." → the first recommendation."""
    found = tool(client, "search_shopsphere", query="I am looking for running shoes, size 10")
    assert found["found"] and found["products"] and found["visitor_cart"]
    return found["visitor_cart"], found["products"][0], found


def ask_about(client, product_id: str, cart: str | None = None) -> dict:
    """ "Is the first shoe waterproof? What are its reviews?" """
    info = tool(client, "get_shopsphere_product", product_id=product_id, size="10",
                **({"visitor_cart": cart} if cart else {}))
    assert info["found"] and info["product_id"] == product_id
    return info


def guest_buys(client, *, decline: bool = False) -> dict:
    cart, shoe, _ = pick_running_shoe(client)
    info = ask_about(client, shoe["product_id"], cart)
    colour = info["available_colors"][0]
    added = tool(client, "add_to_cart", product_id=shoe["product_id"], size="10", color=colour, visitor_cart=cart)
    co = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[added["line_id"]])
    profile = tool(client, "use_guest_profile", guest_token=co["guest_token"], use_shipping=True)
    card = profile["payment_methods"][0]
    review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id=card["payment_method_id"])
    out = {"cart": cart, "shoe": shoe, "info": info, "added": added, "co": co, "profile": profile, "card": card,
           "review": review}
    if not decline:
        out["placed"] = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                             customer_reply="Yes, proceed")
    return out


# ── B: guest, the main demo ────────────────────────────────────────────────────

def test_b_guest_buys_in_a_conversation_with_an_authorized_profile_and_a_sandbox_card(client):
    run = guest_buys(client)
    co, profile, review, placed = run["co"], run["profile"], run["review"], run["placed"]
    # "Let's buy it" → guest: no sign-in, one item (the one the shopper chose), real prices
    assert co["guest"] and co["guest_token"] and [i["name"] for i in co["items"]] == [run["shoe"]["name"]]
    assert co["items"][0]["unit_price"] == run["shoe"]["price"] and run["added"]["delivery_estimate"]["standard"]
    # "I have your shipping information available…" comes from the configured profile, nothing else
    offer = co["saved_profile"]          # before consent: provider, city/state and networks only
    assert offer["available"] and offer["simulated"] and offer["address_areas"][0] == "San Francisco, CA"
    assert "San Francisco, CA" in offer["ask"] and not offer["consented"]
    assert "Jordan" not in json.dumps(offer) and "500 Market" not in json.dumps(offer) and "@" not in json.dumps(offer)
    # "Yes" → delivery date + the configured sandbox cards, network + last 4 only
    assert profile["shipping_from_profile"] and "San Francisco" in profile["ship_to"]
    assert profile["estimated_delivery"] and [p["display"] for p in profile["payment_methods"]] == [
        "Visa •••• 4242 (sandbox)", "Mastercard •••• 4444 (sandbox)"]
    assert not re.search(r"\d{12,}", json.dumps(profile)) and "tok_" not in json.dumps(profile)
    # the summary to authorize: the server's numbers
    assert review["ready"] and review["pay_with"] == "Visa •••• 4242 (sandbox)" and review["authorization"]
    assert review["total"] == round(review["subtotal"] + review["shipping"] + review["tax"], 2)
    assert review["estimated_delivery"] == profile["estimated_delivery"]
    # "Yes, proceed" → the real order
    assert placed["status"] == "authorized" and placed["order_id"].startswith("SS-")
    assert placed["total"] == review["total"] and placed["arrives"] and placed["trace_url"].endswith(
        "/trace/" + placed["trace_url"].rsplit("/", 1)[1])
    (order,) = orders_for(co["checkout_id"])
    assert order.guest_email == "jordan.lee@guest.shopsphere.demo" and order.payment_last4 == "4242"
    assert order.payment_status == "authorized" and order.amount == review["total"]
    # retried (e.g. a network hiccup): the same order, never a second charge
    again = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                 customer_reply="Yes, proceed")
    assert again["duplicate"] and again["order_id"] == placed["order_id"]
    assert len(orders_for(co["checkout_id"])) == 1
    with get_session() as db:
        auths = db.query(PaymentAuthorization).filter(
            PaymentAuthorization.order_id.like(f"{co['checkout_id']}-%")).count()
        assert auths == 1
        # the sandbox card exists only as an opaque token for this guest; never saved on an account
        pm = db.query(PaymentMethod).filter_by(payment_method_id=run["card"]["payment_method_id"]).one()
        assert pm.source == guest_wallet.SOURCE and pm.saved is False and pm.token_ref.startswith("tok_")
    # the guest tracks it like any guest (Order ID + the email used)
    tracked = tool(client, "track_order", order_id=placed["order_id"], email="jordan.lee@guest.shopsphere.demo")
    assert tracked["found"] and tracked["order"]["order_id"] == placed["order_id"]


# ── C: product questions keep the chosen product ───────────────────────────────

def test_c_product_questions_use_only_catalog_facts_and_keep_the_selection(client):
    cart, shoe, _ = pick_running_shoe(client)
    first = ask_about(client, shoe["product_id"], cart)                       # "Is it waterproof?"
    second = ask_about(client, shoe["product_id"], cart)                      # "What are its reviews?"
    assert first == second                                              # the same facts, nothing invented
    reviews = first["reviews"]
    assert reviews["average_rating"] == shoe["rating"] and reviews["review_count"] == shoe["review_count"]
    assert reviews["review_texts_available"] is False and "Don't quote or invent" in reviews["note"]
    assert "waterproof" not in (first["description"] + " ".join(first["highlights"])).lower()
    assert "doesn't list it" in first["answering_questions"]
    assert first["delivery_estimate"]["standard"] and first["delivery_estimate"]["express_price"] > 0
    added = tool(client, "add_to_cart", product_id=shoe["product_id"], size="10", color=first["available_colors"][0],
                 visitor_cart=cart)                                     # "I want the first one"
    assert added["added"] == shoe["name"] and added["unit_price"] == shoe["price"] and added["size"] == "10"
    co = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[added["line_id"]])
    assert [(i["name"], i["size"], i["quantity"]) for i in co["items"]] == [(shoe["name"], "10", 1)]


# ── D: the shopper says no ─────────────────────────────────────────────────────

def test_d_declined_authorization_creates_no_payment_and_no_order(client):
    run = guest_buys(client, decline=True)
    co, review = run["co"], run["review"]
    no = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
              customer_reply="No, not today")
    assert "isn't a clear yes" in no["error"] and "decline_order" in no["error"]
    out = tool(client, "decline_order", guest_token=co["guest_token"], reason="Not today")
    assert out == {**out, "status": "not_placed", "charged": False, "order_created": False}
    assert not orders_for(co["checkout_id"])
    with get_session() as db:
        assert not db.query(PaymentAuthorization).filter(
            PaymentAuthorization.order_id.like(f"{co['checkout_id']}-%")).count()
        assert db.query(AuditEvent).filter_by(event_type="APPROVAL_CANCELLED", order_id=co["checkout_id"]).count()


def test_d_an_authorization_only_covers_the_exact_order(client):
    run = guest_buys(client, decline=True)
    co, review = run["co"], run["review"]
    other = guest_buys(client, decline=True)
    stolen = tool(client, "place_order", guest_token=co["guest_token"], authorization=other["review"]["authorization"],
                  customer_reply="yes")
    assert "isn't valid for this order" in stolen["error"]             # another order's authorization
    tool(client, "update_guest_checkout", guest_token=co["guest_token"], delivery="express")
    changed = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                   customer_reply="yes")
    assert "changed after the shopper authorized" in changed["error"]  # the total changed: approve again
    assert not orders_for(co["checkout_id"])
    fresh = tool(client, "review_order", guest_token=co["guest_token"])
    assert fresh["delivery"] == "express" and fresh["total"] > review["total"]
    placed = tool(client, "place_order", guest_token=co["guest_token"], authorization=fresh["authorization"],
                  customer_reply="Yes, go ahead")
    assert placed["status"] == "authorized" and len(orders_for(co["checkout_id"])) == 1


# ── E: a guest is never routed into the account sign-in ────────────────────────

def test_e_guest_is_never_asked_to_sign_in(client):
    cart, shoe, _ = pick_running_shoe(client)
    colour = ask_about(client, shoe["product_id"], cart)["available_colors"][0]
    added = tool(client, "add_to_cart", product_id=shoe["product_id"], size="10", color=colour, visitor_cart=cart)
    co = tool(client, "guest_checkout", visitor_cart=cart, line_ids=[added["line_id"]])
    # Claude reaching for account tools with the guest's checkout: an answer, not a 401 sign-in
    for name, args in (("update_checkout_choices", {"checkout_id": co["checkout_id"], "delivery": "express"}),
                       ("finish_new_card_payment", {"checkout_id": co["checkout_id"]})):
        out = tool(client, name, **args)
        assert "guest_token" in out["error"] and "no sign-in" in out["error"]
    # a new card instead of a sandbox card: the secure page, not "sign in to pay with a new card"
    tool(client, "update_guest_checkout", guest_token=co["guest_token"], full_name="Pat Guest",
         email="pat@guestmail.net", line1="500 Congress Ave", city="Austin", state="TX", postal_code="78701")
    review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="provider")
    assert review["ready"] and "secure payment page" in review["pay_with"]
    page = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                customer_reply="Yes")
    assert page["status"] == "verification_required" and "/psp/hosted/" in page["secure_page"]
    assert "finish_guest_payment" in page["next_step"] and not orders_for(co["checkout_id"])
    # still: account tools without a guest checkout ask for sign-in, as before
    r, _ = rpc(client, "tools/call", {"name": "my_orders", "arguments": {}})
    assert r.status_code == 401
    r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {"visitor_cart": cart}})
    assert r.status_code == 401


# ── A: existing customer ───────────────────────────────────────────────────────

def test_a_existing_customer_buys_with_saved_details_by_saying_yes(client):
    cart, shoe, _ = pick_running_shoe(client)                           # browsing before sign-in
    colour = ask_about(client, shoe["product_id"], cart)["available_colors"][0]
    added = tool(client, "add_to_cart", product_id=shoe["product_id"], size="10", color=colour, visitor_cart=cart)
    token = sign_in(client, "chris@shopsphere.demo")
    co = call(client, "checkout", token, visitor_cart=cart, line_ids=[added["line_id"]])
    assert co["ship_to"] and co["saved_payment_methods"]
    review = tool(client, "review_order", token, checkout_id=co["checkout_id"])
    assert review["ready"] and review["authorization"] and review["ship_to"] == co["ship_to"]
    # a saved card is a real-world-style credential: the chat yes isn't enough, the customer approves
    # on ShopSphere's approval page in their own browser (simulated passkey), then Claude places it
    step = tool(client, "place_order", token, checkout_id=co["checkout_id"], authorization=review["authorization"],
                customer_reply="Yes, place it")
    assert step["status"] == "approval_required" and step["charged"] is False and not orders_for(co["checkout_id"])
    early = tool(client, "place_order", token, checkout_id=co["checkout_id"], authorization=review["authorization"],
                 customer_reply="Yes, place it")
    assert early["status"] == "approval_required"                       # still not approved: still not paid
    path = "/" + step["approval_page"].split("/", 3)[3]
    page = client.get(path)
    assert page.status_code == 200 and "Approve with passkey (simulated)" in page.text and "ShopSphere" in page.text
    assert client.post(path + "/approve").status_code == 200
    placed = tool(client, "place_order", token, checkout_id=co["checkout_id"], authorization=review["authorization"],
                  customer_reply="I approved it")
    assert placed["status"] == "authorized" and placed["order_id"].startswith("SS-")
    assert len(orders_for(co["checkout_id"])) == 1
    mine = call(client, "my_orders", token, order_id=placed["order_id"])
    assert mine["orders"][0]["order_id"] == placed["order_id"]
    # someone else's checkout can't be reviewed or paid with this customer's token
    other = guest_buys(client, decline=True)
    assert "error" in tool(client, "review_order", token, checkout_id=other["co"]["checkout_id"])
    # F (signed in): sign-in, the visitor's browsing and the payment are one trace
    trace = pt.events(placed["trace_url"].rsplit("/", 1)[1])
    ops = [e["operation"] for e in trace]
    assert "Authorization code exchanged (PKCE S256 verified)" in ops and "Bearer token verified" in ops
    assert "Product search" in ops and "Order confirmed" in ops        # before and after sign-in


# ── F: the trace of that same transaction ──────────────────────────────────────

def test_f_trace_shows_the_real_steps_in_order_without_secrets(client):
    run = guest_buys(client)
    placed = run["placed"]
    tid = placed["trace_url"].rsplit("/", 1)[1]
    assert re.fullmatch(r"tr_[0-9a-f]{24}", tid)
    data = client.get(f"/trace/{tid}/events").json()
    events = data["events"]
    ops = [e["operation"] for e in events]
    want = ["tools/call search_shopsphere", "Product search", "tools/call get_shopsphere_product",
            "Product details (clarification)", "tools/call add_to_cart", "Add to cart", "Guest checkout created",
            "Guest checkout token issued", "Profile sharing consent", "Profile provider response: contact + shipping",
            "Payment instruments listed (opaque references)", "Payment method selected (opaque reference)", "Order re-verified before authorization",
            "Purchase authorization requested", "Customer purchase authorization", "Consent scope",
            "Cardholder approval", "Gateway request sent", "Order confirmed", "Confirmation email"]
    at = [next(i for i, op in enumerate(ops) if op == w or op.startswith(w)) for w in want]
    assert at == sorted(at), list(zip(want, at))                         # chronological, as it happened
    assert any(op.startswith("DPAT check 12/12") for op in ops)
    for e in events:                                                     # every row is complete
        assert e["at"] and e["source"] and e["destination"] and e["protocol"] and e["operation"]
        assert e["status"] in ("success", "failure", "pending", "info")
    mcp = [e for e in events if e["operation"].startswith("tools/call")]
    assert mcp and all(e["protocol"] == pt.MCP and e["source"] == pt.CLAUDE for e in mcp)
    assert any(e["correlation"] == placed["order_id"] for e in events)
    assert any(e["layer"] == "simulated" for e in events)
    # honest labels: no A2A anywhere, protocol patterns qualified
    assert not any("A2A" in (e["protocol"] + (e["pattern"] or "")) for e in events)
    assert all(re.search(r"style|inspired|concept|Simulated|SIMULATED|orchestration|Security|Idempotency|rules|"
                         r"Internal|Order updates|profile|tokens|PaymentGatewayAdapter|Fictional|ShopSphere", e["pattern"])
               for e in events if e["pattern"])
    facts = {p["name"]: p["status"] for p in data["protocols"]}
    assert facts["A2A"] == "Not implemented" and facts["MCP"] == "Implemented"
    # no secrets: tokens, card numbers, full emails, street address
    blob = json.dumps(events)
    assert run["co"]["guest_token"] not in blob and run["review"]["authorization"] not in blob
    assert "eyJ" not in blob and not re.search(r"tok_[A-Za-z0-9_-]{8,}", blob) and not re.search(r"\d{12,}", blob)
    assert "jordan.lee@" not in blob and "500 Market" not in blob and run["cart"] not in blob
    # the page itself
    page = client.get(f"/trace/{tid}")
    assert page.status_code == 200 and "Agent communication" in page.text and tid in page.text
    assert client.get("/trace/tr_nothere").status_code == 404
    assert client.get("/trace/../etc").status_code == 404


def test_f_trace_failures_are_recorded_too(client):
    run = guest_buys(client, decline=True)
    tool(client, "place_order", guest_token=run["co"]["guest_token"], authorization="forged", customer_reply="yes")
    tid = pt.trace_id_for(mcp_visitor(run))
    rows = pt.events(tid)
    failed = [e for e in rows if e["status"] == "failure"]
    assert {"tools/call place_order", "Purchase authorization refused"} <= {e["operation"] for e in failed}
    assert any(e["operation"] == "Customer declined authorization" for e in rows) is False
    tool(client, "decline_order", guest_token=run["co"]["guest_token"])
    assert any(e["operation"] == "Customer declined authorization" for e in pt.events(tid))
    with get_session() as db:
        assert db.query(ProtocolEvent).filter_by(trace_id=tid).count() == len(pt.events(tid))


def mcp_visitor(run: dict) -> str:
    from backend.mcp.auth import read_guest_token
    return read_guest_token(run["co"]["guest_token"])[0]
