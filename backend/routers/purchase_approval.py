"""
The customer's own approval of an agent-prepared order (purchase_consent level "customer_step_up").

When Claude asks ShopSphere to pay with a real-world-style credential (a signed-in customer's card
on file, the Wallet, Visa agent payments, an external provider's reference), the shopper's "yes" in
the chat isn't enough: place_order answers approval_required with a link to this page. The customer
opens it in their own browser, sees the exact order ShopSphere computed, and approves or declines.
Only then can place_order use the authorization (once).

  GET  /approve/{token}           the order to approve (live terms re-checked)
  POST /approve/{token}/approve   approve (the page's simulated passkey step)
  POST /approve/{token}/decline   decline: nothing is charged

The token is a signed, 15-minute link naming one consent row. The "passkey" here is SIMULATED:
not WebAuthn, not 3-D Secure; no card issuer or network is contacted.
"""
import html
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from backend.db.session_utils import get_session
from backend.mcp.auth import read_step_up
from backend.observability import protocol_trace as pt
from backend.shop import checkout as checkout_service
from backend.shop import purchase_consent
from backend.shop.checkout import CheckoutError

router = APIRouter(tags=["purchase-approval"])

_CSS = """
:root{--bg:#f7f7f8;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#e3e5e8;--ok:#13795b;--warn:#a15c00;--btn:#16181d}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--card:#1b1d22;--ink:#eceef2;--muted:#a3a9b4;--line:#2c2f36;
--ok:#4cc79a;--warn:#f0b35a;--btn:#eceef2}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
.row{display:flex;justify-content:space-between;gap:12px;padding:4px 0;overflow-wrap:anywhere}
.muted{color:var(--muted);font-size:13px}.total{font-weight:600;border-top:1px solid var(--line);margin-top:8px;
padding-top:8px}.btns{display:flex;gap:8px;flex-wrap:wrap}button{font:inherit;padding:10px 16px;border-radius:8px;
border:1px solid var(--btn);cursor:pointer}.primary{background:var(--btn);color:var(--bg)}.ghost{background:none;
color:var(--ink)}.ok{color:var(--ok);font-weight:600}.warn{color:var(--warn);font-weight:600}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
                        f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
                        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Referrer-Policy": "no-referrer"})


def _load(db, token: str):
    consent_id = read_step_up(token)
    row = purchase_consent.find(db, consent_id) if consent_id else None
    if row is None:
        raise HTTPException(404, "This approval link isn't valid or has expired.")
    try:
        co = checkout_service.get_checkout(db, row.owner_id, row.checkout_id)
    except CheckoutError:
        raise HTTPException(404, "This approval link isn't valid or has expired.")
    return row, co, checkout_service.snapshot(db, co)


def _live_problem(row, snap: dict) -> str:
    """Why this request can't be approved now ("" = it can)."""
    if row.status == "approved":
        return "already_approved"
    if row.status != "issued":
        return {"used": "This order was already paid with this approval.",
                "declined": "You declined this order. Nothing was charged.",
                "superseded": "A newer summary replaced this one. Ask Claude for the latest one."}.get(
                    row.status, "This approval can't be used.")
    if row.expires_at < time.time():
        return "This approval request expired. Nothing was charged. Ask Claude to review the order again."
    same = (row.checkout_version == snap["checkout_version"] and row.amount_cents == round(snap["total"] * 100)
            and row.currency == snap["currency"] and row.fingerprint == (snap.get("checkout_hash") or "")[:16]
            and row.pay_ref == purchase_consent.pay_ref_of(snap)[:60])
    return "" if same else "The order changed after this request was made, so it can't be approved. Ask Claude " \
                           "to review the order again."


def _paying(snap: dict) -> str:
    if snap.get("pay_with") == "wallet":
        return "ShopSphere Wallet"
    pm = snap.get("payment_method") or {}
    return pm.get("display") or "Card on file"


