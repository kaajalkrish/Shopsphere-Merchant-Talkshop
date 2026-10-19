"""
Simulated Secure Payment Provider — part of the mock gateway / PSP service.

This stands in for a payment provider's hosted payment page / iframe. The store
embeds it (a cross-origin iframe inside its Secure Payment Sheet), so the card form
belongs to the PROVIDER: the store's page and Talkshop can't read it. It is a
SIMULATION, not a PCI boundary: it accepts only well-known TEST card numbers
(anything else is refused), keeps the network + last 4 and drops the rest.

  Merchant (server-to-server, X-Gateway-Key):
    POST /v1/payment_sessions                     create a session bound to merchant +
                                                  checkout + payment_ref + version + amount + currency
    GET  /v1/payment_sessions/{id}                the SAFE result: status, opaque token, network, last 4,
                                                  verification state — never a card number or CVC
    POST /v1/payment_sessions/{id}/authorize      PayIt asks for the authorization (only once the card is
                                                  tokenized AND verified); idempotent on payment_ref
    POST /v1/payment_sessions/{id}/void           cancel / release
  Customer's browser (the provider-owned page; no ShopSphere code runs here):
    GET  /hosted/{id}                             the page for the session's current state
    POST /hosted/{id}/pay                         TOKENIZE the test card (nothing charged)
    POST /hosted/{id}/continue                    after tokenization: verification, or frictionless
    POST /hosted/{id}/challenge                   simulated payment verification: ok | fail | cancel,
                                                  with the one-time challenge

  Session status:
    PENDING ──card──► TOKENIZED ─┬─ verification_required ─► VERIFICATION_REQUIRED ─ok─► VERIFIED
                                 └─ frictionless ─────────────────────────────────────► VERIFIED
    VERIFIED ──PayIt authorize──► CAPTURED | DECLINED | TIMEOUT
    any open state ─► CANCELLED (customer / store) · EXPIRED (10 min) · VERIFICATION_FAILED (3 failed checks)

Verification is modelled the way issuers behave: the provider decides per card whether
a check is needed (here: by test-card fixture). Real issuers may approve frictionlessly,
or ask for an OTP, an app approval, a biometric/passkey or a 3DS challenge; this demo
shows a passkey-style step as one example. Each challenge is single use, lives 5 minutes
and only its hash is stored.
"""
import hashlib
import html
import json
import os
import re
import secrets
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend.db.schema import ProviderPaymentSession
from backend.db.session_utils import get_session
from backend.vic.common import new_ref, now

GATEWAY_KEY = os.getenv("MOCK_GATEWAY_KEY", "shopsphere-demo-merchant-key")
SESSION_TTL = timedelta(minutes=10)
CHALLENGE_TTL = timedelta(minutes=5)
MAX_VERIFICATION_ATTEMPTS = 3
NAME = "Simulated Secure Payment Provider"
MERCHANT_NAMES = {"SHOPSPHERE": "ShopSphere"}
NETWORK_NAMES = {"VISA": "Visa", "MASTERCARD": "Mastercard", "AMEX": "American Express", "DISCOVER": "Discover"}
OPEN = ("PENDING", "TOKENIZED", "VERIFICATION_REQUIRED", "VERIFIED")
CARD_KNOWN = ("TOKENIZED", "VERIFICATION_REQUIRED", "VERIFIED", "CAPTURED", "DECLINED", "TIMEOUT")

router = APIRouter()


def _check_key(key: Optional[str]) -> None:
    if not key or not secrets.compare_digest(key, GATEWAY_KEY):
        raise HTTPException(status_code=401, detail="Unknown merchant credentials")


