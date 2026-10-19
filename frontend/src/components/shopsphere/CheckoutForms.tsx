/** Inline "add address" / "add card" forms — used by the checkout page and
 *  (Phase 5) Talkshop's review card for customers without saved details. */
import { useState, type FormEvent } from "react";
import { Lock, ShieldCheck } from "lucide-react";
import { shop, ShopError, type Address, type Card, type Checkout } from "../../api/shop";
import { Button, Field, Notice } from "../ui";

export function AddressForm({ onSaved, onCancel, defaultName }: { onSaved: (a: Address) => void; onCancel?: () => void; defaultName?: string }) {
  const [f, setF] = useState({ full_name: defaultName ?? "", line1: "", line2: "", city: "", state: "", postal_code: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true); setError(null);
    try { onSaved(await shop.addAddress({ ...f, make_default: true })); }
    catch (err) { setError(err instanceof ShopError ? err.message : "Couldn't save that address."); }
    finally { setSaving(false); }
  };

  return (
    <form onSubmit={submit} className="grid grid-cols-2 gap-3">
      <Field label="Full name" value={f.full_name} onChange={set("full_name")} required className="col-span-2" autoComplete="name" />
      <Field label="Address" value={f.line1} onChange={set("line1")} required className="col-span-2" autoComplete="address-line1" />
      <Field label="Apartment, suite (optional)" value={f.line2} onChange={set("line2")} className="col-span-2" autoComplete="address-line2" />
      <Field label="City" value={f.city} onChange={set("city")} required autoComplete="address-level2" />
      <div className="grid grid-cols-2 gap-3">
        <Field label="State" value={f.state} onChange={set("state")} required maxLength={2} placeholder="TX" autoComplete="address-level1" />
        <Field label="ZIP" value={f.postal_code} onChange={set("postal_code")} required placeholder="78704" autoComplete="postal-code" />
      </div>
      {error && <div className="col-span-2"><Notice>{error}</Notice></div>}
      <div className="col-span-2 flex gap-2">
        <Button type="submit" loading={saving}>Save address</Button>
        {onCancel && <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>}
      </div>
    </form>
  );
}

/** CH-6: ShopSphere never takes a card number or security code on its own pages. A new
 *  card is given on the (simulated) payment provider's own page after the customer approves
 *  the exact invoice; ShopSphere gets back only a token, the network and the last 4. */
export function NoCardEntryNotice({ onCancel, onUseProvider }: { onCancel?: () => void; onUseProvider?: () => void }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-xl bg-panel p-3.5 text-sm">
      <p className="font-medium flex items-center gap-2"><Lock size={14} /> Pay with a new card</p>
      <p className="text-xs text-muted leading-relaxed">
        ShopSphere checkout is for credit-card holders. For your security ShopSphere never takes a card number or
        security code on its own pages: after you approve the order, the Simulated Secure Payment Provider's own page
        takes the card. You can choose to save it there for next time.
      </p>
      {onUseProvider && (
        <button type="button" onClick={onUseProvider}
          className="self-start inline-flex items-center gap-2 h-9 px-4 rounded-full bg-btn text-btn-ink text-xs font-medium">
          <ShieldCheck size={14} /> Pay with a new card (secure payment provider)
        </button>
      )}
      {onCancel && <button type="button" onClick={onCancel} className="self-start text-xs font-medium underline">Back</button>}
    </div>
  );
}

/** Kept for older call sites: there is no card-number form any more (CH-6). */
export function CardForm({ onCancel, onUseProvider }: { onSaved?: (c: Card) => void; onCancel?: () => void;
  allowSave?: boolean; onUseProvider?: () => void }) {
  return <NoCardEntryNotice onCancel={onCancel} onUseProvider={onUseProvider} />;
}

export function GuestDetailsForm({ checkoutId, initial, onSaved, onCancel }:
  { checkoutId: string; initial?: { name?: string | null; email?: string | null }; onSaved: (co: Checkout) => void; onCancel?: () => void }) {
  const [f, setF] = useState({ full_name: initial?.name ?? "", email: initial?.email ?? "", line1: "", line2: "", city: "", state: "", postal_code: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true); setError(null);
    try { onSaved(await shop.guestDetails(checkoutId, f)); }
    catch (err) { setError(err instanceof ShopError ? err.message : "Couldn't save your details."); }
    finally { setSaving(false); }
  };

  return (
    <form onSubmit={submit} className="grid grid-cols-2 gap-3">
      <Field label="Full name" value={f.full_name} onChange={set("full_name")} required className="col-span-2" autoComplete="name" />
      <Field label="Email (for your confirmation)" type="email" value={f.email} onChange={set("email")} required className="col-span-2" autoComplete="email" />
      <Field label="Address" value={f.line1} onChange={set("line1")} required className="col-span-2" autoComplete="address-line1" />
      <Field label="Apartment, suite (optional)" value={f.line2} onChange={set("line2")} className="col-span-2" autoComplete="address-line2" />
      <Field label="City" value={f.city} onChange={set("city")} required autoComplete="address-level2" />
      <div className="grid grid-cols-2 gap-3">
        <Field label="State" value={f.state} onChange={set("state")} required maxLength={2} placeholder="TX" autoComplete="address-level1" />
        <Field label="ZIP" value={f.postal_code} onChange={set("postal_code")} required placeholder="78704" autoComplete="postal-code" />
      </div>
      {error && <div className="col-span-2"><Notice>{error}</Notice></div>}
      <div className="col-span-2 flex gap-2">
        <Button type="submit" loading={saving}>Save details</Button>
        {onCancel && <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>}
      </div>
    </form>
  );
}
