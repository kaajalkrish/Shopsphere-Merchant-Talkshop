"""
Sign-in for the ShopSphere MCP connector (Claude / ChatGPT).

Searching, the cart, guest checkout and guest order tracking are open. Customer checkout,
saved details and "my orders" need the customer's ShopSphere account: the chat app signs the
customer in once through ShopSphere's OAuth 2.1 server (backend/oauth/server.py) and then
sends `Authorization: Bearer <token>` to /mcp.

Tokens are signed JWTs (JWT_SECRET_KEY), so a backend restart doesn't sign anyone out:
  mcp_access     1 day   sub=user, aud=<this server's connector address>, cid=<chat app's client id>
  mcp_refresh    30 days same, used only at /oauth/token
  code           5 min   the one-time authorization code: a short random value; its details
                         (user, app, PKCE challenge) are in the database (hash only), used once
  guest_checkout 2 h     a guest's capability for ONE guest checkout (no account)

McpSignIn guards /mcp: a call to a customer tool without a valid token gets HTTP 401 with
WWW-Authenticate pointing at the protected-resource metadata, which is how the chat app
knows to show "Sign in to ShopSphere".
"""
import json
import os
import re
import secrets
import time
from collections import deque
from typing import Optional

import jwt

from backend.auth.dependencies import CurrentUser

ALG = "HS256"
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300

# Tools that act for a signed-in customer. Everything else is open: search, product details and
# the cart (a visitor cart until the shopper signs in, which they do at checkout).
CUSTOMER_TOOLS = {"my_shopsphere_account", "checkout", "open_checkout", "update_checkout_choices", "approve_and_pay",
                  "start_new_card_payment", "finish_new_card_payment", "my_orders"}

# Temporary sign-in diagnostics (GET /oauth/debug/recent): the last steps of chat-app sign-ins,
# with no personal data: no emails, passwords, codes or tokens.
SIGNIN_LOG: deque = deque(maxlen=60)


def log_signin(step: str, **info) -> None:
    SIGNIN_LOG.append({"t": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "step": step, **info})
    print("SIGNIN", step, json.dumps(info), flush=True)


def client_label(client_id: Optional[str]) -> str:
    if not client_id:
        return "none"
    return "dcr" if client_id.startswith("dcr.") else client_id[:80]


def _secret() -> str:
    return os.getenv("JWT_SECRET_KEY", "demo-jwt-secret-replace-in-production")


def public_base_url(headers: Optional[dict] = None) -> str:
    """This backend's public https address: set on Render (RENDER_EXTERNAL_URL) or
    PUBLIC_BACKEND_URL; otherwise from the request (local testing)."""
    env = os.getenv("PUBLIC_BACKEND_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if env:
        return env.rstrip("/")
    h = {k.lower(): v for k, v in (headers or {}).items()}
    proto = h.get("x-forwarded-proto", "http").split(",")[0].strip()
    return f"{proto}://{h.get('x-forwarded-host') or h.get('host', 'localhost:8000')}"


# The connector's addresses: /mcp, /shop and versioned ones (/shop/v2, /shop/v3, ...), all the
# same server. Chat apps cache a connector's tool list per address, so after new tools are
# added, connecting at the next version makes them load the current list.
_MCP_PATH = re.compile(r"^/(mcp|shop)(/[a-z0-9-]{1,20})?$")


def is_mcp_path(path: str) -> bool:
    return bool(_MCP_PATH.match(path or ""))


def resource_url(base: str, path: str = "/mcp") -> str:
    return f"{base}{path if is_mcp_path(path) else '/mcp'}"


def is_our_resource(base: str, resource) -> bool:
    """One of this server's connector addresses (any version)."""
    return isinstance(resource, str) and resource.startswith(base) and is_mcp_path(resource[len(base):].rstrip("/"))


def _issue(typ: str, ttl: int, **claims) -> str:
    now = int(time.time())
    return jwt.encode({"typ": typ, "iat": now, "exp": now + ttl, "jti": secrets.token_urlsafe(12), **claims},
                      _secret(), algorithm=ALG)


def _read(token: str, typ: str, audience=None) -> Optional[dict]:
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALG], audience=audience,
                            options={"verify_aud": audience is not None})
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == typ else None


