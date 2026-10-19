"""
Confirm & pay — the consent gate between "review your order" and money moving.

Only an explicit approval gets here. From this point it's deterministic code:
re-check stock and prices, then the secure path:

  GreenLight   consent recorded + single-use DPAT scoped to this exact checkout
  PayIt        12-check guardrail engine on the DPAT
  then, by payment path (backend/shop/vic.py, decided by the server):
    vic        VIC-inspired controls on the member's AUTHENTICATED instruction
               → purchase-specific credential → gateway → simulated Visa
               network → simulated Chase issuer → commerce signal
    standard   card-on-file token → gateway → simulated card network → issuer
    wallet     ShopSphere Wallet balance (internal; no gateway)
  TrackIt      order created only after authorization (once), email, tracking

Amounts always come from the server-held checkout, never from the caller.
Each attempt gets its own payment reference (CHK_…-1, -2 …) so a declined
card can be swapped and retried on the same checkout.
"""
import json
import logging
import os
from typing import Optional

from fastapi import HTTPException

from backend.auth.dependencies import CurrentUser
from backend.db.schema import Checkout, Order, PaymentAuthorization, PaymentInstruction, PaymentMethod, SimScenario
from backend.db.session_utils import get_session, write_audit_event
from backend.gateway.adapter import get_adapter
from backend.models.payment import PaymentRequest, PaymentResult
from backend.payment import processor_route
from backend.shop import checkout as checkout_service
from backend.shop import orders as order_service
from backend.shop import cards
from backend.shop import vic as vic_paths
from backend.vic import credentials, signals
from backend.vic.common import ALLOWED, MERCHANT_ID, MERCHANT_NAME, short, transition

log = logging.getLogger(__name__)

CONSENT_MODES = ("button", "typed")      # CH-8: explicit approvals only

DECLINE_MESSAGES = {
    "INSUFFICIENT_BALANCE": "Your wallet balance isn't enough for this order. Choose a card instead.",
    "CARD_DECLINED": "Your card was declined by the issuer. Try a different card.",
    "CARD_EXPIRED": "That card has expired. Try a different card.",
    "NO_ACTIVE_PAYMENT_METHOD": "That card isn't available. Choose another card.",
    "INSUFFICIENT_CREDIT": "That card doesn't have enough available credit. Try a different card.",
    "PAYMENT_TIMEOUT": "The payment network didn't answer in time. Nothing was charged; please try again.",
    "GATEWAY_UNAVAILABLE": "We couldn't reach the payment gateway. Nothing was charged; please try again.",
    "GATEWAY_ERROR": "The payment gateway had a problem. Nothing was charged; please try again.",
    "INSUFFICIENT_FUNDS": "The card issuer declined this payment (insufficient funds). Nothing was charged. Try "
                          "another payment method.",
    "PROVIDER_AUTHORIZATION_FAILED": "The payment provider couldn't authorize the payment. Nothing was charged; please "
                                     "try again.",
}
CONTROL_MESSAGES = {
    "CHECKOUT_MISMATCH": "Your order changed after you approved it, so it wasn't paid. Please approve it again.",
    "AMOUNT_MISMATCH": "The total changed after you approved it, so it wasn't paid. Please approve it again.",
    "INSTRUCTION_EXPIRED": "Your approval expired, so nothing was paid. Please approve the order again.",
    "INSTRUCTION_NOT_AUTHENTICATED": "Please confirm with your passkey before paying.",
    "AGENT_TOKEN_INACTIVE": "Agent payments aren't active for this card, so nothing was paid.",
    "INSTRUCTION_CLOSED": "That approval was cancelled or already used. Please approve the order again.",
    "DUPLICATE_ATTEMPT": "That approval was already used for a payment attempt. Please approve the order again.",
}


def _pop_scenario(db, user_id: str) -> Optional[str]:
    row = db.query(SimScenario).filter_by(user_id=user_id).first()
    if not row:
        return None
    outcome = row.outcome
    db.delete(row)
    return outcome


