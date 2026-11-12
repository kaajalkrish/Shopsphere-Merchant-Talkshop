"""
The agent communication trace panel (presenter view), separate from the shopper's chat.

  GET  /trace/{trace_id}          the timeline page (refreshes itself while the shopper continues)
  GET  /trace/{trace_id}/events   the same rows as JSON
  POST /trace/{trace_id}/clear    start a fresh recording for this shopper
  GET  /trace                     recent traces: local runs only, or with ?key=TRACE_VIEWER_KEY

A trace id is an HMAC of the shopper's identity (backend/observability/protocol_trace.py), handed
out in place_order's result, so a link reaches one shopper's trace only. Rows hold summaries
only: no tokens, full card numbers, CVC, full emails or street addresses.
"""
import html
import os
import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from backend.db.schema import ProtocolEvent
from backend.db.session_utils import get_session
from backend.observability import protocol_trace as pt

router = APIRouter(tags=["trace"])
_ID = re.compile(r"^(tr_[0-9a-f]{24}|tr_connector)$")

# What this system really implements, said once on the panel (no protocol claimed that isn't there).
PROTOCOL_FACTS = [
    ("MCP", "Implemented", "Claude ⇄ ShopSphere MCP server: JSON-RPC 2.0 over Streamable HTTP (FastMCP), tools + "
                           "MCP Apps cards."),
    ("OAuth 2.1", "Implemented", "Account sign-in for the connector: authorization code + PKCE (S256), dynamic client "
                                 "registration, bearer tokens bound to the connector address."),
    ("HTTP to the gateway", "Implemented (simulated service)", "PayIt → mock gateway / PSP over HTTP (in-process ASGI "
                                                               "transport, idempotency keys); network + issuer are "
                                                               "simulators."),
    ("AP2 / ACP / UCP / VIC", "Design patterns only", "Labels like \"AP2-style consent\" name the idea a step follows "
                                                       "(scoped consent, signed single-use token, checkout session). No "
                                                       "AP2, ACP, UCP or Visa messages are exchanged."),
    ("A2A", "Not implemented", "The six agents (VibeCheck, SneakPeek, CartUp, GreenLight, PayIt, TrackIt) are Python "
                               "modules calling each other in-process. There is no agent-to-agent protocol."),
]


def _check(trace_id: str) -> str:
    if not _ID.match(trace_id or ""):
        raise HTTPException(404, "Unknown trace")
    return trace_id


def _summary(events: list[dict]) -> dict:
    by_protocol: dict[str, int] = {}
    for e in events:
        by_protocol[e["protocol"]] = by_protocol.get(e["protocol"], 0) + 1
    orders = [e["correlation"] for e in events if (e["correlation"] or "").startswith("SS-")]
    return {"count": len(events), "by_protocol": by_protocol, "last_order": orders[-1] if orders else None,
            "failures": sum(1 for e in events if e["status"] == "failure"),
            "simulated": sum(1 for e in events if e["layer"] == "simulated")}


@router.get("/trace/{trace_id}/events")
def trace_events(trace_id: str):
    tid = _check(trace_id)
    events = pt.events(tid)
    return {"trace_id": tid, "events": events, "summary": _summary(events),
            "protocols": [{"name": n, "status": s, "detail": d} for n, s, d in PROTOCOL_FACTS]}


@router.post("/trace/{trace_id}/clear")
def trace_clear(trace_id: str):
    return {"cleared": pt.clear(_check(trace_id))}


def _may_list(request: Request, key: str) -> bool:
    want = os.getenv("TRACE_VIEWER_KEY", "")
    if want:
        return bool(key) and secrets.compare_digest(key, want)
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient") and not os.getenv("RENDER")


@router.get("/trace", response_class=HTMLResponse)
def trace_index(request: Request, key: str = ""):
    if not _may_list(request, key):
        raise HTTPException(404, "Not found")
    from sqlalchemy import func
    with get_session() as db:
        rows = (db.query(ProtocolEvent.trace_id, func.max(ProtocolEvent.at), func.count())
                .group_by(ProtocolEvent.trace_id).order_by(func.max(ProtocolEvent.at).desc()).limit(25).all())
    import time
    items = "".join(
        f'<li><a href="/trace/{html.escape(t)}{"?key=" + html.escape(key) if key else ""}">{html.escape(t)}</a>'
        f'<span>{n} events · {time.strftime("%H:%M:%S", time.localtime(at))}</span></li>' for t, at, n in rows)
    return HTMLResponse(_INDEX.replace("__ITEMS__", items or "<li>No traces yet.</li>"))