def _summary(snap: dict) -> str:
    e = html.escape
    rows = "".join(f"<div class=row><span>{e(ln['name'])}{' · size ' + e(str(ln['size'])) if ln.get('size') else ''}"
                   f"{' · ' + e(ln['color']) if ln.get('color') else ''} × {ln['quantity']}</span>"
                   f"<span>${(ln.get('unit_price') or 0) * ln['quantity']:.2f}</span></div>" for ln in snap["lines"])
    disc = "".join(f"<div class=row><span>{e(d['label'])}</span><span>−${d['amount']:.2f}</span></div>"
                   for d in (snap.get("invoice") or {}).get("discounts") or [])
    addr = snap.get("address") or {}
    dl = snap.get("delivery") or {}
    return (f"<div class=card>{rows}{disc}"
            f"<div class=row><span>Shipping ({e(str(dl.get('method') or 'standard'))})</span>"
            f"<span>${snap.get('shipping') or 0:.2f}</span></div>"
            f"<div class=row><span>Tax</span><span>${snap.get('tax') or 0:.2f}</span></div>"
            f"<div class='row total'><span>Total</span><span>{e(snap.get('currency') or 'USD')} "
            f"${snap['total']:.2f}</span></div></div>"
            f"<div class=card><div class=row><span class=muted>Merchant</span><span>ShopSphere</span></div>"
            f"<div class=row><span class=muted>Pay with</span><span>{e(_paying(snap))}</span></div>"
            f"<div class=row><span class=muted>Ship to</span><span>{e(str(addr.get('city') or ''))}, "
            f"{e(str(addr.get('state') or ''))}</span></div>"
            f"<div class=row><span class=muted>Estimated delivery</span><span>{e(str(dl.get('date') or ''))}</span>"
            f"</div></div>")


@router.get("/approve/{token}", response_class=HTMLResponse)
def approval_page(token: str):
    with get_session() as db:
        row, co, snap = _load(db, token)
        problem = _live_problem(row, snap)
        body = "<h1>Approve your ShopSphere order</h1><p class=muted>Claude prepared this order for you. Only you " \
               "can approve the payment, here in your browser.</p>" + _summary(snap)
        if problem == "already_approved":
            body += "<p class=ok>Approved. Go back to Claude and say you've approved it.</p>"
        elif problem:
            body += f"<p class=warn>{html.escape(problem)}</p>"
        else:
            t = html.escape(token, quote=True)
            body += (f"<div class=btns><form method=post action='/approve/{t}/approve'><button class=primary "
                     f"type=submit>Approve with passkey (simulated)</button></form>"
                     f"<form method=post action='/approve/{t}/decline'><button class=ghost type=submit>Decline"
                     f"</button></form></div>"
                     "<p class=muted>Simulated passkey for this demo: not a real passkey or 3-D Secure, and your "
                     "card issuer isn't contacted. Nothing is charged until Claude places the order after your "
                     "approval, and only for exactly this order.</p>")
        return _page("Approve your order", body)


@router.post("/approve/{token}/approve", response_class=HTMLResponse)
def approve(token: str):
    with get_session() as db:
        row, co, snap = _load(db, token)
        problem = _live_problem(row, snap)
        if problem and problem != "already_approved":
            return _page("Not approved", f"<h1>Not approved</h1><p class=warn>{html.escape(problem)}</p>")
        if not problem:
            purchase_consent.approve(db, row, via="approval_page (simulated passkey)")
            pt.record("Customer (browser)", "ShopSphere approval page", "HTTPS form post", "Customer approved the order",
                      trace_id=pt.trace_id_for(row.owner_id), correlation=row.checkout_id,
                      request=f"{row.checkout_id} v{row.checkout_version} {row.currency} {row.amount_cents / 100:.2f}",
                      response="Approval recorded for this single authorization (simulated passkey; no issuer contacted)",
                      pattern="AP2-style human-present confirmation (concept only)")
    return _page("Approved", "<h1>Approved</h1><p class=ok>Thanks. Go back to Claude and say you've approved it; "
                             "Claude then places the order once.</p>")


@router.post("/approve/{token}/decline", response_class=HTMLResponse)
def decline(token: str):
    from backend.shop import payment as payment_service
    with get_session() as db:
        row, co, snap = _load(db, token)
        if row.status in ("issued", "approved"):
            purchase_consent.decline(db, row.owner_id, row.checkout_id, via="approval_page")
            payment_service.record_approval_cancelled(row.owner_id, row.checkout_id, "CUSTOMER_DECLINED")
            pt.record("Customer (browser)", "ShopSphere approval page", "HTTPS form post", "Customer declined the order",
                      trace_id=pt.trace_id_for(row.owner_id), correlation=row.checkout_id,
                      request=row.checkout_id, response="Nothing approved, charged or ordered", status="info",
                      pattern="AP2-style human-present confirmation (concept only)")
    return _page("Declined", "<h1>Declined</h1><p>Nothing was charged and no order was placed.</p>")
