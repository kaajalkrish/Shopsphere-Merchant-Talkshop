"""
The three ways to buy at ShopSphere from Claude, end to end over the MCP HTTP endpoint (the same
JSON-RPC requests Claude sends), ShopSphere's OAuth pages and the payment provider's hosted page:

  1. an existing customer signs in at checkout (saved address + card, simulated passkey)
  2. a new customer creates an account at checkout (secure new-card page, optional save)
  3. a guest buys with no account (details on the card, new card once, email, Order ID tracking)
"""
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest

from backend.db.schema import EmailOutbox, OAuthCode, Order, PaymentMethod, User
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth
from backend.payment.policy import TAX_RATE
from backend.shop import mailer, notifications
from tests.test_mcp_shop_flow import (REDIRECT, _on_secure_page, call, client, pkce, rpc,  # noqa: F401 (fixture)
                                      sign_in, sign_up)


def tool(client, name: str, token: str | None = None, **args) -> tuple[dict, dict]:
    """(the tool's JSON for the model, the raw MCP result incl. the card)."""
    r, msg = rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    assert r.status_code == 200, r.text
    res = msg["result"]
    return json.loads(res["content"][0]["text"]) if not res.get("isError") else {"error": res["content"][0]["text"]}, res


def visitor_cart_with(client, *picks: tuple[str, str | None, int]) -> str:
    """A visitor (no sign-in) adds products to this chat's cart. Returns the visitor_cart id."""
    found, _ = tool(client, "search_shopsphere", query="running shoes", size="10")
    cart = found["visitor_cart"]
    for pid, size, qty in picks:
        colour = next(c["name"] for c in call(client, "get_shopsphere_product", product_id=pid, size=size)["all_colors"]
                      if c["in_stock"])
        added = call(client, "add_to_cart", product_id=pid, size=size, color=colour, quantity=qty, visitor_cart=cart)
        assert added["visitor_cart"] == cart
    return cart


def orders_for_checkout(checkout_id: str) -> list[Order]:
    with get_session() as db:
        return db.query(Order).filter(Order.checkout_id == checkout_id, Order.display_id.isnot(None)).all()


def verify_on_secure_page(client, hosted_url: str, card: str, result: str = "ok") -> str:
    """A card that asks for (simulated) verification: enter it, then Verify / Cancel / Fail."""
    path = "/psp" + hosted_url.split("/psp", 1)[1]
    client.post(f"{path}/pay", data={"cardholder_name": "Pat Guest", "card_number": card, "expiry": "12/30",
                                     "cvc": "123", "save": "1"})
    page = client.post(f"{path}/continue").text
    m = re.search(r'name="challenge" value="([^"]+)"', page)
    if m:
        page = client.post(f"{path}/challenge", data={"result": result, "challenge": m.group(1)}).text
    return page


GUEST = {"full_name": "Pat Guest", "email": "", "line1": "500 Congress Ave", "line2": "Apt 4", "city": "Austin",
         "state": "TX", "postal_code": "78701"}


# ── 1. Existing customer ───────────────────────────────────────────────────────

def test_case1_existing_customer_signs_in_at_checkout_and_pays_with_a_saved_card(client):
    email = "chris@shopsphere.demo"
    token = sign_in(client, email)                                      # empty the account's cart first
    for ln in call(client, "show_cart", token)["items"]:
        rpc(client, "tools/call", {"name": "update_cart_line", "arguments": {"line_id": ln["line_id"], "quantity": 0}},
            token)
    call(client, "add_to_cart", token, product_id="SSP049", color="White")     # already in the account's cart
    cart = visitor_cart_with(client, ("SSP049", None, 1), ("SSP001", "10", 1))  # same item again + a new one
    r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {"visitor_cart": cart}})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]   # Claude: sign in now
    token = sign_in(client, email)                                      # same password as the website
    co = call(client, "checkout", token, visitor_cart=cart)
    names = [i["name"] for i in co["items"]]
    assert len(names) == len(set(names)) == 2                           # merged, no duplicate lines
    assert co["ship_to"] and co["saved_addresses"] and co["saved_payment_methods"]   # the customer's own details
    me = call(client, "my_shopsphere_account", token)
    assert me["email"] == email and me["saved_card"]
    assert call(client, "show_cart", visitor_cart=cart)["item_count"] == 0    # the visitor cart is gone
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"], delivery="express")
    assert "Express" in st["delivery_label"] and st["version"] > co["checkout_version"]
    stale, _ = tool(client, "approve_and_pay", token, checkout_id=co["checkout_id"],
                    checkout_version=co["checkout_version"], final_amount=co["total"], currency="USD")
    assert "error" in stale and not orders_for_checkout(co["checkout_id"])     # the old total can't be paid
    paid, _ = tool(client, "approve_and_pay", token, checkout_id=co["checkout_id"], checkout_version=st["version"],
                   final_amount=st["total"], currency="USD")
    assert paid["status"] == "authorized" and paid["order_id"].startswith("SS-")
    again, _ = tool(client, "approve_and_pay", token, checkout_id=co["checkout_id"], checkout_version=st["version"],
                    final_amount=st["total"], currency="USD")
    assert "error" in again                                             # a second tap can't pay twice
    assert len(orders_for_checkout(co["checkout_id"])) == 1
    mine = call(client, "my_orders", token, order_id=paid["order_id"])["orders"][0]
    assert mine["order_id"] == paid["order_id"] and mine.get("shipment")


