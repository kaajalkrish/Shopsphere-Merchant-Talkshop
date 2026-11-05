"""
Sign-in for the ShopSphere MCP connector (Claude / ChatGPT).

Searching is open. Cart, checkout, payment and orders need the customer's ShopSphere
account: the chat app signs the customer in once through ShopSphere's OAuth 2.1 server
(backend/oauth/server.py) and then sends `Authorization: Bearer <token>` to /mcp.

Tokens are signed JWTs (JWT_SECRET_KEY), so nothing has to be stored and a backend
restart doesn't sign anyone out:
  mcp_access   1 day   sub=user, aud=<this server>/mcp, cid=<chat app's client id>
  mcp_refresh  30 days same, used only at /oauth/token
  mcp_code     5 min   the one-time authorization code (PKCE challenge inside)

McpSignIn guards /mcp: a call to a customer tool without a valid token gets HTTP 401 with
WWW-Authenticate pointing at the protected-resource metadata, which is how the chat app
knows to show "Sign in to ShopSphere".
"""
import json
import os
import secrets
import time
from typing import Optional

import jwt

from backend.auth.dependencies import CurrentUser

ALG = "HS256"
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300

# Tools that act for a signed-in customer. Everything else (search, product details) is open.
CUSTOMER_TOOLS = {"my_shopsphere_account", "add_to_cart", "show_cart", "update_cart_line", "checkout",
                  "approve_and_pay", "my_orders"}

_used_codes: dict[str, float] = {}          # jti -> expiry: each authorization code works once


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


def resource_url(base: str) -> str:
    return f"{base}/mcp"


def _issue(typ: str, ttl: int, **claims) -> str:
    now = int(time.time())
    return jwt.encode({"typ": typ, "iat": now, "exp": now + ttl, "jti": secrets.token_urlsafe(12), **claims},
                      _secret(), algorithm=ALG)


def _read(token: str, typ: str, audience: Optional[str] = None) -> Optional[dict]:
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALG], audience=audience,
                            options={"verify_aud": audience is not None})
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == typ else None


def issue_code(**claims) -> str:
    return _issue("mcp_code", CODE_TTL, **claims)


def redeem_code(code: str) -> Optional[dict]:
    """The code's claims, once. A second use (or an expired code) gives None."""
    claims = _read(code, "mcp_code")
    if not claims:
        return None
    now = time.time()
    for jti in [j for j, exp in _used_codes.items() if exp < now]:
        del _used_codes[jti]
    if claims["jti"] in _used_codes:
        return None
    _used_codes[claims["jti"]] = claims["exp"]
    return claims


def issue_tokens(user_id: str, client_id: str, resource: str) -> dict:
    return {"access_token": _issue("mcp_access", ACCESS_TTL, sub=user_id, aud=resource, cid=client_id),
            "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": _issue("mcp_refresh", REFRESH_TTL, sub=user_id, aud=resource, cid=client_id),
            "scope": "shop"}


def read_refresh(token: str) -> Optional[dict]:
    return _read(token, "mcp_refresh")


def user_for(authorization: Optional[str], resource: str) -> Optional[str]:
    """The signed-in customer's user_id from an `Authorization: Bearer` header, or None."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    claims = _read(authorization[7:].strip(), "mcp_access", audience=resource)
    if not claims:
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
    user_id = user_for(headers.get("authorization"), resource_url(public_base_url(headers)))
    if not user_id:
        raise ToolError("Please sign in to ShopSphere first.")
    from backend.db.schema import User
    from backend.db.session_utils import get_session
    with get_session() as db:
        u = db.query(User).filter_by(user_id=user_id).first()
        return CurrentUser(user_id=u.user_id, name=u.name or "", email=u.email or "")


class McpSignIn:
    """ASGI guard in front of the MCP app: customer tools need a valid ShopSphere token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path", "").rstrip("/") != "/mcp":
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
        if (auth and not user_for(auth, resource_url(base))) or (needs_customer and not auth):
            return await _challenge(send, base)

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


async def _challenge(send, base: str) -> None:
    meta = f"{base}/.well-known/oauth-protected-resource/mcp"
    body = json.dumps({"error": "invalid_token", "error_description": "Sign in to ShopSphere to continue."}).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"www-authenticate", f'Bearer error="invalid_token", error_description="Sign in to ShopSphere", '
                              f'resource_metadata="{meta}"'.encode()),
    ]})
    await send({"type": "http.response.body", "body": body})