async def confirm(user: CurrentUser, checkout_id: str, consent: bool, consent_mode: str = "button",
                  instruction_id: Optional[str] = None, *, action: Optional[str] = None,
                  checkout_version: Optional[int] = None, final_amount: Optional[float] = None,
                  currency: Optional[str] = None, return_url: Optional[str] = None,
                  provider_session: Optional[dict] = None) -> dict:
    """consent_mode: "button" (Approve & Pay tapped) or "typed" (GO AHEAD typed in Talkshop
    on the Visa path, still followed by the passkey). CH-8: there is no automatic or
    countdown consent — any other mode is refused. The Visa path also needs the
    member's AUTHENTICATED payment instruction (simulated passkey).

    CH-4 scoped consent: the approval names action="PAY", the checkout_version, the
    final_amount and the currency the customer saw. If the order is no longer exactly
    that — before or after the live price/stock refresh — nothing is charged and the
    fresh invoice comes back (409 STALE_CHECKOUT) to be approved again.

    CH-6 new card (pay_with "provider"): the approval opens a session with the
    Simulated Secure Payment Provider and returns {"status": "provider_handoff", …};
    the customer finishes on the provider's own page; complete_provider() then
    re-enters here with the provider's verified, SAFE result (provider_session) and
    the normal GreenLight → PayIt → order path runs, PayIt capturing the session."""
    # Imported here: the routers import this module's callers.
    from backend.routers.authorizations import ApproveRequest, approve_authorization
    from backend.routers.payments import ExecutePaymentRequest, execute_payment_endpoint

    if consent is not True:
        raise checkout_service.CheckoutError(
            "CONSENT_REQUIRED", "Payment needs your explicit go-ahead.", 400)
    if consent_mode not in CONSENT_MODES:
        raise checkout_service.CheckoutError(
            "EXPLICIT_APPROVAL_REQUIRED", "Payments only happen when you tap Approve & Pay. Nothing was charged.", 400)

    with get_session() as db:
        from backend.shop import eligibility       # second line of defence (CH-2): a signed-in customer
        code = eligibility.check_access(db, user)  # (any issuer) …
        if code:
            raise checkout_service.CheckoutError(code, eligibility.MESSAGES[code], 403)
        co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        code = eligibility.check_payment(db, user, co.pay_with or "card")   # … who is a credit-card holder
        if code:
            raise checkout_service.CheckoutError(code, eligibility.MESSAGES[code], 403)
        completing = provider_session is not None
        if completing:
            if co.status != "awaiting_payment" or co.provider_session_id != provider_session.get("payment_session_id"):
                raise checkout_service.CheckoutError(
                    "SESSION_MISMATCH", "That payment session isn't for this order.", 409)
        elif co.status == "awaiting_payment":           # Approve tapped again while the provider page is open
            raise checkout_service.CheckoutError(
                "PAYMENT_IN_PROGRESS", "This order is waiting for the secure payment provider. Finish (or cancel) "
                "there first.", 409, {"payment_session_id": co.provider_session_id,
                                      "hosted_url": get_adapter().hosted_url(co.provider_session_id)})
        elif co.status != "open":
            raise checkout_service.CheckoutError(
                "CHECKOUT_CLOSED", f"This checkout is {co.status}.", 409)
        if action != "PAY" or checkout_version is None or final_amount is None or not currency:
            raise checkout_service.CheckoutError(
                "CONSENT_SCOPE_REQUIRED", "Approve the exact order total first (Approve & Pay on the final invoice).", 400)

        def stale() -> bool:
            return (co.version or 1) != int(checkout_version) or abs(co.total - float(final_amount)) > 0.005                 or (co.currency or "USD") != currency

        def stale_error() -> checkout_service.CheckoutError:
            return checkout_service.CheckoutError(
                "STALE_CHECKOUT", "Your order changed since you approved it, so nothing was charged. Please review the "
                f"new total of ${co.total:.2f} and approve again.", 409, {"checkout": checkout_service.snapshot(db, co)})

        if stale():
            raise stale_error()
        path = vic_paths.payment_path(db, co)
        if path["path"] == "vic_setup_required":
            raise checkout_service.CheckoutError(
                "AGENT_PAY_NOT_ENROLLED", "Set up Visa agent payments for this card first (a quick simulated "
                "passkey step), or choose another card.", 409, {"card_account_id": path.get("card_account_id")})
        if path["path"] == "vic":
            if not instruction_id:
                raise checkout_service.CheckoutError(
                    "INSTRUCTION_REQUIRED", "Approve the order and confirm with your passkey first.", 409)
            if not db.query(PaymentInstruction).filter_by(instruction_id=instruction_id, user_id=user.user_id,
                                                          checkout_id=checkout_id).first():
                raise checkout_service.CheckoutError(
                    "UNKNOWN_INSTRUCTION", "That approval doesn't belong to this order.", 404)
        issues = checkout_service.refresh(db, co)      # live stock + prices + Chase benefits, right before paying
        db.commit()
        if stale():                                     # a price or benefit changed since the approval
            raise stale_error()
        snap = checkout_service.snapshot(db, co, issues)
        if not snap["ready"]:
            raise checkout_service.CheckoutError(
                "NOT_READY", "This order can't be placed yet.", 409, {"issues": snap["issues"]})
        attempt = db.query(PaymentAuthorization).filter(
            PaymentAuthorization.order_id.like(f"{checkout_id}-%")).count() + 1
        payment_ref = co.pending_payment_ref if completing else f"{checkout_id}-{attempt}"
        if path["path"] == "provider_handoff" and not completing:
            return await _start_provider_session(db, user, co, payment_ref, consent_mode, return_url, path)
        first = snap["lines"][0]
        title = first["name"] if len(snap["lines"]) == 1 else f"{first['name']} + {len(snap['lines']) - 1} more"
        amounts = dict(total=co.total, subtotal=co.subtotal, tax=co.tax, shipping=co.shipping,
                       currency=co.currency, checkout_hash=co.checkout_hash)
        version = co.version or 1
        pay_with = "wallet" if co.pay_with == "wallet" else "card"
        payment_method_id = co.payment_method_id if pay_with == "card" and not completing else None
        mode = consent_mode
        benefits = json.loads(co.benefits_json) if co.benefits_json else {"applied": [], "removed": []}
        co.status = "consented"
        write_audit_event(db, "CHECKOUT_CONSENT", user_id=user.user_id, order_id=payment_ref,
                          metadata={"mode": mode, "checkout_hash": co.checkout_hash, "total": co.total,
                                    "action": "PAY", "merchant_id": MERCHANT_ID, "currency": co.currency,
                                    "checkout_version": co.version, "payment_path": path["path"],
                                    "instruction": short(instruction_id) if instruction_id else None})
        if path["path"] == "vic":
            instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id).one()
            instr.payment_ref = payment_ref
        db.commit()

    # Live trace (info view). Real backend steps only; no secrets: references are
    # shortened and no card data ever appears here.
    trace: list[dict] = [
        {"agent": "GreenLight", "check": "Consent gate", "result": "pass", "protocol": "AP2-style consent",
         "detail": ("GO AHEAD typed, then passkey" if mode == "typed" else "Approve & Pay tapped")
                   + f" for checkout {checkout_id}, total ${amounts['total']:.2f}"},
        {"agent": "GreenLight", "check": "Consent scope", "result": "pass", "protocol": "AP2-style consent",
         "event": "GREENLIGHT_CONSENT_VALIDATED",
         "detail": f"action PAY · merchant {MERCHANT_ID} · order {checkout_id} · {amounts['currency']} "
                   f"{amounts['total']:.2f} · checkout version {version}: matches the approved invoice"},
        {"agent": "GreenLight", "check": "Live stock + price refresh", "result": "pass", "protocol": "ACP-style checkout",
         "detail": "Re-checked against the catalog right before paying; order fingerprint (SHA-256) recomputed; "
                   "Chase benefits revalidated against the selected card; version unchanged"},
        # CH-8: GreenLight states what it validated before any token exists
        {"agent": "GreenLight", "check": "Benefit eligibility", "result": "pass", "protocol": "Mock entitlement rules",
         "detail": ("Applied: " + ", ".join(b["title"] for b in benefits["applied"]) if benefits["applied"]
                    else "No Chase benefit on this order")
                   + ("; not applied: " + ", ".join(f"{b['title']} ({b['reason']})" for b in benefits["removed"])
                      if benefits["removed"] else "")},
        {"agent": "GreenLight", "check": "Payment readiness", "result": "pass", "protocol": "Payment orchestration",
         "detail": f"Route {path.get('route', path['path'])} ready: address, stock and payment method all present"},
    ]
    trace.append({"agent": "PayIt", "check": "Payment route", "result": "info", "protocol": "Payment orchestration",
                  "layer": "local",
                  "detail": {"wallet": "ShopSphere Wallet (internal stored value, no card network)",
                             "vic": f"Visa agent payment (VIC-inspired): {(path.get('card') or {}).get('display')}",
                             "standard": f"Saved card token: {(path.get('card') or {}).get('display')} → simulated "
                                         f"{(path.get('network') or 'card').title()} network → {path.get('issuer')}; "
                                         "no card entry page (the provider tokenized this card earlier)",
                             }.get(path["path"], path.get("label") or path["path"])})
    if path["path"] == "vic":
        trace.append({"agent": "GreenLight", "check": "Payment instruction", "result": "pass",
                      "protocol": "VIC-inspired", "layer": "local",
                      "detail": f"Instruction {short(instruction_id)} found for this checkout (authentication checked "
                                "by the VIC controls below)"})

    async def reopen(outcome: str, reason: str, message: str) -> dict:
        if provider_session:                            # release the provider's authorization; start over
            await get_adapter().void_payment_session(provider_session["payment_session_id"])
        with get_session() as db:
            co = db.query(Checkout).filter_by(checkout_id=checkout_id).one()
            co.status = "open"                         # change card / retry on the same checkout
            co.provider_session_id = co.pending_payment_ref = None
            # Spec: no order is created unless payment is authorized. The audit
            # trail and authorization records keep the attempt's full history.
            db.query(Order).filter_by(order_id=payment_ref).delete()
            db.commit()
            return {"status": outcome, "reason": reason, "message": message, "payment_path": path["path"],
                    "checkout": checkout_service.snapshot(db, co), "trace": trace}

    # 1. Consent recorded + single-use scoped token (GreenLight)
    try:
        approval = await approve_authorization(ApproveRequest(
            checkout_id=payment_ref, merchant_id=MERCHANT_ID, merchant_name=MERCHANT_NAME,
            product_id=first["product_id"], product_title=title, **amounts,
        ), user)
    except HTTPException as exc:                       # policy refused before any token existed
        trace.append({"agent": "GreenLight", "check": "Spending policy", "result": "blocked", "protocol": "AP2-style",
                      "detail": str(exc.detail)[:160]})
        return await reopen("blocked", "POLICY_DENIED", str(exc.detail))
    with get_session() as db:                          # CH-4: the consent's scope, on the authorization record
        auth = db.query(PaymentAuthorization).filter_by(authorization_id=approval.authorization_id).first()
        if auth:
            auth.checkout_version, auth.action, auth.consent_status = version, "PAY", "approved"
            db.commit()
    trace += [
        {"agent": "GreenLight", "check": "Spending policy", "result": "pass", "protocol": "AP2-style",
         "detail": f"Approved merchant, {amounts['currency']}, ${amounts['total']:.2f} within the $2,500 limit"},
        {"agent": "GreenLight", "check": "DPAT issued", "result": "pass", "protocol": "AP2-style scoped token",
         "detail": f"Internal delegated-payment token …{approval.token_id[-6:]}: HMAC-SHA256 signed, single use, "
                   "15 min, bound to PayIt + ShopSphere + this amount + checkout fingerprint"},
    ]

    # 2. PayIt's 12 DPAT checks, then the charge step through the gateway route
    gateway_info: dict = {}
    if pay_with == "card":
        processor = (_provider_route(provider_session, payment_ref, trace, gateway_info) if completing
                     else _gateway_route(user, checkout_id, payment_ref, path, instruction_id, trace, gateway_info))
        route_token = processor_route.ACTIVE.set(processor)
    try:
        result = await execute_payment_endpoint(ExecutePaymentRequest(
            token_id=approval.token_id, checkout_id=payment_ref, merchant_id=MERCHANT_ID,
            merchant_name=MERCHANT_NAME, product_id=first["product_id"], product_title=title,
            payment_method=pay_with, payment_method_id=payment_method_id, **amounts,
        ), user)
    finally:
        if pay_with == "card":
            processor_route.ACTIVE.reset(route_token)

    names = {"token_exists": "Token exists", "token_active": "Token is active", "token_not_expired": "Token not expired",
             "token_not_consumed": "Token not used before", "agent_id_match": "Right agent (PayIt)",
             "merchant_id_match": "Right merchant", "order_id_match": "Right order", "currency_match": "Right currency",
             "amount_within_limit": "Amount within authorized limit", "amount_matches_checkout": "Amount = checkout total",
             "checkout_hash_match": "Order fingerprint unchanged", "consent_exists": "Consent record exists"}
    checks = [{"agent": "PayIt", "check": f"DPAT check {c['n']}/12 · {names.get(c['name'], c['name'])}",
               "result": "pass" if c["passed"] else "blocked", "protocol": "AP2-style verification", "layer": "local",
               "detail": None if c["passed"] else c.get("reason")} for c in result.guardrail_checks]
    if len(checks) == 12 and all(c["result"] == "pass" for c in checks):
        checks[-1]["event"] = "DPAT_CHECKS_PASSED"
    # The gateway steps were recorded while PayIt charged; the 12 checks ran before them.
    at = gateway_info.get("trace_start", len(trace))
    trace[at:at] = checks
    if pay_with == "wallet" and result.status == "success":
        trace.append({"agent": "PayIt", "check": "Charge", "result": "pass", "protocol": "Internal stored value",
                      "detail": f"ShopSphere Wallet charged ${amounts['total']:.2f} (no card network involved)"})

    if result.status != "success":
        reason = result.blocked_reason or "PAYMENT_FAILED"
        if reason.startswith("VIC_CONTROL:"):
            code = reason.split(":", 1)[1]
            return await reopen("blocked", code, CONTROL_MESSAGES.get(code, "Payment wasn't authorized by the "
                                                                    "agent-payment controls. Nothing was charged."))
        outcome = "declined" if reason in DECLINE_MESSAGES else "blocked"
        return await reopen(outcome, reason, DECLINE_MESSAGES.get(reason, "Payment wasn't authorized."))

    # 3. Authorized → create the order (once). If that fails, void the
    # authorization so the member is never charged for an order that doesn't exist.
    try:
        with get_session() as db:
            co = db.query(Checkout).filter_by(checkout_id=checkout_id).one()
            card = ({"brand": cards.network_display(provider_session["network"]), "last4": provider_session["last4"]}
                    if completing else None)
            order = order_service.finalize(db, co, payment_ref, payment_path=path["path"], provider_card=card)
            outcome = {"status": "authorized", "order": order_service.order_dict(db, order),
                       "transaction_id": result.transaction_id, "payment_path": path["path"], "trace": trace}
            if path["path"] == "vic":
                instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id).one()
                instr.status = "CONSUMED"
                transition(db, instr, "ORDER_CONFIRMED", f"Order {order.display_id}")
                for sig in signals.for_instruction(db, instruction_id):
                    sig.order_id = order.display_id
                db.commit()
            o = outcome["order"]
            extras = [f"{o['points']['used']:,} points redeemed" if o["points"]["used"] else "",
                      f"offer {o['offer']['title']} −${o['offer']['discount']:.2f}" if o.get("offer") else "",
                      f"+{o['points']['earned']:,} points" if o["points"]["earned"] else "",
                      f"${o['cashback']:.2f} cashback (simulated statement credit)" if o.get("cashback") else ""]
            trace.append({"agent": "TrackIt", "check": "Order confirmed", "result": "pass",
                          "protocol": "ACP-style complete checkout", "layer": "local", "event": "ORDER_CONFIRMED",
                          "detail": f"{order.display_id}: created once, after authorization; stock committed (guarded); "
                                    + "; ".join(x for x in extras if x)})
    except Exception:
        log.exception("order creation failed after authorization (%s); voiding", payment_ref)
        trace.append({"agent": "TrackIt", "check": "Order created", "result": "blocked", "protocol": "ACP-style",
                      "detail": "Order couldn't be created, so the authorization was voided: nothing charged"})
        if not completing:                             # (a provider session is voided by reopen)
            await _void(result.transaction_id, pay_with)
        if path["path"] == "vic":
            with get_session() as db:
                instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id).first()
                if instr and instr.lifecycle_state == "SIGNAL_RECORDED":
                    instr.status = "FAILED"
                    transition(db, instr, "ORDER_FAILED", "Authorization voided")
                    db.commit()
        return await reopen("order_failed", "ORDER_CREATION_FAILED",
                      "Your payment was approved, but ShopSphere couldn't create the order, so the payment "
                      "was released and you haven't been charged. Please try again.")

    # 3b. CH-6: keep the new card ONLY if the customer ticked "save" on the provider's page —
    # and then only the provider's opaque token, the network and the last 4 digits.
    if completing:
        outcome["saved_payment_method"] = None
        if provider_session.get("save_requested") and not user.is_visitor and provider_session.get("payment_token"):
            with get_session() as db:
                from backend.shop import profile
                pm = profile.save_provider_card(db, user.user_id, network=provider_session["network"],
                                                last4=provider_session["last4"],
                                                payment_token=provider_session["payment_token"],
                                                exp_month=provider_session.get("card_exp_month") or 12,
                                                exp_year=provider_session.get("card_exp_year"),
                                                cardholder_name=provider_session.get("cardholder_display"))
                outcome["saved_payment_method"] = profile.card_dict(pm)
        trace.append({"agent": "PayIt", "check": "Save payment method", "result": "info",
                      "protocol": "Simulated provider token", "layer": "simulated",
                      "detail": ("Saved at the customer's request: opaque provider token, network, last 4, "
                                 "expiry and name for display (no card number, no CVC)"
                                 if outcome["saved_payment_method"] else
                                 "Not saved: the customer didn't tick \"save\" (no reusable credential kept)")})

    # 4. Confirmation email (outbox / SMTP). The order already exists, so a
    # problem here never undoes the payment.
    try:
        from backend.shop import notifications
        with get_session() as db:
            order = db.query(Order).filter_by(order_id=payment_ref).one()
            outcome["confirmation_email"] = notifications.send_order_confirmation(db, order)
            mail = notifications.confirmation_for(db, order) or {}
            how = {"sending": "Queued for real delivery over SMTP (TLS); sent in the background",
                   "outbox": "Kept in the demo outbox (no SMTP set, or a made-up address like .demo)"}
            trace.append({"agent": "TrackIt", "check": "Confirmation email", "result": "pass",
                          "protocol": "Order updates", "layer": "local",
                          "detail": f"{how.get(mail.get('delivery'), 'Recorded')}. Contains Order ID, items, total, "
                                    "tracking link; no card data"})
    except Exception:
        log.exception("confirmation email failed for %s", payment_ref)
        outcome["confirmation_email"] = None
    return outcome


