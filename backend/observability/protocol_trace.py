"""
Agent communication trace: what really happened behind one shopper's conversation, in order.

Every row is recorded by the code that did the work, while it ran: the MCP server records each
tool call Claude made (request and response summaries), the services it called record their own
steps, and the payment engine's step list (GreenLight, PayIt, gateway, network, issuer, TrackIt)
is copied in as it was returned. Nothing is written ahead of time or made up for the panel.

Honest labels, two columns:
  protocol  the real transport of that step: "MCP JSON-RPC (Streamable HTTP)", "OAuth 2.1 (HTTP)",
            "HTTP (in-process ASGI)", "In-process call" or "Simulated (in-process)"
  pattern   the design idea a step follows, always qualified exactly as the engine names it
            ("AP2-style consent", "ACP-style checkout", "VIC-inspired"): ideas, not protocol messages.
There is no A2A and no AP2/ACP wire protocol in this system, so no row ever claims one.

One trace per shopper: the chat's anonymous visitor before sign-in, the customer after it (the
visitor's rows move along when the cart does). The trace id is an HMAC of that identity, so it
can't be guessed from an order or checkout id. Summaries are scrubbed: no tokens, full card
numbers, CVC, full emails or street addresses.
"""
import contextvars
import hashlib
import hmac
import os
import re
import time
from typing import Optional

from backend.db.schema import ProtocolEvent
from backend.db.session_utils import get_session
from backend.shop.wording import neutral

MCP = "MCP JSON-RPC (Streamable HTTP)"
OAUTH = "OAuth 2.1 (HTTP)"
IN_PROCESS = "In-process call"
SIMULATED = "Simulated (in-process)"
GATEWAY_HTTP = "HTTP (in-process ASGI)"

CLAUDE = "Claude (MCP client)"
MCP_SERVER = "ShopSphere MCP server"
CONNECTOR_TRACE = "tr_connector"          # anonymous discovery: not tied to any one shopper
MAX_EVENTS = 600                          # per trace; the oldest go first


def trace_id_for(subject: str) -> str:
    # Demo only: TRACE_DEMO_SINGLE_URL=1 sends every shopper's rows to the one fixed page
    # /trace/tr_connector, so the link never changes. Off by default (per-shopper traces).
    if os.getenv("TRACE_DEMO_SINGLE_URL") == "1":
        return CONNECTOR_TRACE
    secret = os.getenv("JWT_SECRET_KEY", "demo-jwt-secret-replace-in-production")
    return "tr_" + hmac.new(secret.encode(), f"trace:{subject}".encode(), hashlib.sha256).hexdigest()[:24]


# ── Scrubbing: applied to every summary before it is stored ─────────────────────

