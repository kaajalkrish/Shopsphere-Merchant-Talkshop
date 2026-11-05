"""
ShopSphere's OAuth 2.1 authorization server for the MCP connector (Claude / ChatGPT).

  1. The chat app finds this server from /.well-known/oauth-protected-resource/mcp.
  2. It identifies itself either with a Client ID Metadata Document (CIMD: client_id is an
     https URL the app hosts, e.g. Claude's published identity) or by registering
     (POST /oauth/register, DCR). Both are stateless: a DCR client_id is signed, and CIMD
     documents are fetched (from an allow-listed host) and cached.
  3. The customer signs in on GET /oauth/authorize with their ShopSphere email and password.
  4. A one-time code (PKCE S256) is exchanged at POST /oauth/token for an access token
     bound to this server's /mcp, plus a refresh token (backend/mcp/auth.py).
"""
import base64
import hashlib
import hmac
import html
import json
import os
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.auth.security import verify_password
from backend.db.schema import User
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth

router = APIRouter(tags=["oauth"])

# Hosts whose client metadata documents we fetch (no fetching arbitrary URLs).
_CIMD_HOSTS = tuple(h.strip().lower() for h in os.getenv(
    "OAUTH_CIMD_HOSTS", "claude.ai,claude.com,anthropic.com,chatgpt.com,openai.com").split(",") if h.strip())
_cimd_cache: dict[str, tuple[float, dict]] = {}


def _base(request: Request) -> str:
    return mcp_auth.public_base_url(dict(request.headers))


def _error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "error_description": description},
                        headers={"Cache-Control": "no-store"})


# ── Discovery ───────────────────────────────────────────────────────────────────

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    base = _base(request)
    return {"resource": mcp_auth.resource_url(base), "authorization_servers": [base],
            "scopes_supported": ["shop"], "bearer_methods_supported": ["header"],
            "resource_name": "ShopSphere"}


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def authorization_server(request: Request):
    base = _base(request)
    return {"issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["shop"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True}


# ── Clients: CIMD (URL client ids) and stateless DCR ────────────────────────────

def _ok_redirect(uri: str) -> bool:
    p = urlparse(uri)
    return (p.scheme == "https" and bool(p.netloc)) or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))


def _sign(data: str) -> str:
    return hmac.new(mcp_auth._secret().encode(), data.encode(), hashlib.sha256).hexdigest()[:32]


@router.post("/oauth/register", status_code=201)
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "invalid_client_metadata", "Send JSON client metadata.")
    uris = [str(u) for u in (body.get("redirect_uris") or [])]
    if not uris or not all(_ok_redirect(u) for u in uris):
        return _error(400, "invalid_redirect_uri", "redirect_uris must be https (or http on localhost).")
    name = str(body.get("client_name") or "Chat app")[:80]
    packed = base64.urlsafe_b64encode(json.dumps({"r": uris, "n": name}).encode()).decode().rstrip("=")
    client_id = f"dcr.{packed}.{_sign(packed)}"
    return {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name,
            "redirect_uris": uris, "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}


def _client(client_id: str) -> Optional[dict]:
    """{"name", "redirect_uris"} for a CIMD URL or a signed DCR id; None if unknown."""
    if client_id.startswith("dcr."):
        try:
            _, packed, sig = client_id.split(".")
        except ValueError:
            return None
        if not hmac.compare_digest(sig, _sign(packed)):
            return None
        data = json.loads(base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)))
        return {"name": data["n"], "redirect_uris": data["r"]}
    p = urlparse(client_id)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in _CIMD_HOSTS):
        return None
    hit = _cimd_cache.get(client_id)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        r = httpx.get(client_id, timeout=5.0, follow_redirects=False)
        doc = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    uris = [str(u) for u in doc.get("redirect_uris") or []]
    if doc.get("client_id") != client_id or not uris:
        return None
    client = {"name": str(doc.get("client_name") or host)[:80], "redirect_uris": uris}
    _cimd_cache[client_id] = (time.time() + 3600, client)
    return client


def _check(params: dict) -> tuple[Optional[dict], Optional[str]]:
    client = _client(params.get("client_id", ""))
    if not client:
        return None, "This app isn't recognised. Remove the connector and add it again."
    if params.get("redirect_uri") not in client["redirect_uris"]:
        return None, "The return address doesn't match this app."
    if params.get("response_type") != "code":
        return None, "Only response_type=code is supported."
    if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        return None, "PKCE (S256) is required."
    return client, None


# ── Sign-in page ────────────────────────────────────────────────────────────────

_KEEP = ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type",
         "resource", "scope")