async def cancel(user: CurrentUser, checkout_id: str, reason: str = "CUSTOMER_CANCELLED") -> dict:
    """CH-8: the customer cancels before paying. Nothing is charged; an open provider
    session is released and the cancellation is recorded (APPROVAL_CANCELLED)."""
    with get_session() as db:
        co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        was, session_id = co.status, co.provider_session_id
    if was == "awaiting_payment" and session_id:
        await get_adapter().void_payment_session(session_id)
    with get_session() as db:
        co = checkout_service.cancel_checkout(db, user.user_id, checkout_id)
        if was in ("open", "consented", "awaiting_payment"):
            write_audit_event(db, "APPROVAL_CANCELLED", user_id=user.user_id, order_id=checkout_id,
                              metadata={"reason": reason, "stage": was, "checkout_version": co.version,
                                        "total": co.total, "charged": False})
            db.commit()
        return checkout_service.snapshot(db, co)


async def abandon_provider(user: CurrentUser, checkout_id: str, session_id: str) -> dict:
    """The customer closed the Secure Payment Sheet before finishing. The provider session
    is released (nothing can be authorized with it any more), the order goes back to
    review, and the cancellation is recorded. Nothing is charged. A payment that has
    already gone through is left alone."""
    with get_session() as db:
        co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        if co.status != "awaiting_payment" or co.provider_session_id != session_id:
            return {"status": "noop", "checkout": checkout_service.snapshot(db, co)}
    await get_adapter().void_payment_session(session_id)
    with get_session() as db:
        co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        if co.status == "awaiting_payment" and co.provider_session_id == session_id:
            co.status, co.provider_session_id, co.pending_payment_ref = "open", None, None
            write_audit_event(db, "APPROVAL_CANCELLED", user_id=user.user_id, order_id=checkout_id,
                              metadata={"reason": "PAYMENT_SHEET_CLOSED", "stage": "awaiting_payment",
                                        "provider_session": short(session_id), "checkout_version": co.version,
                                        "total": co.total, "charged": False})
            db.commit()
        return {"status": "cancelled", "reason": "PAYMENT_SHEET_CLOSED", "checkout": checkout_service.snapshot(db, co)}


