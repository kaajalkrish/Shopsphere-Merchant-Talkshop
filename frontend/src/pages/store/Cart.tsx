import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Check, ShoppingBag, Trash2 } from "lucide-react";
import { money, shop, ShopError, type CartLine } from "../../api/shop";
import { Button, Empty, Notice, Spinner, Stepper, cx } from "../../components/ui";
import { useCart } from "../../store/cart";
import { useAuth } from "../../auth/AuthContext";
import { rememberCheckout } from "../../auth/afterSignIn";

function Checkbox({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={label} onClick={onChange}
      className={cx("w-5 h-5 rounded-md border-2 grid place-items-center shrink-0 transition-colors",
        checked ? "bg-ink border-ink text-canvas" : "border-line-strong hover:border-ink")}>
      {checked && <Check size={13} strokeWidth={3} />}
    </button>
  );
}

export default function CartPage() {
  const { cart, setCart, refresh } = useCart();
  const { isCustomer } = useAuth();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const known = useRef<Set<string>>(new Set());   // lines seen before — only brand-new ones auto-select

  useEffect(() => { refresh(); }, [refresh]);
  // Everything is selected by default; newly added lines join the selection.
  useEffect(() => {
    if (!cart) return;
    setSelected((prev) => {
      const ids = cart.lines.map((l) => l.line_id);
      if (!prev) return new Set(ids);
      return new Set(ids.filter((id) => prev.has(id) || !known.current.has(id)));
    });
    known.current = new Set(cart.lines.map((l) => l.line_id));
  }, [cart]);

  const lines = cart?.lines ?? [];
  const chosen = useMemo(() => lines.filter((l) => selected?.has(l.line_id)), [lines, selected]);
  const subtotal = chosen.reduce((s, l) => s + l.line_total, 0);
  const blocked = chosen.some((l) => !l.in_stock);

  const toggle = (id: string) => setSelected((s) => {
    const next = new Set(s ?? []);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const allChosen = lines.length > 0 && chosen.length === lines.length;

  const update = async (line: CartLine, qty: number) => {
    setBusy(line.line_id); setError(null);
    try { setCart(qty < 1 ? await shop.removeLine(line.line_id) : await shop.setQuantity(line.line_id, qty)); }
    catch (e) { setError(e instanceof ShopError ? e.message : "Couldn't update your cart."); }
    finally { setBusy(null); }
  };

  const checkout = async () => {
    if (!isCustomer) {                         // log in first, then straight to checkout
      rememberCheckout(chosen.map((l) => l.line_id));
      navigate("/login?next=checkout");
      return;
    }
    setCheckingOut(true); setError(null);
    try {
      const co = await shop.createCheckout(chosen.map((l) => l.line_id));
      navigate(`/checkout/${co.checkout_id}`);
    } catch (e) {
      setError(e instanceof ShopError ? e.message : "Couldn't start checkout.");
      setCheckingOut(false);
    }
  };

  if (!cart) return <Spinner label="Loading your cart…" />;
  if (!lines.length) return (
    <Empty icon={<ShoppingBag size={22} />} title="Your cart is empty">
      <p>Find something you love.</p>
      <Link to="/" className="inline-flex mt-4 h-11 px-6 items-center rounded-full bg-btn text-btn-ink text-sm font-medium">Continue shopping</Link>
    </Empty>
  );

  return (
    <div className="mx-auto max-w-[1200px] px-4 sm:px-6 pt-10">
      <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight">Your cart</h1>
      <div className="mt-8 grid lg:grid-cols-[1fr_360px] gap-10 items-start">
        <div>
          <div className="flex items-center gap-3 pb-3 border-b border-line text-sm">
            <Checkbox checked={allChosen} label="Select all"
              onChange={() => setSelected(allChosen ? new Set() : new Set(lines.map((l) => l.line_id)))} />
            <span className="text-muted">{chosen.length} of {lines.length} selected for checkout</span>
          </div>
          <ul className="divide-y divide-line">
            {lines.map((line) => (
              <li key={line.line_id} className="py-6 flex gap-4 sm:gap-6">
                <div className="pt-1"><Checkbox checked={!!selected?.has(line.line_id)} onChange={() => toggle(line.line_id)} label={`Select ${line.name}`} /></div>
                <Link to={`/p/${line.product_id}`} className="w-24 h-24 sm:w-32 sm:h-32 shrink-0 rounded-2xl bg-photo overflow-hidden">
                  {line.image_url && <img src={line.image_url} alt={line.name} className="h-full w-full object-cover" />}
                </Link>
                <div className="flex-1 min-w-0 flex flex-col gap-1">
                  <div className="flex justify-between gap-4">
                    <div className="min-w-0">
                      <p className="text-[11px] uppercase tracking-[0.12em] text-muted">{line.brand}</p>
                      <Link to={`/p/${line.product_id}`} className="font-medium hover:underline line-clamp-2">{line.name}</Link>
                      <p className="text-sm text-muted">{line.size ? `${line.option_label} ${line.size} · ` : ""}{line.color}</p>
                    </div>
                    <p className="font-semibold tabular-nums">{money(line.line_total)}</p>
                  </div>
                  {!line.in_stock && <p className="text-sm text-bad">Only {line.stock} left — reduce the quantity to check out.</p>}
                  <div className="mt-auto pt-3 flex items-center gap-4">
                    <Stepper value={line.quantity} busy={busy === line.line_id} max={Math.max(line.stock, line.quantity)}
                      onChange={(n) => update(line, n)} />
                    <button onClick={() => update(line, 0)} className="text-sm text-muted hover:text-bad inline-flex items-center gap-1.5">
                      <Trash2 size={14} /> Remove
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <aside className="lg:sticky lg:top-24 rounded-3xl border border-line p-6 flex flex-col gap-4">
          <h2 className="text-lg font-semibold">Order summary</h2>
          <div className="flex flex-col gap-2 text-sm">
            <div className="flex justify-between"><span className="text-muted">Items ({chosen.reduce((n, l) => n + l.quantity, 0)})</span><span className="tabular-nums">{money(subtotal)}</span></div>
            <div className="flex justify-between"><span className="text-muted">Delivery</span><span>Free standard</span></div>
            <div className="flex justify-between"><span className="text-muted">Tax</span><span className="text-muted">At checkout</span></div>
          </div>
          <div className="flex justify-between border-t border-line pt-4 font-semibold"><span>Subtotal</span><span className="tabular-nums">{money(subtotal)}</span></div>
          {error && <Notice>{error}</Notice>}
          <Button size="lg" onClick={checkout} loading={checkingOut} disabled={!chosen.length || blocked}>
            {chosen.length ? `Checkout selected (${chosen.length})` : "Select items to check out"}
          </Button>
          {!isCustomer && <p className="text-center text-xs text-muted -mt-1">At checkout, sign in or create a ShopSphere account. Your cart is saved.</p>}
          <Link to="/" className="text-center text-sm text-muted hover:text-ink">Continue shopping</Link>
        </aside>
      </div>
    </div>
  );
}

