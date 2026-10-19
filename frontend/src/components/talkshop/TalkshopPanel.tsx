/**
 * Talkshop AI — ShopSphere's assistant, docked on the right of every store page
 * (open by default on desktop; a bottom sheet on phones), minimisable to an
 * "Ask Talkshop" button. Mounted once in the store layout, so the
 * conversation carries on as the shopper moves between pages.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Info, Minus, RotateCcw, Sparkles, X } from "lucide-react";
import { ShopError } from "../../api/shop";
import { PasskeyCancelled, usePasskey } from "../../member/PasskeyContext";
import { useMember } from "../../member/MemberContext";
import type { Stage, TalkAction, TalkEvent } from "../../api/talkshop";
import type { Checkout } from "../../api/shop";
import type { TraceStep } from "../../talkshop/useTalkshop";
import { useTalkshopShared } from "../../talkshop/TalkshopContext";
import Composer from "./Composer";
import SecurePaymentSheet from "../payments/SecurePaymentSheet";
import {
  AgentPaySetupCard, ApprovalCard, ProviderHandoffCard, CartUpdatedCard, CheckoutCountdown, OptionChips, OrderConfirmedCard, PaymentStatusCard, QuickReplies,
  OrderTrackingCard, RecommendationCards, ReviewOrderCard, SecureDetailsCard, SelectedProduct, SignInCard, TrackLookupCard,
} from "./cards";
import { cx } from "../ui";

// Customer-friendly progress (Part 2): Find → Choose → Review → Approve → Done
const STEPS = ["Find", "Choose", "Review", "Approve", "Done"];
const STEP_OF: Record<Stage, number> = {
  GREETING: 0, SEARCHING: 0, RECOMMENDED: 1, PRODUCT_SELECTED: 1, ASK_SIZE: 1, ASK_COLOR: 1, VARIANT_CONFIRMED: 1,
  IN_CART: 1, OFFER_CHECKOUT: 1, CHECKOUT_DETAILS: 2, AWAITING_CONSENT: 2, PAYING: 3, ORDER_CONFIRMED: 4,
};
const AGENTS: Record<string, string> = {
  VibeCheck: "Understands the request", SneakPeek: "Searches the ShopSphere catalog only",
  CartUp: "Cart, offers & authoritative totals", GreenLight: "Consent + scoped payment token",
  PayIt: "Verifies the token, then the gateway", TrackIt: "Order, email & tracking",
};

export default function TalkshopPanel() {
  const ts = useTalkshopShared();
  const { open, setOpen } = ts;
  const [presenter, setPresenter] = useState(false);

  const sheet = useSecurePaymentSheet(ts);
  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        className="fixed z-50 bottom-5 right-5 inline-flex items-center gap-2 h-12 pl-4 pr-5 rounded-full bg-talk text-white shadow-float hover:opacity-95">
        <Sparkles size={18} /> <span className="text-sm font-semibold">Ask Talkshop</span>
      </button>
    );
  }

  return (
    <>
      <div className="lg:hidden fixed inset-0 z-40 bg-black/30" onClick={() => setOpen(false)} />
      {/* The secure payment sheet covers this panel only (no new window, no navigation). */}
      <aside
        aria-label="Talkshop assistant"
        className={cx("z-50 flex flex-col bg-canvas border-line",
          "fixed inset-x-0 bottom-0 h-[88vh] rounded-t-3xl border-t shadow-float",
          "lg:sticky lg:top-[var(--ss-header-h,4rem)] lg:inset-auto lg:h-[calc(100vh-var(--ss-header-h,4rem))] lg:w-[400px] lg:shrink-0 lg:rounded-none lg:border-t-0 lg:border-l lg:shadow-none")}>
        <Header stage={ts.stage} onMinimise={() => setOpen(false)} onRestart={ts.restart}
          presenter={presenter} onPresenter={() => setPresenter((p) => !p)} />
        <div className="relative flex-1 min-h-0">
          <Conversation ts={ts} sheetOpen={sheet.open} onOpenSheet={sheet.reopen} />
          {presenter && <PresenterTrace trace={ts.trace} onClose={() => setPresenter(false)} />}
        </div>
        <Composer disabled={ts.busy || sheet.open} onSend={(text, image) => ts.send(image ? { text, image_base64: image } : { text })} />
        {sheet.open && sheet.ev && (
          <SecurePaymentSheet merchant="ShopSphere" amount={sheet.ev.amount} currency={sheet.ev.currency}
            hostedUrl={sheet.ev.hosted_url} sessionId={sheet.ev.payment_session_id} onClose={sheet.close}
            onResult={sheet.report} onAbandon={sheet.abandon} />
        )}
      </aside>
    </>
  );
}