def record_approval_cancelled(user_id: str, checkout_id: str, reason: str) -> None:
    """The customer closed an approval step (e.g. the passkey) without approving."""
    with get_session() as db:
        write_audit_event(db, "APPROVAL_CANCELLED", user_id=user_id, order_id=checkout_id,
                          metadata={"reason": reason, "charged": False})
        db.commit()


ALLOWED_RETURN_ORIGINS = tuple(o.strip().rstrip("/") for o in (os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:3000,"
                        "http://127.0.0.1:5173,http://127.0.0.1:5174")).split(",") if o.strip())


def _safe_return_url(url: Optional[str]) -> Optional[str]:
    """Only send the customer back to ShopSphere's own pages (no open redirect)."""
    if not url:
        return None
    return url if any(url == o or url.startswith(o + "/") for o in ALLOWED_RETURN_ORIGINS) and len(url) <= 300 else None


async def _start_provider_session(db, user: CurrentUser, co: Checkout, payment_ref: str, consent_mode: str,
                                  return_url: Optional[str], path: dict) -> dict:
    """CH-6: the customer approved the exact invoice; open a session with the Simulated
    Secure Payment Provider bound to it (server-to-server). The browser then goes to the
    provider's own page; nothing about a card passes through ShopSphere or Talkshop."""
    adapter = get_adapter()
    status, res = await adapter.create_payment_session({
        "merchant_id": MERCHANT_ID, "checkout_id": co.checkout_id, "payment_ref": payment_ref,
        "checkout_version": co.version or 1, "amount": co.total, "currency": co.currency,
        "customer_ref": user.user_id, "save_allowed": not user.is_visitor, "return_url": _safe_return_url(return_url)})
    if status != 200:
        raise checkout_service.CheckoutError(
            "PROVIDER_UNAVAILABLE", "We couldn't reach the secure payment provider. Nothing was charged; please try "
            "again.", 502)
    co.status, co.provider_session_id, co.pending_payment_ref = "awaiting_payment", res["payment_session_id"], payment_ref
    mode = consent_mode if consent_mode in ("button", "typed") else "button"
    write_audit_event(db, "CHECKOUT_CONSENT", user_id=user.user_id, order_id=payment_ref,
                      metadata={"mode": mode, "checkout_hash": co.checkout_hash, "total": co.total, "action": "PAY",
                                "merchant_id": MERCHANT_ID, "currency": co.currency, "checkout_version": co.version,
                                "payment_path": "provider_handoff", "provider_session": short(res["payment_session_id"])})
    db.commit()
    hosted = adapter.hosted_url(res["payment_session_id"])
    trace = [
        {"agent": "GreenLight", "check": "Consent gate", "result": "pass", "protocol": "AP2-style consent",
         "detail": f"Approve & Pay for checkout {co.checkout_id}, total ${co.total:.2f}"},
        {"agent": "GreenLight", "check": "Consent scope", "result": "pass", "protocol": "AP2-style consent",
         "detail": f"action PAY · merchant {MERCHANT_ID} · order {co.checkout_id} · {co.currency} {co.total:.2f} · "
                   f"checkout version {co.version}: matches the approved invoice"},
        {"agent": "PayIt", "check": "Payment route", "result": "info", "protocol": "Payment orchestration",
         "layer": "local", "detail": "New card → Simulated Secure Payment Provider (hosted page owned by the provider)"},
        {"agent": "Simulated Secure Payment Provider", "check": "Payment session created", "result": "pass",
         "protocol": "SIMULATED", "layer": "simulated", "event": "SECURE_PAYMENT_SESSION_CREATED",
         "detail": f"Session {short(res['payment_session_id'])}: 10 min, single use, bound to {MERCHANT_ID} + "
                   f"{co.checkout_id} + version {co.version} + {co.currency} {co.total:.2f}; the card is given on the "
                   "provider's page, never to ShopSphere or Talkshop"},
        {"agent": "Simulated Secure Payment Provider", "check": "Secure payment sheet opened", "result": "info",
         "protocol": "SIMULATED", "layer": "simulated", "event": "PAYMENT_PROVIDER_OPENED",
         "detail": "The provider's page opens in the Secure Payment Sheet inside Talkshop (no new window, no "
                   "redirect); Talkshop can't read what is typed there"},
    ]
    return {"status": "provider_handoff", "payment_session_id": res["payment_session_id"], "hosted_url": hosted,
            "expires_at": res["expires_at"], "payment_path": path["path"], "page_owner": "Simulated Secure Payment Provider",
            "checkout": checkout_service.snapshot(db, co), "trace": trace}