@router.get("/trace/{trace_id}", response_class=HTMLResponse)
def trace_page(trace_id: str):
    return HTMLResponse(_PAGE.replace("__TRACE_ID__", _check(trace_id)))


_INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agent traces</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;background:#f6f7f9;color:#14161a}
main{max-width:720px;margin:auto}ul{list-style:none;padding:0}li{display:flex;justify-content:space-between;gap:12px;
padding:10px 12px;background:#fff;border:1px solid #e3e6ea;border-radius:8px;margin:6px 0}a{color:#2456d6}
span{color:#5d6670}</style></head><body><main><h1>Recent agent traces</h1><ul>__ITEMS__</ul></main></body></html>"""

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Transaction Trace</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--ink:#14161a;--muted:#5d6670;--line:#e2e5ea;--accent:#2456d6;
--ok:#137a3f;--ok-bg:#e5f5ec;--bad:#b42318;--bad-bg:#fdecea;--wait:#9a6700;--wait-bg:#fff4d6;--info:#475467;--info-bg:#eef1f5;
--sim:#6941c6;--sim-bg:#f1ecfd;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--panel:#171a20;--ink:#e8eaee;--muted:#9aa3ad;
--line:#2a2f37;--accent:#7aa2ff;--ok:#5dd39e;--ok-bg:#12291d;--bad:#ff8a80;--bad-bg:#3a1714;--wait:#f5c451;--wait-bg:#33290f;
--info:#b6c0cc;--info-bg:#232830;--sim:#c4a8ff;--sim-bg:#261d3d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line);padding:14px 16px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:18px;margin:0}.sub{color:var(--muted);font-size:13px;margin-top:2px;overflow-wrap:anywhere}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px}
.stat b{font-variant-numeric:tabular-nums}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;align-items:center}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;
padding:4px 12px;cursor:pointer}button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
button.ghost{border-radius:8px}
main{padding:16px}
details.facts{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px}
details.facts summary{cursor:pointer;font-weight:600}
.facts table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
.facts td{border-top:1px solid var(--line);padding:6px 8px 6px 0;vertical-align:top}
.facts td:first-child{font-weight:600;white-space:nowrap}
ol{list-style:none;margin:0;padding:0;position:relative}
ol:before{content:"";position:absolute;left:11px;top:6px;bottom:6px;width:2px;background:var(--line)}
li.ev{position:relative;padding-left:34px;margin-bottom:10px}
li.ev:before{content:"";position:absolute;left:6px;top:16px;width:12px;height:12px;border-radius:50%;background:var(--info);
border:2px solid var(--panel)}
li.success:before{background:var(--ok)}li.failure:before{background:var(--bad)}li.pending:before{background:var(--wait)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.top{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.time{font:12px var(--mono);color:var(--muted)}.n{font:12px var(--mono);color:var(--muted)}
.op{font-weight:600}.route{color:var(--muted);font-size:13px}.route b{color:var(--ink);font-weight:500}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{font-size:12px;border-radius:999px;padding:1px 8px;background:var(--info-bg);color:var(--info);white-space:nowrap}
.chip.success{background:var(--ok-bg);color:var(--ok)}.chip.failure{background:var(--bad-bg);color:var(--bad)}
.chip.pending{background:var(--wait-bg);color:var(--wait)}.chip.sim{background:var(--sim-bg);color:var(--sim)}
.chip.proto{background:transparent;border:1px solid var(--line);color:var(--ink)}
.chip.corr{font-family:var(--mono)}
dl{display:grid;grid-template-columns:76px 1fr;gap:2px 10px;margin:8px 0 0;font-size:13px}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.empty{color:var(--muted);padding:30px 0;text-align:center}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.live i{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
html,body{overflow-x:hidden}.card,.top,.route,.op{min-width:0;overflow-wrap:anywhere}
@media (max-width:560px){dl{grid-template-columns:1fr}dt{margin-top:4px}.chip{white-space:normal}
.stat{font-size:12px;padding:4px 8px}main{padding:12px 16px}}
</style></head>
<body>
<header><div class="wrap">
  <h1>Agent communication &amp; protocol trace</h1>
  <div class="sub">Trace <span id="tid">__TRACE_ID__</span> · what really ran behind this shopper's conversation, oldest
    first · <span class="live"><i></i>live</span></div>
  <div class="stats" id="stats"></div>
  <div class="bar" id="filters" role="group" aria-label="Filter events">
    <button data-f="all" aria-pressed="true">All</button>
    <button data-f="mcp" aria-pressed="false">MCP calls</button>
    <button data-f="svc" aria-pressed="false">Merchant services</button>
    <button data-f="pay" aria-pressed="false">Payment</button>
    <button data-f="sim" aria-pressed="false">Simulated only</button>
    <button data-f="fail" aria-pressed="false">Failures</button>
    <span style="flex:1"></span>
    <button class="ghost" id="clear" title="Delete this trace's events and start fresh">Clear trace</button>
  </div>
</div></header>
<main><div class="wrap">
  <details class="facts"><summary>Which protocols are real here</summary><table id="facts"></table></details>
  <ol id="list"><li class="empty">Waiting for the first event…</li></ol>
</div></main>
<script>
const TID = "__TRACE_ID__";
let filter = "all", last = "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = t => { const d = new Date(t * 1000); return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit",
  second:"2-digit"}) + "." + String(d.getMilliseconds()).padStart(3, "0"); };
