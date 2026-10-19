/** The cards Talkshop draws inside the panel (plan §6.1). Each renders a
 *  server event as-is — prices, stock, totals and order ids are never
 *  computed here. Only the latest card of a kind is interactive. */
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowUpRight, Bot, Check, CheckCircle2, CreditCard, Fingerprint, Loader2, Lock, MapPin, ShieldCheck, ShoppingBag, Truck, UserRound, XCircle } from "lucide-react";
import type { OptionChoice, TalkAction, TalkEvent } from "../../api/talkshop";
import { money, niceDate, type Card, type Cart, type Checkout, type Order, type Product, type TrackedOrder } from "../../api/shop";
import { ShipmentProgress } from "../shopsphere/ShipmentProgress";
import { AddressForm, CardForm, GuestDetailsForm } from "../shopsphere/CheckoutForms";
import { Button, Field, Notice, Rating, Stepper, cx } from "../ui";
import { useAuth } from "../../auth/AuthContext";
import { RESUME_KEY } from "../../talkshop/TalkshopContext";

type Act = (action: TalkAction & { label?: string }) => void;

function Shell({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("rounded-2xl border border-line bg-canvas overflow-hidden", className)}>{children}</div>;
}

/* ── Top 3 ─────────────────────────────────────────────────────────────── */
export function RecommendationCards({ products, active, onAct }:
  { products: (Product & { reason?: string | null })[]; active: boolean; onAct: Act }) {
  return (
    <div className="flex flex-col gap-2">
      {products.map((p, i) => (
        <Shell key={p.product_id} className="flex gap-3 p-2.5">
          <div className="relative w-20 h-20 shrink-0 rounded-xl bg-photo overflow-hidden">
            {p.image_url && <img src={p.image_url} alt={p.name} className="h-full w-full object-cover" />}
            <span className="absolute left-1 top-1 w-5 h-5 rounded-full bg-canvas/90 text-[11px] font-semibold grid place-items-center">{i + 1}</span>
          </div>
          <div className="flex-1 min-w-0 flex flex-col">
            <div className="flex justify-between gap-2">
              <p className="text-sm font-medium leading-tight line-clamp-1">{p.name}</p>
              <p className="text-sm font-semibold tabular-nums shrink-0">{money(p.price)}</p>
            </div>
            <Rating value={p.rating} size={11} />
            {p.reason && <p className="text-xs text-muted leading-snug mt-0.5 line-clamp-2">{p.reason}</p>}
            <div className="mt-auto pt-1.5 flex items-center gap-3">
              <Button size="sm" variant={active ? "primary" : "secondary"} disabled={!active}
                onClick={() => onAct({ type: "select", product_id: p.product_id, label: p.name })}>Select</Button>
              <Link to={`/p/${p.product_id}`} className="text-xs text-muted hover:text-ink inline-flex items-center gap-0.5">
                View on ShopSphere <ArrowUpRight size={12} />
              </Link>
            </div>
          </div>
        </Shell>
      ))}
    </div>
  );
}

/* ── Selected product header ───────────────────────────────────────────── */
export function SelectedProduct({ event }: { event: Extract<TalkEvent, { type: "product_selected" }> }) {
  const p = event.product;
  return (
    <Shell className="flex items-center gap-3 p-2.5">
      <div className="w-12 h-12 rounded-lg bg-photo overflow-hidden shrink-0">{p.image_url && <img src={p.image_url} alt="" className="h-full w-full object-cover" />}</div>
      <div className="min-w-0 flex-1">
        <p className="text-[11px] uppercase tracking-wider text-muted">{event.from_page ? "From this page" : "Selected"}</p>
        <p className="text-sm font-medium line-clamp-1">{p.name}</p>
      </div>
      <p className="text-sm font-semibold tabular-nums">{money(p.price)}</p>
    </Shell>
  );
}