PROVIDER_PENDING = ("PENDING", "TOKENIZED", "VERIFICATION_REQUIRED")
PROVIDER_FAILURES = {
    "DECLINED": ("declined", "The payment provider declined the card. Nothing was charged. Try another card."),
    "TIMEOUT": ("declined", "The payment provider didn't answer in time. Nothing was charged; please try again."),
    "EXPIRED": ("blocked", "The secure payment page expired before you finished. Nothing was charged; please try again."),
    "CANCELLED": ("cancelled", "You cancelled the payment verification. Nothing was charged; your order is still "
                               "here to review."),
    "VERIFICATION_FAILED": ("blocked", "The payment couldn't be verified, so nothing was charged. You can try again or "
                                       "use another payment method."),
    "VOIDED": ("blocked", "That payment was released. Nothing was charged; please try again."),
}


def _credential_trace(res: dict) -> list[dict]:
    """What the provider's SAFE result tells us about the card step: tokenized, verified
    (or frictionless), ready for PayIt. Network + last 4 only; no token, no challenge."""
    card = f"{cards.network_display(res.get('network'))} •••• {res.get('last4')}"
    rows = [{"agent": "Simulated Secure Payment Provider", "check": "Card tokenized", "result": "pass",
             "protocol": "SIMULATED", "layer": "simulated", "event": "CARD_TOKENIZED",
             "detail": f"{card}: the provider holds the card as an opaque token; ShopSphere and Talkshop never saw the "
                       "card number or CVC"}]
    if res.get("verification_required"):
        rows += [{"agent": "Simulated Secure Payment Provider", "check": "Issuer verification required",
                  "result": "info", "protocol": "SIMULATED payment verification", "layer": "simulated",
                  "event": "ISSUER_VERIFICATION_REQUIRED",
                  "detail": "The (simulated) card issuer asked the customer to verify this exact purchase"},
                 {"agent": "Simulated Secure Payment Provider", "check": "Customer verified", "result": "pass",
                  "protocol": "SIMULATED payment verification", "layer": "simulated", "event": "USER_VERIFIED",
                  "detail": f"One-time verification for {MERCHANT_NAME}, ${res.get('amount', 0):.2f}, one-time purchase "
                            "(single use, 5 min)"}]
    else:
        rows.append({"agent": "Simulated Secure Payment Provider", "check": "Issuer verification", "result": "info",
                     "protocol": "SIMULATED payment verification", "layer": "simulated",
                     "detail": "Not required for this card (frictionless)"})
    rows.append({"agent": "Simulated Secure Payment Provider", "check": "Payment credential ready", "result": "pass",
                 "protocol": "SIMULATED", "layer": "simulated", "event": "PAYMENT_CREDENTIAL_READY",
                 "detail": f"{card} ready for PayIt's authorization, bound to {MERCHANT_ID} + {res.get('checkout_id')} "
                           f"+ version {res.get('checkout_version')} + {res.get('currency')} {res.get('amount', 0):.2f}"})
    return rows


