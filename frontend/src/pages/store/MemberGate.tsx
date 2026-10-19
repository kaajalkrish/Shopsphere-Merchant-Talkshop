import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, BadgePercent, Bot, CreditCard, Gift, Landmark, LockKeyhole, ShieldCheck } from "lucide-react";
import { useMember } from "../../member/MemberContext";
import { member as memberApi } from "../../api/member";
import { Button, Field, Notice } from "../../components/ui";

/**
 * Chase card benefits (/membership). CH-2: this is NOT a gate. Every credit-card
 * holder can shop and check out on ShopSphere; linking a Chase credit card only
 * adds Chase-exclusive benefits (offers). Visitors are pointed to sign in / sign
 * up; signed-in customers can link a Chase card (name + email on the account and
 * the card's last four digits must match the simulated Chase directory — a
 * matching factor, not proof of ownership).
 */
export default function MemberGate() {
  const { info, chase, refresh } = useMember();
  const [last4, setLast4] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const link = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await memberApi.verifyCard(last4); await refresh(); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const signedIn = !!info?.signed_in;

  return (
    <div>
      <section className="bg-navy text-white relative overflow-hidden">
        <div className="absolute -right-40 -top-40 w-[560px] h-[560px] rounded-full bg-navy-2 opacity-60" aria-hidden />
        <div className="relative mx-auto max-w-[1200px] px-4 sm:px-6 py-16 sm:py-24 grid lg:grid-cols-[1.2fr_1fr] gap-10 items-center">
          <div className="flex flex-col gap-5">
            <p className="text-[11px] font-semibold tracking-[0.24em] text-gold uppercase">Chase card benefits</p>
            <h1 className="text-4xl sm:text-5xl font-semibold tracking-tight leading-[1.08]">
              {chase ? `Your Chase benefits are on, ${info?.member?.name?.split(" ")[0] ?? "there"}.`
                : "Shop with any credit card. Chase cardholders get more."}
            </h1>
            <p className="text-white/75 text-[17px] leading-relaxed max-w-xl">
              {chase ? "Chase-exclusive offers are applied automatically at checkout when you pay with your Chase credit card."
                : "ShopSphere is for every credit-card holder: Visa, Mastercard, American Express or Discover, from any bank. "
                  + "Chase credit-card holders also get Chase-exclusive offers."}
            </p>
            {signedIn && !chase && (
              <form onSubmit={link} className="rounded-2xl bg-white text-ink p-5 flex flex-col gap-3 max-w-md">
                <p className="text-sm font-semibold">Have a Chase credit card? Link it for Chase benefits</p>
                <p className="text-xs text-muted leading-relaxed">
                  Optional: you can already shop and check out. Your account name ({info?.member?.name}) and email must
                  match your Chase account. Enter only the last four digits of the card (checked with Chase, simulated).
                </p>
                <Field label="Last 4 digits of your Chase credit card" value={last4} required inputMode="numeric"
                  maxLength={4} autoComplete="off" onChange={(e) => setLast4(e.target.value.replace(/\D/g, "").slice(0, 4))} />
                {error && <Notice>{error}</Notice>}
                <Button type="submit" loading={busy} disabled={last4.length !== 4}>Link Chase card</Button>
              </form>
            )}
            <div className="flex flex-wrap gap-3 pt-2">
              {signedIn ? (
                <Link to={chase ? "/offers" : "/"} className="inline-flex items-center gap-2 h-12 px-6 rounded-full bg-white text-navy text-[15px] font-semibold hover:opacity-90">
                  {chase ? "See your Chase offers" : "Keep shopping"} <ArrowRight size={16} />
                </Link>
              ) : (
                <>
                  <Link to="/login" className="inline-flex items-center gap-2 h-12 px-6 rounded-full bg-white text-navy text-[15px] font-semibold hover:opacity-90">
                    Sign in <ArrowRight size={16} />
                  </Link>
                  <Link to="/login?signup=1" className="inline-flex items-center h-12 px-6 rounded-full border border-white/30 text-[15px] font-medium hover:border-white">
                    New to ShopSphere? Create an account
                  </Link>
                </>
              )}
              <Link to="/" className="inline-flex items-center h-12 px-2 text-[15px] font-medium text-white/80 hover:text-white underline">
                Browse the marketplace
              </Link>
            </div>
          </div>
          <div className="rounded-3xl bg-white/[0.06] border border-white/10 p-6 flex flex-col gap-4">
            <p className="text-sm font-semibold flex items-center gap-2"><LockKeyhole size={16} className="text-gold" /> Who gets what</p>
            <ul className="flex flex-col gap-3 text-sm">
              {[
                ["Anyone", "Search, browse, Talkshop, cart"],
                ["Credit-card holder with an account", "Checkout, orders, ShopSphere points"],
                ["Chase credit-card holder", "+ Chase-exclusive offers"],
              ].map(([who, what]) => (
                <li key={who} className="flex items-center justify-between gap-3 rounded-xl bg-white/[0.05] px-4 py-3">
                  <span className="text-white/85">{who}</span>
                  <span className="text-[#86e0ad] text-xs font-semibold text-right">{what}</span>
                </li>
              ))}
            </ul>
            <p className="text-xs text-white/50">Checked by ShopSphere's servers on every request, not just hidden in the page.</p>
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-[1200px] px-4 sm:px-6 mt-14 grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          { icon: <CreditCard size={18} />, title: "Any credit card", text: "Visa, Mastercard, Amex or Discover, from any bank" },
          { icon: <Gift size={18} />, title: "Points for everyone", text: "Earn demo points on every purchase" },
          { icon: <BadgePercent size={18} />, title: "Chase-exclusive offers", text: "Extra savings when you pay with a Chase credit card" },
          { icon: <Bot size={18} />, title: "Talkshop AI", text: "Describe it; Talkshop finds it and builds the order" },
        ].map((b) => (
          <div key={b.title} className="rounded-2xl border border-line p-5 flex flex-col gap-2">
            <span className="w-9 h-9 rounded-full bg-accent-soft text-accent grid place-items-center">{b.icon}</span>
            <p className="font-semibold">{b.title}</p>
            <p className="text-sm text-muted">{b.text}</p>
          </div>
        ))}
      </section>

      <section className="mx-auto max-w-[1200px] px-4 sm:px-6 mt-10">
        <div className="rounded-2xl bg-panel p-5 flex flex-col sm:flex-row gap-3 sm:items-center text-sm text-muted">
          <span className="flex items-center gap-2 font-medium text-ink whitespace-nowrap"><Landmark size={16} /><ShieldCheck size={16} /> Concept demo</span>
          ShopSphere is an original concept marketplace and is not affiliated with JPMorgan Chase, Citi, American Express,
          Visa or Mastercard. Identities, accounts, cards, rewards and payments are simulated with demo data.
        </div>
      </section>
    </div>
  );
}