# ── 2. New customer ────────────────────────────────────────────────────────────

def test_case2_new_customer_signs_up_keeps_the_cart_and_pays_on_the_secure_page(client):
    cart = visitor_cart_with(client, ("SSP049", None, 2))
    email = f"neha.{secrets.token_hex(4)}@example.com"
    _, token = sign_up(client, email)                                   # state + PKCE checked inside
    with get_session() as db:
        u = db.query(User).filter_by(email=email).one()
        assert not u.is_guest and u.password_hash and "riya-secret-1" not in u.password_hash   # hashed
    co = call(client, "checkout", token, visitor_cart=cart)
    assert [i["quantity"] for i in co["items"]] == [2] and "12 Oak St" in co["ship_to"]
    assert "secure payment page" in co["pay_with"] and not co["saved_payment_methods"]
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"])
    started, _ = tool(client, "start_new_card_payment", token, checkout_id=co["checkout_id"],
                      checkout_version=st["version"], final_amount=st["total"], currency="USD")
    assert "/psp/hosted/" in started["hosted_url"]
    assert "Payment verified" in _on_secure_page(client, started["hosted_url"], save=False)   # not saved
    paid = call(client, "finish_new_card_payment", token, checkout_id=co["checkout_id"])
    assert paid["status"] == "authorized" and len(orders_for_checkout(co["checkout_id"])) == 1
    assert call(client, "my_shopsphere_account", token)["saved_card"] is None   # no consent: nothing kept
    call(client, "add_to_cart", token, product_id="SSP049", color="White")
    co2 = call(client, "checkout", token)
    st2 = call(client, "update_checkout_choices", token, checkout_id=co2["checkout_id"])
    started, _ = tool(client, "start_new_card_payment", token, checkout_id=co2["checkout_id"],
                      checkout_version=st2["version"], final_amount=st2["total"], currency="USD")
    _on_secure_page(client, started["hosted_url"], save=True)
    call(client, "finish_new_card_payment", token, checkout_id=co2["checkout_id"])
    assert call(client, "my_shopsphere_account", token)["saved_card"].endswith("3055")   # saved with consent
    with get_session() as db:
        pm = db.query(PaymentMethod).filter_by(user_id=u.user_id).one()
        assert pm.last4 == "3055" and "4000000000003055" not in json.dumps(
            {c.name: str(getattr(pm, c.name)) for c in PaymentMethod.__table__.columns})


def test_case2_signup_retry_after_the_account_exists_signs_in_instead_of_duplicating(client):
    email = f"retry.{secrets.token_hex(4)}@example.com"
    _, first = sign_up(client, email)
    assert first
    page, again = sign_up(client, email)                    # e.g. the app's sign-in failed after the account was made
    assert again and not page                               # same email + password: signed in to that account
    assert call(client, "my_shopsphere_account", again)["email"] == email
    with get_session() as db:
        assert db.query(User).filter(User.email == email).count() == 1
    page, nope = sign_up(client, email, password="a-different-pass")
    assert not nope and "already exists" in page and 'id="signin">' in page   # shown the Sign in form instead
    assert f'value="{email}"' in page


