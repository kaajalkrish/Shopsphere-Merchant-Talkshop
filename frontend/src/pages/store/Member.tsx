import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, BadgePercent, Coins, Gift, Sparkles, Wallet } from "lucide-react";
import { member as memberApi, type MemberInfo, type Offer, type Rewards } from "../../api/member";
import { money } from "../../api/shop";
import { Skeleton, cx } from "../../components/ui";
import { useMember } from "../../member/MemberContext";

/** The member's eligible card, drawn as an original ShopSphere card (no issuer or network artwork). */
export function MemberCard({ info }: { info: MemberInfo }) {
  const m = info.member!;
  const card = m.primary_card;
  const agent = m.agent_pay?.find((a) => a.card_account_id === card?.account_id);
  return (
    <div className="w-full max-w-sm justify-self-center lg:justify-self-end">
      <div className="aspect-[1.586] rounded-2xl p-5 flex flex-col justify-between bg-[linear-gradient(135deg,#1c3f6e,#0a2342_55%,#07182e)] border border-white/15 shadow-[0_20px_40px_rgba(0,0,0,0.35)]">
        <div className="flex items-start justify-between">
          <span className="text-[11px] font-semibold tracking-[0.2em] text-white/70 uppercase">ShopSphere member</span>
          <span className="text-[10px] rounded-full border border-gold/60 text-gold px-2 py-0.5 uppercase tracking-wider">Demo</span>
        </div>
        <div className="w-10 h-7 rounded-md bg-[linear-gradient(135deg,#d9c08f,#a8844f)] opacity-90" aria-hidden />
        <div className="flex items-end justify-between">
          <div>
            <p className="font-mono text-lg tracking-[0.2em]">•••• {card?.last4}</p>
            <p className="text-xs text-white/70 mt-1">{m.name} · {card?.product_name}</p>
          </div>
          <span className="text-sm font-semibold italic text-white/90">{card?.network}</span>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-xl bg-white/[0.07] px-3 py-2">
          <p className="text-white/60">Rewards</p>
          <p className="font-semibold text-sm">{m.rewards_balance.toLocaleString()} pts</p>
        </div>
        <div className="rounded-xl bg-white/[0.07] px-3 py-2">
          <p className="text-white/60">Agent payments</p>
          <p className="font-semibold text-sm">{card?.network !== "Visa" ? "Standard card path" : agent?.status === "ACTIVE" ? "Active" : "Not set up"}</p>
        </div>
      </div>
    </div>
  );
}

const KIND = {
  discount_percent: { label: "Discount", icon: <BadgePercent size={16} />, tone: "bg-accent-soft text-accent" },
  discount_amount: { label: "Discount", icon: <BadgePercent size={16} />, tone: "bg-accent-soft text-accent" },
  bonus_points: { label: "Bonus points", icon: <Coins size={16} />, tone: "bg-gold-soft text-gold" },
  cashback: { label: "Cashback (simulated credit)", icon: <Wallet size={16} />, tone: "bg-good-soft text-good" },
} as const;

const OFFER_LINKS: Record<string, string> = { shoes: "/c/shoes", clothing: "/c/women", electronics: "/c/electronics" };

export function OfferCard({ offer, compact, onEnroll }: { offer: Offer; compact?: boolean; onEnroll?: (id: string) => void }) {
  const k = KIND[offer.kind];
  const to = offer.department ? OFFER_LINKS[offer.department] ?? "/" : "/";
  return (
    <Link to={to} className={cx("group rounded-2xl border border-line p-5 flex flex-col gap-3 hover:border-line-strong hover:shadow-card transition-shadow",
      !offer.eligible && "opacity-90")}>
      <div className="flex items-center justify-between">
        <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold", k.tone)}>{k.icon}{k.label}</span>
        <span className="text-[10px] uppercase tracking-wider text-faint">{offer.label}</span>
      </div>
      <p className="text-lg font-semibold leading-snug">{offer.title}</p>
      {!compact && <p className="text-sm text-muted leading-relaxed">{offer.description}</p>}
      {!offer.eligible && offer.reason_text && (
        <div className="flex items-center justify-between gap-3 rounded-xl bg-panel px-3 py-2 text-xs">
          <span className="text-ink-soft">{offer.reason_text}</span>
          {offer.reason === "NOT_ENROLLED" && onEnroll && (
            <button type="button" onClick={(e) => { e.preventDefault(); onEnroll(offer.offer_id); }}
              className="shrink-0 rounded-full bg-btn text-btn-ink px-3 py-1 font-medium">Enroll</button>
          )}
        </div>
      )}
      <div className="mt-auto flex items-center justify-between text-xs text-muted">
        <span>{offer.requires_payment_path === "vic" ? "Pay with your Chase Visa through agent payments"
          : offer.pay_with_chase_card ? "Pay with your Chase credit card" : "Chase credit-card holders"}
          {offer.uses_left !== null ? ` · ${offer.uses_left} use${offer.uses_left === 1 ? "" : "s"} left` : ""}</span>
        <ArrowRight size={14} className="group-hover:translate-x-0.5 transition-transform" />
      </div>
    </Link>
  );
}