async def complete_provider(user: CurrentUser, checkout_id: str, session_id: str) -> dict:
    """The provider's page says the card step is done. ShopSphere asks the provider itself
    (server-to-server) for the SAFE result — never trusting the browser — checks it is the
    payment this order's approval was for, and only once the card is tokenized AND verified
    runs GreenLight → PayIt's 12 checks → PayIt asks the provider to authorize → order.
    Safe to call twice."""
    adapter = get_adapter()
    with get_session() as db:
        co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        if co.status == "paid" and co.pending_payment_ref:
            order = db.query(Order).filter_by(order_id=co.pending_payment_ref).first()
            if order and order.display_id:
                return {"status": "authorized", "duplicate": True, "order": order_service.order_dict(db, order),
                        "payment_path": "provider_handoff", "trace": []}
        if not co.provider_session_id or co.provider_session_id != session_id:
            raise checkout_service.CheckoutError("SESSION_MISMATCH", "That payment session isn't for this order.", 409)
        if co.status != "awaiting_payment":
            raise checkout_service.CheckoutError("CHECKOUT_CLOSED", f"This checkout is {co.status}.", 409)
        expected = {"merchant_id": MERCHANT_ID, "checkout_id": co.checkout_id, "payment_ref": co.pending_payment_ref,
                    "customer_ref": user.user_id, "checkout_version": co.version or 1, "currency": co.currency}
        total = co.total
    status, res = await adapter.payment_session(session_id)
    if status != 200:
        raise checkout_service.CheckoutError(
            "PROVIDER_UNAVAILABLE", "We couldn't reach the secure payment provider. Nothing was charged.", 502)

    async def start_over(outcome: str, reason: str, message: str) -> dict:
        if res.get("status") in (*PROVIDER_PENDING, "VERIFIED", "CAPTURED"):
            await adapter.void_payment_session(session_id)
        with get_session() as db:
            co = checkout_service.get_checkout(db, user.user_id, checkout_id)
            co.status, co.provider_session_id, co.pending_payment_ref = "open", None, None
            db.commit()
            return {"status": outcome, "reason": reason, "message": message, "payment_path": "provider_handoff",
                    "checkout": checkout_service.snapshot(db, co),
                    "trace": [{"agent": "Simulated Secure Payment Provider", "check": "Payment session result",
                               "result": "blocked", "protocol": "SIMULATED", "layer": "simulated",
                               "detail": f"{res.get('status')}: {reason}; nothing charged, no order"}]}

    if any(res.get(k) != v for k, v in expected.items()) or abs((res.get("amount") or 0) - total) > 0.005:
        return await start_over("blocked", "SESSION_BINDING_MISMATCH",
                                "That payment wasn't for this exact order, so nothing was charged. Please approve again.")
    if res["status"] in PROVIDER_PENDING:
        return {"status": "pending", "payment_session_id": session_id, "hosted_url": adapter.hosted_url(session_id),
                "provider_status": res["status"],
                "message": "The secure payment provider is still waiting for you to finish."}
    if res["status"] != "VERIFIED":
        outcome, message = PROVIDER_FAILURES.get(res["status"], ("blocked", "The payment wasn't completed."))
        reason = {"EXPIRED": "SESSION_EXPIRED", "TIMEOUT": "PAYMENT_TIMEOUT"}.get(
            res["status"], res.get("decline_reason") or res["status"])
        return await start_over(outcome, reason, message)
    with get_session() as db:
        write_audit_event(db, "PAYMENT_CREDENTIAL_READY", user_id=user.user_id, order_id=res["payment_ref"],
                          metadata={"provider_session": short(session_id), "network": res.get("network"),
                                    "last4": res.get("last4"), "verification_required": res.get("verification_required"),
                                    "checkout_version": res["checkout_version"], "amount": res["amount"]})
        db.commit()
    try:
        done = await confirm(user, checkout_id, True, "button", action="PAY", checkout_version=res["checkout_version"],
                             final_amount=res["amount"], currency=res["currency"], provider_session=res)
        done["trace"] = _credential_trace(res) + done.get("trace", [])
        done["card"] = f"{cards.network_display(res.get('network'))} •••• {res.get('last4')}"
        return done
    except checkout_service.CheckoutError as exc:      # e.g. the price changed meanwhile: release, approve again
        if exc.code in ("STALE_CHECKOUT", "NOT_READY"):
            await adapter.void_payment_session(session_id)
            with get_session() as db:
                co = checkout_service.get_checkout(db, user.user_id, checkout_id)
                co.status, co.provider_session_id, co.pending_payment_ref = "open", None, None
                db.commit()
                if exc.code == "STALE_CHECKOUT":
                    exc.detail["checkout"] = checkout_service.snapshot(db, co)
        raise