def _code_hash(code: str) -> str:
    import hashlib
    return hashlib.sha256((code or "").encode()).hexdigest()


def issue_code(**claims) -> str:
    """A short one-time code for the redirect back to the chat app. Its details live in the
    database (only the code's hash is stored), so whichever server worker receives the token
    request can redeem it: an in-memory code was lost when another worker answered."""
    from backend.db.schema import OAuthCode
    from backend.db.session_utils import get_session
    now = time.time()
    code = secrets.token_urlsafe(32)
    with get_session() as db:
        db.query(OAuthCode).filter(OAuthCode.expires_at < now).delete()
        db.add(OAuthCode(code_hash=_code_hash(code), claims_json=json.dumps(claims), expires_at=now + CODE_TTL))
        db.commit()
    return code


def redeem_code(code: str) -> Optional[dict]:
    """The code's details, once. A second use, an unknown or an expired code gives None. The
    row is deleted in the same statement that claims it, so two concurrent uses can't both win."""
    from backend.db.schema import OAuthCode
    from backend.db.session_utils import get_session
    if not code or len(code) > 200:
        return None
    with get_session() as db:
        row = db.query(OAuthCode).filter_by(code_hash=_code_hash(code)).first()
        if row is None:
            return None
        claims, exp = json.loads(row.claims_json), row.expires_at
        claimed = db.query(OAuthCode).filter_by(code_hash=row.code_hash).delete()
        db.commit()
    return claims if claimed == 1 and exp >= time.time() else None


def issue_tokens(user_id: str, client_id: str, resource: str) -> dict:
    return {"access_token": _issue("mcp_access", ACCESS_TTL, sub=user_id, aud=resource, cid=client_id),
            "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": _issue("mcp_refresh", REFRESH_TTL, sub=user_id, aud=resource, cid=client_id)}


def read_refresh(token: str) -> Optional[dict]:
    return _read(token, "mcp_refresh")


