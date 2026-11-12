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
    if not scope or not scope["trace"]:
        return None
    rows = ([first] if first else []) + scope["events"]
    _write(scope["trace"], rows)
    return scope["trace"]


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


def events(trace_id: str, limit: int = MAX_EVENTS) -> list[dict]:
    with get_session() as db:
        rows = (db.query(ProtocolEvent).filter_by(trace_id=trace_id)
                .order_by(ProtocolEvent.at, ProtocolEvent.id).all())[-limit:]
        return [{"seq": i + 1, "at": r.at, "source": r.source, "destination": r.destination, "protocol": r.protocol,
                 "pattern": r.pattern, "operation": r.operation, "request": r.request, "response": r.response,
                 "status": r.status, "correlation": r.correlation, "layer": r.layer} for i, r in enumerate(rows)]


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
