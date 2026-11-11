"""
ShopSphere in Claude / ChatGPT, step 2: sign-in (OAuth 2.1 + PKCE) and the full Talkshop-style
flow over the MCP endpoint: search (open) -> sign in -> add to cart -> cart -> checkout ->
Approve & pay (simulated passkey) -> order. Everything goes through HTTP like the chat app.
"""
import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.mcp import auth as mcp_auth
from backend.oauth import server as oauth_server

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client():
    with TestClient(app) as c:                        # runs the MCP app's lifespan
        yield c


def rpc(client, method: str, params: dict | None = None, token: str | None = None, path: str = "/mcp"):
    headers = dict(MCP_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}))
    r = client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if r.status_code != 200:
        return r, None
    line = next(ln for ln in r.text.splitlines() if ln.startswith("data: "))
    return r, json.loads(line[6:])


def call(client, tool: str, token: str | None = None, **args) -> dict:
    r, msg = rpc(client, "tools/call", {"name": tool, "arguments": args}, token)
    assert r.status_code == 200, r.text
    res = msg["result"]
    text = res["content"][0]["text"] if res.get("content") else "{}"
    assert not res.get("isError"), text
    return json.loads(text)


def pkce():
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def sign_in(client, email: str, password: str = "demo1234", resource: str | None = None) -> str:
    """The chat app's OAuth dance: register, show the login page, sign in, swap the code."""
    meta = client.get("/.well-known/oauth-authorization-server").json()
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "xyz",
              "code_challenge": challenge, "code_challenge_method": "S256", **({"resource": resource} if resource else {})}
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Sign in to ShopSphere" in page.text and "Claude" in page.text
    done = client.post("/oauth/authorize", data={**params, "email": email, "password": password},
                       follow_redirects=False)
    assert done.status_code == 302, done.text
    q = parse_qs(urlparse(done.headers["location"]).query)
    assert q["state"] == ["xyz"] and "iss" not in q and len(q["code"][0]) < 64      # short code, no iss
    assert meta["issuer"].startswith("http")
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                            "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                            "code_verifier": verifier}).json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"]
    return tok["access_token"]


def test_discovery_documents(client):
    pr = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"].endswith("/mcp") and pr["authorization_servers"]
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["client_id_metadata_document_supported"]
    assert meta["token_endpoint"].endswith("/oauth/token") and meta["registration_endpoint"].endswith("/oauth/register")


def test_search_and_cart_are_open_and_checkout_asks_for_sign_in(client):
    r, msg = rpc(client, "tools/list")
    tools = {t["name"]: t for t in msg["result"]["tools"]}
    assert {"search_shopsphere", "add_to_cart", "show_cart", "checkout", "my_orders"} <= set(tools)
    for app_only in ("approve_and_pay", "update_cart_line"):                  # card buttons only: hidden from the model
        assert tools[app_only]["_meta"]["ui"]["visibility"] == ["app"]
    assert "model" in tools["add_to_cart"]["_meta"]["ui"]["visibility"]
    assert call(client, "search_shopsphere", query="running shoes", size="10")["found"]
    r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {}})
    assert r.status_code == 401
    assert 'resource_metadata="' in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]
    r, _ = rpc(client, "tools/list", token="not-a-real-token")
    assert r.status_code == 401                                                 # a bad token is never ignored


def test_wrong_pkce_and_reused_codes_are_refused(client):
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    assert "Incorrect email or password" in client.post(
        "/oauth/authorize", data={**params, "email": "kaajal@shopsphere.demo", "password": "nope"}).text
    loc = client.post("/oauth/authorize", data={**params, "email": "kaajal@shopsphere.demo", "password": "demo1234"},
                      follow_redirects=False).headers["location"]
    code = parse_qs(urlparse(loc).query)["code"][0]
    base = {"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"], "redirect_uri": REDIRECT}
    assert client.post("/oauth/token", data={**base, "code_verifier": "wrong"}).json()["error"] == "invalid_grant"
    assert client.post("/oauth/token", data={**base, "code_verifier": verifier}).json()["error"] == "invalid_grant"
    other = client.get("/oauth/authorize", params={**params, "redirect_uri": "https://evil.example/cb"})
    assert other.status_code == 400                                             # unregistered return address