def test_case2_oauth_codes_are_one_time_hashed_expiring_and_bound_to_the_connector_address(client):
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()

    def code_for(resource: str, state: str = "Zz+/=~.-_9") -> tuple[str, str]:
        verifier, challenge = pkce()
        params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource}
        loc = client.post("/oauth/authorize", data={**params, "email": "chris@shopsphere.demo", "password": "demo1234"},
                          follow_redirects=False).headers["location"]
        q = parse_qs(urlparse(loc).query)
        assert q["state"] == [state]                                     # the app's state, exactly
        return q["code"][0], verifier

    base = client.get("/.well-known/oauth-protected-resource/shop/v5").json()["resource"]
    code, verifier = code_for(base)
    with get_session() as db:                                            # stored as a hash, never the code
        assert not db.query(OAuthCode).filter_by(code_hash=code).first()
        assert db.query(OAuthCode).filter_by(code_hash=mcp_auth._code_hash(code)).first()
    swap = {"grant_type": "authorization_code", "client_id": reg["client_id"], "redirect_uri": REDIRECT}
    assert client.post("/oauth/token", data={**swap, "code": "made-up", "code_verifier": verifier}).status_code == 400
    tok = client.post("/oauth/token", data={**swap, "code": code, "code_verifier": verifier}).json()
    assert client.post("/oauth/token", data={**swap, "code": code, "code_verifier": verifier}).json()["error"] \
        == "invalid_grant"                                               # replayed
    r, msg = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, tok["access_token"], path="/shop/v5")
    assert r.status_code == 200 and not msg["result"].get("isError")    # works at its own address
    r, _ = rpc(client, "tools/call", {"name": "my_shopsphere_account", "arguments": {}}, tok["access_token"],
               path="/shop/v4")
    assert r.status_code == 401                                          # not at another connector address
    code, verifier = code_for(base)
    with get_session() as db:
        db.query(OAuthCode).update({"expires_at": time.time() - 1})
        db.commit()
    assert client.post("/oauth/token", data={**swap, "code": code, "code_verifier": verifier}).json()["error"] \
        == "invalid_grant"                                               # expired


def test_case2_signin_diagnostics_are_not_public(client, monkeypatch):
    assert client.get("/oauth/debug/recent").status_code == 404
    monkeypatch.setenv("OAUTH_DEBUG_TOKEN", "x" * 24)
    assert client.get("/oauth/debug/recent", headers={"X-Debug-Token": "wrong"}).status_code == 404
    assert "events" in client.get("/oauth/debug/recent", headers={"X-Debug-Token": "x" * 24}).json()


# ── 3. Guest ───────────────────────────────────────────────────────────────────

def guest_start(client, *picks) -> tuple[str, dict, dict]:
    cart = visitor_cart_with(client, *picks)
    _, cart_card = tool(client, "show_cart", visitor_cart=cart)
    card = json.dumps(cart_card["structuredContent"])
    assert "Sign in & check out" in card and "Create account & check out" in card and "Check out as guest" in card
    r, msg = rpc(client, "tools/call", {"name": "guest_checkout", "arguments": {"visitor_cart": cart}})
    assert r.status_code == 200                                          # no 401: guest checkout never asks to sign in
    co = json.loads(msg["result"]["content"][0]["text"])
    assert co["guest"] and co["guest_token"] and not co["ready"]
    return cart, co, msg["result"]


def guest_details(client, co: dict, email: str, **extra) -> dict:
    st, _ = tool(client, "update_guest_checkout", guest_token=co["guest_token"], **{**GUEST, "email": email, **extra})
    return st


def guest_pay(client, co: dict, st: dict) -> dict:
    started, _ = tool(client, "start_guest_payment", guest_token=co["guest_token"], checkout_version=st["version"],
                      final_amount=st["total"], currency="USD")
    return started