function Header({ stage, onMinimise, onRestart, presenter, onPresenter }:
  { stage: Stage; onMinimise: () => void; onRestart: () => void; presenter: boolean; onPresenter: () => void }) {
  const { open: approving } = usePasskey();
  // While the passkey prompt is open the member is on the Approve step.
  const current = approving && stage === "AWAITING_CONSENT" ? 3 : STEP_OF[stage];
  return (
    <div className="border-b border-line px-4 pt-3 pb-3">
      <div className="lg:hidden mx-auto mb-2 h-1 w-10 rounded-full bg-line-strong" />
      <div className="flex items-center gap-2.5">
        <span className="w-9 h-9 rounded-full bg-talk text-white grid place-items-center"><Sparkles size={17} /></span>
        <div className="flex-1 min-w-0 leading-tight">
          <p className="font-semibold">Talkshop AI</p>
          <p className="text-xs text-muted">Your ShopSphere shopping assistant</p>
        </div>
        <IconBtn title="Developer trace: how Talkshop works" on={presenter} onClick={onPresenter}><Info size={16} /></IconBtn>
        <IconBtn title="New conversation" onClick={onRestart}><RotateCcw size={15} /></IconBtn>
        <IconBtn title="Minimise" onClick={onMinimise}><span className="hidden lg:block"><Minus size={17} /></span><span className="lg:hidden"><X size={17} /></span></IconBtn>
      </div>
      <ol className="mt-3 flex items-center gap-1" aria-label="Your progress">
        {STEPS.map((s, i) => (
          <li key={s} className="flex-1 flex flex-col items-center gap-1" aria-current={i === current ? "step" : undefined}>
            <span className={cx("h-1 w-full rounded-full transition-colors", i <= current ? "bg-talk" : "bg-line")} />
            <span className={cx("text-[10px]", i === current ? "text-talk font-semibold" : "text-faint")}>{s}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function IconBtn({ title, on, onClick, children }: { title: string; on?: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" title={title} aria-label={title} onClick={onClick}
      className={cx("w-8 h-8 grid place-items-center rounded-full transition-colors", on ? "bg-talk-soft text-talk" : "text-muted hover:text-ink hover:bg-panel")}>
      {children}
    </button>
  );
}

/* ── Conversation ──────────────────────────────────────────────────────── */

type Item =
  | { kind: "user"; text: string; image?: boolean }
  | { kind: "bot"; text: string }
  | { kind: "event"; ev: TalkEvent; index: number }
  | { kind: "review"; checkout: Checkout; index: number }
  | { kind: "payment"; ev: Extract<TalkEvent, { type: "payment_status" }> };

function buildItems(events: TalkEvent[]): Item[] {
  const items: Item[] = [];
  events.forEach((ev, index) => {
    if (ev.type === "user_message") items.push({ kind: "user", text: ev.text, image: ev.image });
    else if (ev.type === "message") items.push({ kind: "bot", text: ev.text });
    else if (ev.type === "checkout_ready") {
      // Re-shown review (e.g. after typing "go ahead") moves down instead of duplicating
      const prev = items.findIndex((it) => it.kind === "review" && it.checkout.checkout_id === ev.checkout.checkout_id);
      if (prev >= 0) items.splice(prev, 1);
      items.push({ kind: "review", checkout: ev.checkout, index });
    }
    else if (ev.type === "checkout_updated") {
      for (let i = items.length - 1; i >= 0; i--) {     // newest review of this checkout shows the latest state
        const it = items[i];
        if (it.kind === "review" && it.checkout.checkout_id === ev.checkout.checkout_id) { it.checkout = ev.checkout; break; }
      }
    } else if (ev.type === "payment_status") {
      const prev = items[items.length - 1];
      if (prev?.kind === "payment") prev.ev = ev; else items.push({ kind: "payment", ev });
    } else if (ev.type !== "variant_confirmed") items.push({ kind: "event", ev, index });
  });
  return items;
}

function lastIndex(events: TalkEvent[], type: TalkEvent["type"]) {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === type) return i;
  return -1;
}

function Conversation({ ts, sheetOpen, onOpenSheet }: { ts: ReturnType<typeof useTalkshopShared>; sheetOpen: boolean;
  onOpenSheet: () => void }) {
  const { events, stage, busy, status, error } = ts;
  const items = useMemo(() => buildItems(events), [events]);
  const scroller = useRef<HTMLDivElement>(null);
  // Scroll only the chat — never the store page behind it.
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [items, busy, status]);

  const act = (action: TalkAction & { label?: string }) => { if (!busy) ts.send({ action }); };
  const lastUser = lastIndex(events, "user_message");
  const lastRecs = lastIndex(events, "recommendations");
  const lastAsk = lastIndex(events, "ask_option");
  const lastOffer = lastIndex(events, "offer_checkout");
  const lastReview = lastIndex(events, "checkout_ready");
  const lastSuggest = lastIndex(events, "suggestions");
  const lastLogin = lastIndex(events, "login_required");
  const lastDetails = lastIndex(events, "checkout_details_needed");
  const lastTrackForm = lastIndex(events, "track_order_form");
  const lastApproval = lastIndex(events, "approval_required");
  const lastSetup = lastIndex(events, "agent_pay_setup");
  const lastHandoff = lastIndex(events, "provider_handoff");
  const { approveAgentPayment, enrollAgentPay } = usePasskey();
  const member = useMember();
  const [vicError, setVicError] = useState<string | null>(null);

  // Visa path: ShopSphere's own passkey prompt approves this exact order; Talkshop
  // only receives the resulting instruction id with the go-ahead.
  const approve = async (ev: Extract<TalkEvent, { type: "approval_required" }>) => {
    setVicError(null);
    try {
      const id = await approveAgentPayment(ev.checkout_id, { amount: ev.amount, card: ev.payment ?? undefined }, ev.consent_mode);
      ts.send({ action: { type: "go_ahead", checkout_id: ev.checkout_id, instruction_id: id, label: "Approved with passkey" } });
    } catch (e) {
      if (e instanceof PasskeyCancelled) ts.send({ action: { type: "approval_cancelled", label: "Cancelled the approval" } });
      else setVicError(e instanceof ShopError ? `${e.message} Nothing was charged.` : "Approval failed. Nothing was charged.");
    }
  };
  const setup = async (ev: Extract<TalkEvent, { type: "agent_pay_setup" }>, card: string) => {
    setVicError(null);
    try {
      await enrollAgentPay(ev.card_account_id, card);
      void member.refresh();
      ts.send({ action: { type: "agent_pay_ready", label: "Set up Visa agent payments" } });
    } catch (e) {
      setVicError(e instanceof PasskeyCancelled || e instanceof ShopError ? e.message : "Setup didn't complete.");
    }
  };
  // Each auto-checkout countdown fires at most once, whatever re-renders after it.
  // Keyed by the offer event itself, not its position: after a restart the
  // positions begin again at 0, and a new offer must still count down.
  const countdownsFired = useRef(new WeakSet<object>());

  return (
    <div ref={scroller} className="absolute inset-0 overflow-y-auto ss-scroll px-4 py-4 flex flex-col gap-3 [&>*]:shrink-0">
      {items.map((it, i) => {
        if (it.kind === "user") return (
          <div key={i} className="self-end max-w-[85%] rounded-2xl rounded-br-md bg-ink text-canvas px-3.5 py-2 text-sm">
            {it.image && <span className="opacity-70">📷 </span>}{it.text}
          </div>
        );
        if (it.kind === "bot") return (
          <div key={i} className="self-start max-w-[92%] rounded-2xl rounded-bl-md bg-panel px-3.5 py-2 text-sm leading-relaxed">{it.text}</div>
        );
        if (it.kind === "payment") return <PaymentStatusCard key={i} state={it.ev.state} payment={it.ev.payment} message={it.ev.message} />;
        if (it.kind === "review") return (
          <ReviewOrderCard key={i} checkout={it.checkout} busy={busy} onAct={act}
            active={it.index === lastReview && stage === "AWAITING_CONSENT"} />
        );
        const ev = it.ev;
        switch (ev.type) {
          case "suggestions":
            return <QuickReplies key={i} active={it.index === lastSuggest && lastUser < it.index && !busy}
              choices={ev.chips.map((c) => ({ value: c, label: c, primary: !!ev.chip_actions?.[c] }))}
              onPick={(c) => { const a = ev.chip_actions?.[c.label]; ts.send(a ? { action: { ...a, label: c.label } } : { text: c.label }); }} />;
          case "recommendations":
            return <RecommendationCards key={i} products={ev.products} onAct={act}
              active={it.index === lastRecs && !busy && !["CHECKOUT_DETAILS", "AWAITING_CONSENT", "PAYING"].includes(stage)} />;
          case "product_selected":
            return <SelectedProduct key={i} event={ev} />;
          case "ask_option":
            return <OptionChips key={i} option={ev.option} label={ev.label} choices={ev.choices} onAct={act}
              active={it.index === lastAsk && !busy && stage === (ev.option === "size" ? "ASK_SIZE" : "ASK_COLOR")} />;
          case "cart_updated":
            return <CartUpdatedCard key={i} cart={ev.cart} lineId={ev.added_line_id} />;
          case "offer_checkout": {
            const live = it.index === lastOffer && lastOffer > lastReview && !busy && (stage === "OFFER_CHECKOUT" || stage === "IN_CART");
            const pick = (c: { value: string; label: string }) => {
              countdownsFired.current.add(ev);
              act({ type: c.value === "checkout" ? "checkout" : "keep_shopping", label: c.label });
            };
            // The step after this offer has already happened (a tap, the auto-checkout,
            // the sign-in card): the offer is done — never count down again.
            const settled = lastUser > it.index || lastLogin > it.index;
            const counting = live && !settled && stage === "OFFER_CHECKOUT"
              && it.index >= ts.restoredCount && !countdownsFired.current.has(ev);
            if (counting) return <CheckoutCountdown key={i} onPick={pick} />;
            return <QuickReplies key={i} choices={ev.choices} active={live && !settled} onPick={pick} />;
          }
          case "order_confirmed":
            return <OrderConfirmedCard key={i} order={ev.order} email={ev.email} />;
          case "track_order_form":
            return <TrackLookupCard key={i} orderId={ev.order_id} onAct={act}
              active={it.index === lastTrackForm && lastUser < it.index && !busy} />;
          case "order_tracking":
            return <OrderTrackingCard key={i} order={ev.order} />;
          case "checkout_details_needed":
            return <SecureDetailsCard key={i} checkoutId={ev.checkout_id} needs={ev.needs} guest={ev.guest} wallet={ev.wallet} onAct={act}
              active={it.index === lastDetails && stage === "CHECKOUT_DETAILS" && !busy} />;
          case "approval_required":
            return <ApprovalCard key={i} amount={ev.amount} payment={ev.payment}
              active={it.index === lastApproval && stage === "AWAITING_CONSENT" && !busy && lastUser < it.index}
              autoStart={it.index >= ts.restoredCount} onApprove={() => approve(ev)} />;
          case "provider_handoff":
            return <ProviderHandoffCard key={i} amount={ev.amount} currency={ev.currency}
              active={it.index === lastHandoff && stage === "AWAITING_CONSENT" && lastUser < it.index}
              sheetOpen={sheetOpen} onOpen={onOpenSheet}
              onCheck={() => act({ type: "provider_complete", checkout_id: ev.checkout_id, payment_session_id: ev.payment_session_id,
                label: "Check my payment" })} />;
          case "agent_pay_setup": {
            const reviewEv = events[lastReview];
            const card = reviewEv?.type === "checkout_ready" ? reviewEv.checkout.payment_method?.display ?? "Visa" : "Visa";
            return <AgentPaySetupCard key={i} active={it.index === lastSetup && stage === "AWAITING_CONSENT" && !busy && lastUser < it.index}
              onSetup={() => setup(ev, card)} />;
          }
          case "login_required":
            return <SignInCard key={i} reason={ev.reason}
              active={it.index === lastLogin && lastReview < it.index && lastDetails < it.index && lastUser < it.index} />;
          default:
            return null;
        }
      })}
      {busy && (
        <div className="self-start flex items-center gap-2 rounded-2xl bg-panel px-3.5 py-2.5 text-sm text-muted">
          <span className="flex gap-1">{[0, 1, 2].map((d) => (
            <span key={d} className="w-1.5 h-1.5 rounded-full bg-talk animate-bounce" style={{ animationDelay: `${d * 120}ms` }} />))}</span>
          {status ?? "Thinking…"}
        </div>
      )}
      {error && <p className="self-start text-xs text-bad">{error}</p>}
      {vicError && <p className="self-start text-xs text-bad">{vicError}</p>}
    </div>
  );
}

/* ── Developer trace (presenter view) ──────────────────────────────────── */
/* Both lists follow the order a purchase runs in, matching the Find → Choose → Review →
   Approve → Done stepper and the order of the Live trace. */
const GUARDRAILS: { step: string; agent: string; checks: string[] }[] = [
  { step: "Find", agent: "VibeCheck · understands the request", checks: [
    "Card numbers, CVC and expiry masked in the browser and again on the server, before the AI or the transcript sees them",
    "Emails masked before the AI sees the history; typed addresses kept out of the chat",
    "Input guardrail: keyword list + NeMo; not-sold blocker",
    "The AI may only pick an action allowed at this stage; paying is never one of them",
    "Order questions are answered from records, not by the AI"] },
  { step: "Find", agent: "SneakPeek · finds products", checks: [
    "ShopSphere's catalog only (merchant-restricted search)",
    "Prices, stock, ratings, offers and points come from the server, never the AI",
    "AI relevance check (no unrelated products)"] },
  { step: "Choose", agent: "CartUp · cart", checks: [
    "Exact size/colour stock check before adding to the cart",
    "Same ShopSphere cart as the website; visitors keep their cart when they sign up"] },
  { step: "Review", agent: "CartUp · access + checkout", checks: [
    "Browse, Talkshop and the cart: everyone. Checkout: a signed-in customer (any card issuer). Paying: a credit-card holder",
    "Chase status is an entitlement, not the gate: Chase offers and 2× points only when paying with an eligible Chase credit card",
    "Merchant-owned checkout: totals, SHA-256 fingerprint and version are server-only",
    "Secure forms: address and payment choice go to ShopSphere; the AI gets IDs only",
    "Changing the order cancels any open Visa instruction"] },
  { step: "Approve", agent: "GreenLight · consent (the mandate)", checks: [
    "Only the shopper's own tap approves (Approve & Pay / Continue to Secure Payment); no automatic or countdown payment",
    "Consent scoped to action PAY · merchant · order · amount · currency · checkout version; a changed order is refused (STALE_CHECKOUT)",
    "Live stock, price and Chase-benefit revalidation against the selected card",
    "Spending policy: approved merchant, USD, up to $2,500",
    "Consent record + HMAC-signed, single-use, 15-minute payment token (DPAT)",
    "Cancelled approvals are recorded (APPROVAL_CANCELLED); nothing charged"] },
  { step: "Approve", agent: "PayIt · payment", checks: [
    "Token signature verified, token used once, then the 12 DPAT checks before any charge",
    "One route: saved card (any network) · Visa agent payment (agent-pay capable Visa only) · new card on the Secure Payment Sheet · wallet",
    "Visa route: passkey-authenticated instruction, 12 VIC-inspired controls, one-time purchase credential",
    "New card: provider-owned page in the sheet (test cards only), card tokenized and verified by the provider; Talkshop sees only network + last 4",
    "New card: result checked server-to-server, then PayIt authorizes once; closing the sheet or cancelling verification = back to review, nothing charged",
    "Idempotency on every request; a decline or timeout means no order and no double charge"] },
  { step: "Done", agent: "TrackIt · order", checks: [
    "Order created once, only after authorization; released if creation fails",
    "Points and offers recorded once per order (no double redemption)",
    "Confirmation email without card data; tracking by Order ID + email"] },
];

const PROTOCOLS: { step: string; name: string; what: string; where: string }[] = [
  { step: "Find", name: "MCP-style", what: "Tool choice: the AI picks one allowed action",
    where: "The AI reads the request and picks a tool from a fixed list for the current stage (search, select, checkout…). Code checks the choice; no MCP server is used." },
  { step: "Find", name: "UCP", what: "Common commerce capabilities: discovery and identity",
    where: "UCP-inspired product discovery from ShopSphere's catalog, and identity linking (sign in or sign up inside Talkshop, cart kept). Local REST/JSON, not full UCP compliance." },
  { step: "Choose · Review", name: "ACP", what: "Agentic checkout: the merchant owns the order",
    where: "ACP-style cart and merchant-owned checkout (create / update): server-side totals, fingerprint and version; secure forms for details. Kept separate from the payment credential." },
  { step: "Approve", name: "AP2", what: "Agent payments: proof the shopper approved this exact cart",
    where: "AP2-style mandate: scoped consent, consent record, HMAC-signed single-use payment token (DPAT), then 12 checks. Local simulation, not production-compliant." },
  { step: "Approve · Visa route", name: "VIC", what: "Visa Intelligent Commerce (Visa's agent-payment capabilities)",
    where: "Only for agent-pay capable Visa cards. Inspired by: agent token, passkey step-up, authenticated instruction, 12 controls, purchase credential, commerce signal. Local simulation, not connected to Visa, not VIC compliant." },
  { step: "Approve · new card", name: "PSP", what: "Payment provider's hosted page (Secure Payment Sheet)",
    where: "Simulated Secure Payment Provider page inside the sheet: test cards only, card tokenized and verified by the provider; ShopSphere gets a token, network and last 4, and PayIt asks for the authorization. A simulation of the flow, not a PCI boundary." },
  { step: "Approve · saved card", name: "Gateway", what: "Saved card through the payment gateway",
    where: "Saved card token (no card number) → simulated gateway → simulated card network (Visa, Mastercard, Amex, Discover) → simulated issuer, with an idempotency key." },
  { step: "Done", name: "ACP", what: "Complete checkout: order and updates",
    where: "ACP-style complete: the order is created once, only after authorization; confirmation email and UCP-inspired order tracking." },
];

const EMAIL_STEPS = [
  "Order created (only after the payment is authorized)",
  "Recipient: the member's account email",
  "Message written to the outbox: Order ID, items, total, status, tracking link (no card data)",
  "Real address + SMTP set in .env → sent in the background (TLS)",
  "Made-up address (.demo, example.com) or no SMTP → kept in the demo outbox",
  "Outbox records sent / failed; an email problem never undoes the order",
];

const TOKEN_VERIFIED = "Payment token verified";

/** The Visa path's 12 milestones (brief §8), matched against real trace entries. */
const VIC_STEPS: { label: string; match: (t: TraceStep) => boolean }[] = [
  { label: "Customer access", match: (t) => t.check === "VIC control 1/12 · Customer session + access" },
  { label: "Visa card + agent token active", match: (t) => t.check === "VIC control 3/12 · Active agent token" },
  { label: "Checkout created", match: (t) => t.check === "Merchant-owned checkout" },
  { label: "Payment instruction authenticated", match: (t) => t.check === "VIC control 4/12 · Instruction authenticated" },
  { label: "Payment token verified", match: (t) => t.check === TOKEN_VERIFIED },
  { label: "Visa agent-payment controls", match: (t) => t.check === "VIC control 12/12 · Inventory + final checkout state" },
  { label: "Purchase credential issued", match: (t) => t.check === "Purchase credential issued" },
  { label: "Mock gateway request sent", match: (t) => t.check === "Gateway request sent" },
  { label: "Simulated Visa network controls", match: (t) => t.check === "Simulated Visa network" },
  { label: "Simulated Chase issuer", match: (t) => t.check === "Simulated Chase issuer" },
  { label: "Commerce signal recorded", match: (t) => t.check === "Commerce signal recorded" },
  { label: "Order confirmed", match: (t) => t.check === "Order confirmed" },
];

type InfoTab = "trace" | "vic" | "guardrails" | "protocols" | "email";

/** The payment token's internals stay off screen: its issue step shows without
 *  details, and PayIt's individual token checks show as one "verified" row. */
function hideTokenInternals(trace: TraceStep[]): TraceStep[] {
  const out: TraceStep[] = [];
  for (const t of trace) {
    if (t.check === "DPAT issued") {
      out.push({ ...t, check: "Payment token issued", message: "", protocol: undefined });
    } else if (t.check?.startsWith("DPAT check ")) {
      const prev = out[out.length - 1];
      const blocked = t.result === "blocked";
      if (prev?.check === TOKEN_VERIFIED && !t.check.startsWith("DPAT check 1/")) {   // same run of checks
        if (blocked) Object.assign(prev, { result: "blocked", message: "Payment stopped: nothing charged" });
      } else {
        out.push({ ...t, check: TOKEN_VERIFIED, protocol: undefined,
          result: blocked ? "blocked" : "pass", message: blocked ? "Payment stopped: nothing charged" : "" });
      }
    } else {
      out.push(t);
    }
  }
  return out;
}

/** Live trace: the six agents only. Each run of steps by a simulated outside
 *  service (VIC simulator, gateway, card network, issuer, payment provider)
 *  shows as one PayIt row; a failure keeps its plain-language reason. */
function agentsOnly(trace: TraceStep[]): TraceStep[] {
  const out: TraceStep[] = [];
  for (const t of trace) {
    if (t.agent in AGENTS) { out.push(t); continue; }
    const check = t.agent !== "VIC simulator" ? "Card network authorization"
      : t.check === "Commerce signal recorded" ? "Purchase reported to Visa" : "Visa agent payment checks";
    const prev = out[out.length - 1];
    const blocked = t.result === "blocked";
    if (prev?.agent === "PayIt" && prev.check === check && prev.layer === "simulated") {
      if (blocked && prev.result !== "blocked") Object.assign(prev, { result: "blocked", message: t.message });
    } else {
      out.push({ agent: "PayIt", check, at: t.at, layer: "simulated",
        result: blocked ? "blocked" : "pass", message: blocked ? t.message : "" });
    }
  }
  return out;
}

function PresenterTrace({ trace: rawTrace, onClose }: { trace: TraceStep[]; onClose: () => void }) {
  const trace = hideTokenInternals(rawTrace);
  const [tab, setTab] = useState<InfoTab>("vic");
  const tabs: [InfoTab, string][] = [["vic", "Payment"], ["trace", "Live trace"], ["guardrails", "Guards"], ["protocols", "Protocols"], ["email", "Email"]];
  const chip = (r?: string) => r === "pass" ? "bg-good-soft text-good" : r === "blocked" ? "bg-bad-soft text-bad" : "bg-panel text-muted";
  // The latest purchase attempt: everything since the last consent gate.
  const startAt = (() => { for (let i = trace.length - 1; i >= 0; i--) if (trace[i].check === "Consent gate") return i; return -1; })();
  const attempt = startAt >= 0 ? trace.slice(Math.max(0, startAt - 4)) : [];
  const checkoutStep = [...trace.slice(0, startAt >= 0 ? startAt : trace.length)].reverse().find((t) => t.check === "Merchant-owned checkout");
  const vicRows = VIC_STEPS.map((s) => {
    const hit = s.label === "Checkout created" ? checkoutStep : attempt.find(s.match);
    return { label: s.label, step: hit };
  });
  const failed = attempt.find((t) => t.result === "blocked");
  const isVic = attempt.some((t) => t.agent === "VIC simulator");
  return (
    <div className="absolute inset-0 z-10 bg-canvas/97 backdrop-blur overflow-y-auto ss-scroll p-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold">Developer trace</p>
        <button onClick={onClose} className="text-xs underline">Back to chat</button>
      </div>
      <p className="text-xs text-muted leading-relaxed">
        Real backend steps, as they ran. <span className="font-medium text-ink">Local</span> = ShopSphere code that ran here;
        {" "}<span className="font-medium text-gold">Simulated</span> = a local stand-in for an external service (gateway, card
        network, issuer, VIC). No Visa or Chase system is called. Secrets are never shown.
      </p>
      <div role="tablist" className="grid grid-cols-5 rounded-full bg-panel p-1 text-[11px] font-medium">
        {tabs.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
            className={cx("h-7 rounded-full", tab === key ? "bg-canvas shadow-card text-ink" : "text-muted hover:text-ink")}>{label}</button>
        ))}
      </div>

      {tab === "vic" && (
        <div className="flex flex-col gap-2">
          {startAt < 0 ? <p className="text-xs text-faint">Approve and pay for an order to see its payment lifecycle here.</p> : (
            <>
              <p className="text-xs text-muted">
                {isVic ? "Visa agent payment (VIC-inspired local simulation)" : "Standard card path (no VIC token services)"}
                {failed ? " · stopped at a failed check" : ""}
              </p>
              <ol className="flex flex-col gap-1.5">
                {(isVic ? vicRows : vicRows.filter((r) => !/VIC|credential|instruction|agent token|Commerce/.test(r.label))).map((r, i) => (
                  <li key={r.label} className="flex items-start gap-2 text-xs">
                    <span className="w-5 text-faint tabular-nums shrink-0">{i + 1}.</span>
                    <span className="flex-1 min-w-0">
                      <span className="font-medium">{r.label}</span>
                      {r.step?.message && <span className="block text-muted leading-snug">{r.step.message}</span>}
                    </span>
                    <span className={cx("rounded-full px-1.5 text-[10px] font-semibold uppercase shrink-0",
                      r.step ? chip(r.step.result) : "bg-panel text-faint")}>
                      {r.step ? (r.step.result === "pass" ? (r.label.includes("issuer") ? "approved" : "passed") : r.step.result) : "—"}
                    </span>
                  </li>
                ))}
              </ol>
              {failed && <p className="text-xs text-bad">Blocked at: {failed.agent} · {failed.check}{failed.message ? ` (${failed.message})` : ""}</p>}
              {!isVic && <p className="text-[11px] text-muted">Mastercard and non-agent cards use GreenLight + PayIt + the gateway's card-on-file path.</p>}
            </>
          )}
        </div>
      )}

      {tab === "trace" && (
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-3 gap-1.5">
            {Object.entries(AGENTS).map(([name, role]) => (
              <div key={name} className="rounded-xl border border-line px-2 py-1.5">
                <p className="text-[11px] font-semibold text-talk">{name}</p><p className="text-[10px] text-muted leading-snug">{role}</p>
              </div>
            ))}
          </div>
          {trace.length === 0 ? <p className="text-xs text-faint">Each step and guardrail appears here as Talkshop works.</p> : (
            <ol className="flex flex-col gap-2" aria-label="Live guardrail trace">
              {agentsOnly(trace).map((t, i) => (
                <li key={i} className="flex gap-2 text-xs">
                  <span className="text-faint tabular-nums w-10 shrink-0">{new Date(t.at).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" })}</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-1">
                      <span className="font-semibold text-talk">{t.agent}</span>
                      {t.check && t.check !== t.agent && <span className="font-medium text-ink">· {t.check}</span>}
                      {t.result && <span className={cx("rounded-full px-1.5 text-[10px] font-semibold uppercase", chip(t.result))}>{t.result}</span>}
                      {t.layer && <span className={cx("rounded-full px-1.5 text-[10px] font-semibold", t.layer === "simulated" ? "bg-gold-soft text-gold" : "bg-panel text-muted")}>
                        {t.layer === "simulated" ? "Simulated" : "Local"}</span>}
                      {t.protocol && <span className="rounded-full border border-line px-1.5 text-[10px] text-muted">{t.protocol}</span>}
                    </span>
                    {t.message && <span className="block text-muted leading-snug">{t.message}</span>}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {tab === "guardrails" && (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] text-muted">The checks each agent runs, in the order a purchase goes through them.</p>
          <ol className="flex flex-col gap-2.5">
            {GUARDRAILS.map((g, i) => (
              <FlowItem key={g.agent} n={i + 1} step={g.step} last={i === GUARDRAILS.length - 1}>
                <p className="text-xs font-semibold text-talk mb-1">{g.agent}</p>
                <ul className="flex flex-col gap-0.5 text-[11px] text-ink-soft leading-snug list-disc pl-4">
                  {g.checks.map((c) => <li key={c}>{c}</li>)}
                </ul>
              </FlowItem>
            ))}
          </ol>
        </div>
      )}

      {tab === "protocols" && (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] text-muted">The protocols Demo 1 uses, in the order a purchase meets them. At Approve,
            one payment route runs. "Inspired" or "-style" means a local concept demo, not a certified or compliant
            implementation.</p>
          <ol className="flex flex-col gap-2.5">
            {PROTOCOLS.map((p, i) => (
              <FlowItem key={`${p.step}-${p.name}`} n={i + 1} step={p.step} last={i === PROTOCOLS.length - 1}>
                <p className="text-xs font-semibold"><span className="text-talk">{p.name}</span> · {p.what}</p>
                <p className="text-[11px] text-muted leading-snug mt-0.5">{p.where}</p>
              </FlowItem>
            ))}
          </ol>
        </div>
      )}

      {tab === "email" && (
        <ol className="flex flex-col gap-1.5 text-[11px] text-ink-soft leading-snug list-decimal pl-4">
          {EMAIL_STEPS.map((step) => <li key={step}>{step}</li>)}
        </ol>
      )}
    </div>
  );
}

/** The secure payment sheet's state: it opens by itself when Talkshop hands a new card
 *  payment to the provider (not when an old conversation is restored), can be collapsed
 *  and reopened, and reports the provider's final status so ShopSphere can verify it. */
/** One numbered step in the Guards / Protocols lists: number, stepper label, connector to the next step. */
function FlowItem({ n, step, last, children }: { n: number; step: string; last: boolean; children: ReactNode }) {
  return (
    <li className="relative flex gap-2.5">
      {!last && <span className="absolute left-[11px] top-6 -bottom-2.5 w-px bg-line" aria-hidden />}
      <span className="relative z-[1] shrink-0 w-6 h-6 rounded-full bg-talk text-white text-[11px] font-semibold grid place-items-center">{n}</span>
      <div className="flex-1 min-w-0 rounded-xl border border-line p-2.5">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-muted mb-0.5">{step}</p>
        {children}
      </div>
    </li>
  );
}

function useSecurePaymentSheet(ts: ReturnType<typeof useTalkshopShared>) {
  const idx = lastIndex(ts.events, "provider_handoff");
  const ev = idx >= 0 ? ts.events[idx] as Extract<TalkEvent, { type: "provider_handoff" }> : null;
  const live = !!ev && ts.stage === "AWAITING_CONSENT" && lastIndex(ts.events, "user_message") < idx;
  const [openFor, setOpenFor] = useState<number | null>(null);
  useEffect(() => { if (live && idx >= ts.restoredCount) setOpenFor(idx); }, [idx, live]);   // eslint-disable-line react-hooks/exhaustive-deps
  const close = useCallback(() => setOpenFor(null), []);
  const reopen = useCallback(() => setOpenFor(idx), [idx]);
  // The provider's page finished (session + status only): ShopSphere checks it server-to-server.
  const report = useCallback((status: string) => {
    if (!ev) return;
    const label = status === "VERIFIED" ? "Payment verified with the payment provider"
      : status === "CANCELLED" ? "Cancelled the payment verification" : "Payment not completed";
    ts.send({ action: { type: "provider_complete", checkout_id: ev.checkout_id, payment_session_id: ev.payment_session_id, label } });
  }, [ev, ts]);
  // Closed with ✕ before finishing: release the session, back to the review (nothing charged).
  const abandon = useCallback(() => {
    if (!ev) return;
    setOpenFor(null);
    ts.send({ action: { type: "provider_abandon", checkout_id: ev.checkout_id, payment_session_id: ev.payment_session_id,
      label: "Closed secure payment" } });
  }, [ev, ts]);
  return { ev, open: live && openFor === idx, close, reopen, report, abandon };
}