def test_token_for_another_server_is_refused(client):
    stray = mcp_auth.issue_tokens("USR001", "x", "https://some-other-server.example/mcp")["access_token"]
    r, _ = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, stray)
    assert r.status_code == 401


def test_claude_published_identity_cimd(client, monkeypatch):
    cid = "https://claude.ai/oauth/claude-client.json"

    class Doc:
        status_code = 200

        def json(self):
            return {"client_id": cid, "client_name": "Claude", "redirect_uris": [REDIRECT]}
    monkeypatch.setattr(oauth_server.httpx, "get", lambda *a, **k: Doc())
    oauth_server._cimd_cache.clear()
    _, challenge = pkce()
    page = client.get("/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                                  "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 200 and "Claude" in page.text
    untrusted = client.get("/oauth/authorize", params={"response_type": "code", "client_id": "https://evil.example/c.json",
                                                       "redirect_uri": REDIRECT, "code_challenge": challenge,
                                                       "code_challenge_method": "S256"})
    assert untrusted.status_code == 400                                         # metadata only from known hosts


def _buy(client, email: str, product_id: str, size: str) -> dict:
    token = sign_in(client, email)
    me = call(client, "my_shopsphere_account", token)
    assert me["signed_in"] and me["email"] == email
    for ln in call(client, "show_cart", token)["items"]:                       # start from an empty cart
        r, msg = rpc(client, "tools/call", {"name": "update_cart_line",
                                            "arguments": {"line_id": ln["line_id"], "quantity": 0}}, token)
        assert r.status_code == 200 and not msg["result"].get("isError")
    pick = call(client, "get_shopsphere_product", product_id=product_id, size=size)
    colour = next(c["name"] for c in pick["all_colors"] if c["in_stock"])
    added = call(client, "add_to_cart", token, product_id=product_id, size=size, color=colour)
    assert added["cart_items"] == 1 and "charged" in added["next_step"]
    co = call(client, "checkout", token, delivery="express")
    assert co["ready"] and co["delivery"] == "express" and co["total"] > 0
    r, msg = rpc(client, "tools/call", {"name": "approve_and_pay", "arguments": {
        "checkout_id": co["checkout_id"], "checkout_version": co["checkout_version"],
        "final_amount": co["total"], "currency": co["currency"]}}, token)
    paid = json.loads(msg["result"]["content"][0]["text"])
    assert paid["status"] == "authorized" and paid["order_id"].startswith("SS-"), paid
    orders = call(client, "my_orders", token)["orders"]
    assert any(o.get("order_id") == paid["order_id"] for o in orders)
    assert call(client, "show_cart", token)["item_count"] == 0                 # bought items leave the cart
    return paid


def test_chase_visa_member_buys_inside_the_chat_on_the_visa_agent_path(client):
    _buy(client, "kaajal@shopsphere.demo", "SSP001", "10")


def test_mastercard_member_buys_inside_the_chat_on_the_saved_card_path(client):
    _buy(client, "dana@shopsphere.demo", "SSP002", "9")


def test_paying_a_changed_order_is_refused(client):
    token = sign_in(client, "marcus@shopsphere.demo")
    added = call(client, "add_to_cart", token, product_id="SSP049", color="White")
    assert added["cart_items"] >= 1
    co = call(client, "checkout", token)
    r, msg = rpc(client, "tools/call", {"name": "approve_and_pay", "arguments": {
        "checkout_id": co["checkout_id"], "checkout_version": co["checkout_version"],
        "final_amount": co["total"] - 1, "currency": co["currency"]}}, token)  # not the total the shopper saw
    assert msg["result"]["isError"]


def test_the_fresh_shop_address_is_the_same_connector(client):
    """/shop serves the same MCP server (a new address makes chat apps reload the tool list)."""
    r, msg = rpc(client, "tools/list", path="/shop")
    assert {"search_shopsphere", "add_to_cart", "checkout"} <= {t["name"] for t in msg["result"]["tools"]}
    r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {}}, path="/shop")
    assert r.status_code == 401 and "/.well-known/oauth-protected-resource/shop" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-protected-resource/shop").json()
    assert meta["resource"].endswith("/shop")
    token = sign_in(client, "chris@shopsphere.demo", resource=meta["resource"])
    r, msg = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, token, path="/shop")
    assert r.status_code == 200 and not msg["result"].get("isError")
    stray = mcp_auth.issue_tokens("USR001", "x", "https://elsewhere.example/shop")["access_token"]
    r, _ = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, stray, path="/shop")
    assert r.status_code == 401