def test_case3_guest_buys_without_an_account_gets_an_email_and_tracks_the_order(client, monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)               # SMTP "configured": a fake server
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(notifications, "_dispatch", lambda job: job())
    email = f"pat.{secrets.token_hex(4)}@guestmail.net"
    cart, co, raw = guest_start(client, ("SSP049", None, 2), ("SSP001", "10", 1))
    # Conversational: guest_checkout answers with data (no form card); the cart card's guest button
    # still opens the in-card form (tests/test_mcp_guest_card_ui.py).
    shown = json.dumps({k: v for k, v in co.items() if k != "next_step"})
    assert "card_number" not in shown and co["saved_profile"]["available"]
    assert "Chase" not in shown and "points" not in shown.lower() and "Wallet" not in shown   # no account-only benefits
    early, _ = tool(client, "start_guest_payment", guest_token=co["guest_token"],
                    checkout_version=co["checkout_version"], final_amount=co["total"], currency="USD")
    assert "error" in early                                              # no details yet: can't pay
    bad = tool(client, "update_guest_checkout", guest_token=co["guest_token"], **{**GUEST, "email": "not-an-email"})[0]
    assert "valid email" in bad["error"]
    bad = tool(client, "update_guest_checkout", guest_token=co["guest_token"], **{**GUEST, "email": email,
                                                                                    "postal_code": "ABC"})[0]
    assert "ZIP" in bad["error"]
    st = guest_details(client, co, email, delivery="express")
    assert st["payable"] and st["has_details"] and "Austin, TX 78701" in st["ship_to"] and email not in json.dumps(st)
    sub = round(sum(i["line_total"] for i in call(client, "show_cart", visitor_cart=cart)["items"]), 2)
    assert st["subtotal_text"] == f"${sub:.2f}" and st["shipping_text"] == "$9.99"
    assert st["tax_text"] == f"${round(sub * TAX_RATE, 2):.2f}"
    assert st["total"] == round(sub + round(sub * TAX_RATE, 2) + 9.99, 2)   # the server's numbers
    started = guest_pay(client, co, st)
    assert "/psp/hosted/" in started["hosted_url"]
    early, _ = tool(client, "finish_guest_payment", guest_token=co["guest_token"])
    assert "isn't finished" in early["error"] and not orders_for_checkout(co["checkout_id"])
    page = client.get("/psp" + started["hosted_url"].split("/psp", 1)[1]).text
    assert 'name="save"' not in page                                     # a guest card can't be saved
    assert "Payment verified" in _on_secure_page(client, started["hosted_url"], save=True)
    paid, _ = tool(client, "finish_guest_payment", guest_token=co["guest_token"])
    assert paid["status"] == "authorized" and paid["order_id"].startswith("SS-") and paid["arrives"]
    assert paid["email_delivery"] == "sent" and paid["sent_to"].endswith("@guestmail.net") and email not in json.dumps(paid)
    again, _ = tool(client, "finish_guest_payment", guest_token=co["guest_token"])
    assert again["order_id"] == paid["order_id"]                          # repeated: same order, no second charge
    (order,) = orders_for_checkout(co["checkout_id"])
    assert order.guest_email == email and order.payment_status == "authorized" and order.payment_last4 == "3055"
    with get_session() as db:
        owner = db.query(User).filter_by(user_id=order.user_id).one()
        assert owner.is_guest and not db.query(PaymentMethod).filter_by(user_id=owner.user_id).count()
        assert not db.query(User).filter(User.email == email).count()   # no customer account with that email
        mail = db.query(EmailOutbox).filter_by(order_id=order.order_id).one()
        assert mail.to_email == email and mail.delivery == "sent"
    (to, subject, body), = sent
    assert to == email and paid["order_id"] in subject and paid["order_id"] in body and "/track?order=" in body
    assert "3055" not in body.replace("•••• 3055", "") and "4000" not in body and "CVC" not in body
    # a guest can't sign in: no login was created
    reg =client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    assert "Incorrect email or password" in client.post("/oauth/authorize", data={
        **params, "email": owner.email, "password": "anything"}).text
    # tracking later, with no sign-in: Order ID + the checkout email
    tracked = call(client, "track_order", order_id=paid["order_id"], email=email.upper())
    assert tracked["found"] and tracked["order"]["order_id"] == paid["order_id"]
    assert tracked["order"]["ship_to"] == {"city": "Austin", "state": "TX"}   # never the full address
    assert "500 Congress" not in json.dumps(tracked)
    wrong = call(client, "track_order", order_id=paid["order_id"], email="someone@else.net")
    missing = call(client, "track_order", order_id="SS-00000", email=email)
    assert wrong == missing == {"found": False, "message": wrong["message"]}  # one answer for both
    site = client.post("/api/orders/track", json={"order_id": paid["order_id"], "email": email})
    assert site.status_code == 200 and site.json()["order_id"] == paid["order_id"]   # the website too


