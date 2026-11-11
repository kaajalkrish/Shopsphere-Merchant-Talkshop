"""
ShopSphere's OAuth 2.1 authorization server for the MCP connector (Claude / ChatGPT).

  1. The chat app finds this server from /.well-known/oauth-protected-resource/mcp.
  2. It identifies itself either with a Client ID Metadata Document (CIMD: client_id is an
     https URL the app hosts, e.g. Claude's published identity) or by registering
     (POST /oauth/register, DCR). Both are stateless: a DCR client_id is signed, and CIMD
     documents are fetched (from an allow-listed host) and cached.
  3. The customer signs in on GET /oauth/authorize with their ShopSphere email and password,
     or creates an account there (name, email, password, shipping address; never a card).
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
from pydantic import ValidationError
from sqlalchemy import func

from backend.auth.security import verify_password
from backend.db.schema import User
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth
from backend.routers import auth as account
from backend.routers.auth import RegisterRequest
from backend.shop import profile

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
@router.get("/.well-known/oauth-protected-resource/{rest:path}")
def protected_resource(request: Request, rest: str = "mcp"):
    base = _base(request)
    path = "/" + rest.strip("/")
    if not mcp_auth.is_mcp_path(path):
        return _error(404, "not_found", "Unknown resource.")
    return {"resource": mcp_auth.resource_url(base, path), "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
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
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": False}


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


def _field(form: dict, label: str, name: str, kind: str = "text", auto: str = "", extra: str = "",
           required: bool = True, value: str = "") -> str:
    val = "" if kind == "password" else html.escape(value or form.get(name, ""), quote=True)
    req = " required" if required else ""
    ac = f' autocomplete="{auto}"' if auto else ""
    return f'<label>{label}</label><input type="{kind}" name="{name}" value="{val}"{req}{ac} {extra}>'


def _page(params: dict, client: dict, error: str = "", email: str = "", mode: str = "signin",
          form: Optional[dict] = None) -> HTMLResponse:
    """The sign-in page, with a Create account tab for new ShopSphere customers."""
    form = form or {}
    signup = mode == "signup"
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(params.get(k, ""), quote=True)}">'
                     for k in _KEEP)
    kept = {k: params[k] for k in _KEEP if params.get(k)}
    other = html.escape("/oauth/authorize?" + urlencode({**kept, "mode": "signin" if signup else "signup"}), quote=True)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    app = html.escape(client["name"])
    f = lambda *a, **k: _field(form, *a, **k)
    if signup:
        body = f"""<h1>Create your ShopSphere account</h1>
<p>Free to join. <b>{app}</b> can then shop for you at ShopSphere, but it can never pay by itself.
Every payment needs your approval. Card details go only on the secure payment page, never in the chat.</p>{err}
<form method="post" action="/oauth/authorize">{hidden}<input type="hidden" name="mode" value="signup">
{f("Full name", "name", auto="name", extra='maxlength="80"')}
{f("Email", "email", "email", "email", value=email)}
{f("Password (8 or more characters)", "password", "password", "new-password", 'minlength="8"')}
<h2>Shipping address</h2>
{f("Street address", "line1", auto="address-line1", extra='maxlength="120"')}
{f("Apt, suite (optional)", "line2", auto="address-line2", extra='maxlength="120"', required=False)}
<div class="three"><div>{f("City", "city", auto="address-level2", extra='maxlength="80"')}</div>
<div>{f("State", "state", auto="address-level1", extra='maxlength="2" placeholder="TX"')}</div>
<div>{f("ZIP", "postal_code", auto="postal-code", extra='maxlength="10" placeholder="78704"')}</div></div>
<button type="submit">Create account and allow</button></form>
<p class="switch">Already have an account? <a href="{other}">Sign in</a></p>"""
    else:
        body = f"""<h1>Sign in to ShopSphere</h1>