def _fresh_cart(client, token):
    for ln in call(client, "show_cart", token)["items"]:
        rpc(client, "tools/call", {"name": "update_cart_line", "arguments": {"line_id": ln["line_id"], "quantity": 0}},
            token)


def _pay(client, token, checkout_id, state):
    r, msg = rpc(client, "tools/call", {"name": "approve_and_pay", "arguments": {
        "checkout_id": checkout_id, "checkout_version": state["version"], "final_amount": state["total"],
        "currency": state["currency"]}}, token)
    text = msg["result"]["content"][0]["text"]
    assert not msg["result"].get("isError"), text
    return json.loads(text)


def test_change_address_card_and_delivery_on_the_checkout_card_then_pay(client):
    token = sign_in(client, "kaajal@shopsphere.demo")
    _fresh_cart(client, token)
    call(client, "add_to_cart", token, product_id="SSP003", size="9", color="Blue")
    co = call(client, "checkout", token)
    work = next(a for a in co["saved_addresses"] if a["label"] == "Work")
    mc = next(c for c in co["saved_payment_methods"] if "Mastercard" in c["display"])
    r, msg = rpc(client, "tools/call", {"name": "checkout", "arguments": {}}, token)
    page = json.dumps(msg["result"]["structuredContent"])
    assert "Change your order" in page and "Ship to Work" in page and "Update order" in page   # the choices are on the card
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"], address_id=work["address_id"],
              pay_with=mc["payment_method_id"], delivery="express")
    assert "Work" in st["ship_to"] or "Lavaca" in st["ship_to"]
    assert "Mastercard" in st["pay_with_text"] and "Express" in st["delivery_label"]
    assert st["version"] > co["checkout_version"] and st["payable"]
    # Kaajal's Chase Mastercard is the demo's declined card: no order, and the order stays open
    r, msg = rpc(client, "tools/call", {"name": "approve_and_pay", "arguments": {
        "checkout_id": co["checkout_id"], "checkout_version": st["version"], "final_amount": st["total"],
        "currency": st["currency"]}}, token)
    assert msg["result"]["isError"] and "declined" in msg["result"]["content"][0]["text"]
    assert not any(o.get("order_id") for o in call(client, "my_orders", token)["orders"]
                   if "Lavaca" in json.dumps(o))
    visa = next(c for c in co["saved_payment_methods"] if "Visa" in c["display"])
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"], pay_with=visa["payment_method_id"])
    assert "Visa agent payment" in st["pay_with_text"]
    paid = _pay(client, token, co["checkout_id"], st)                  # pays exactly the updated order
    assert paid["status"] == "authorized", paid


def test_pay_with_the_shopsphere_wallet_from_the_card(client):
    token = sign_in(client, "kaajal@shopsphere.demo")
    _fresh_cart(client, token)
    call(client, "add_to_cart", token, product_id="SSP049", color="White")
    co = call(client, "checkout", token)
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"], pay_with="wallet")
    assert "Wallet" in st["pay_with_text"] and st["payable"]
    assert _pay(client, token, co["checkout_id"], st)["status"] == "authorized"


def test_versioned_addresses_are_the_same_connector(client):
    """/shop/v2 etc.: a new address makes chat apps reload the tool list after an update."""
    r, msg = rpc(client, "tools/list", path="/shop/v2")
    assert "update_checkout_choices" in {t["name"] for t in msg["result"]["tools"]}
    meta = client.get("/.well-known/oauth-protected-resource/shop/v2").json()
    assert meta["resource"].endswith("/shop/v2")
    assert client.get("/.well-known/oauth-protected-resource/somewhere/else").status_code == 404
    token = sign_in(client, "chris@shopsphere.demo", resource=meta["resource"])
    r, msg = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, token, path="/shop/v2")
    assert r.status_code == 200 and not msg["result"].get("isError")
    r, _ = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}}, token, path="/shop/v3")
    assert r.status_code == 401                       # a token is for the address it was issued to