const PAY = /GreenLight|PayIt|gateway|network|issuer|Payment Provider|TrackIt|PSP/i;
function keep(e) {
  if (filter === "mcp") return /^MCP|^OAuth/.test(e.protocol);
  if (filter === "svc") return e.protocol === "In-process call" && !PAY.test(e.source + e.destination);
  if (filter === "pay") return PAY.test(e.source + " " + e.destination);
  if (filter === "sim") return e.layer === "simulated";
  if (filter === "fail") return e.status === "failure";
  return true;
}
function row(e) {
  const req = e.request ? `<dt>Request</dt><dd>${esc(e.request)}</dd>` : "";
  const res = e.response ? `<dt>Response</dt><dd>${esc(e.response)}</dd>` : "";
  return `<li class="ev ${esc(e.status)}"><div class="card">
    <div class="top"><span class="n">#${e.seq}</span><span class="time">${fmt(e.at)}</span>
      <span class="op">${esc(e.operation)}</span></div>
    <div class="route"><b>${esc(e.source)}</b> → <b>${esc(e.destination)}</b></div>
    <div class="chips"><span class="chip ${esc(e.status)}">${esc(e.status)}</span>
      <span class="chip proto">${esc(e.protocol)}</span>
      ${e.pattern ? `<span class="chip" title="Design pattern, not a wire protocol">${esc(e.pattern)}</span>` : ""}
      ${e.layer === "simulated" ? `<span class="chip sim">simulated</span>` : ""}
      ${e.correlation ? `<span class="chip corr" title="Correlation id">${esc(e.correlation)}</span>` : ""}</div>
    <dl>${req}${res}</dl></div></li>`;
}
async function load() {
  try {
    const r = await fetch(`/trace/${TID}/events`, {cache: "no-store"});
    if (!r.ok) return;
    const data = await r.json();
    const sig = JSON.stringify([data.events.length, data.events.at(-1)?.at, filter]);
    if (!document.getElementById("facts").innerHTML)
      document.getElementById("facts").innerHTML = data.protocols.map(p =>
        `<tr><td>${esc(p.name)}</td><td>${esc(p.status)}</td><td>${esc(p.detail)}</td></tr>`).join("");
    if (sig === last) return;
    last = sig;
    const s = data.summary;
    document.getElementById("stats").innerHTML =
      `<span class="stat"><b>${s.count}</b> events</span>` +
      Object.entries(s.by_protocol).map(([k, v]) => `<span class="stat">${esc(k)} <b>${v}</b></span>`).join("") +
      `<span class="stat">simulated <b>${s.simulated}</b></span><span class="stat">failures <b>${s.failures}</b></span>` +
      (s.last_order ? `<span class="stat">order <b>${esc(s.last_order)}</b></span>` : "");
    const shown = data.events.filter(keep);
    document.getElementById("list").innerHTML = shown.length ? shown.map(row).join("")
      : `<li class="empty">${data.events.length ? "No events match this filter." : "Waiting for the first event…"}</li>`;
  } catch (e) { /* offline for a moment: try again on the next tick */ }
}
document.getElementById("filters").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-f]"); if (!b) return;
  filter = b.dataset.f;
  document.querySelectorAll("button[data-f]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  load();
});
document.getElementById("clear").addEventListener("click", async () => {
  if (!confirm("Delete this trace's events?")) return;
  await fetch(`/trace/${TID}/clear`, {method: "POST"}); last = ""; load();
});
load(); setInterval(load, 2500);
</script></body></html>"""