def user_for(authorization: Optional[str], base: str, exact: Optional[str] = None) -> Optional[str]:
    """The signed-in customer's user_id from an `Authorization: Bearer` header, or None. The
    token must be for this server: for `exact` (the address used) or any of its addresses."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    claims = _read(authorization[7:].strip(), "mcp_access")
    aud = (claims or {}).get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not claims or not any((a == exact) if exact else is_our_resource(base, a) for a in auds):
        return None
    from backend.db.schema import User
    from backend.db.session_utils import get_session
    with get_session() as db:
        u = db.query(User).filter_by(user_id=claims["sub"]).first()
        return u.user_id if u and not u.is_guest and u.status == "active" else None


def current_customer() -> CurrentUser:
    """Inside a customer tool: the signed-in customer (McpSignIn already checked the token)."""
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_headers

    headers = get_http_headers(include_all=True)
    user_id = user_for(headers.get("authorization"), public_base_url(headers))
    if not user_id:
        raise ToolError("Please sign in to ShopSphere first.")
    from backend.db.schema import User
    from backend.db.session_utils import get_session
    with get_session() as db:
        u = db.query(User).filter_by(user_id=user_id).first()
        return CurrentUser(user_id=u.user_id, name=u.name or "", email=u.email or "")


def optional_customer() -> Optional[CurrentUser]:
    """Inside an open tool: the signed-in customer if the chat app sent a valid token, else None."""
    from fastmcp.server.dependencies import get_http_headers

    headers = get_http_headers(include_all=True)
    if not user_for(headers.get("authorization"), public_base_url(headers)):
        return None
    return current_customer()


# A visitor cart (before sign-in) is named by a signed id the chat carries from tool to tool:
# "vc_<visitor user id>_<signature>". It only reaches that anonymous cart, nothing else.
def visitor_cart_id(visitor_id: str) -> str:
    import hashlib
    import hmac
    sig = hmac.new(_secret().encode(), f"visitor:{visitor_id}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"vc_{visitor_id}_{sig}"


def read_visitor_cart(cart_id: Optional[str]) -> Optional[str]:
    """The visitor user id inside a valid visitor cart id, else None."""
    import hmac
    m = re.fullmatch(r"vc_(VIS[0-9A-F]{8,12})_[0-9a-f]{16}", (cart_id or "").strip())
    if not m or not hmac.compare_digest(visitor_cart_id(m.group(1)), cart_id.strip()):
        return None
    return m.group(1)


# Guest checkout (no account, no sign-in): a signed, short-lived token that names ONE guest
# checkout and the anonymous visitor who owns it. Knowing a checkout id alone reaches nothing;
# the token is checked on every guest call and the checkout must still belong to that visitor.
GUEST_TTL = 2 * 3600


def issue_guest_token(visitor_id: str, checkout_id: str) -> str:
    return _issue("guest_checkout", GUEST_TTL, sub=visitor_id, chk=checkout_id)


def read_guest_token(token: Optional[str]) -> Optional[tuple[str, str]]:
    """(visitor id, checkout id) from a valid, unexpired guest token, else None."""
    claims = _read((token or "").strip(), "guest_checkout") if token and len(token) < 1000 else None
    if not claims or not str(claims.get("sub", "")).startswith("VIS") or not claims.get("chk"):
        return None
    return claims["sub"], claims["chk"]


class McpSignIn:
    """ASGI guard in front of the MCP app: customer tools need a valid ShopSphere token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/") if scope["type"] == "http" else ""
        if path != "/mcp" and is_mcp_path(path):     # same MCP server under a fresh address
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        if scope["type"] != "http" or scope.get("method") != "POST" or not is_mcp_path(path):
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        base = public_base_url(headers)
        auth = headers.get("authorization")
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            payload = None
        calls = payload if isinstance(payload, list) else [payload]
        wanted = {(c.get("params") or {}).get("name") for c in calls
                  if isinstance(c, dict) and c.get("method") == "tools/call"}
        needs_customer = bool(wanted & CUSTOMER_TOOLS)
        methods = sorted({str(c.get("method")) for c in calls if isinstance(c, dict)})
        valid = bool(auth) and bool(user_for(auth, base, resource_url(base, path)))
        if auth and not valid:
            claims = _read(auth[7:].strip(), "mcp_access") if auth.lower().startswith("bearer ") else None
            why = "unreadable or expired" if not claims else f"aud={claims.get('aud')}"
            if needs_customer:                       # a customer tool: sign in (or refresh) first
                log_signin("mcp_401_bad_token", path=path, methods=methods, tools=sorted(t for t in wanted if t),
                           token=why, expected=resource_url(base, path))
                return await _challenge(send, base, path)
            # Public work (search, cart, guest checkout, tracking): an expired or wrong token must not
            # turn into a sign-in pop-up. The call goes on as an anonymous visitor, with the token
            # removed so no tool can read it.
            log_signin("mcp_bad_token_ignored_for_public_call", path=path, methods=methods,
                       tools=sorted(t for t in wanted if t), token=why)
            scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", [])
                                          if k.decode("latin-1").lower() != "authorization"]}
            auth = None
        if needs_customer and not auth:
            log_signin("mcp_401_needs_sign_in", path=path, tools=sorted(t for t in wanted if t))
            return await _challenge(send, base, path)
        if auth and ("initialize" in methods or "tools/list" in methods):
            log_signin("mcp_signed_in_call", path=path, methods=methods)

        sent = False

        async def replay():
            # The body we already read, once; after that the real connection (which reports
            # a disconnect only when the client really goes away; streaming replies wait on it).
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await self.app(scope, replay, send)


async def _challenge(send, base: str, path: str = "/mcp") -> None:
    meta = f"{base}/.well-known/oauth-protected-resource{path if is_mcp_path(path) else '/mcp'}"
    body = json.dumps({"error": "invalid_token", "error_description": "Sign in to ShopSphere to continue."}).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"www-authenticate", f'Bearer error="invalid_token", error_description="Sign in to ShopSphere", '
                              f'resource_metadata="{meta}"'.encode()),
    ]})
    await send({"type": "http.response.body", "body": body})