def _load(db, session_id: str) -> ProviderPaymentSession:
    row = db.query(ProviderPaymentSession).filter_by(session_id=session_id).first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_SESSION", "message": "Unknown payment session"})
    if row.status in OPEN and now() > row.expires_at:
        row.status = "EXPIRED"
        db.commit()
    return row


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_view(row: ProviderPaymentSession) -> dict:
    """What the merchant may know. The opaque token, network and last 4 once the card is
    tokenized; display metadata (name, expiry) only when the customer asked to save it.
    Never a card number, CVC or challenge (the provider doesn't keep the first two)."""
    known = row.status in CARD_KNOWN
    saving = bool(row.save_requested) and known
    return {"payment_session_id": row.session_id, "status": row.status, "decline_reason": row.decline_reason,
            "merchant_id": row.merchant_id, "checkout_id": row.checkout_id, "payment_ref": row.payment_ref,
            "checkout_version": row.checkout_version, "amount": row.amount, "currency": row.currency,
            "customer_ref": row.customer_ref,
            "payment_token": row.payment_token if known else None,
            "network": row.network if known else None, "last4": row.last4 if known else None,
            "verification_required": bool(row.verification_required) if known else None,
            "tokenized_at": row.tokenized_at.isoformat() if row.tokenized_at else None,
            "verified_at": row.verified_at.isoformat() if row.verified_at else None,
            "authorization_reference": row.authorization_reference if row.status == "CAPTURED" else None,
            "save_requested": saving,
            "cardholder_display": row.cardholder_display if saving else None,
            "card_exp_month": row.card_exp_month if saving else None,
            "card_exp_year": row.card_exp_year if saving else None,
            "expires_at": row.expires_at.isoformat(), "used": row.used_at is not None, "simulated": True}


# ── merchant API (server-to-server) ──────────────────────────────────────────

class NewSession(BaseModel):
    merchant_id: str
    checkout_id: str
    payment_ref: str
    checkout_version: int = Field(..., ge=1)
    amount: float = Field(..., gt=0)
    currency: str = "USD"
    customer_ref: str
    save_allowed: bool = False
    return_url: Optional[str] = Field(None, max_length=300)


@router.post("/v1/payment_sessions")
def create_session(req: NewSession, x_gateway_key: Optional[str] = Header(None)):
    _check_key(x_gateway_key)
    with get_session() as db:
        row = ProviderPaymentSession(session_id=new_ref("pps"), status="PENDING", expires_at=now() + SESSION_TTL,
                                     **req.model_dump())
        db.add(row)
        db.commit()
        return {**safe_view(row), "hosted_path": f"/hosted/{row.session_id}"}


@router.get("/v1/payment_sessions/{session_id}")
def get_session_result(session_id: str, x_gateway_key: Optional[str] = Header(None)):
    _check_key(x_gateway_key)
    with get_session() as db:
        return safe_view(_load(db, session_id))


class Authorize(BaseModel):
    merchant_id: str
    payment_ref: str
    amount: float
    currency: str


@router.post("/v1/payment_sessions/{session_id}/authorize")
def authorize(session_id: str, req: Authorize, x_gateway_key: Optional[str] = Header(None)):
    """PayIt's authorization request. Only a VERIFIED credential can be authorized, only for
    exactly what the session was created for, and only once: a repeat with the same
    payment_ref returns the original answer (no double charge)."""
    _check_key(x_gateway_key)
    with get_session() as db:
        row = _load(db, session_id)
        if (row.merchant_id, row.payment_ref, row.currency) != (req.merchant_id, req.payment_ref, req.currency) \
                or abs(row.amount - req.amount) > 0.005:
            raise HTTPException(status_code=409, detail={"code": "SESSION_BINDING_MISMATCH",
                                                         "message": "Not the payment this session was created for"})
        if row.status in ("CAPTURED", "DECLINED", "TIMEOUT") and row.used_at:
            return {**safe_view(row), "duplicate": True}
        if row.status != "VERIFIED":
            raise HTTPException(status_code=409, detail={"code": "CREDENTIAL_NOT_READY", "message": row.status})
        outcome, reason = _outcome_for(row)
        row.used_at = now()
        if outcome == "approve":
            row.status = "CAPTURED"
            row.authorization_reference = f"AUTH_{secrets.token_hex(5).upper()}"
        elif outcome == "timeout":
            row.status, row.decline_reason = "TIMEOUT", reason
        else:
            row.status, row.decline_reason = "DECLINED", reason
        db.commit()
        return {**safe_view(row), "duplicate": False}


@router.post("/v1/payment_sessions/{session_id}/void")
def void(session_id: str, x_gateway_key: Optional[str] = Header(None)):
    _check_key(x_gateway_key)
    with get_session() as db:
        row = _load(db, session_id)
        if row.status == "CAPTURED":
            row.status = "VOIDED"
        elif row.status in OPEN:
            row.status = "CANCELLED"
        row.challenge_hash = None
        db.commit()
        return safe_view(row)