/* ── Size / colour chips ───────────────────────────────────────────────── */
export function OptionChips({ option, label, choices, active, onAct }:
  { option: "size" | "color"; label: string; choices: OptionChoice[]; active: boolean; onAct: Act }) {
  const pick = (c: OptionChoice) => onAct({ type: option === "size" ? "choose_size" : "choose_color", value: c.value,
    label: option === "size" ? `${label} ${c.value}` : c.value });
  if (option === "color") {
    return (
      <div className="flex flex-wrap gap-2">
        {choices.map((c) => (
          <button key={c.value} type="button" disabled={!active || !c.available} onClick={() => pick(c)}
            title={c.available ? c.value : `${c.value} — out of stock`}
            className={cx("flex items-center gap-2 h-10 pl-1.5 pr-3 rounded-full border text-sm transition-colors",
              c.available ? "border-line-strong hover:border-ink" : "border-line text-faint line-through",
              !active && "opacity-50 cursor-default")}>
            {c.image_url ? <img src={c.image_url} alt="" className="w-7 h-7 rounded-full object-cover bg-photo" />
              : <span className="w-6 h-6 rounded-full border border-black/10" style={{ background: c.hex }} />}
            {c.value}
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {choices.map((c) => (
        <button key={c.value} type="button" disabled={!active || !c.available} onClick={() => pick(c)}
          title={c.available ? undefined : "Out of stock"}
          className={cx("min-w-10 h-9 px-2.5 rounded-lg border text-sm font-medium transition-colors",
            c.available ? "border-line-strong hover:border-ink hover:bg-panel" : "border-line text-faint line-through bg-panel",
            !active && "opacity-50 cursor-default")}>
          {c.value}
        </button>
      ))}
    </div>
  );
}

/* ── Added to cart ─────────────────────────────────────────────────────── */
export function CartUpdatedCard({ cart, lineId }: { cart: Cart; lineId: string }) {
  const line = cart.lines.find((l) => l.line_id === lineId);
  if (!line) return null;
  return (
    <Shell className="p-3">
      <p className="flex items-center gap-1.5 text-xs font-semibold text-good mb-2"><Check size={14} /> Added to your ShopSphere cart</p>
      <div className="flex items-center gap-3">
        <div className="w-12 h-12 rounded-lg bg-photo overflow-hidden shrink-0">{line.image_url && <img src={line.image_url} alt="" className="h-full w-full object-cover" />}</div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium line-clamp-1">{line.name}</p>
          <p className="text-xs text-muted">{line.size ? `${line.option_label} ${line.size} · ` : ""}{line.color} · Qty {line.quantity}</p>
        </div>
        <p className="text-sm font-semibold tabular-nums">{money(line.line_total)}</p>
      </div>
      <div className="mt-2.5 pt-2.5 border-t border-line flex justify-between text-xs text-muted">
        <span className="inline-flex items-center gap-1"><ShoppingBag size={13} /> {cart.item_count} item{cart.item_count === 1 ? "" : "s"} in cart</span>
        <span>Cart subtotal <span className="text-ink font-medium tabular-nums">{money(cart.subtotal)}</span></span>
      </div>
    </Shell>
  );
}

/* ── Quick replies (checkout offer, suggestions) ───────────────────────── */
export function QuickReplies({ choices, active, onPick }:
  { choices: { value: string; label: string; primary?: boolean }[]; active: boolean; onPick: (c: { value: string; label: string }) => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {choices.map((c, i) => (
        <Button key={c.value} size="sm" variant={active && (c.primary ?? i === 0) ? "talk" : "secondary"} disabled={!active}
          onClick={() => onPick(c)}>{c.label}</Button>
      ))}
    </div>
  );
}

/* ── Auto-checkout countdown ───────────────────────────────────────────── */

export const AUTO_CHECKOUT_SECONDS = 6;

/** After an item is added: move to checkout automatically in 6 seconds unless
 *  the shopper taps Keep shopping. Checkout only opens the review card; paying
 *  still needs GO AHEAD. The shopper can pause and resume it; it also pauses
 *  if they start typing to Talkshop. */
export function CheckoutCountdown({ onPick }: { onPick: (c: { value: string; label: string }) => void }) {
  const [left, setLeft] = useState(AUTO_CHECKOUT_SECONDS);
  const [paused, setPaused] = useState(false);
  const fired = useRef(false);
  const [draining, setDraining] = useState(false);       // the bar empties smoothly, one second at a time
  useEffect(() => { const f = requestAnimationFrame(() => setDraining(true)); return () => cancelAnimationFrame(f); }, []);
  // A pause holds the current second; resuming counts it again from the start.
  const barWidth = `${(Math.max(0, draining && !paused ? left - 1 : left) / AUTO_CHECKOUT_SECONDS) * 100}%`;
  const pick = (c: { value: string; label: string }) => {
    if (fired.current) return;
    fired.current = true;
    onPick(c);
  };

  useEffect(() => {
    if (paused) return;
    if (left <= 0) { pick({ value: "checkout", label: "Continue to checkout" }); return; }
    const timer = setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => clearTimeout(timer);
  }, [left, paused]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {       // typing to Talkshop means the shopper is busy — don't move on without them
    const onFocus = (e: FocusEvent) => {
      const el = e.target as HTMLElement | null;
      if (el?.matches?.("textarea, input") && el.closest("[aria-label='Talkshop assistant']")) setPaused(true);
    };
    document.addEventListener("focusin", onFocus);
    return () => document.removeEventListener("focusin", onFocus);
  }, []);

  return (
    <div className="rounded-2xl border border-talk/40 bg-talk-soft/40 p-3.5 flex flex-col gap-2.5">
      <p className="sr-only" role="status">
        {paused ? `Automatic checkout paused with ${left} seconds left.` : `Moving to checkout shortly. Tap Pause or Keep shopping to stay.`}
      </p>
      <p aria-hidden="true" className="text-sm font-medium tabular-nums">{paused ? `Auto-checkout paused · ${left}s left` : `Moving to checkout in ${left}s`}</p>
      <div aria-hidden="true" className="h-1 rounded-full bg-line overflow-hidden">
        <div className={`h-full ${paused ? "bg-muted" : "bg-talk"} ease-linear motion-reduce:transition-none ${paused ? "" : "transition-[width] duration-1000"}`}
          style={{ width: barWidth }} />
      </div>
      <p className="text-xs text-muted leading-snug">
        {paused ? "Resume when you're ready, or check out now." : "Need a moment? Tap Pause."} Nothing is charged until you approve the order.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="talk" onClick={() => pick({ value: "checkout", label: "Checkout now" })}>Checkout now</Button>
        <Button size="sm" variant="secondary" aria-pressed={paused} onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </Button>
        <Button size="sm" variant="secondary" onClick={() => pick({ value: "keep_shopping", label: "Keep shopping" })}>Keep shopping</Button>
      </div>
    </div>
  );
}

/** Loyalty points on the review card: use / remove, and what this order earns. */
function PointsLine({ co, live, busy, onUse }: { co: Checkout; live: boolean; busy: boolean; onUse: (n: number) => void }) {
  const pts = co.points;
  if (!pts) {
    return co.guest ? <p className="text-xs text-muted">Create a ShopSphere account to earn {co.earn_points.toLocaleString()} points on orders like this.</p> : null;
  }
  return (
    <div className="flex flex-col gap-1 text-xs">
      {pts.used > 0 ? (
        <p className="flex items-center justify-between gap-2">
          <span>Using {pts.used.toLocaleString()} points (−{money(pts.discount)})</span>
          {live && <button type="button" disabled={busy} onClick={() => onUse(0)} className="font-medium underline">Remove</button>}
        </p>
      ) : pts.max_usable > 0 ? (
        <p className="flex items-center justify-between gap-2">
          <span>You have {pts.balance.toLocaleString()} points</span>
          {live && <button type="button" disabled={busy} onClick={() => onUse(pts.max_usable)} className="font-medium text-talk underline">
            Use {pts.max_usable.toLocaleString()} (−{money(pts.max_usable * pts.value_per_point)})</button>}
        </p>
      ) : null}
      <p className="text-muted">You'll earn {co.earn_points.toLocaleString()} points with this order
        {co.bonus_points?.length ? ` (incl. ${co.bonus_points.reduce((n, b) => n + b.points, 0).toLocaleString()} bonus)` : ""}.</p>
      {co.cashback?.map((c) => (
        <p key={c.offer_id} className="text-good">+{money(c.amount)} cashback after payment (simulated statement credit)</p>
      ))}
    </div>
  );
}

/** Which payment path the server chose, in one line (Part 2). */
function PathNote({ co }: { co: Checkout }) {
  const p = co.payment_path?.path;
  if (p === "vic") return <p className="text-[11px] rounded-lg bg-good-soft text-good px-2.5 py-1.5 leading-snug">
    Visa agent payment: you'll confirm with a simulated passkey; a one-time credential pays for this order only.</p>;
  if (p === "vic_setup_required") return <p className="text-[11px] rounded-lg bg-gold-soft text-gold px-2.5 py-1.5 leading-snug">
    Set up Visa agent payments first (simulated passkey), or choose another card.</p>;
  if (p === "standard") return <p className="text-[11px] rounded-lg bg-panel text-muted px-2.5 py-1.5 leading-snug">
    Saved card payment through ShopSphere's gateway on the {co.payment_path?.network ?? "card"} network (no Visa agent token).</p>;
  if (p === "provider_handoff") return <p className="text-[11px] rounded-lg bg-panel text-muted px-2.5 py-1.5 leading-snug">
    New card: after you approve, the Simulated Secure Payment Provider's own page takes it. Talkshop never sees the card number.</p>;
  return null;
}

/* ── Review your order (the consent gate) ──────────────────────────────── */
/** Payment needs the member's explicit tap on Approve and pay — there is no
 *  automatic payment countdown. */
export function ReviewOrderCard({ checkout: co, active, busy, onAct }:
  { checkout: Checkout; active: boolean; busy: boolean; onAct: Act }) {
  const [changing, setChanging] = useState<"address" | "card" | null>(null);
  const [adding, setAdding] = useState<"address" | "card" | null>(null);
  const live = active && co.status === "open";
  const update = (changes: Omit<Extract<TalkAction, { type: "update_checkout" }>, "type">) => {
    onAct({ type: "update_checkout", ...changes });
  };
  const needsAddress = !co.address, needsCard = !co.payment_method;

  return (
    <Shell className={cx(live && "border-talk/50 shadow-card")}>
      <div className="px-4 py-3 bg-panel flex items-center justify-between">
        <p className="text-xs font-bold tracking-[0.16em]">REVIEW YOUR ORDER</p>
        {co.status === "cancelled" && <span className="text-xs text-muted">Cancelled</span>}
        {co.status === "paid" && <span className="text-xs text-good font-medium">Placed</span>}
      </div>
      <div className="p-4 flex flex-col gap-3">
        {co.lines.map((l) => (
          <div key={l.line_id} className="flex items-center gap-3">
            <div className="w-14 h-14 rounded-xl bg-photo overflow-hidden shrink-0">{l.image_url && <img src={l.image_url} alt="" className="h-full w-full object-cover" />}</div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium line-clamp-1">{l.name}</p>
              <p className="text-xs text-muted">{l.size ? `${l.option_label} ${l.size} · ` : ""}{l.color}</p>
              {live ? <div className="mt-1"><Stepper value={l.quantity} busy={busy} max={10}
                onChange={(n) => update({ quantities: { [l.line_id]: n } })} /></div>
                : <p className="text-xs text-muted">Qty {l.quantity}</p>}
            </div>
            <p className="text-sm font-semibold tabular-nums">{money(l.line_total)}</p>
          </div>
        ))}

        <div className="border-t border-line pt-3 flex flex-col gap-1 text-sm">
          <Row label="Product" value={money(co.subtotal)} />
          {co.offer && <Row label={`Chase card offer · ${co.offer.title}`} value={`−${money(co.offer.discount)}`} />}
          {co.points_discount > 0 && <Row label={`Points (${co.points?.used.toLocaleString()})`} value={`−${money(co.points_discount)}`} />}
          <Row label={`Tax`} value={money(co.tax)} />
          {co.shipping > 0 && <Row label="Express delivery" value={money(co.shipping)} />}
          <div className="flex justify-between font-semibold text-[15px] pt-1.5 border-t border-line mt-1"><span>Total</span><span className="tabular-nums">{money(co.total)}</span></div>
          <p className="text-[11px] text-faint">Invoice {co.checkout_id} · version {co.checkout_version} · {co.currency}</p>
        </div>
        {(co.benefits?.removed ?? []).length > 0 && (
          <div className="rounded-xl border border-gold/40 bg-gold-soft/40 px-3 py-2 text-xs flex flex-col gap-0.5">
            <p className="font-semibold">Chase benefits not applied</p>
            {co.benefits.removed.map((b) => <p key={b.offer_id} className="text-ink-soft">{b.title}: {b.reason_text}</p>)}
          </div>
        )}

        <PointsLine co={co} live={live} busy={busy} onUse={(n) => update({ points_used: n })} />

        <div className="flex flex-col gap-1.5">
          <p className="flex items-center gap-1.5 text-xs font-medium text-muted"><Truck size={13} /> Delivery</p>
          <div className="grid grid-cols-2 gap-1.5">
            {co.delivery.options.map((o) => (
              <button key={o.method} type="button" disabled={!live || busy}
                onClick={() => o.method !== co.delivery.method && update({ delivery_method: o.method })}
                className={cx("text-left rounded-xl border px-3 py-2 text-xs transition-colors",
                  co.delivery.method === o.method ? "border-ink bg-panel" : "border-line hover:border-line-strong", !live && "cursor-default")}>
                <span className="flex justify-between font-medium text-ink"><span>{o.label}</span><span>{o.price ? money(o.price) : "Free"}</span></span>
                <span className="text-muted">{niceDate(o.date)}</span>
              </button>
            ))}
          </div>
        </div>

        {co.guest?.email && (
          <p className="text-xs text-muted -mb-1">Guest checkout · confirmation to <span className="text-ink">{co.guest.email}</span></p>
        )}
        <Detail icon={<MapPin size={13} />} label="Ship to" value={co.address?.display}
          canChange={live && co.saved_addresses.length > 0} onChange={() => { setChanging(changing === "address" ? null : "address"); setAdding(null); }} />
        {live && changing === "address" && (
          <Choices items={co.saved_addresses.map((a) => ({ id: a.address_id, label: `${a.label} · ${a.line1}`, on: a.address_id === co.address?.address_id }))}
            onPick={(id) => { update({ address_id: id }); setChanging(null); }} onAdd={() => { setAdding("address"); setChanging(null); }} addLabel="+ New address" />
        )}
        {live && (adding === "address" || needsAddress) && (
          <div className="rounded-xl border border-line p-3"><AddressForm onCancel={needsAddress ? undefined : () => setAdding(null)}
            onSaved={(a) => { setAdding(null); update({ address_id: a.address_id }); }} /></div>
        )}

        <Detail icon={<CreditCard size={13} />} label="Payment" value={(co.payment_method as Card | null)?.label ?? co.payment_method?.display}
          canChange={live && (co.saved_payment_methods.length > 0 || !!co.wallet || !!co.provider_option)} onChange={() => { setChanging(changing === "card" ? null : "card"); setAdding(null); }} />
        {co.payment_method && <PathNote co={co} />}
        {live && changing === "card" && (
          <Choices items={[
              ...(co.wallet ? [{ id: "wallet", label: `ShopSphere Wallet · ${money(co.wallet.balance)}`
                + (co.saved_payment_methods.length ? "" : " (needs a saved credit card)"), on: co.pay_with === "wallet" }] : []),
              ...co.saved_payment_methods.map((c) => ({ id: c.payment_method_id, label: c.label ?? c.display,
                on: co.pay_with === "card" && c.payment_method_id === co.payment_method?.payment_method_id })),
              ...(co.provider_option ? [{ id: "provider", label: "New card · secure payment provider", on: co.pay_with === "provider" }] : []),
            ]}
            onPick={(id) => { update(id === "wallet" ? { pay_with: "wallet" } : id === "provider" ? { pay_with: "provider" }
              : { payment_method_id: id }); setChanging(null); }}
            onAdd={() => { setAdding("card"); setChanging(null); }} addLabel="+ New card" />
        )}
        {live && (adding === "card" || needsCard) && (
          <div className="rounded-xl border border-line p-3"><CardForm allowSave={!co.guest} onCancel={needsCard ? undefined : () => setAdding(null)}
            onUseProvider={co.provider_option ? () => { setAdding(null); update({ pay_with: "provider" }); } : undefined} /></div>
        )}

        {co.issues.filter((i) => i.code === "OUT_OF_STOCK").map((i) => (
          <p key={i.name} className="text-xs text-bad">{i.name}: only {i.available} left.</p>
        ))}
        {live && !co.ready && <BlockedReasons co={co} busy={busy} onNewCard={() => update({ pay_with: "provider" })} />}

        {live && (
          <>
            <Button size="lg" variant="talk" className="w-full tracking-wide" disabled={!co.ready || busy}
              onClick={() => onAct({ type: "go_ahead", checkout_id: co.checkout_id, label: "Approve and pay",
                checkout_version: co.checkout_version, final_amount: co.total, currency: co.currency,
                return_url: `${window.location.origin}${window.location.pathname}` })}>
              {co.payment_path?.path === "vic" ? <Fingerprint size={16} /> : co.pay_with === "provider" ? <ShieldCheck size={16} /> : <Lock size={15} />}
              {co.payment_path?.path === "vic_setup_required" ? "Set up agent payments"
                : co.pay_with === "provider" ? `Continue to Secure Payment · ${money(co.total)}` : `Approve & Pay ${money(co.total)}`}
            </Button>
            <button type="button" disabled={busy} onClick={() => onAct({ type: "cancel_checkout", label: "Cancel order" })}
              className="text-xs text-muted hover:text-ink -mt-1">Cancel</button>
            <p className="flex items-start gap-1.5 text-[11px] text-muted leading-snug">
              <ShieldCheck size={13} className="shrink-0" />
              {co.payment_path?.path === "vic" ? "Nothing is charged until you approve and confirm with your passkey." : "Nothing is charged until you tap Approve and pay."} Talkshop never sees your card number.
            </p>
          </>
        )}
      </div>
    </Shell>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between"><span className="text-muted">{label}</span><span className="tabular-nums">{value}</span></div>;
}

function Detail({ icon, label, value, canChange, onChange }: { icon: ReactNode; label: string; value?: string; canChange: boolean; onChange: () => void }) {
  return (
    <div className="flex items-start justify-between gap-3 text-sm">
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-xs font-medium text-muted">{icon} {label}</p>
        <p className={cx("leading-snug", value ? "text-ink" : "text-faint")}>{value ?? "Not added yet"}</p>
      </div>
      {canChange && <button type="button" onClick={onChange} className="text-xs font-medium underline shrink-0">Change</button>}
    </div>
  );
}

function Choices({ items, onPick, onAdd, addLabel }: { items: { id: string; label: string; on: boolean }[]; onPick: (id: string) => void; onAdd: () => void; addLabel: string }) {
  return (
    <div className="flex flex-col gap-1 -mt-1">
      {items.map((it) => (
        <button key={it.id} type="button" onClick={() => onPick(it.id)}
          className={cx("text-left text-xs rounded-lg border px-3 py-2", it.on ? "border-ink bg-panel" : "border-line hover:border-line-strong")}>{it.label}</button>
      ))}
      <button type="button" onClick={onAdd} className="self-start text-xs font-medium hover:underline mt-0.5">{addLabel}</button>
    </div>
  );
}

/* ── Payment status ────────────────────────────────────────────────────── */
const STEPS = ["processing", "authorizing", "authorized"] as const;
export function PaymentStatusCard({ state, payment, message }: { state: string; payment: string | null; message?: string }) {
  if (state === "handoff") return (
    <Shell className="p-4"><p className="text-xs text-muted">Payment: <span className="text-ink font-medium">{payment ?? "—"}</span></p>
      <p className="text-sm mt-2 flex items-center gap-2"><ShieldCheck size={16} className="text-talk" /> Approved · waiting for the secure payment provider</p>
    </Shell>
  );
  if (state === "cancelled") return (
    <Shell className="p-4"><p className="text-sm flex items-center gap-2"><XCircle size={16} className="text-muted" /> {message ?? "Payment cancelled"}</p>
      <p className="text-xs text-muted mt-1">Nothing was authorized or charged.</p>
    </Shell>
  );
  const failed = state === "declined" || state === "failed";
  const reached = failed ? 1 : STEPS.indexOf(state as (typeof STEPS)[number]);
  const labels = ["Processing your order", "Authorizing payment", "Payment authorized"];
  return (
    <Shell className="p-4">
      <p className="text-xs text-muted mb-3">Payment: <span className="text-ink font-medium">{payment ?? "—"}</span></p>
      <ol className="flex flex-col gap-2.5">
        {labels.map((l, i) => {
          const done = !failed && i <= reached, current = !failed && i === reached && state !== "authorized";
          const fail = failed && i === 2;
          return (
            <li key={l} className="flex items-center gap-2.5 text-sm">
              {fail ? <XCircle size={18} className="text-bad" /> : current ? <Loader2 size={18} className="animate-spin text-talk" />
                : done || (failed && i < 2) ? <CheckCircle2 size={18} className="text-good" /> : <span className="w-[18px] h-[18px] rounded-full border-2 border-line" />}
              <span className={cx(fail && "text-bad font-medium", state === "authorized" && i === 2 && "font-semibold text-good")}>
                {fail ? "Payment not authorized" : l}
              </span>
            </li>
          );
        })}
      </ol>
      {failed && message && <p className="text-xs text-bad mt-2.5">{message}</p>}
    </Shell>
  );
}

/* ── Order confirmed ───────────────────────────────────────────────────── */
export function OrderConfirmedCard({ order, email }: { order: Order; email?: string | null }) {
  return (
    <Shell className="border-good/40">
      <div className="px-4 py-3 bg-good-soft text-good flex items-center gap-2">
        <CheckCircle2 size={18} /><p className="text-sm font-bold tracking-wide">ORDER CONFIRMED</p>
        <span className="ml-auto text-sm font-semibold">{order.order_id}</span>
      </div>
      <div className="p-4 flex flex-col gap-3 text-sm">
        {order.lines.map((l) => (
          <div key={l.sku} className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-lg bg-photo overflow-hidden shrink-0">{l.image_url && <img src={l.image_url} alt="" className="h-full w-full object-cover" />}</div>
            <div className="min-w-0 flex-1">
              <p className="font-medium line-clamp-1">{l.name}</p>
              <p className="text-xs text-muted">{l.size ? `${l.size} – ` : ""}{l.color} · Qty {l.quantity}</p>
            </div>
          </div>
        ))}
        <div className="flex flex-col gap-1 border-t border-line pt-3">
          {order.offer && <Row label={`Chase card offer · ${order.offer.title}`} value={`−${money(order.offer.discount)}`} />}
          {!!order.points?.used && <Row label={`Points used (${order.points.used.toLocaleString()})`} value={`−${money(order.points.discount)}`} />}
          <Row label="Total" value={money(order.total)} />
          <Row label="Paid with" value={`${order.payment?.display ?? "—"}${order.payment_path === "vic" ? " · Visa agent payment" : ""}`} />
          <Row label="Expected delivery" value={niceDate(order.delivery_date)} />
          {!!order.points?.earned && <Row label="Points earned" value={`+${order.points.earned.toLocaleString()}`} />}
          {!!order.cashback && <Row label="Cashback (simulated credit)" value={`+${money(order.cashback)}`} />}
        </div>
        {email && <p className="text-xs text-muted">Confirmation emailed to <span className="text-ink break-all">{email}</span></p>}
        {order.guest ? (
          <Link to={`/track?order=${encodeURIComponent(order.order_id)}`}
            className="inline-flex items-center justify-center gap-1.5 h-10 rounded-full border border-line-strong text-sm font-medium hover:border-ink">
            Track your order <ArrowUpRight size={14} />
          </Link>
        ) : (
          <Link to={`/orders/${order.order_id}`}
            className="inline-flex items-center justify-center gap-1.5 h-10 rounded-full border border-line-strong text-sm font-medium hover:border-ink">
            View in My Orders <ArrowUpRight size={14} />
          </Link>
        )}
      </div>
    </Shell>
  );
}

/* ── Sign in to check out (visitors) ───────────────────────────────────── */
const DEMO = { email: "kaajal@shopsphere.demo", password: "demo1234" };

/** ShopSphere's own secure forms, shown when an order is missing an address or
 *  a card. They post straight to ShopSphere (profile API / card tokenization)
 *  and hand Talkshop only the saved id, never the address or card details, so
 *  nothing typed here reaches the chat history or the LLM. */
export function SecureDetailsCard({ checkoutId, needs, guest, wallet, active, onAct }:
  { checkoutId: string; needs: { guest?: boolean; address: boolean; payment: boolean }; guest?: boolean;
    wallet?: { balance: number; enough: boolean } | null; active: boolean; onAct: Act }) {
  const { user } = useAuth();
  const step = needs.guest ? "guest" : needs.address ? "address" : "payment";
  if (!active) {
    return <p className="self-start text-xs text-muted inline-flex items-center gap-1.5"><Lock size={12} />
      {step === "guest" ? "Your details" : step === "address" ? "Shipping address" : "Card"} handled in ShopSphere's secure form</p>;
  }
  return (
    <Shell className="border-talk/50 shadow-card">
      <div className="px-4 py-3 bg-panel flex items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-xs font-bold tracking-[0.16em]">
          <ShieldCheck size={15} className="text-talk" /> {step === "payment" ? "SECURE PAYMENT" : "SECURE SHOPSPHERE CHECKOUT"}
        </p>
        {(needs.address || needs.guest) && needs.payment && <span className="text-[11px] text-muted shrink-0">Step 1 of 2</span>}
      </div>
      <div className="p-4 flex flex-col gap-3">
        <p className="text-sm font-medium">{step === "guest" ? "Your details" : step === "address" ? "Shipping address" : "A credit card is needed"}</p>
        {step === "guest" && (
          <p className="text-xs text-muted -mt-2">Checking out as a guest: no account is created. Your confirmation goes to this email.</p>
        )}
        {step === "guest"
          ? <GuestDetailsForm checkoutId={checkoutId}
              onSaved={() => onAct({ type: "details_added", label: "Details added" })} />
          : step === "address"
          ? <AddressForm defaultName={user?.name}
              onSaved={(a) => onAct({ type: "details_added", address_id: a.address_id, label: "Shipping address added" })} />
          : <CardForm allowSave={!guest} onUseProvider={guest ? undefined
              : () => onAct({ type: "details_added", pay_with: "provider", label: "Pay with a new card (secure payment provider)" })} />}
        {step === "payment" && wallet && (
          <button type="button" disabled={!wallet.enough}
            onClick={() => onAct({ type: "details_added", pay_with: "wallet", label: "Pay with ShopSphere Wallet" })}
            className="self-start text-sm font-medium hover:underline disabled:opacity-50 disabled:no-underline">
            Or pay with your ShopSphere Wallet ({money(wallet.balance)} available)
          </button>
        )}
        <p className="flex items-start gap-1.5 text-[11px] text-muted leading-snug">
          <ShieldCheck size={13} className="shrink-0" />
          {step !== "payment"
            ? "This information is sent directly to ShopSphere checkout and is not processed as chat content."
            : "Talkshop never sees card numbers. It only ever sees a card's type and last 4 digits."}
        </p>
        <button type="button" onClick={() => onAct({ type: "cancel_checkout", label: "Not now" })}
          className="text-xs text-muted hover:text-ink">Not now</button>
      </div>
    </Shell>
  );
}

/** Shown when a visitor wants to check out (CH-2/CH-7). Searching, browsing and the cart
 *  are open to everyone; checkout needs a ShopSphere account (any credit-card holder —
 *  Chase status doesn't matter). New shoppers can create the account right here: the
 *  form posts straight to ShopSphere's sign-up (never into the chat), the cart and this
 *  conversation come along, and Talkshop carries on to checkout. No card is asked for. */
export function SignInCard({ active }: { active: boolean; reason?: string }) {
  const { register } = useAuth();
  const location = useLocation();
  const next = encodeURIComponent(location.pathname + location.search);
  const resume = () => { try { sessionStorage.setItem(RESUME_KEY, "checkout"); } catch { /* private mode */ } };
  const [mode, setMode] = useState<"choose" | "signup">("choose");
  const [f, setF] = useState({ name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!active) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    resume();                                     // after the account exists, Talkshop goes on to checkout
    try { await register(f.name, f.email, f.password); }
    catch (err) {
      try { sessionStorage.removeItem(RESUME_KEY); } catch { /* private mode */ }
      setError((err as Error).message);
    } finally { setBusy(false); setF((x) => ({ ...x, password: "" })); }
  };
  return (
    <Shell className="border-talk/50 shadow-card">
      <div className="px-4 py-3 bg-panel flex items-center gap-2">
        <UserRound size={15} className="text-talk" /><p className="text-xs font-bold tracking-[0.16em]">SIGN IN TO CHECK OUT</p>
      </div>
      <div className="p-4 flex flex-col gap-2.5">
        <p className="text-xs text-muted leading-snug">
          ShopSphere checkout is for credit-card holders with a ShopSphere account. Your cart and this conversation
          come with you.
        </p>
        {mode === "signup" ? (
          <form onSubmit={submit} className="flex flex-col gap-2.5">
            <Field label="Full name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required autoComplete="name" />
            <Field label="Email" type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required autoComplete="email" />
            <Field label="Create a password (min 8 characters)" type="password" value={f.password} minLength={8} required
              onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" />
            {error && <Notice>{error}</Notice>}
            <Button type="submit" loading={busy}>Create account and continue</Button>
            <p className="flex items-start gap-1.5 text-[11px] text-muted leading-snug"><Lock size={12} className="shrink-0 mt-0.5" />
              Sent straight to ShopSphere's sign-up, not into the chat. No card details: you'll give a card on the secure
              payment provider's page at checkout.</p>
            <button type="button" onClick={() => setMode("choose")} className="text-xs text-muted hover:text-ink">Back</button>
          </form>
        ) : (
          <>
            <Link to={`/login?next=${next}`} onClick={resume}
              className="inline-flex items-center justify-center h-10 rounded-full bg-talk text-white text-sm font-medium hover:opacity-90">
              Sign in
            </Link>
            <button type="button" onClick={() => setMode("signup")}
              className="inline-flex items-center justify-center h-10 rounded-full border border-line-strong text-sm font-medium hover:border-ink">
              New to ShopSphere? Create an account here
            </button>
          </>
        )}
      </div>
    </Shell>
  );
}

/* ── Orders already placed (Phase 11) ──────────────────────────────────── */

/** Order ID + email, straight to ShopSphere's order lookup (like the website's
 *  Track Order). The email is sent with the lookup, not as chat text, so it
 *  never reaches the LLM. */
export function TrackLookupCard({ orderId, active, onAct }: { orderId: string | null; active: boolean; onAct: Act }) {
  const [id, setId] = useState(orderId ?? "");
  const [email, setEmail] = useState("");
  if (!active) return null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const order = id.trim().toUpperCase();
    onAct({ type: "track_order", order_id: order, email: email.trim(), label: `Track order ${order}` });
  };
  return (
    <Shell className="border-talk/50 shadow-card">
      <div className="px-4 py-3 bg-panel flex items-center gap-2">
        <Truck size={15} className="text-talk" /><p className="text-xs font-bold tracking-[0.16em]">TRACK YOUR ORDER</p>
      </div>
      <form onSubmit={submit} className="p-4 flex flex-col gap-3">
        <Field label="Order ID" value={id} onChange={(e) => setId(e.target.value)} required placeholder="SS-12345" autoComplete="off" />
        <Field label="Email used for the order" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          required placeholder="you@example.com" autoComplete="email" />
        <Button type="submit" variant="talk">Show my order</Button>
        <p className="flex items-start gap-1.5 text-[11px] text-muted leading-snug">
          <ShieldCheck size={13} className="shrink-0" /> Your email is only used to find your order and keep it private.
        </p>
      </form>
    </Shell>
  );
}

export function OrderTrackingCard({ order }: { order: TrackedOrder }) {
  return (
    <Shell>
      <div className="px-4 py-3 bg-panel flex items-center justify-between gap-2">
        <p className="text-xs font-bold tracking-[0.16em]">ORDER {order.order_id}</p>
        <span className="text-xs font-medium text-talk">{order.shipment.status_label}</span>
      </div>
      <div className="p-4 flex flex-col gap-4 text-sm">
        {order.lines.map((l, i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-lg bg-photo overflow-hidden shrink-0">{l.image_url && <img src={l.image_url} alt="" className="h-full w-full object-cover" />}</div>
            <div className="min-w-0 flex-1">
              <p className="font-medium line-clamp-1">{l.name}</p>
              <p className="text-xs text-muted">{l.size ? `${l.size} · ` : ""}{l.color} · Qty {l.quantity}</p>
            </div>
            <p className="font-semibold tabular-nums">{money(l.line_total)}</p>
          </div>
        ))}
        <div className="flex flex-col gap-1 border-t border-line pt-3">
          <Row label="Total" value={money(order.total)} />
          <Row label="Paid with" value={order.payment?.display ?? "—"} />
          <Row label="Shipping to" value={[order.ship_to.city, order.ship_to.state].filter(Boolean).join(", ") || "—"} />
        </div>
        <ShipmentProgress shipment={order.shipment} />
        <Link to={`/track?order=${encodeURIComponent(order.order_id)}`}
          className="inline-flex items-center justify-center gap-1.5 h-10 rounded-full border border-line-strong text-sm font-medium hover:border-ink">
          Open in Track Order <ArrowUpRight size={14} />
        </Link>
      </div>
    </Shell>
  );
}

/* ── Visa agent payments (Part 2) ──────────────────────────────────────── */

/** The member confirms this exact order with a simulated passkey. The prompt is
 *  ShopSphere's (PasskeyContext → /api/vic/*); Talkshop only gets the resulting
 *  instruction id. Opens by itself once when it arrives live. */
export function ApprovalCard({ amount, payment, active, autoStart, onApprove }:
  { amount: number; payment: string | null; active: boolean; autoStart: boolean; onApprove: () => void }) {
  const started = useRef(false);
  useEffect(() => {
    if (active && autoStart && !started.current) { started.current = true; onApprove(); }
  }, [active, autoStart, onApprove]);
  return (
    <Shell className={cx("p-4 flex flex-col gap-2.5", active && "border-talk/50")}>
      <p className="flex items-center gap-2 text-sm font-semibold"><Fingerprint size={17} className="text-talk" /> Confirm with your passkey</p>
      <p className="text-xs text-muted leading-snug">
        Approve {money(amount)} at ShopSphere with {payment ?? "your Visa"}. This approval covers this exact order only.
        <span className="block mt-1 text-faint">Simulated passkey authentication (demo, not a real Visa passkey).</span>
      </p>
      {active && <Button size="sm" variant="talk" onClick={onApprove}><Fingerprint size={14} /> Approve with passkey</Button>}
    </Shell>
  );
}

/** Persona E: set up Visa agent payments (simulated agent token + passkey) before paying. */
/** Why Approve & Pay is disabled, in plain words, with a way forward. */
export function BlockedReasons({ co, busy, onNewCard }: { co: Checkout; busy: boolean; onNewCard: () => void }) {
  const reasons = co.issues.filter((i) => i.code !== "OUT_OF_STOCK" && i.message);
  if (!reasons.length) return null;
  const needsCard = co.issues.some((i) => i.code === "CREDIT_CARD_REQUIRED" || i.code === "NO_PAYMENT_METHOD");
  return (
    <div className="rounded-xl border border-gold/50 bg-gold-soft/40 px-3 py-2.5 text-xs flex flex-col gap-1.5">
      <p className="font-semibold">Why you can't approve yet</p>
      {reasons.map((i) => <p key={i.code} className="text-ink-soft leading-snug">{i.message}</p>)}
      {needsCard && co.provider_option && co.pay_with !== "provider" && (
        <button type="button" disabled={busy} onClick={onNewCard}
          className="self-start mt-0.5 rounded-full bg-btn text-btn-ink px-3 py-1.5 font-medium">Pay with a new card instead</button>
      )}
    </div>
  );
}

/** The transcript's record of a new-card payment: the card itself is entered on the
 *  Secure Payment Sheet (the payment provider's page over this panel), never here. */
export function ProviderHandoffCard({ amount, currency, active, sheetOpen, onOpen, onCheck }:
  { amount: number; currency: string; active: boolean; sheetOpen: boolean; onOpen: () => void; onCheck: () => void }) {
  return (
    <Shell className={cx("p-4 flex flex-col gap-2", active && "border-talk/60")}>
      <p className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck size={16} className="text-talk" /> Secure payment</span>
        <span className="text-xs text-muted tabular-nums">{money(amount)} {currency}</span>
      </p>
      <p className="text-xs text-muted leading-snug">
        {!active ? "Handled on the secure payment sheet."
          : sheetOpen ? "Enter your card on the secure payment sheet. Talkshop never sees your card number or CVC."
          : "Your card is entered on the secure payment sheet. Talkshop never sees your card number or CVC."}
      </p>
      {active && !sheetOpen && (
        <div className="flex flex-col gap-1.5">
          <Button size="sm" variant="talk" onClick={onOpen}><Lock size={14} /> Open secure payment</Button>
          <button type="button" onClick={onCheck} className="self-center text-[11px] text-muted underline hover:text-ink">
            Already paid? Check status</button>
        </div>
      )}
    </Shell>
  );
}

export function AgentPaySetupCard({ active, onSetup }: { active: boolean; onSetup: () => void }) {
  return (
    <Shell className={cx("p-4 flex flex-col gap-2.5", active && "border-gold/60")}>
      <p className="flex items-center gap-2 text-sm font-semibold"><Bot size={17} className="text-gold" /> Set up Visa agent payments</p>
      <p className="text-xs text-muted leading-snug">
        A one-time setup links your Visa to Talkshop AI for purchases you approve. No card number is shared.
        <span className="block mt-1 text-faint">Simulated agent token, not a Visa Token Service token.</span>
      </p>
      {active && <Button size="sm" onClick={onSetup}><Fingerprint size={14} /> Set up with passkey</Button>}
    </Shell>
  );
}