def _page(params: dict, client: dict, error: str = "", email: str = "") -> HTMLResponse:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(params.get(k, ""), quote=True)}">'
                     for k in _KEEP)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    app = html.escape(client["name"])
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in to ShopSphere</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;background:#f6f7f9;color:#14171c;margin:0}}
 .box{{max-width:380px;margin:56px auto;background:#fff;border:1px solid #e3e6ea;border-radius:16px;padding:28px}}
 h1{{font-size:22px;margin:0 0 6px}} p{{color:#555;line-height:1.45}} .err{{color:#b00020}}
 label{{display:block;font-size:14px;margin:14px 0 6px}}
 input[type=email],input[type=password]{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd;border-radius:10px;font-size:15px}}
 button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:999px;background:#0b2a4a;color:#fff;font-size:15px;cursor:pointer}}
 .hint{{font-size:13px;color:#777;margin-top:16px}}
</style></head><body><div class="box">
<h1>Sign in to ShopSphere</h1>
<p><b>{app}</b> wants to shop for you at ShopSphere: see your cart, prepare checkouts and show your orders.
It can never pay by itself. Every payment needs your approval.</p>{err}
<form method="post" action="/oauth/authorize">{hidden}
<label>Email</label><input type="email" name="email" required autocomplete="username" value="{html.escape(email, quote=True)}">
<label>Password</label><input type="password" name="password" required autocomplete="current-password">
<button type="submit">Sign in and allow</button></form>
<p class="hint">Demo accounts use the password <b>demo1234</b>, e.g. kaajal@shopsphere.demo.</p>
</div></body></html>""", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@router.get("/oauth/authorize")
def authorize_page(request: Request):
    params = dict(request.query_params)
    client, problem = _check(params)
    if problem:
        return HTMLResponse(f"<p>{html.escape(problem)}</p>", status_code=400)
    return _page(params, client)


@router.post("/oauth/authorize")
async def authorize_submit(request: Request):
    params = {k: str(v) for k, v in (await request.form()).items()}
    client, problem = _check(params)
    if problem:
        return HTMLResponse(f"<p>{html.escape(problem)}</p>", status_code=400)
    email = params.get("email", "").strip().lower()
    with get_session() as db:
        u = db.query(User).filter(User.email == email).first()
        ok = bool(u and not u.is_guest and u.status == "active" and verify_password(params.get("password", ""),
                                                                                    u.password_hash))
        user_id = u.user_id if ok else None
    if not user_id:
        return _page(params, client, "Incorrect email or password.", email)
    base = _base(request)
    code = mcp_auth.issue_code(sub=user_id, cid=params["client_id"], ru=params["redirect_uri"],
                               cc=params["code_challenge"],
                               res=params.get("resource") or mcp_auth.resource_url(base))
    query = {"code": code, "iss": base, **({"state": params["state"]} if params.get("state") else {})}
    sep = "&" if urlparse(params["redirect_uri"]).query else "?"
    return RedirectResponse(f"{params['redirect_uri']}{sep}{urlencode(query)}", status_code=302)


# ── Tokens ──────────────────────────────────────────────────────────────────────

def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


@router.post("/oauth/token")
async def token(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    base = _base(request)
    resource = mcp_auth.resource_url(base)
    if form.get("resource") and form["resource"].rstrip("/") != resource:
        return _error(400, "invalid_target", "This server only issues tokens for its /mcp endpoint.")
    grant = form.get("grant_type")
    if grant == "authorization_code":
        claims = mcp_auth.redeem_code(form.get("code", ""))
        if not claims:
            return _error(400, "invalid_grant", "The code is invalid, expired or already used.")
        if claims["cid"] != form.get("client_id") or claims["ru"] != form.get("redirect_uri"):
            return _error(400, "invalid_grant", "The code was issued to a different app.")
        if not _pkce_ok(form.get("code_verifier", ""), claims["cc"]):
            return _error(400, "invalid_grant", "PKCE verification failed.")
        return JSONResponse(mcp_auth.issue_tokens(claims["sub"], claims["cid"], resource),
                            headers={"Cache-Control": "no-store"})
    if grant == "refresh_token":
        claims = mcp_auth.read_refresh(form.get("refresh_token", ""))
        if not claims or claims.get("cid") != form.get("client_id"):
            return _error(400, "invalid_grant", "Please sign in to ShopSphere again.")
        return JSONResponse(mcp_auth.issue_tokens(claims["sub"], claims["cid"], resource),
                            headers={"Cache-Control": "no-store"})
    return _error(400, "unsupported_grant_type", "Use authorization_code or refresh_token.")