# ── the provider-owned hosted page (customer's browser) ──────────────────────

_CSS = """body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f5f7;color:#1b1f24;margin:0}
main{max-width:460px;margin:32px auto;background:#fff;border-radius:18px;padding:24px;box-shadow:0 6px 24px rgba(0,0,0,.08)}
body.framed{background:#fff}body.framed main{margin:0 auto;box-shadow:none;padding:16px 18px;border-radius:0}
body.framed .standalone{display:none}
h1{font-size:17px;margin:6px 0 2px}h2{font-size:15px;margin:0 0 12px}.sub{color:#5b6470;font-size:12px;margin:0 0 12px}
.amt{font-size:26px;font-weight:700;margin:2px 0 12px}.row{color:#5b6470;font-size:13px;margin:0}
.badge{display:inline-block;background:#fff4d6;color:#7a5a00;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:600}
.f{display:flex;flex-direction:column;gap:4px;margin:0 0 10px;font-size:12px;color:#3a414a}
.f input{font-size:15px;padding:10px 12px;border:1px solid #cfd5dc;border-radius:10px;font-family:inherit}
.f input:focus{outline:2px solid #0b5cad33;border-color:#0b5cad}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button{display:block;width:100%;margin:8px 0;padding:12px;border-radius:12px;border:1px solid #d5d9df;background:#fff;font-size:15px;cursor:pointer;font-family:inherit}
button.ok{background:#0b5cad;color:#fff;border-color:#0b5cad;font-weight:600}
button[disabled]{opacity:.6;cursor:default}
label.save{display:flex;gap:8px;align-items:flex-start;font-size:12px;margin:6px 0 4px;color:#333}
.err{background:#fdecec;color:#8a1c1c;border-radius:10px;padding:9px 12px;font-size:13px;margin:0 0 10px}
.note{font-size:11px;color:#5b6470;margin-top:12px;line-height:1.5}.done{font-size:15px;font-weight:600;margin:6px 0}
.tests{font-size:11px;color:#5b6470;background:#f6f7f9;border-radius:10px;padding:8px 10px;margin-top:10px;line-height:1.6}
.tests code{font-size:11px;color:#1b1f24}
.center{display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;padding:24px 0 8px}
.tick{width:44px;height:44px;border-radius:999px;background:#e6f6ec;color:#17803d;display:grid;place-items:center;font-size:22px;font-weight:700}
.card{font-size:15px;font-weight:600;letter-spacing:.02em}.muted{color:#5b6470;font-size:12px}
.v{border-radius:16px;overflow:hidden;border:1px solid #e3e6ea}
.vh{background:#0b2342;color:#fff;padding:14px 16px}.vh .eb{font-size:10px;font-weight:700;letter-spacing:.18em;color:#d9b45a;text-transform:uppercase;margin:0}
.vh h2{color:#fff;margin:4px 0 2px;font-size:17px}.vh p{margin:0;font-size:11px;color:#ffffffb3}
.vb{padding:14px 16px}dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0 0 12px;font-size:13px}
dt{color:#5b6470}dd{margin:0;text-align:right;font-weight:600}
button.fp{display:flex;align-items:center;justify-content:center;gap:8px;border-radius:999px;height:48px;padding:0}
button.fp.busy svg{animation:p 1s ease-in-out infinite}@keyframes p{50%{opacity:.35}}
.links{display:flex;justify-content:space-between;font-size:12px}.links button{width:auto;margin:0;padding:4px 0;border:0;background:none;color:#5b6470;text-decoration:underline;font-size:12px}
.foot{display:flex;gap:6px;font-size:11px;color:#5b6470;line-height:1.45;margin-top:10px}
a.back{display:block;text-align:center;margin-top:12px;padding:11px;border-radius:12px;background:#0b5cad;color:#fff;text-decoration:none}"""