_JWT = re.compile(r"eyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}")
_TOKENS = re.compile(r"\b(tok|vat|cred|gc|ps|pss|sess)_[A-Za-z0-9_-]{8,}")
_VISITOR_CART = re.compile(r"vc_VIS[0-9A-F]{8,12}_[0-9a-f]{16}")
_PAN = re.compile(r"\b(?:\d[ -]?){12,19}\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def _pan(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return f"•••• {digits[-4:]}"


def scrub(value) -> str:
    text = value if isinstance(value, str) else str(value)
    text = _JWT.sub("‹signed token›", text)
    text = _VISITOR_CART.sub("‹visitor cart id›", text)
    text = _TOKENS.sub(lambda m: f"{m.group(1)}_‹…›", text)
    text = _PAN.sub(_pan, text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}•••@{m.group(2)}", text)
    return text[:600]


# ── Recording ────────────────────────────────────────────────────────────────────

# The MCP middleware opens a scope per tool call: {"trace": id or None, "events": [...]}.
# Tools and services add rows to it; the trace id may only become known during the call
# (a visitor's first search creates the visitor), so rows are written when the call ends.
_SCOPE: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("protocol_trace_scope", default=None)


def open_scope(trace_id: Optional[str] = None) -> contextvars.Token:
    return _SCOPE.set({"trace": trace_id, "events": []})


def close_scope(token: contextvars.Token, first: Optional[dict] = None) -> Optional[str]:
    """Write the call's rows (the call itself first) to its trace; returns the trace id."""
    scope = _SCOPE.get()
    _SCOPE.reset(token)
    if not scope:
        return None
    # A call that no shopper is known for yet (no visitor cart, no sign-in) still shows on the
    # connector trace, so every prompt and button click appears in the live flow.
    target = scope["trace"] or CONNECTOR_TRACE
    rows = ([first] if first else []) + scope["events"]
    _write(target, rows)
    return target


def bind(subject_trace: Optional[str]) -> None:
    """The current call belongs to this trace (e.g. once the visitor is known)."""
    scope = _SCOPE.get()
    if scope is not None and subject_trace and not scope["trace"]:
        scope["trace"] = subject_trace


def current() -> Optional[str]:
    scope = _SCOPE.get()
    return scope["trace"] if scope else None


def event(source: str, destination: str, protocol: str, operation: str, *, request="", response="",
          status: str = "success", pattern: Optional[str] = None, correlation: Optional[str] = None,
          layer: str = "real", at: Optional[float] = None) -> dict:
    # Each part is named by its role (backend/shop/wording.py): "Card issuer", not "Simulated card issuer".
    source, destination, protocol, operation, request, response = (
        neutral(source), neutral(destination), neutral(protocol), neutral(operation), neutral(request),
        neutral(response))
    pattern = neutral(pattern) if pattern else pattern
    return {"at": at or time.time(), "source": source[:80], "destination": destination[:80], "protocol": protocol[:80],
            "pattern": (pattern or "")[:80] or None, "operation": scrub(operation)[:120],
            "request": scrub(request) if request else "", "response": scrub(response) if response else "",
            "status": status if status in ("success", "failure", "pending", "info") else "info",
            "correlation": (correlation or "")[:60] or None, "layer": layer}


def record(source: str, destination: str, protocol: str, operation: str, *, trace_id: Optional[str] = None,
           **kw) -> None:
    """Add one row: to the open call scope, or straight to trace_id (outside an MCP call)."""
    row = event(source, destination, protocol, operation, **kw)
    scope = _SCOPE.get()
    if trace_id is None and scope is not None:
        scope["events"].append(row)
    elif trace_id:
        _write(trace_id, [row])


def _write(trace_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        with get_session() as db:
            for r in rows:
                db.add(ProtocolEvent(trace_id=trace_id, **r))
            db.commit()
            count = db.query(ProtocolEvent).filter_by(trace_id=trace_id).count()
            if count > MAX_EVENTS:
                old = [e.id for e in db.query(ProtocolEvent.id).filter_by(trace_id=trace_id)
                       .order_by(ProtocolEvent.at, ProtocolEvent.id).limit(count - MAX_EVENTS)]
                db.query(ProtocolEvent).filter(ProtocolEvent.id.in_(old)).delete(synchronize_session=False)
                db.commit()
    except Exception as exc:                       # the trace must never break shopping
        print(f"[trace] not recorded: {type(exc).__name__}", flush=True)


def merge(from_trace: str, into_trace: str) -> None:
    """The visitor signed in: their earlier rows join the customer's trace."""
    if not from_trace or not into_trace or from_trace == into_trace:
        return
    with get_session() as db:
        db.query(ProtocolEvent).filter_by(trace_id=from_trace).update({"trace_id": into_trace})
        db.commit()
    resolve(trace_id=into_trace, operations=(SIGN_IN_CHALLENGE,), status="success",
            outcome="Signed in; Claude retried with a valid token")


# ── Waiting steps get their outcome ───────────────────────────────────────────────
# A "pending" row is a step that waits on something later: the shopper's authorization, the
# customer's approval page, a sign-in, the secure payment page. When that later thing happens,
# the waiting row is closed with what actually happened (its request stays as it was), so a
# finished step never keeps showing as pending. One left waiting past its lifetime is shown as
# expired when the page is read.

AUTH_REQUESTED = "Purchase authorization requested"
STEP_UP = "Customer approval required (step-up)"
SIGN_IN_CHALLENGE = "Sign-in required (HTTP 401 challenge)"
AUTH_WAITS = (AUTH_REQUESTED, STEP_UP)
SECURE_PAGE_CALLS = tuple(f"tools/call {t}" for t in ("place_order", "card_place_guest_order",
                                                         "start_new_card_payment", "start_guest_payment"))
_LIFETIME = {AUTH_REQUESTED: (15 * 60, "Expired unused after 15 minutes; nothing charged"),
             STEP_UP: (15 * 60, "Expired: not approved within 15 minutes; nothing charged")}


def _closed(response: Optional[str], outcome: str) -> str:
    note = f" → {outcome}"
    return scrub((response or "")[:600 - len(note)] + note)


def resolve(*, operations: tuple, status: str, outcome: str, correlation: Optional[str] = None,
            trace_id: Optional[str] = None) -> int:
    """Close the still-pending rows for these operations (by checkout/order id, or in one trace)."""
    if not correlation and not trace_id:
        return 0
    n = 0
    scope = _SCOPE.get()
    for r in (scope or {}).get("events", []):              # rows of this same call, not written yet
        if (r["status"] == "pending" and r["operation"] in operations
                and (not correlation or r["correlation"] == correlation)):
            r["status"], r["response"] = status, _closed(r["response"], outcome)
            n += 1
    try:
        with get_session() as db:
            q = db.query(ProtocolEvent).filter(ProtocolEvent.status == "pending",
                                               ProtocolEvent.operation.in_(operations))
            if correlation:
                q = q.filter(ProtocolEvent.correlation == correlation)
            if trace_id:
                q = q.filter(ProtocolEvent.trace_id == trace_id)
            for e in q.all():
                e.status, e.response = status, _closed(e.response, outcome)
                n += 1
            db.commit()
    except Exception as exc:                       # the trace must never break shopping
        print(f"[trace] outcome not recorded: {type(exc).__name__}", flush=True)
    return n


# Rows recorded before waiting steps were closed: the later step in the same trace says what happened.
_LATER = {AUTH_REQUESTED: ("Customer purchase authorization", "Approved by the shopper; used once"),
          STEP_UP: ("Customer approved the order", "Customer approved on ShopSphere's approval page")}


def _as_read(r: ProtocolEvent, now: float, done: set) -> tuple[str, str]:
    if r.status != "pending":
        return r.status, r.response
    later = _LATER.get(r.operation)
    if later and (later[0], r.correlation) in done:
        return "success", _closed(r.response, later[1])
    life = _LIFETIME.get(r.operation)
    if life and now - (r.at or now) > life[0]:
        return "info", _closed(r.response, life[1])
    return r.status, r.response


def events(trace_id: str, limit: int = MAX_EVENTS) -> list[dict]:
    now = time.time()
    with get_session() as db:
        rows = (db.query(ProtocolEvent).filter_by(trace_id=trace_id)
                .order_by(ProtocolEvent.at, ProtocolEvent.id).all())[-limit:]
        done = {(r.operation, r.correlation) for r in rows if r.status == "success" and r.correlation}
        out = []
        for i, r in enumerate(rows):
            status, response = _as_read(r, now, done)
            out.append({"seq": i + 1, "at": r.at, "source": neutral(r.source), "destination": neutral(r.destination),
                        "protocol": neutral(r.protocol), "pattern": neutral(r.pattern) or None,
                        "operation": neutral(r.operation), "request": neutral(r.request),
                        "response": neutral(response), "status": status, "correlation": r.correlation,
                        "layer": r.layer})
        return out


def clear(trace_id: str) -> int:
    with get_session() as db:
        n = db.query(ProtocolEvent).filter_by(trace_id=trace_id).delete()
        db.commit()
        return n


# ── The payment engine's own step list → trace rows ─────────────────────────────

_GREENLIGHT = "GreenLight (consent & policy)"
_PAYIT = "PayIt (payment execution)"
_TRACKIT = "TrackIt (orders & email)"
_AGENTS = {"GreenLight": _GREENLIGHT, "PayIt": _PAYIT, "TrackIt": _TRACKIT}


def _hop(agent: str, check: str) -> tuple[str, str, str]:
    """(source, destination, transport) for one engine step."""
    a = _AGENTS.get(agent, agent)
    if agent == "GreenLight":
        return a, "ShopSphere checkout + policy store", IN_PROCESS
    if agent == "PayIt":
        if check.startswith("DPAT check"):
            return a, "GreenLight token (verify)", IN_PROCESS
        if check == "Gateway request sent":
            return a, "Mock gateway / PSP", GATEWAY_HTTP
        if check == "Provider authorization":
            return a, "Simulated Secure Payment Provider", GATEWAY_HTTP
        return a, "Payment router", IN_PROCESS
    if agent == "TrackIt":
        return a, ("Email outbox / SMTP" if "email" in check.lower() else "Orders database"), IN_PROCESS
    if "gateway" in agent.lower():
        return agent, "Simulated card network", SIMULATED
    if "network" in agent.lower():
        return agent, "Simulated card issuer", SIMULATED
    if "issuer" in agent.lower():
        return agent, "Mock gateway / PSP (response)", SIMULATED
    return agent, "ShopSphere payment engine", SIMULATED


def from_engine(steps: list[dict], correlation: Optional[str] = None, at: Optional[float] = None) -> None:
    """Copy the engine's returned steps into the current call (in their order)."""
    base = at or time.time()
    for i, s in enumerate(steps or []):
        agent, check = str(s.get("agent") or "ShopSphere"), str(s.get("check") or "")
        src, dst, transport = _hop(agent, check)
        if check == agent:                         # the gateway's own hops name only the actor
            low = agent.lower()
            check = ("Authorize card-on-file token" if "gateway" in low else "Route to card issuer" if "network" in low
                     else "Authorization decision" if "issuer" in low else check)
        simulated = s.get("layer") == "simulated" or "SIMULATED" in str(s.get("protocol") or "").upper()
        record(src, dst, SIMULATED if simulated and transport == IN_PROCESS else transport, check,
               response=str(s.get("detail") or ""), pattern=s.get("protocol") or None,
               status={"pass": "success", "blocked": "failure", "info": "info"}.get(s.get("result"), "info"),
               correlation=correlation, layer="simulated" if simulated else "real", at=base + i * 0.0005)