def test_case3_guest_sessions_are_isolated_and_payment_needs_the_shoppers_own_steps(client):
    _, a, _ = guest_start(client, ("SSP049", None, 1))
    _, b, _ = guest_start(client, ("SSP049", None, 1))
    st_a = guest_details(client, a, "a@guestmail.net")
    vid_b, _ = mcp_auth.read_guest_token(b["guest_token"])
    crossed = mcp_auth.issue_guest_token(vid_b, a["checkout_id"])        # B's identity, A's checkout id
    for token in (crossed, a["guest_token"][:-3] + "abc", "", "gc_" + a["checkout_id"]):
        out, _ = tool(client, "update_guest_checkout", guest_token=token, delivery="express")
        assert "error" in out
        out, _ = tool(client, "start_guest_payment", guest_token=token, checkout_version=st_a["version"],
                      final_amount=st_a["total"], currency="USD")
        assert "error" in out
    expired = mcp_auth._issue("guest_checkout", -10, sub=read_vid(a), chk=a["checkout_id"])
    assert "error" in tool(client, "update_guest_checkout", guest_token=expired, delivery="express")[0]
    # a customer token can't be used as a guest token, and guest payment tools are hidden from the model
    _, tools = rpc(client, "tools/list")
    vis = {t["name"]: t.get("_meta", {}).get("ui", {}).get("visibility") for t in tools["result"]["tools"]}
    assert vis["start_guest_payment"] == ["app"]
    assert vis["update_guest_checkout"] == ["app", "model"]     # a guest may give their details in the chat
    assert vis["approve_and_pay"] == ["app"] and vis["start_new_card_payment"] == ["app"]
    # opening the secure page (even by a direct call) moves no money: only the shopper's card + verification there
    started = guest_pay(client, a, st_a)
    assert "error" in tool(client, "finish_guest_payment", guest_token=a["guest_token"],
                           payment_session_id=started["payment_session_id"])[0]
    assert not orders_for_checkout(a["checkout_id"])
    # the website's customer-only payment route still refuses a visitor
    vis_login = client.post("/api/auth/visitor").json()
    hdr = {"Authorization": f"Bearer {vis_login['access_token']}"}
    assert client.post(f"/api/checkouts/{a['checkout_id']}/confirm", json={"consent": True}, headers=hdr).status_code \
        in (403, 404)


def read_vid(co: dict) -> str:
    return mcp_auth.read_guest_token(co["guest_token"])[0]


@pytest.mark.parametrize("card,step,expected", [
    ("4000 0000 0000 0002", "ok", "declined"),          # the issuer declines
    ("4242 4242 4242 4242", "cancel", "cancelled"),      # the shopper cancels the verification
])
def test_case3_declined_or_cancelled_guest_payment_makes_no_order_and_can_be_retried(client, card, step, expected):
    _, co, _ = guest_start(client, ("SSP049", None, 1))
    st = guest_details(client, co, "retry@guestmail.net")
    started = guest_pay(client, co, st)
    verify_on_secure_page(client, started["hosted_url"], card, step)
    out, _ = tool(client, "finish_guest_payment", guest_token=co["guest_token"])
    assert "error" in out and expected[:6] in out["error"].lower()      # declined / cancelled, said plainly
    assert not orders_for_checkout(co["checkout_id"])
    again = guest_pay(client, co, st)                    # retry: a fresh secure page for the same order
    assert again["payment_session_id"] != started["payment_session_id"]
    assert "Payment verified" in verify_on_secure_page(client, again["hosted_url"], "4242 4242 4242 4242")
    paid, _ = tool(client, "finish_guest_payment", guest_token=co["guest_token"])
    assert paid["status"] == "authorized" and len(orders_for_checkout(co["checkout_id"])) == 1


def test_case3_expired_secure_page_and_stale_total_are_handled(client):
    from backend.db.schema import ProviderPaymentSession
    _, co, _ = guest_start(client, ("SSP049", None, 1))
    st = guest_details(client, co, "late@guestmail.net")
    first = guest_pay(client, co, st)
    assert guest_pay(client, co, st)["payment_session_id"] == first["payment_session_id"]   # still open: same page
    with get_session() as db:
        db.query(ProviderPaymentSession).filter_by(session_id=first["payment_session_id"]).update(
            {"expires_at": ProviderPaymentSession.created_at})
        db.commit()
    fresh = guest_pay(client, co, st)                                    # the old page expired: a new one
    assert fresh["payment_session_id"] != first["payment_session_id"] and not orders_for_checkout(co["checkout_id"])
    # the shopper changes delivery after approving: the old total can't be paid
    _, co2, _ = guest_start(client, ("SSP049", None, 1))
    st2 = guest_details(client, co2, "stale@guestmail.net")
    st3, _ = tool(client, "update_guest_checkout", guest_token=co2["guest_token"], delivery="express")
    assert st3["version"] > st2["version"]
    stale, _ = tool(client, "start_guest_payment", guest_token=co2["guest_token"], checkout_version=st2["version"],
                    final_amount=st2["total"], currency="USD")
    assert "changed" in stale["error"] and not orders_for_checkout(co2["checkout_id"])


def test_case3_tracking_is_rate_limited_without_revealing_anything(client):
    for _ in range(5):
        assert not call(client, "track_order", order_id="SS-12345", email="guess@guestmail.net")["found"]
    with get_session() as db:
        order = db.query(Order).filter(Order.display_id.isnot(None)).first()
        real_id = order.display_id if order else None
    locked = call(client, "track_order", order_id="SS-12345", email="any@guestmail.net")
    assert locked == {"found": False, "message": locked["message"]}      # locked: still the same answer
    if real_id:
        assert "found" in call(client, "track_order", order_id=real_id, email="x@guestmail.net")