FINGERPRINT = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12C2 6.5 6.5 2 12 2a10 10 '
               '0 0 1 8 4"/><path d="M5 19.5C5.5 18 6 15 6 12a6 6 0 0 1 .34-2"/><path d="M17.29 21.02c.12-.6.43-2.3.5-3.02"/>'
               '<path d="M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4"/><path d="M8.65 22c.21-.66.45-1.32.57-2"/><path d="M14 '
               '13.12c0 2.38 0 6.38-1 8.88"/><path d="M2 16h.01"/><path d="M21.8 16c.2-2 .131-5.354 0-6"/><path d="M9 6.8a6 6 '
               '0 0 1 9 5.2c0 .47 0 1.17-.02 2"/></svg>')

# Simulation-only TEST cards (the well-known public test numbers). Anything else is refused,
# so nobody can type a real card into the demo. The number is used only to pick the
# simulated behaviour and is never stored or returned: the merchant gets network + last 4 + token.
#   number → (network, authorization outcome, reason, verification_required)
TEST_CARDS = {
    "4242424242424242": ("VISA", "approve", None, True), "5555555555554444": ("MASTERCARD", "approve", None, True),
    "378282246310005": ("AMEX", "approve", None, True), "6011111111111117": ("DISCOVER", "approve", None, True),
    "4000002760003184": ("VISA", "approve", None, True),
    "4000000000003055": ("VISA", "approve", None, False),            # frictionless: no verification needed
    "4000000000000002": ("VISA", "decline", "CARD_DECLINED", True),
    "4000000000009995": ("VISA", "decline", "INSUFFICIENT_FUNDS", True),
    "4000000000000119": ("VISA", "timeout", "PAYMENT_TIMEOUT", True),
    "4000000000000101": ("VISA", "tokenize_fail", "TOKENIZATION_FAILED", True),
}
TEST_CARD_HELP = ("<b>Test cards only (simulation):</b> Visa <code>4242 4242 4242 4242</code> · Mastercard "
                  "<code>5555 5555 5555 4444</code> · Amex <code>3782 822463 10005</code> · Discover "
                  "<code>6011 1111 1111 1117</code> · no verification needed <code>4000 0000 0000 3055</code> · "
                  "declined <code>4000 0000 0000 0002</code> · timeout <code>4000 0000 0000 0119</code> · can't be "
                  "secured <code>4000 0000 0000 0101</code>. Any future expiry, any CVC.")
FRAME_ORIGINS = tuple(o.strip().rstrip("/") for o in os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:3000,"
                        "http://127.0.0.1:5173,http://127.0.0.1:5174").split(",") if o.strip())