<p><b>{app}</b> wants to shop for you at ShopSphere: see your cart, prepare checkouts and show your orders.
It can never pay by itself. Every payment needs your approval.</p>{err}
<form method="post" action="/oauth/authorize">{hidden}<input type="hidden" name="mode" value="signin">
{f("Email", "email", "email", "username", value=email)}
{f("Password", "password", "password", "current-password")}
<button type="submit">Sign in and allow</button></form>
<p class="switch">New to ShopSphere? <a href="{other}">Create an account</a></p>
<p class="hint">Demo accounts use the password <b>demo1234</b>, e.g. kaajal@shopsphere.demo.</p>"""
    title = "Create your ShopSphere account" if signup else "Sign in to ShopSphere"
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;background:#f6f7f9;color:#14171c;margin:0}}
 .box{{max-width:400px;margin:40px auto;background:#fff;border:1px solid #e3e6ea;border-radius:16px;padding:28px}}
 h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:22px 0 0}} p{{color:#555;line-height:1.45}}
 .err{{color:#b00020}}
 label{{display:block;font-size:14px;margin:14px 0 6px}}
 input[type=email],input[type=password],input[type=text]{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd;border-radius:10px;font-size:15px}}
 .three{{display:grid;grid-template-columns:2fr 1fr 1.3fr;gap:8px}}
 button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:999px;background:#0b2a4a;color:#fff;font-size:15px;cursor:pointer}}
 .switch{{text-align:center;font-size:14px;margin-top:16px}} .switch a{{color:#0b2a4a;font-weight:600}}
 .hint{{font-size:13px;color:#777;margin-top:10px}}
 @media (max-width:440px){{.box{{margin:0;border-radius:0;border:0;padding:20px 16px}} .three{{grid-template-columns:1fr 1fr}}}}
</style></head><body><div class="box">{body}</div></body></html>""",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@router.get("/oauth/debug/recent")
def signin_debug():
    """Temporary: the last chat-app sign-in steps (no personal data), to diagnose sign-in failures."""
    return {"events": list(mcp_auth.SIGNIN_LOG)}


def _params_info(params: dict) -> dict:
    return {"client": mcp_auth.client_label(params.get("client_id")), "resource": params.get("resource") or "-",
            "redirect": params.get("redirect_uri") or "-", "scope": params.get("scope") or "-",
            "has_state": bool(params.get("state")), "mode": params.get("mode") or "signin"}


@router.get("/oauth/authorize")
def authorize_page(request: Request):
    params = dict(request.query_params)
    client, problem = _check(params)
    mcp_auth.log_signin("authorize_page", **_params_info(params), problem=problem or "-")
    if problem:
        return HTMLResponse(f"<p>{html.escape(problem)}</p>", status_code=400)
    return _page(params, client, mode=params.get("mode", "signin"))


_ADDRESS_FIELDS = ("line1", "line2", "city", "state", "postal_code")


def _sign_up(params: dict) -> tuple[Optional[str], str]:
    """Create a ShopSphere customer with their shipping address: (user_id, "") or (None, problem).
    The same account the website's sign-up makes; no card details are ever asked for here."""
    name = " ".join(params.get("name", "").split())[:80]
    password = params.get("password", "")
    try:
        email = str(RegisterRequest(name=name or "-", email=params.get("email", "").strip(),
                                    password=password).email).lower()
    except ValidationError as exc:
        bad = {str(e["loc"][0]) for e in exc.errors()}
        return None, ("Use a password of 8 or more characters." if "password" in bad else "Enter a valid email address.")
    if not name:
        return None, "Please enter your name."
    if account.probe_blocked(email):
        return None, "Too many attempts for this email. Please wait 15 minutes, or sign in instead."
    with get_session() as db:
        if db.query(User).filter(func.lower(User.email) == email).first():
            account.note_signup_probe(email)
            return None, "An account with this email already exists. Sign in instead."
        user_id = account.add_customer(db, name, email, password, source="chat_app")
        db.flush()
        try:                                       # commits the account and its address together
            profile.add_address(db, user_id, full_name=name, label="Home", make_default=True,
                                **{k: params.get(k, "") for k in _ADDRESS_FIELDS})
        except profile.ProfileError as exc:
            db.rollback()
            return None, exc.message
    return user_id, ""


