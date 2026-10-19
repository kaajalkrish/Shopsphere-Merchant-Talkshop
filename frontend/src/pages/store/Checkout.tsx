import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { BadgePercent, Bot, CreditCard, Fingerprint, Lock, MapPin, ShieldCheck, Trash2, Truck, UserRound, Wallet } from "lucide-react";
import { money, niceDate, shop, ShopError, type Card, type Checkout } from "../../api/shop";
import { AddressForm, CardForm, GuestDetailsForm } from "../../components/shopsphere/CheckoutForms";
import { Button, Empty, Notice, Spinner, Stepper, cx } from "../../components/ui";
import { useAuth } from "../../auth/AuthContext";
import { useCart } from "../../store/cart";
import { PasskeyCancelled, usePasskey } from "../../member/PasskeyContext";
import { useMember } from "../../member/MemberContext";

function Section({ n, title, icon, action, children }: { n: number; title: string; icon: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-3xl border border-line p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="flex items-center gap-3 font-semibold">
          <span className="w-7 h-7 rounded-full bg-panel grid place-items-center text-xs">{n}</span>
          <span className="text-muted">{icon}</span>{title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function Radio({ checked, onClick, children }: { checked: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={checked}
      className={cx("w-full flex items-start gap-3 rounded-2xl border p-4 text-left transition-colors",
        checked ? "border-ink bg-panel" : "border-line hover:border-line-strong")}>
      <span className={cx("mt-0.5 w-4 h-4 rounded-full border-2 shrink-0", checked ? "border-ink bg-[radial-gradient(circle,var(--ss-ink)_45%,transparent_50%)]" : "border-line-strong")} />
      <span className="flex-1 min-w-0">{children}</span>
    </button>
  );
}

/** Trash icon beside a saved address/card; asks "Delete / Keep" before removing it. */
function RemoveSaved({ what, onRemove }: { what: string; onRemove: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  if (!confirming) return (
    <button type="button" onClick={() => setConfirming(true)} aria-label={`Delete ${what}`} title="Delete"
      className="shrink-0 p-2.5 rounded-full text-muted hover:text-bad hover:bg-panel transition-colors">
      <Trash2 size={16} />
    </button>
  );
  return (
    <span className="shrink-0 flex items-center gap-3 text-sm">
      <button type="button" disabled={removing} className="font-medium text-bad hover:underline"
        onClick={async () => { setRemoving(true); await onRemove(); setRemoving(false); setConfirming(false); }}>
        {removing ? "Deleting…" : "Delete"}
      </button>
      <button type="button" onClick={() => setConfirming(false)} className="text-muted hover:text-ink">Keep</button>
    </span>
  );
}

export default function CheckoutPage() {
  const { checkoutId = "" } = useParams();
  const { user } = useAuth();
  const { refresh } = useCart();
  const { approveAgentPayment, enrollAgentPay } = usePasskey();
  const member = useMember();
  const navigate = useNavigate();
  const [co, setCo] = useState<Checkout | null | undefined>(undefined);
  const [editing, setEditing] = useState<"address" | "card" | null>(null);
  const [adding, setAdding] = useState<"address" | "card" | null>(null);
  const [editingGuest, setEditingGuest] = useState(false);
  const [busy, setBusy] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [params, setParams] = useSearchParams();
  const providerSession = params.get("provider_session");
  const [waiting, setWaiting] = useState<string | null>(null);     // CH-6: provider session still open
  const [handoff, setHandoff] = useState<{ sessionId: string; hostedUrl: string } | null>(null);

  useEffect(() => { shop.checkout(checkoutId).then(setCo).catch(() => setCo(null)); }, [checkoutId]);

  // CH-6: back from the Simulated Secure Payment Provider's page. ShopSphere asks the provider
  // itself (server-to-server) for the result; the browser's word is never taken for it.
  const finishProvider = async (sessionId: string) => {
    setPlacing(true); setError(null);
    try {
      const res = await shop.completeProvider(checkoutId, sessionId);
      if (res.status === "authorized") {
        await refresh();
        navigate(`/orders/${res.order.order_id}?placed=1`);
        return;
      }
      if (res.status === "pending") { setWaiting(res.hosted_url); return; }
      if (res.status === "provider_handoff") return;
      setWaiting(null);
      setCo(res.checkout);
      setError(res.status === "cancelled" ? res.message : `Payment wasn't completed: ${res.message} No order was created.`);
    } catch (e) {
      setError(e instanceof ShopError ? e.message : "Couldn't check the payment. Please try again.");
      if (e instanceof ShopError && e.detail.checkout) setCo(e.detail.checkout as Checkout);
      else shop.checkout(checkoutId).then(setCo).catch(() => {});
    } finally {
      setPlacing(false);
      void member.refresh();
    }
  };
  // The embedded provider form tells us when it's done (session + status only); we then verify.
  useEffect(() => {
    const active = handoff ?? (co?.status === "awaiting_payment" && co.provider_session
      ? { sessionId: co.provider_session.payment_session_id, hostedUrl: co.provider_session.hosted_url } : null);
    if (!active) return;
    let origin = "";
    try { origin = new URL(active.hostedUrl).origin; } catch { return; }
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== origin || e.data?.type !== "shopsphere-provider-result" || e.data?.session !== active.sessionId) return;
      setHandoff(null);
      void finishProvider(active.sessionId);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [handoff, co?.status, co?.provider_session?.payment_session_id]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!providerSession) return;
    setParams((p) => { p.delete("provider_session"); return p; }, { replace: true });
    void finishProvider(providerSession);
  }, [providerSession]);   // eslint-disable-line react-hooks/exhaustive-deps

  const change = async (changes: Parameters<typeof shop.updateCheckout>[1]) => {
    setBusy(true); setError(null);
    try { setCo(await shop.updateCheckout(checkoutId, changes)); setEditing(null); }
    catch (e) { setError(e instanceof ShopError ? e.message : "Couldn't update your order."); }
    finally { setBusy(false); }
  };

  // Delete a saved address/card. ShopSphere moves this checkout to the next
  // saved one (or none, which brings the form back) — reload to show it.
  const removeSaved = async (kind: "address" | "card", id: string) => {
    setError(null);
    try {
      await (kind === "address" ? shop.deleteAddress(id) : shop.deleteCard(id));
      const fresh = await shop.checkout(checkoutId);
      setCo(fresh);
      if (!(kind === "address" ? fresh.saved_addresses : fresh.saved_payment_methods).length) setEditing(null);
    } catch (e) { setError(e instanceof ShopError ? e.message : `Couldn't delete that ${kind}.`); }
  };

  const place = async () => {
    setPlacing(true); setError(null);
    try {
      const path = co?.payment_path?.path;
      if (path === "vic_setup_required") {
        // Persona E: set up the agent token first (simulated passkey), then review again.
        await enrollAgentPay(co!.payment_path!.card_account_id!, `Visa •••• ${co!.payment_path!.card?.last4}`);
        setCo(await shop.checkout(checkoutId));
        void member.refresh();
        return;
      }
      // Visa path: this click creates the payment instruction for this exact order; the member
      // then confirms it with the (simulated) passkey. Standard path: this click is the go-ahead.
      const instructionId = path === "vic"
        ? await approveAgentPayment(checkoutId, { amount: co!.total, card: (co!.payment_method as Card | null)?.display })
        : undefined;
      const res = await shop.confirm(co!, instructionId);
      if (res.status === "provider_handoff") {
        // CH-6: approved — the provider's secure card form opens right here (embedded)
        setHandoff({ sessionId: res.payment_session_id, hostedUrl: res.hosted_url });
        return;
      }
      if (res.status === "pending") { setWaiting(res.hosted_url); return; }
      if (res.status === "authorized") {
        await refresh();
        if (co?.guest) {
          // A guest has no account and no My Orders: confirmation + tracking by Order ID and email.
          navigate("/track", { state: { orderId: res.order.order_id, email: res.confirmation_email ?? co.guest.email, placed: true } });
        } else {
          navigate(`/orders/${res.order.order_id}?placed=1`);
        }
        return;
      }
      setCo(res.checkout);
      setError(res.status === "order_failed" ? res.message : `Payment wasn't authorized: ${res.message} No order was created.`);
    } catch (e) {
      setError(e instanceof PasskeyCancelled || e instanceof ShopError ? e.message : "Couldn't place your order. Please try again.");
      // CH-4: a changed order comes back with the fresh invoice to approve again
      if (e instanceof ShopError && e.code === "STALE_CHECKOUT" && e.detail.checkout) setCo(e.detail.checkout as Checkout);
      else if (e instanceof ShopError && e.code === "PAYMENT_IN_PROGRESS") setWaiting(String(e.detail.hosted_url ?? ""));
      else if (e instanceof ShopError) shop.checkout(checkoutId).then(setCo).catch(() => {});
    } finally {
      setPlacing(false);
      void member.refresh();
    }
  };

  // Closing the provider's page: release its session and go back to the review. Nothing is charged.
  const backToReview = async (sessionId: string) => {
    setHandoff(null);
    try { setCo((await shop.abandonProvider(checkoutId, sessionId)).checkout); }
    catch { shop.checkout(checkoutId).then(setCo).catch(() => {}); }
  };

  const back = async () => {
    try { await shop.cancelCheckout(checkoutId); } catch { /* already closed */ }
    navigate("/cart");
  };

  if (co === undefined) return <Spinner label="Preparing your order…" />;
  if (co === null) return <Empty title="Checkout not found"><Link to="/cart" className="underline">Back to cart</Link></Empty>;
  const providerFrame = handoff ?? (co.status === "awaiting_payment" && co.provider_session
    ? { sessionId: co.provider_session.payment_session_id, hostedUrl: co.provider_session.hosted_url } : null);
  if (providerFrame) return (
    <div className="mx-auto max-w-[560px] px-4 sm:px-6 py-10 flex flex-col gap-4">
      <div className="text-center flex flex-col gap-1.5">
        <ShieldCheck size={26} className="mx-auto text-talk" />
        <h1 className="text-2xl font-semibold">Pay {money(co.total)} with your card</h1>
        <p className="text-sm text-muted">This card form belongs to the payment provider: ShopSphere never sees the card
          number, only a secure token, the card type and the last 4 digits. Your order is placed as soon as the card is
          verified and authorized.</p>
      </div>
      {error && <Notice>{error}</Notice>}
      <iframe src={providerFrame.hostedUrl} title="Simulated Secure Payment Provider"
        className="w-full h-[600px] rounded-2xl border border-line bg-white" sandbox="allow-forms allow-scripts allow-same-origin allow-top-navigation-by-user-activation" />
      {placing && <p className="text-sm text-muted text-center">Confirming your order…</p>}
      <div className="flex flex-wrap justify-center gap-3 text-sm">
        <button type="button" className="underline text-muted" onClick={() => finishProvider(providerFrame.sessionId)}>
          Payment done but nothing happened? Check status</button>
        <button type="button" className="underline text-muted" onClick={() => backToReview(providerFrame.sessionId)}>
          Back to review (nothing charged)</button>
        <button type="button" className="underline text-muted" onClick={back}>Cancel order</button>
      </div>
    </div>
  );
  if (co.status !== "open") return (
    <Empty title={co.status === "paid" ? "This order has already been placed" : "This checkout was closed"}>
      <Link to={co.status === "paid" ? "/orders" : "/cart"} className="underline">{co.status === "paid" ? "View my orders" : "Back to cart"}</Link>
    </Empty>
  );

  const missing = co.issues.filter((i) => ["NO_GUEST_DETAILS", "NO_ADDRESS", "NO_PAYMENT_METHOD", "INSUFFICIENT_WALLET",
    "CREDIT_CARD_REQUIRED"].includes(i.code))
    .map((i) => i.message);
  const guest = co.guest;
  const onWallet = co.pay_with === "wallet";
  const onProvider = co.pay_with === "provider";
  const card = onWallet || onProvider ? null : (co.payment_method as Card | null);
  const canChangePayment = co.saved_payment_methods.length > 0 || !!co.wallet || !!co.provider_option;
  const expiry = (c: Card) => `Expires ${String(c.exp_month).padStart(2, "0")}/${String(c.exp_year).slice(-2)}`;
  const stock = co.issues.filter((i) => i.code === "OUT_OF_STOCK");

  return (
    <div className="mx-auto max-w-[1200px] px-4 sm:px-6 pt-10">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight">Checkout</h1>
        <button onClick={back} className="text-sm text-muted hover:text-ink">Back to cart</button>
      </div>

      <div className="mt-8 grid lg:grid-cols-[1fr_380px] gap-8 items-start">
        <div className="flex flex-col gap-5">
          {guest ? (
            <Section n={1} title="Your details" icon={<UserRound size={16} />}
              action={guest.email && !editingGuest && (
                <button onClick={() => setEditingGuest(true)} className="text-sm font-medium underline">Edit</button>)}>
              <p className="text-sm text-muted -mt-1 mb-4">
                Checking out as a guest. No account is created; your confirmation goes to this email.
              </p>
              {!guest.email || editingGuest ? (
                <GuestDetailsForm checkoutId={co.checkout_id} initial={guest}
                  onCancel={guest.email ? () => setEditingGuest(false) : undefined}
                  onSaved={(fresh) => { setCo(fresh); setEditingGuest(false); }} />
              ) : (
                <p className="text-sm"><span className="font-medium">{guest.name}</span>
                  <span className="block text-muted">{guest.email}</span>
                  <span className="block text-muted">{co.address?.display}</span></p>
              )}
            </Section>
          ) : (
          <Section n={1} title="Shipping address" icon={<MapPin size={16} />}
            action={co.saved_addresses.length > 0 && !adding && (
              <button onClick={() => setEditing(editing === "address" ? null : "address")} className="text-sm font-medium underline">
                {editing === "address" ? "Done" : "Change"}
              </button>)}>
            {adding === "address" ? (
              <AddressForm defaultName={user?.name} onCancel={co.saved_addresses.length ? () => setAdding(null) : undefined}
                onSaved={(a) => { setAdding(null); change({ address_id: a.address_id }); }} />
            ) : editing === "address" ? (
              <div className="flex flex-col gap-2">
                {co.saved_addresses.map((a) => (
                  <div key={a.address_id} className="flex items-center gap-1">
                    <Radio checked={a.address_id === co.address?.address_id} onClick={() => change({ address_id: a.address_id })}>
                      <span className="text-sm font-medium">{a.label}</span><span className="block text-sm text-muted">{a.display}</span>
                    </Radio>
                    <RemoveSaved what={`address ${a.line1}`} onRemove={() => removeSaved("address", a.address_id)} />
                  </div>
                ))}
                <button onClick={() => setAdding("address")} className="self-start text-sm font-medium mt-1 hover:underline">+ Add a new address</button>
              </div>
            ) : co.address ? (
              <p className="text-sm"><span className="font-medium">{co.address.label}</span><span className="block text-muted">{co.address.display}</span></p>
            ) : (
              <AddressForm defaultName={user?.name} onSaved={(a) => change({ address_id: a.address_id })} />
            )}
          </Section>
          )}

          <Section n={2} title="Delivery" icon={<Truck size={16} />}>
            <div className="grid sm:grid-cols-2 gap-2">
              {co.delivery.options.map((o) => (
                <Radio key={o.method} checked={co.delivery.method === o.method} onClick={() => change({ delivery_method: o.method })}>
                  <span className="flex justify-between text-sm font-medium"><span>{o.label}</span><span>{o.price ? money(o.price) : "Free"}</span></span>
                  <span className="block text-sm text-muted">Arrives {niceDate(o.date)}</span>
                </Radio>
              ))}
            </div>
          </Section>

          <Section n={3} title="Payment" icon={<CreditCard size={16} />}
            action={canChangePayment && (co.payment_method || adding === "card") && (
              <button onClick={() => { setAdding(null); setEditing(editing === "card" ? null : "card"); }} className="text-sm font-medium underline">
                {editing === "card" ? "Done" : "Change"}
              </button>)}>
            {adding === "card" ? (
              <CardForm allowSave={!guest} onCancel={() => setAdding(null)}
                onUseProvider={() => { setAdding(null); change({ pay_with: "provider" }); }} />
            ) : editing === "card" ? (
              <div className="flex flex-col gap-2">
                {co.wallet && (
                  <Radio checked={onWallet} onClick={() => change({ pay_with: "wallet" })}>
                    <span className="flex items-center gap-2 text-sm font-medium"><Wallet size={15} /> ShopSphere Wallet</span>
                    <span className={cx("block text-sm", co.wallet.enough ? "text-muted" : "text-bad")}>
                      Balance {money(co.wallet.balance)}{co.wallet.enough ? "" : " · not enough for this order"}
                    </span>
                    {!co.saved_payment_methods.length && (
                      <span className="block text-xs text-gold">Needs a credit card saved on your account first</span>)}
                  </Radio>
                )}
                {co.saved_payment_methods.map((c) => (
                  <div key={c.payment_method_id} className="flex items-center gap-1">
                    <Radio checked={!onWallet && c.payment_method_id === co.payment_method?.payment_method_id}
                      onClick={() => change({ payment_method_id: c.payment_method_id })}>
                      <span className="text-sm font-medium">{c.label ?? c.display}</span>
                      <span className="block text-sm text-muted">{expiry(c)}</span>
                    </Radio>
                    <RemoveSaved what={`card ${c.display}`} onRemove={() => removeSaved("card", c.payment_method_id)} />
                  </div>
                ))}
                {co.provider_option && (
                  <Radio checked={onProvider} onClick={() => change({ pay_with: "provider" })}>
                    <span className="flex items-center gap-2 text-sm font-medium"><ShieldCheck size={15} /> New card</span>
                    <span className="block text-sm text-muted">On the Simulated Secure Payment Provider's page, after you approve</span>
                  </Radio>
                )}
              </div>
            ) : onProvider ? (
              <div className="flex flex-col gap-3">
                <p className="text-sm"><span className="flex items-center gap-2 font-medium"><ShieldCheck size={15} /> New card</span>
                  <span className="block text-muted">Simulated Secure Payment Provider</span></p>
                <PaymentPathNote co={co} />
              </div>
            ) : onWallet && co.wallet ? (
              <p className="text-sm"><span className="flex items-center gap-2 font-medium"><Wallet size={15} /> ShopSphere Wallet</span>
                <span className="block text-muted">Balance {money(co.wallet.balance)}</span></p>
            ) : card ? (
              <div className="flex flex-col gap-3">
                <p className="text-sm"><span className="font-medium">{card.label ?? card.display}</span>
                  <span className="block text-muted">{expiry(card)}</span></p>
                <PaymentPathNote co={co} />
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <CardForm allowSave={!guest} onUseProvider={co.provider_option ? () => change({ pay_with: "provider" }) : undefined} />
                {co.wallet && (
                  <button type="button" onClick={() => change({ pay_with: "wallet" })} disabled={!co.wallet.enough}
                    className="self-start inline-flex items-center gap-2 text-sm font-medium hover:underline disabled:opacity-50 disabled:no-underline">
                    <Wallet size={15} /> Or pay with your ShopSphere Wallet ({money(co.wallet.balance)} available)
                  </button>
                )}
              </div>
            )}
          </Section>

          <Section n={4} title={`Items (${co.item_count})`} icon={null}>
            <ul className="divide-y divide-line">
              {co.lines.map((l) => (
                <li key={l.line_id} className="py-4 first:pt-0 last:pb-0 flex gap-3 sm:gap-4 items-center">
                  <div className="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl bg-photo overflow-hidden shrink-0">
                    {l.image_url && <img src={l.image_url} alt={l.name} className="h-full w-full object-cover" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium line-clamp-2 sm:line-clamp-1">{l.name}</p>
                    <p className="text-sm text-muted">{l.size ? `${l.option_label} ${l.size} · ` : ""}{l.color}</p>
                    {!l.in_stock && <p className="text-sm text-bad">Not enough stock for this quantity.</p>}
                  </div>
                  {/* Price above the quantity on phones; side by side on wider screens */}
                  <div className="flex flex-col-reverse sm:flex-row items-end sm:items-center gap-2 sm:gap-4 shrink-0">
                    <Stepper value={l.quantity} busy={busy} onChange={(n) => change({ quantities: { [l.line_id]: n } })} />
                    <p className="sm:w-20 text-right font-semibold tabular-nums">{money(l.line_total)}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Section>
        </div>

        <aside className="lg:sticky lg:top-24 rounded-3xl border border-line p-6 flex flex-col gap-4">
          <h2 className="text-lg font-semibold">Order summary</h2>
          <div className="flex flex-col gap-2 text-sm">
            <div className="flex justify-between"><span className="text-muted">Items</span><span className="tabular-nums">{money(co.subtotal)}</span></div>
            <div className="flex justify-between"><span className="text-muted">Delivery ({co.delivery.method === "express" ? "Express" : "Standard"})</span>
              <span className="tabular-nums">{co.shipping ? money(co.shipping) : "Free"}</span></div>
            {co.offer && (
              <div className="flex justify-between gap-3"><span className="text-muted flex items-center gap-1.5"><BadgePercent size={14} className="text-accent" />
                Chase card offer · {co.offer.title}</span>
                <span className="tabular-nums text-accent shrink-0">−{money(co.offer.discount)}</span></div>
            )}
            {co.points_discount > 0 && (
              <div className="flex justify-between"><span className="text-muted">Points ({co.points?.used.toLocaleString()})</span>
                <span className="tabular-nums text-good">−{money(co.points_discount)}</span></div>
            )}
            <div className="flex justify-between"><span className="text-muted">Tax ({(co.tax_rate * 100).toFixed(2)}%)</span><span className="tabular-nums">{money(co.tax)}</span></div>
          </div>
          <RemovedBenefits co={co} />
          {co.points ? (
            <div className="rounded-2xl bg-panel p-4 text-sm flex flex-col gap-1.5">
              <p className="font-medium">ShopSphere points · {co.points.balance.toLocaleString()}</p>
              {co.points.used > 0 ? (
                <p className="flex justify-between gap-2"><span className="text-muted">Using {co.points.used.toLocaleString()} (−{money(co.points.discount)})</span>
                  <button type="button" disabled={busy} onClick={() => change({ points_used: 0 })} className="font-medium underline">Remove</button></p>
              ) : co.points.max_usable > 0 ? (
                <button type="button" disabled={busy} onClick={() => change({ points_used: co.points!.max_usable })}
                  className="self-start font-medium underline">Use {co.points.max_usable.toLocaleString()} points (−{money(co.points.max_usable * co.points.value_per_point)})</button>
              ) : <p className="text-muted">100 points = $1 off. Keep shopping to earn more.</p>}
              <p className="text-muted">You'll earn {co.earn_points.toLocaleString()} points with this order
                {co.bonus_points?.length ? ` (incl. ${co.bonus_points.reduce((n, b) => n + b.points, 0).toLocaleString()} bonus)` : ""}.</p>
              {co.cashback?.map((c) => (
                <p key={c.offer_id} className="text-good">+{money(c.amount)} cashback after payment (simulated statement credit)</p>
              ))}
            </div>
          ) : guest && (
            <p className="text-sm text-muted">Create a ShopSphere account to earn {co.earn_points.toLocaleString()} points on orders like this.</p>
          )}
          <div className="flex justify-between border-t border-line pt-4 text-lg font-semibold"><span>Total</span><span className="tabular-nums">{money(co.total)}</span></div>
          <p className="text-sm text-muted -mt-2">Arrives {niceDate(co.delivery.date)}</p>
          {missing.length > 0 && (
            <Notice tone="talk">
              <span className="font-medium">Why you can't place the order yet: </span>{missing.join(" ")}
              {co.provider_option && co.pay_with !== "provider"
                && co.issues.some((i) => i.code === "CREDIT_CARD_REQUIRED" || i.code === "NO_PAYMENT_METHOD") && (
                <button type="button" className="block mt-1.5 underline font-medium" onClick={() => change({ pay_with: "provider" })}>
                  Pay with a new card instead</button>)}
            </Notice>
          )}
          {stock.length > 0 && <Notice>{stock.map((s) => `${s.name}: only ${s.available} left.`).join(" ")}</Notice>}
          {error && <Notice>{error}</Notice>}
          {waiting !== null && (
            <Notice tone="talk">
              The secure payment provider is waiting for you to finish.{" "}
              {waiting && <a href={waiting} className="underline font-medium">Open the payment page</a>}{" "}
              {co.provider_session && <button type="button" className="underline font-medium"
                onClick={() => finishProvider(co.provider_session!.payment_session_id)}>I've finished: check payment</button>}
            </Notice>
          )}
          <Button size="lg" onClick={place} loading={placing} disabled={!co.ready || busy}>
            {co.payment_path?.path === "vic" ? <><Fingerprint size={16} /> Approve and pay · {money(co.total)}</>
              : co.payment_path?.path === "vic_setup_required" ? <><Bot size={16} /> Set up Visa agent payments</>
              : <><Lock size={15} /> Approve &amp; Pay · {money(co.total)}</>}
          </Button>
          <p className="flex items-start gap-2 text-xs text-muted">
            <ShieldCheck size={14} className="shrink-0 mt-px" />
            {co.payment_path?.path === "vic"
              ? "You'll confirm this exact order with a simulated passkey. Nothing is charged until you do."
              : "You're charged only when you approve, after ShopSphere's payment security checks."}
          </p>
          <p className="text-[11px] text-faint -mt-2">Invoice {co.invoice?.order_ref} · version {co.checkout_version} · {co.currency}.
            If anything changes before payment, you'll be asked to approve the new total.</p>
        </aside>
      </div>
    </div>
  );
}


/** Which payment path the server chose for this card (Part 2), in plain words. */
function PaymentPathNote({ co }: { co: Checkout }) {
  const p = co.payment_path;
  if (!p || p.path === "none" || p.path === "wallet") return null;
  const text = p.path === "vic" ? "Visa agent payment: you approve with a passkey (simulated), then a one-time payment credential is used for this order only."
    : p.path === "vic_setup_required" ? "To pay with this Visa through agent payments, set it up once with a simulated passkey, or choose another card."
    : p.path === "provider_handoff" ? "After you approve, a Simulated Secure Payment Provider page opens to take the card. ShopSphere and Talkshop never see the card number."
    : `Saved card payment through ShopSphere's payment gateway: ${p.card?.display ?? "card"} on the simulated ${p.network ?? "card"} network, ${p.issuer ?? "simulated issuer"}.`;
  return (
    <p className={cx("rounded-xl px-3 py-2 text-xs leading-relaxed", p.path === "vic" ? "bg-good-soft text-good"
      : p.path === "vic_setup_required" ? "bg-gold-soft text-gold" : "bg-panel text-muted")}>{text}</p>
  );
}

/** CH-4: Chase benefits this order would get with an eligible Chase card but doesn't with the
 *  selected way to pay — said plainly, with the server's reason. */
export function RemovedBenefits({ co }: { co: Checkout }) {
  const removed = co.benefits?.removed ?? [];
  if (!removed.length) return null;
  return (
    <div className="rounded-2xl border border-gold/40 bg-gold-soft/40 p-3.5 text-xs flex flex-col gap-1">
      <p className="font-semibold text-ink">Chase benefits not applied</p>
      {removed.map((b) => (
        <p key={b.offer_id} className="text-ink-soft">
          {b.title}{b.amount ? ` (−${money(b.amount)})` : b.points ? ` (+${b.points.toLocaleString()} pts)` : ""}: {b.reason_text}
        </p>
      ))}
    </div>
  );
}