def _origin(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parts = url.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 and parts[0] in ("http:", "https:") else None


def _post_to_store(row: ProviderPaymentSession, message: dict) -> str:
    """A script that tells the embedding store page something SAFE (session, step or status,
    network + last 4 at most) — only ever to the store's own origin. Never card data, never
    the token, never a challenge."""
    parent = _origin(row.return_url)
    if not parent:
        return ""
    payload = json.dumps({**message, "session": row.session_id})
    return f"<script>if(window.parent!==window){{window.parent.postMessage({payload},{json.dumps(parent)});}}</script>"


def _page(row: Optional[ProviderPaymentSession], body: str, *, step: Optional[dict] = None,
          result: bool = False) -> HTMLResponse:
    """The provider-owned page. Only ShopSphere's own origins may embed it (frame-ancestors).
    Inside the store's Secure Payment Sheet the provider's own header is hidden (the sheet
    already shows merchant and amount); opened on its own, the page shows everything."""
    script = ""
    if row and step:
        script += _post_to_store(row, {"type": "shopsphere-provider-step", **step})
    if row and result:
        script += _post_to_store(row, {"type": "shopsphere-provider-result", "status": row.status,
                                       **({"network": row.network, "last4": row.last4}
                                          if row.status in ("VERIFIED", "CAPTURED") else {})})
    resp = HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{NAME}</title>
<style>{_CSS}</style></head>
<body><script>if(window.parent!==window)document.body.className='framed';</script><main>
<div class="standalone"><span class="badge">SIMULATION · test cards only</span>
<h1>{NAME}</h1><p class="sub">Secure payment page run by the payment provider, not by the store. The store and its
AI assistant never see your card number.</p>{_summary(row) if row else ""}</div>
{body}
<p class="note">Simulated payment provider: the store receives only the result, an opaque token, the card network and
the last 4 digits. Never enter a real card.</p></main>
{script}</body></html>""")
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self' " + " ".join(FRAME_ORIGINS)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _merchant(row: ProviderPaymentSession) -> str:
    return MERCHANT_NAMES.get(row.merchant_id, row.merchant_id.title())


def _summary(row: ProviderPaymentSession) -> str:
    return (f'<p class="row">Pay {html.escape(_merchant(row))} · order {html.escape(row.checkout_id)}</p>'
            f'<p class="amt">{html.escape(row.currency)} {row.amount:,.2f}</p>')


def _card_label(row: ProviderPaymentSession) -> str:
    return f"{NETWORK_NAMES.get(row.network or '', 'Card')} •••• {row.last4}"


def _finished(row: ProviderPaymentSession, message: str, title: str = "") -> HTMLResponse:
    if row.return_url:
        sep = "&" if "?" in row.return_url else "?"
        url = f"{row.return_url}{sep}provider_session={row.session_id}"
        back = (f'<a class="back standalone" target="_top" href="{html.escape(url, quote=True)}">Return to '
                f'{html.escape(_merchant(row))}</a>')
    else:
        back = '<p class="note standalone">You can return to the store now.</p>'
    head = f'<div class="center"><div class="tick">&#10003;</div><p class="done">{title}</p>' if title else ""
    tail = "</div>" if title else ""
    return _page(row, head + f'<p class="{"muted" if title else "done"}">{message}</p>' + tail + back, result=True)


def _card_form(row: ProviderPaymentSession, action: str, error: str = "", name: str = "") -> HTMLResponse:
    save = ('<label class="save"><input type="checkbox" name="save" value="1"> Save this payment method for future '
            'purchases (the store keeps only a token, the card network and the last 4 digits)</label>'
            if row.save_allowed else "")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    securing = _post_to_store(row, {"type": "shopsphere-provider-step", "step": "securing"}).replace(
        "<script>", "").replace("</script>", "")
    return _page(row, err + f"""
<h2>Enter test card details</h2>
<form id="cardform" method="post" action="{action}" autocomplete="on">
<label class="f">Name on card<input name="cardholder_name" required maxlength="80" autocomplete="cc-name" value="{html.escape(name, quote=True)}"></label>
<label class="f">Card number<input name="card_number" required inputmode="numeric" maxlength="23" autocomplete="cc-number" placeholder="4242 4242 4242 4242"></label>
<div class="two"><label class="f">Expiry (MM/YY)<input name="expiry" required maxlength="7" autocomplete="cc-exp" placeholder="12/30"></label>
<label class="f">CVC<input name="cvc" required inputmode="numeric" maxlength="4" autocomplete="cc-csc" placeholder="•••"></label></div>
{save}<button class="ok" type="submit">Continue securely</button></form>
<div class="tests">{TEST_CARD_HELP}</div>
<script>document.getElementById('cardform').addEventListener('submit',function(){{
var b=this.querySelector('button[type=submit]');b.disabled=true;b.textContent='Securing your payment method…';{securing}}});</script>""",
                 step={"step": "card"})


def _verification_page(db, row: ProviderPaymentSession, action: str, error: str = "") -> HTMLResponse:
    """Simulated payment verification (generic — any network). A fresh one-time challenge
    is issued for this view; only its hash is stored, and it expires in 5 minutes."""
    challenge = secrets.token_urlsafe(18)
    row.challenge_hash, row.challenge_expires_at = _hash(challenge), now() + CHALLENGE_TTL
    db.commit()
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(row, err + f"""
<div class="v"><div class="vh"><p class="eb">Simulated payment verification</p><h2>Verify your payment</h2>
<p>Your card issuer asks you to confirm this purchase. Demo only: not a real bank or card-network check.</p></div>
<div class="vb"><dl><dt>Merchant</dt><dd>{html.escape(_merchant(row))}</dd>
<dt>Amount</dt><dd>{html.escape(row.currency)} {row.amount:,.2f}</dd>
<dt>Card</dt><dd>{html.escape(_card_label(row))}</dd><dt>Purpose</dt><dd>One-time purchase</dd></dl>
<form id="verify" method="post" action="{action}"><input type="hidden" name="challenge" value="{challenge}">
<button class="ok fp" type="submit" name="result" value="ok" autofocus>{FINGERPRINT} Verify payment</button>
<div class="links"><button type="submit" name="result" value="cancel">Cancel</button>
<button type="submit" name="result" value="fail" title="Demo: show a failed verification">Simulate failed verification</button></div></form>
<p class="foot"><span>&#128737;</span><span>This verification approves this exact {html.escape(_merchant(row))} purchase only. If the merchant, amount or order changes, you will be asked again.</span></p></div></div>
<p class="note">Real card issuers decide per payment: many approve without an extra step, others ask for a one-time
code, an app approval or a biometric check. This demo shows one such check.</p>
<script>document.getElementById('verify').addEventListener('submit',function(e){{
if(e.submitter&&e.submitter.value==='ok'){{e.submitter.classList.add('busy');e.submitter.lastChild.textContent=' Verifying…';}}
var f=this;setTimeout(function(){{f.querySelectorAll('button').forEach(function(b){{b.disabled=true;}});}},0);}});</script>""",
                 step={"step": "verify", "network": row.network, "last4": row.last4})


def _paths(request: Request, row: ProviderPaymentSession) -> dict:
    base = f'{request.scope.get("root_path", "")}/hosted/{row.session_id}'
    return {"page": base, "pay": f"{base}/pay", "continue": f"{base}/continue", "challenge": f"{base}/challenge"}


@router.get("/hosted/{session_id}", response_class=HTMLResponse)
def hosted_page(session_id: str, request: Request):
    with get_session() as db:
        row = _load(db, session_id)
        paths = _paths(request, row)
        if row.status == "PENDING":
            return _card_form(row, paths["pay"])
        if row.status == "TOKENIZED":
            # The card is secured (tokenized) by the provider; the store hears only "Visa •••• 4242".
            return _page(row, f"""
<div class="center"><div class="tick">&#10003;</div><p class="done">Card secured</p>
<p class="card">{html.escape(_card_label(row))}</p>
<p class="muted">Your card is now a secure token held by the payment provider.</p></div>
<form id="next" method="post" action="{paths['continue']}"><button class="ok" type="submit">Continue</button></form>
<script>setTimeout(function(){{var f=document.getElementById('next');f.querySelector('button').disabled=true;f.submit();}},1300);</script>""",
                         step={"step": "tokenized", "network": row.network, "last4": row.last4})
        if row.status == "VERIFICATION_REQUIRED":
            return _verification_page(db, row, paths["challenge"])
        if row.status == "VERIFIED":
            return _verified(row)
        return _ended(row)


def _verified(row: ProviderPaymentSession) -> HTMLResponse:
    how = ("You verified this payment." if row.verification_required
           else "Your card issuer didn't need an extra check for this payment.")
    return _finished(row, f"{html.escape(_card_label(row))} · {how} Returning to the store to complete your order…",
                     title="Payment verified")


def _ended(row: ProviderPaymentSession) -> HTMLResponse:
    messages = {
        "CANCELLED": "Payment cancelled. Nothing was charged.",
        "VERIFICATION_FAILED": "The payment couldn't be verified, so nothing was charged.",
        "EXPIRED": "This payment session expired. Nothing was charged.",
        "CAPTURED": "This payment is complete.",
        "DECLINED": "This payment was declined. Nothing was charged.",
        "TIMEOUT": "This payment didn't complete. Nothing was charged.",
        "VOIDED": "This payment was released. Nothing was charged.",
    }
    return _finished(row, messages.get(row.status, f"This payment is {row.status.lower()}."))


@router.post("/hosted/{session_id}/pay", response_class=HTMLResponse)
def pay(session_id: str, request: Request, cardholder_name: str = Form(""), card_number: str = Form(""),
        expiry: str = Form(""), cvc: str = Form(""), save: Optional[str] = Form(None)):
    """TOKENIZATION. The card is checked HERE, on the provider's side, and turned into an
    opaque token; only the network and last 4 (and, if the customer asks to save it, the
    name and expiry for display) are kept. Test cards only. Nothing is charged here."""
    with get_session() as db:
        row = _load(db, session_id)
        paths = _paths(request, row)
        if row.status != "PENDING":
            return RedirectResponse(paths["page"], status_code=303)
        digits = "".join(ch for ch in card_number if ch.isdigit())
        card = TEST_CARDS.get(digits)
        m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*", expiry or "")
        error, year = "", 0
        if not card:
            error = "This simulated provider only accepts the test card numbers listed below. Never enter a real card."
        elif not m or not 1 <= int(m.group(1)) <= 12:
            error = "Enter the expiry as MM/YY."
        else:
            year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
            t = now()
            if (year, int(m.group(1))) < (t.year, t.month):
                error = "That card has expired."
            elif not re.fullmatch(r"\d{4}" if card[0] == "AMEX" else r"\d{3}", cvc or ""):
                error = "Check the security code (CVC)."
            elif card[1] == "tokenize_fail":
                error = "This card couldn't be secured, so no payment was attempted. Try another card."
        if error:
            return _card_form(row, paths["pay"], error, name=cardholder_name[:80])
        network, _, _, verification_required = card
        row.network, row.last4 = network, digits[-4:]
        digits = card_number = cvc = ""                        # never kept, never echoed back
        row.status = "TOKENIZED"
        row.payment_token = f"pm_mock_{secrets.token_urlsafe(12)}"
        row.verification_required = verification_required
        row.save_requested = bool(save) and row.save_allowed
        if row.save_requested:                                 # display metadata for a saved card only
            row.cardholder_display = " ".join(cardholder_name.split())[:80] or None
            row.card_exp_month, row.card_exp_year = int(m.group(1)), year
        row.tokenized_at = now()
        db.commit()
        return RedirectResponse(paths["page"], status_code=303)


@router.post("/hosted/{session_id}/continue", response_class=HTMLResponse)
def continue_payment(session_id: str, request: Request):
    """After tokenization: the issuer's verification if this card needs one, otherwise the
    credential is ready straight away (frictionless). Nothing is authorized here: PayIt
    asks for the authorization after ShopSphere's own checks."""
    with get_session() as db:
        row = _load(db, session_id)
        if row.status == "TOKENIZED":
            if row.verification_required:
                row.status, row.verification_attempts = "VERIFICATION_REQUIRED", 0
            else:
                row.status, row.verified_at = "VERIFIED", now()
            db.commit()
        return RedirectResponse(_paths(request, row)["page"], status_code=303)


@router.post("/hosted/{session_id}/challenge", response_class=HTMLResponse)
def challenge(session_id: str, request: Request, result: Literal["ok", "fail", "cancel"] = Form(...),
              challenge: str = Form("")):
    """The simulated payment verification. The one-time challenge must match and be fresh."""
    with get_session() as db:
        row = _load(db, session_id)
        paths = _paths(request, row)
        if row.status != "VERIFICATION_REQUIRED":
            return RedirectResponse(paths["page"], status_code=303)
        if result == "cancel":
            row.status, row.decline_reason, row.challenge_hash = "CANCELLED", "CUSTOMER_CANCELLED", None
            db.commit()
            return _ended(row)
        fresh = row.challenge_expires_at and now() <= row.challenge_expires_at
        if not row.challenge_hash or not challenge or not secrets.compare_digest(row.challenge_hash, _hash(challenge)):
            return _verification_page(db, row, paths["challenge"],
                                      "That verification request is no longer valid. Please verify again.")
        if not fresh:
            return _verification_page(db, row, paths["challenge"], "That verification expired. Please verify again.")
        row.challenge_hash = None                              # single use
        if result == "ok":
            row.status, row.verified_at = "VERIFIED", now()
            db.commit()
            return _verified(row)
        row.verification_attempts = (row.verification_attempts or 0) + 1
        if row.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
            row.status, row.decline_reason = "VERIFICATION_FAILED", "VERIFICATION_FAILED"
            db.commit()
            return _ended(row)
        left = MAX_VERIFICATION_ATTEMPTS - row.verification_attempts
        return _verification_page(db, row, paths["challenge"],
                                  f"Verification failed. Please try again ({left} attempt{'s' if left > 1 else ''} left).")


def _outcome_for(row: ProviderPaymentSession) -> tuple[str, Optional[str]]:
    """The tokenized test card's simulated authorization outcome, looked up by network +
    last 4 (the full number is gone by now)."""
    for number, (network, outcome, reason, _) in TEST_CARDS.items():
        if network == row.network and number[-4:] == row.last4:
            return outcome, reason
    return "decline", "CARD_DECLINED"