def _provider_route(session: dict, payment_ref: str, trace: list[dict], info: dict):
    """PayIt's charge step for a new card: after GreenLight and the 12 DPAT checks pass,
    PayIt asks the provider to authorize the tokenized + verified credential — once, for
    exactly this merchant, payment_ref, amount and currency (idempotent on payment_ref)."""

    async def charge(request: PaymentRequest) -> PaymentResult:
        info["trace_start"] = len(trace)
        status, res = await get_adapter().authorize_payment_session(session["payment_session_id"], {
            "merchant_id": MERCHANT_ID, "payment_ref": payment_ref, "amount": request.amount,
            "currency": request.currency})
        card = f"{cards.network_display(session['network'])} •••• {session['last4']}"
        ok = status == 200 and res.get("status") == "CAPTURED"
        if ok:
            detail = (f"{card} · ${request.amount:.2f} authorized once, reference {res['authorization_reference']} "
                      "(PayIt sent only the session reference; no card number)")
        elif status == 200:
            detail = f"{card}: {res.get('status')} ({res.get('decline_reason')}); nothing charged"
        else:
            err = res.get("detail") if isinstance(res.get("detail"), dict) else res
            detail = f"Not authorized: {err.get('code') or status}; nothing charged"
        trace.append({"agent": "PayIt", "check": "Provider authorization", "result": "pass" if ok else "blocked",
                      "protocol": "SIMULATED", "layer": "simulated", "detail": detail,
                      **({"event": "PAYMENT_AUTHORIZED"} if ok else {})})
        if not ok:
            reason = ({"TIMEOUT": "PAYMENT_TIMEOUT"}.get(res.get("status")) or res.get("decline_reason") or "CARD_DECLINED"
                      ) if status == 200 else "PROVIDER_AUTHORIZATION_FAILED"
            return PaymentResult(status="declined", decline_reason=reason, amount=request.amount,
                                 currency=request.currency)
        return PaymentResult(status="success", transaction_id=res["authorization_reference"],
                             authorization_code=res["authorization_reference"][-6:], amount=request.amount,
                             currency=request.currency)

    return charge


async def _void(transaction_id: Optional[str], pay_with: str) -> None:
    if pay_with == "card" and transaction_id and transaction_id.startswith("gw_"):
        await get_adapter().void(transaction_id)
    else:
        from backend.payment.mock_processor import void_authorization
        void_authorization(transaction_id)