def sign_up(client, email: str, **fields) -> tuple[str, str]:
    """A brand-new shopper: the chat app's sign-in page, Create an account tab. Returns (page, token)."""
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s1",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    assert "Create an account" in client.get("/oauth/authorize", params=params).text
    page = client.get("/oauth/authorize", params={**params, "mode": "signup"}).text
    assert "Create your ShopSphere account" in page and "Shipping address" in page and "card_number" not in page
    form = {"mode": "signup", "name": "Riya Shah", "email": email, "password": "riya-secret-1", "line1": "12 Oak St",
            "line2": "", "city": "Austin", "address_state": "tx", "postal_code": "78701", **fields}
    done = client.post("/oauth/authorize", data={**params, **form}, follow_redirects=False)
    if done.status_code != 302:
        return done.text, ""
    back = parse_qs(urlparse(done.headers["location"]).query)
    assert back["state"] == ["s1"]                    # the app's own state goes back unchanged
    code = back["code"][0]
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                            "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                            "code_verifier": verifier}).json()
    return "", tok["access_token"]


def _on_secure_page(client, hosted_url: str, card: str = "4000 0000 0000 3055", save: bool = True):
    """The shopper on the (simulated) provider's own page, in their browser: never through the chat."""
    path = "/psp" + hosted_url.split("/psp", 1)[1]
    assert "Enter test card details" in client.get(path).text
    data = {"cardholder_name": "Riya Shah", "card_number": card, "expiry": "12/30", "cvc": "123",
            **({"save": "1"} if save else {})}
    client.post(f"{path}/pay", data=data)
    client.post(f"{path}/continue")
    return client.get(path).text


def test_a_new_shopper_signs_up_and_pays_with_a_new_card_on_the_secure_page(client):
    email = f"riya.{secrets.token_hex(4)}@example.com"
    _, token = sign_up(client, email)
    me = call(client, "my_shopsphere_account", token)
    assert me["signed_in"] and me["email"] == email and me["saved_card"] is None
    call(client, "add_to_cart", token, product_id="SSP049", color="White")
    r, msg = rpc(client, "tools/call", {"name": "checkout", "arguments": {}}, token)
    co = json.loads(msg["result"]["content"][0]["text"])
    assert "12 Oak St" in co["ship_to"] and "TX" in co["ship_to"]          # the address from sign-up
    assert "secure payment page" in co["pay_with"]                          # no saved card: a new card
    card = json.dumps(msg["result"]["structuredContent"])
    assert "start_new_card_payment" in card and "I've finished paying" in card
    st = call(client, "update_checkout_choices", token, checkout_id=co["checkout_id"])
    assert st["payable"] and st["new_card"]
    r, msg = rpc(client, "tools/call", {"name": "start_new_card_payment", "arguments": {
        "checkout_id": co["checkout_id"], "checkout_version": st["version"], "final_amount": st["total"],
        "currency": st["currency"]}}, token)
    started = json.loads(msg["result"]["content"][0]["text"])
    assert started["status"] == "provider_handoff" and "/psp/hosted/" in started["hosted_url"]
    # tapping "I've finished" before the card is entered changes nothing
    r, msg = rpc(client, "tools/call", {"name": "finish_new_card_payment", "arguments": {
        "checkout_id": co["checkout_id"], "payment_session_id": started["payment_session_id"]}}, token)
    assert msg["result"]["isError"] and "isn't finished" in msg["result"]["content"][0]["text"]
    assert "Payment verified" in _on_secure_page(client, started["hosted_url"])
    paid = call(client, "finish_new_card_payment", token, checkout_id=co["checkout_id"])   # Claude can finish it too
    assert paid["status"] == "authorized" and paid["order_id"].startswith("SS-"), paid
    assert any(o.get("order_id") == paid["order_id"] for o in call(client, "my_orders", token)["orders"])
    # the saved card (a provider token, network and last 4 only) pays the next order with the passkey
    assert call(client, "my_shopsphere_account", token)["saved_card"].endswith("3055")
    call(client, "add_to_cart", token, product_id="SSP049", color="White")
    co2 = call(client, "checkout", token)
    assert "3055" in co2["pay_with"]
    assert _pay(client, token, co2["checkout_id"], call(client, "update_checkout_choices", token,
                                                        checkout_id=co2["checkout_id"]))["status"] == "authorized"