export function OffersPage() {
  const { chase } = useMember();
  const [offers, setOffers] = useState<Offer[] | null>(null);
  useEffect(() => { memberApi.offers().then(setOffers).catch(() => setOffers([])); }, []);
  if (!chase) return (
    <div className="mx-auto max-w-[900px] px-4 sm:px-6 py-10">
      <p className="text-xs font-semibold tracking-[0.2em] text-gold uppercase">Chase credit-card holders</p>
      <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight mt-1">Offers are a Chase card benefit</h1>
      <p className="text-muted mt-3 max-w-2xl leading-relaxed">
        You can shop and check out on ShopSphere with any credit card, and you earn ShopSphere points on every order.
        Offers are exclusive to Chase credit-card holders. Have a Chase credit card?
      </p>
      <Link to="/membership" className="mt-6 inline-flex items-center gap-2 h-11 px-5 rounded-full bg-btn text-btn-ink text-sm font-medium">
        Link a Chase card <ArrowRight size={15} />
      </Link>
      <p className="mt-8 text-xs text-faint">Demo offers created for this concept. They are not real Chase or Visa benefits.</p>
    </div>
  );
  return (
    <div className="mx-auto max-w-[1200px] px-4 sm:px-6 py-10">
      <p className="text-xs font-semibold tracking-[0.2em] text-gold uppercase">Exclusive to Chase credit-card holders</p>
      <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight mt-1">Your Chase offers</h1>
      <p className="text-muted mt-2 max-w-2xl">
        When you pay with your Chase credit card, ShopSphere applies the offer that saves you the most, automatically,
        and you earn 2× points. Bonus points add up; cashback arrives as a simulated statement credit after an approved
        Visa agent payment. Paying with another card or the wallet means no Chase offer on that order.
      </p>
      <div className="grid sm:grid-cols-2 gap-4 mt-8">
        {offers ? offers.map((o) => <OfferCard key={o.offer_id} offer={o}
            onEnroll={(id) => memberApi.enroll(id).then(setOffers).catch(() => {})} />)
          : Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-44" />)}
      </div>
      <p className="mt-8 text-xs text-faint">Demo offers created for this concept. They are not real Chase or Visa benefits.</p>
    </div>
  );
}

const LEDGER_LABEL: Record<string, string> = {
  earned: "Points earned", bonus: "Bonus points", redeemed: "Points redeemed", welcome: "Welcome bonus",
  cashback: "Cashback credit (simulated)", discount: "Chase offer discount",
};

export function RewardsPage() {
  const [r, setR] = useState<Rewards | null>(null);
  useEffect(() => { memberApi.rewards().then(setR).catch(() => {}); }, []);
  return (
    <div className="mx-auto max-w-[1200px] px-4 sm:px-6 py-10">
      <p className="text-xs font-semibold tracking-[0.2em] text-gold uppercase">Rewards</p>
      <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight mt-1">Your rewards</h1>
      {!r ? <Skeleton className="h-40 mt-8" /> : (
        <>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-8">
            {[
              { icon: <Gift size={18} />, label: "Points balance", value: `${r.balance.toLocaleString()} pts`, sub: `worth ${money(r.value)} off products` },
              { icon: <Sparkles size={18} />, label: "Lifetime points", value: r.lifetime_points.toLocaleString(), sub: "earned and welcome bonus" },
              { icon: <BadgePercent size={18} />, label: "Saved with Chase offers", value: money(r.offer_savings_total), sub: "discounts at checkout" },
              { icon: <Wallet size={18} />, label: "Cashback credits", value: money(r.cashback_total), sub: "simulated statement credits" },
            ].map((c) => (
              <div key={c.label} className="rounded-2xl border border-line p-5">
                <span className="w-9 h-9 rounded-full bg-accent-soft text-accent grid place-items-center">{c.icon}</span>
                <p className="text-xs text-muted mt-3">{c.label}</p>
                <p className="text-2xl font-semibold tracking-tight">{c.value}</p>
                <p className="text-xs text-muted mt-0.5">{c.sub}</p>
              </div>
            ))}
          </div>
          <p className="text-sm text-muted mt-4">{r.programme}. Earn {r.earn_rate}. {r.redemption_rate}.</p>
          <h2 className="text-xl font-semibold mt-10 mb-3">Activity</h2>
          {r.ledger.length === 0 ? <p className="text-sm text-muted">No activity yet.</p> : (
            <div className="rounded-2xl border border-line divide-y divide-line">
              {r.ledger.map((e, i) => (
                <div key={i} className="flex items-center justify-between gap-4 px-4 sm:px-5 py-3 text-sm">
                  <div className="min-w-0">
                    <p className="font-medium">{LEDGER_LABEL[e.kind] ?? e.kind}</p>
                    <p className="text-xs text-muted truncate">{e.reason}{e.order_id ? ` · ${e.order_id}` : ""}</p>
                  </div>
                  <p className={cx("font-semibold tabular-nums shrink-0",
                    e.points !== null && e.points < 0 ? "text-bad" : e.kind === "discount" ? "text-accent" : "text-good")}>
                    {e.points !== null ? `${e.points > 0 ? "+" : ""}${e.points.toLocaleString()} pts` : `${e.kind === "discount" ? "−" : "+"}${money(e.amount)}`}
                  </p>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      <p className="mt-8 text-xs text-faint">ShopSphere demo points are not a real Chase rewards programme. <Link to="/offers" className="underline">See offers</Link></p>
    </div>
  );
}
