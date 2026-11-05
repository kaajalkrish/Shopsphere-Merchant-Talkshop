"""
ShopSphere in Claude / ChatGPT, step 2: sign-in (OAuth 2.1 + PKCE) and the full Talkshop-style
flow over the MCP endpoint: search (open) -> sign in -> add to cart -> cart -> checkout ->
Approve & pay (simulated passkey) -> order. Everything goes through HTTP like the chat app.
"""
import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

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


def rpc(client, method: str, params: dict | None = None, token: str | None = None):
    headers = dict(MCP_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}))
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
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


def sign_in(client, email: str, password: str = "demo1234") -> str:
    """The chat app's OAuth dance: register, show the login page, sign in, swap the code."""
    meta = client.get("/.well-known/oauth-authorization-server").json()
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "xyz",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Sign in to ShopSphere" in page.text and "Claude" in page.text
    done = client.post("/oauth/authorize", data={**params, "email": email, "password": password},
                       follow_redirects=False)
    assert done.status_code == 302, done.text
    q = parse_qs(urlparse(done.headers["location"]).query)
    assert q["state"] == ["xyz"] and q["iss"] == [meta["issuer"]]
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


def test_search_is_open_and_customer_tools_ask_for_sign_in(client):
    r, msg = rpc(client, "tools/list")
    tools = {t["name"]: t for t in msg["result"]["tools"]}
    assert {"search_shopsphere", "add_to_cart", "show_cart", "checkout", "my_orders"} <= set(tools)
    for app_only in ("approve_and_pay", "update_cart_line"):                  # card buttons only: hidden from the model
        assert tools[app_only]["_meta"]["ui"]["visibility"] == ["app"]
    assert "model" in tools["add_to_cart"]["_meta"]["ui"]["visibility"]
    assert call(client, "search_shopsphere", query="running shoes", size="10")["found"]
    r, _ = rpc(client, "tools/call", {"name": "show_cart", "arguments": {}})
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