@router.post("/oauth/authorize")
async def authorize_submit(request: Request):
    params = {k: str(v) for k, v in (await request.form()).items()}
    client, problem = _check(params)
    if problem:
        mcp_auth.log_signin("authorize_submit", **_params_info(params), result="refused: " + problem)
        return HTMLResponse(f"<p>{html.escape(problem)}</p>", status_code=400)
    email = params.get("email", "").strip().lower()
    if params.get("mode") == "signup":
        user_id, problem = _sign_up(params)
        if not user_id:
            mcp_auth.log_signin("authorize_submit", **_params_info(params), result="form problem: " + problem)
            return _page(params, client, problem, email, mode="signup",
                         form={k: params.get(k, "") for k in ("name", *_ADDRESS_FIELDS)})
    else:
        with get_session() as db:
            u = db.query(User).filter(func.lower(User.email) == email).first()
            ok = bool(u and not u.is_guest and u.status == "active" and verify_password(params.get("password", ""),
                                                                                        u.password_hash))
            user_id = u.user_id if ok else None
        if not user_id:
            mcp_auth.log_signin("authorize_submit", **_params_info(params), result="wrong email or password")
            return _page(params, client, "Incorrect email or password.", email)
    base = _base(request)
    code = mcp_auth.issue_code(sub=user_id, cid=params["client_id"], ru=params["redirect_uri"],
                               cc=params["code_challenge"],
                               res=_resource(base, params.get("resource")))
    query = {"code": code, **({"state": params["state"]} if params.get("state") else {})}
    sep = "&" if urlparse(params["redirect_uri"]).query else "?"
    mcp_auth.log_signin("authorize_submit", **_params_info(params), result="code issued, back to the app",
                        code_for=_resource(base, params.get("resource")))
    return RedirectResponse(f"{params['redirect_uri']}{sep}{urlencode(query)}", status_code=302)


# ── Tokens ──────────────────────────────────────────────────────────────────────

def _resource(base: str, wanted) -> str:
    """The MCP address a token is for: the one the chat app asked for (/mcp or /shop)."""
    wanted = (wanted[0] if isinstance(wanted, list) and wanted else wanted) or ""
    wanted = str(wanted).rstrip("/")
    return wanted if mcp_auth.is_our_resource(base, wanted) else mcp_auth.resource_url(base)


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


@router.post("/oauth/token")
async def token(request: Request):
    raw = await request.body()
    ctype = request.headers.get("content-type", "-")
    try:
        form = {k: str(v) for k, v in (await request.form()).items()}
    except Exception:                               # not a form: note what came, refuse
        form = {}
    res = _token(form, _base(request))
    info = {"grant": form.get("grant_type") or "-", "client": mcp_auth.client_label(form.get("client_id")),
            "resource": form.get("resource") or "-", "redirect": form.get("redirect_uri") or "-",
            "has_verifier": bool(form.get("code_verifier")), "content_type": ctype[:60],
            "basic_auth": request.headers.get("authorization", "").lower().startswith("basic"),
            "fields": sorted(form), "body_bytes": len(raw), "status": res.status_code}
    if res.status_code != 200:
        try:
            info["error"] = json.loads(res.body).get("error_description")
        except ValueError:
            pass
    mcp_auth.log_signin("token", **info)
    return res


def _token(form: dict, base: str):
    if form.get("resource") and not mcp_auth.is_our_resource(base, form["resource"].rstrip("/")):
        return _error(400, "invalid_target", "This server only issues tokens for its MCP endpoints.")
    grant = form.get("grant_type")
    if grant == "authorization_code":
        claims = mcp_auth.redeem_code(form.get("code", ""))
        if not claims:
            return _error(400, "invalid_grant", "The code is invalid, expired or already used.")
        if claims["cid"] != form.get("client_id") or claims["ru"] != form.get("redirect_uri"):
            return _error(400, "invalid_grant", "The code was issued to a different app.")
        if not _pkce_ok(form.get("code_verifier", ""), claims["cc"]):
            return _error(400, "invalid_grant", "PKCE verification failed.")
        resource = _resource(base, form.get("resource") or claims.get("res"))
        return JSONResponse(mcp_auth.issue_tokens(claims["sub"], claims["cid"], resource),
                            headers={"Cache-Control": "no-store"})
    if grant == "refresh_token":
        claims = mcp_auth.read_refresh(form.get("refresh_token", ""))
        if not claims or claims.get("cid") != form.get("client_id"):
            return _error(400, "invalid_grant", "Please sign in to ShopSphere again.")
        resource = _resource(base, form.get("resource") or claims.get("aud"))
        return JSONResponse(mcp_auth.issue_tokens(claims["sub"], claims["cid"], resource),
                            headers={"Cache-Control": "no-store"})
    return _error(400, "unsupported_grant_type", "Use authorization_code or refresh_token.")