def test_sign_up_problems_are_shown_on_the_page_and_make_no_account(client):
    email = f"sam.{secrets.token_hex(4)}@example.com"
    page, token = sign_up(client, email, postal_code="ABC")
    assert not token and "ZIP code" in page and "12 Oak St" in page        # kept what they typed
    page, token = sign_up(client, email, password="short")
    assert not token and "8 or more" in page
    _, token = sign_up(client, email)
    assert token                                                            # the email was still free
    page, again = sign_up(client, email)
    assert not again and "already exists" in page
    page, token = sign_up(client, "kaajal@shopsphere.demo")
    assert not token and "already exists" in page


def test_add_to_cart_without_signing_in_then_sign_in_at_checkout(client):
    """Browse and fill the cart as a visitor; sign in (or sign up) only at checkout; the cart comes along."""
    r, msg = rpc(client, "tools/call", {"name": "search_shopsphere", "arguments": {"query": "running shoes", "size": "10"}})
    found = json.loads(msg["result"]["content"][0]["text"])
    cart_id = found["visitor_cart"]
    assert cart_id.startswith("vc_VIS")
    assert cart_id in json.dumps(msg["result"]["structuredContent"])           # the cards' Add to cart uses it
    pid = found["products"][0]["product_id"]
    colour = next(c["name"] for c in call(client, "get_shopsphere_product", product_id=pid, size="10")["all_colors"]
                  if c["in_stock"])
    added = call(client, "add_to_cart", product_id=pid, size="10", color=colour, visitor_cart=cart_id)
    assert added["cart_items"] == 1 and added["visitor_cart"] == cart_id        # no sign-in asked
    again = call(client, "show_cart", visitor_cart=cart_id)
    assert again["item_count"] == 1 and again["visitor_cart"] == cart_id
    assert call(client, "show_cart")["item_count"] == 0                       # another chat: its own cart
    forged = cart_id[:-4] + ("0000" if not cart_id.endswith("0000") else "1111")
    assert call(client, "show_cart", visitor_cart=forged)["item_count"] == 0  # a tampered id reaches nothing
    r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {"visitor_cart": cart_id}})
    assert r.status_code == 401                                               # checkout: sign in now
    _, token = sign_up(client, f"vis.{secrets.token_hex(4)}@example.com")
    co = call(client, "checkout", token, visitor_cart=cart_id)
    assert [i["quantity"] for i in co["items"]] == [1] and co["ready"]          # the visitor cart came along
    assert call(client, "show_cart", token)["item_count"] == 1
    assert call(client, "show_cart", visitor_cart=cart_id)["item_count"] == 0  # and the visitor cart is gone


def test_the_address_state_never_replaces_the_apps_sign_in_state(client):
    """The sign-up page has both Claude's OAuth `state` and the address's US state: they must not mix
    (sending state=TX back made Claude refuse the sign-in)."""
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "state": "Ab+c/d=e~f.g_h-i", "code_challenge": challenge, "code_challenge_method": "S256"}
    page = client.get("/oauth/authorize", params=params).text
    assert 'name="address_state"' in page and page.count('name="state"') == 2        # hidden, in both forms
    form = [*params.items(), ("mode", "signup"), ("name", "Tex Ann"), ("email", f"tx.{secrets.token_hex(4)}@example.com"),
            ("password", "texas-pass-1"), ("line1", "1 Main St"), ("city", "Austin"), ("state", "TX"),
            ("address_state", "TX"), ("postal_code", "78701")]
    done = client.post("/oauth/authorize", content=urlencode(form),
                       headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert done.status_code == 302
    assert parse_qs(urlparse(done.headers["location"]).query)["state"] == ["Ab+c/d=e~f.g_h-i"]