def _gateway_route(user: CurrentUser, checkout_id: str, payment_ref: str, path: dict,
                   instruction_id: Optional[str], trace: list[dict], info: dict):
    """PayIt's charge step for card payments: runs after the 12 DPAT checks pass."""

    async def charge(request: PaymentRequest) -> PaymentResult:
        info["trace_start"] = len(trace)
        adapter = get_adapter()

        def result(status: str, reason: Optional[str] = None, txn: Optional[str] = None,
                   code: Optional[str] = None) -> PaymentResult:
            return PaymentResult(status=status, decline_reason=reason, transaction_id=txn,
                                 authorization_code=code, amount=request.amount, currency=request.currency)

        with get_session() as db:
            co = db.query(Checkout).filter_by(checkout_id=checkout_id).one()
            instr, cred_ref = None, None
            if path["path"] == "vic":
                instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id).one()
                ok, controls = credentials.run_controls(db, user, instr, co, snapshot_ready=True)
                for c in controls:
                    trace.append({"agent": "VIC simulator", "check": f"VIC control {c['n']}/12 · {c['name']}",
                                  "result": "pass" if c["passed"] else "blocked", "protocol": "VIC-inspired controls",
                                  "layer": "simulated", "detail": c["reason"]})
                if not ok:
                    failed = controls[-1]["reason"]
                    if instr.status in ("PENDING_AUTHENTICATION", "AUTHENTICATED"):
                        instr.status, instr.status_reason = "FAILED", failed
                    if "CONTROLS_FAILED" in ALLOWED.get(instr.lifecycle_state, set()):
                        transition(db, instr, "CONTROLS_FAILED", failed)
                    db.commit()
                    return result("declined", f"VIC_CONTROL:{failed}")
                cred = credentials.issue(db, instr)
                db.commit()
                cred_ref = cred.credential_ref
                trace.append({"agent": "VIC simulator", "check": "Purchase credential issued", "result": "pass",
                              "protocol": "VIC-inspired credential", "layer": "simulated",
                              "detail": f"{short(cred_ref)}: single use, 5 min, bound to ShopSphere + "
                                        f"{checkout_id} + ${cred.amount:.2f} (opaque; the simulated cryptogram stays "
                                        "server-side)"})
                instrument = {"type": "vic_credential", "credential_ref": cred_ref}
                idem_key = f"{instr.instruction_id}:1"     # one transaction per instruction
            else:
                card = db.query(PaymentMethod).filter_by(payment_method_id=co.payment_method_id,
                                                         user_id=user.user_id).first()
                if not card:
                    return result("declined", "NO_ACTIVE_PAYMENT_METHOD")
                instrument = {"type": "card_on_file", "token_ref": card.token_ref}
                idem_key = payment_ref
                cred_ref = None
            scenario = _pop_scenario(db, user.user_id)
            db.commit()

        body = {"merchant_id": MERCHANT_ID, "amount": request.amount, "currency": request.currency,
                "order_ref": payment_ref, "checkout_id": checkout_id, "customer_ref": user.user_id,
                "instrument": instrument}
        trace.append({"agent": "PayIt", "check": "Gateway request sent", "result": "info",
                      "protocol": "PaymentGatewayAdapter", "layer": "local",
                      "detail": f"{adapter.transport_label}; idempotency key …{idem_key[-6:]}; "
                                + ("VIC credential reference only" if instrument["type"] == "vic_credential"
                                   else "PSP card token only") + " (no card number)"})
        gw = await adapter.authorize(body, idem_key, scenario)
        if scenario == "duplicate":                     # demo: the same request submitted twice
            again = await adapter.authorize(body, idem_key, None)
            trace.append({"agent": "PayIt", "check": "Duplicate submission", "result": "pass",
                          "protocol": "Idempotency", "layer": "simulated",
                          "detail": f"Second request with the same key returned the original result "
                                    f"({'same' if again.gateway_ref == gw.gateway_ref else 'DIFFERENT'} reference, "
                                    f"duplicate={again.duplicate}); not authorized twice"})
        for step in gw.steps:
            trace.append({"agent": step["actor"], "check": step["actor"],
                          "result": {"approved": "pass", "declined": "blocked", "timeout": "blocked"}
                          .get(step["result"], step["result"]),
                          "protocol": "SIMULATED", "layer": "simulated", "detail": step.get("detail")})

        outcome_map = {"approved": "APPROVED", "declined": "DECLINED", "timeout": "TIMEOUT"}
        with get_session() as db:
            if instr is not None:
                instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id).one()
                state = {"approved": "PAYMENT_AUTHORIZED", "timeout": "PAYMENT_TIMEOUT"}.get(gw.status, "PAYMENT_DECLINED")
                transition(db, instr, state, f"{gw.status}{' · ' + gw.decline_reason if gw.decline_reason else ''}")
                signals.record(db, instr, outcome=outcome_map.get(gw.status, "ERROR"), amount=request.amount,
                               currency=request.currency, checkout_id=checkout_id, merchant_id=MERCHANT_ID,
                               credential_ref=cred_ref, gateway_ref=gw.gateway_ref,
                               reason=gw.decline_reason)
                if gw.status == "approved":
                    transition(db, instr, "SIGNAL_RECORDED", "Outcome linked to the member's instruction")
                else:
                    instr.status, instr.status_reason = "FAILED", gw.decline_reason
                    if cred_ref and gw.status != "declined":       # never reached the network
                        credentials.revoke(db, cred_ref)
                db.commit()
                trace.append({"agent": "VIC simulator", "check": "Commerce signal recorded", "result": "pass",
                              "protocol": "VIC-inspired commerce signal", "layer": "simulated",
                              "detail": f"{outcome_map.get(gw.status, 'ERROR')} linked to instruction "
                                        f"{short(instruction_id)} (local record; no Visa API called)"})
        if gw.status == "approved":
            return result("success", txn=gw.gateway_ref, code=gw.auth_code)
        return result("declined", gw.decline_reason or "CARD_DECLINED")

    return charge
