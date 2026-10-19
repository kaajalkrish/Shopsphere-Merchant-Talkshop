import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, BadgePercent, Bot, Gift, ShieldCheck, Sparkles } from "lucide-react";
import { shop, type Product } from "../../api/shop";
import { member as memberApi, type Offer } from "../../api/member";
import ProductTile from "../../components/shopsphere/ProductTile";
import TalkshopPicks from "../../components/talkshop/TalkshopPicks";
import { useTalkshopShared } from "../../talkshop/TalkshopContext";
import { useMember } from "../../member/MemberContext";
import { Skeleton } from "../../components/ui";
import { MemberCard, OfferCard } from "./Member";

const DEPT_TILES = [
  { slug: "shoes", label: "Shoes", blurb: "Runners, sneakers & boots", query: { department: "shoes" } },
  { slug: "women", label: "Women", blurb: "Dresses, knits & denim", query: { gender: "women", department: "clothing" } },
  { slug: "accessories", label: "Accessories", blurb: "Bags, watches & shades", query: { department: "accessories" } },
  { slug: "electronics", label: "Electronics", blurb: "Audio, phones & laptops", query: { department: "electronics" } },
];

/** The landing page (CH-2): open to everyone; signed-in customers see their points;
 *  Chase credit-card holders also see their Chase-exclusive offers. Every price,
 *  offer and point comes from the server. */
export default function StoreHome() {
  const { info, eligible, chase } = useMember();
  const m = eligible ? info?.member : null;
  const [offers, setOffers] = useState<Offer[] | null>(null);
  const [recommended, setRecommended] = useState<Product[] | null>(null);
  const [arrivals, setArrivals] = useState<Product[] | null>(null);
  const [tiles, setTiles] = useState<Record<string, Product | undefined>>({});
  const { picks, selectedId } = useTalkshopShared();
  const pickIds = new Set(picks.map((p) => p.product_id));

  useEffect(() => {
    if (chase) memberApi.offers().then(setOffers).catch(() => setOffers([]));
    shop.products({ category: "running_shoes", sort: "rating", limit: 4 }).then(setRecommended).catch(() => setRecommended([]));
    shop.products({ new: true, sort: "rating", limit: 8 }).then(setArrivals).catch(() => setArrivals([]));
    Promise.all(DEPT_TILES.map((t) => shop.products({ ...t.query, sort: "rating", limit: 1 })))
      .then((res) => setTiles(Object.fromEntries(DEPT_TILES.map((t, i) => [t.slug, res[i][0]]))))
      .catch(() => {});
  }, [chase]);

  const firstName = m?.name.split(" ")[0] ?? "there";
  return (
    <div>
      {/* Hero */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 pt-6">
        <div className="relative overflow-hidden rounded-3xl bg-navy text-white">
          <div className="absolute -right-24 -top-24 w-[460px] h-[460px] rounded-full bg-navy-2 opacity-70" aria-hidden />
          <div className="relative grid lg:grid-cols-[1.25fr_1fr] items-center gap-10 p-7 sm:p-12 lg:p-14">
            <div className="flex flex-col gap-5 max-w-xl">
              <p className="text-[11px] font-semibold tracking-[0.24em] text-gold uppercase">
                {chase ? "Your Chase card benefits are on" : "The ShopSphere marketplace"}</p>
              <h1 className="text-[34px] sm:text-5xl font-semibold tracking-tight leading-[1.06]">
                {eligible ? `Welcome back, ${firstName}.${chase ? " Chase offers are on." : ""}`
                  : "Browse everything. Check out with any credit card."}
              </h1>
              <p className="text-white/75 text-[16px] sm:text-[17px] leading-relaxed">
                {chase ? "Chase-exclusive offers, points on every order, and Talkshop AI to find it for you. You approve every payment."
                  : eligible ? "Points on every order and Talkshop AI to find it for you. You approve every payment."
                  : "Ask Talkshop AI to find anything in the store. Check out with any credit card; Chase credit-card holders also get exclusive offers."}
              </p>
              <div className="flex flex-wrap gap-3 pt-1">
                {chase ? (
                  <Link to="/offers" className="inline-flex items-center gap-2 h-12 px-6 rounded-full bg-white text-navy text-[15px] font-semibold hover:opacity-90">
                    See your Chase offers <ArrowRight size={16} />
                  </Link>
                ) : (
                  <Link to={info?.signed_in ? "/membership" : "/login"} className="inline-flex items-center gap-2 h-12 px-6 rounded-full bg-white text-navy text-[15px] font-semibold hover:opacity-90">
                    {info?.signed_in ? "Have a Chase card? Link it" : "Sign in"} <ArrowRight size={16} />
                  </Link>
                )}
                <Link to="/c/shoes" className="inline-flex items-center h-12 px-6 rounded-full border border-white/30 text-[15px] font-medium hover:border-white">
                  Shop the marketplace
                </Link>
              </div>
            </div>
            {m ? <MemberCard info={info!} /> : (
              <div className="rounded-3xl bg-white/[0.06] border border-white/10 p-6 flex flex-col gap-3 text-sm">
                <p className="font-semibold">Open to everyone</p>
                <p className="text-white/75 flex gap-2"><Bot size={16} className="text-gold shrink-0 mt-0.5" /> Browse products and ask Talkshop AI</p>
                <p className="text-white/75 flex gap-2"><ShieldCheck size={16} className="text-gold shrink-0 mt-0.5" /> Check out with any credit card; points on every order</p>
                <p className="font-semibold pt-2">For Chase credit-card holders</p>
                <p className="text-white/75 flex gap-2"><BadgePercent size={16} className="text-gold shrink-0 mt-0.5" /> Chase-exclusive offers at checkout</p>
                {!info?.signed_in && <Link to="/login?signup=1" className="text-gold underline pt-1">New customer? Create an account</Link>}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Member value strip */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 mt-6 grid sm:grid-cols-3 gap-3">
        {[
          { icon: <BadgePercent size={18} />, title: "Chase offers", text: "For Chase credit-card holders, applied at checkout" },
          { icon: <Gift size={18} />, title: m ? `${m.rewards_balance.toLocaleString()} points` : "Rewards points",
            text: m ? "100 points = $1 off products (demo points)" : "Every customer earns 1 point per $1 (demo points)" },
          { icon: <ShieldCheck size={18} />, title: "You approve every payment", text: "Visa agent payments with passkey step-up (simulated)" },
        ].map((b) => (
          <div key={b.title} className="flex items-center gap-3 rounded-2xl border border-line px-5 py-4">
            <span className="w-9 h-9 rounded-full bg-accent-soft text-accent grid place-items-center shrink-0">{b.icon}</span>
            <div><p className="text-sm font-semibold">{b.title}</p><p className="text-xs text-muted">{b.text}</p></div>
          </div>
        ))}
      </section>

      {/* Exclusive offers */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 mt-14">
        <div className="flex items-end justify-between mb-5">
          <div>
            <p className="text-xs font-semibold tracking-[0.2em] text-gold uppercase">{chase ? "Exclusive to you" : "For Chase credit-card holders"}</p>
            <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight mt-1">Chase offers</h2>
          </div>
          {chase && <Link to="/offers" className="text-sm font-medium inline-flex items-center gap-1 hover:underline">All offers <ArrowRight size={14} /></Link>}
        </div>
        {chase ? (
          <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-4">
            {offers ? offers.map((o) => <OfferCard key={o.offer_id} offer={o} compact />)
              : Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-36" />)}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-line-strong p-6 flex flex-col sm:flex-row sm:items-center gap-4 justify-between">
            <p className="text-sm text-muted max-w-2xl">Chase credit-card holders see their offers here, such as discounts on
              running shoes and bonus points on clothing, applied automatically at checkout when they pay with their Chase card.</p>
            <Link to={info?.signed_in ? "/membership" : "/login"} className="inline-flex items-center gap-2 h-11 px-5 rounded-full bg-btn text-btn-ink text-sm font-medium whitespace-nowrap">
              {info?.signed_in ? "Link a Chase card" : "Sign in"} <ArrowRight size={15} />
            </Link>
          </div>
        )}
      </section>

      <TalkshopPicks />

      {/* Recommended */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 mt-16">
        <div className="flex items-end justify-between mb-6">
          <div>
            <p className="text-xs font-semibold tracking-[0.2em] text-muted uppercase">Recommended for you</p>
            <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight mt-1">
              {chase ? "Top-rated runners, 15% off with your Chase card" : "Top-rated runners"}</h2>
          </div>
          <span className="hidden sm:inline-flex items-center gap-1.5 text-sm text-talk"><Bot size={15} /> Or just ask Talkshop AI</span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-x-5 gap-y-10">
          {recommended ? recommended.map((p, i) => <ProductTile key={p.product_id} product={p} priority={i < 4}
              picked={pickIds.has(p.product_id)} selected={p.product_id === selectedId} />)
            : Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="aspect-square" />)}
        </div>
      </section>

      {/* Categories */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 mt-20">
        <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight mb-6">Shop by category</h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-5">
          {DEPT_TILES.map((t) => (
            <Link key={t.slug} to={`/c/${t.slug}`} className="group relative overflow-hidden rounded-2xl bg-photo aspect-[4/5]">
              {tiles[t.slug]?.image_url && (
                <img src={tiles[t.slug]!.image_url!} alt="" className="absolute inset-0 h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.04]" />
              )}
              <div className="absolute inset-x-0 bottom-0 p-4 sm:p-5 bg-gradient-to-t from-navy/80 to-transparent text-white">
                <p className="text-lg font-semibold">{t.label}</p>
                <p className="text-sm text-white/80">{t.blurb}</p>
              </div>
            </Link>
          ))}
        </div>
      </section>

      {/* Featured */}
      <section className="mx-auto max-w-[1400px] px-4 sm:px-6 mt-20">
        <div className="flex items-end justify-between mb-6">
          <div>
            <p className="text-xs font-semibold tracking-[0.2em] text-muted uppercase">Featured</p>
            <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight mt-1">New in the marketplace</h2>
          </div>
          <Link to="/c/new" className="text-sm font-medium inline-flex items-center gap-1 hover:underline">View all <ArrowRight size={14} /></Link>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-x-5 gap-y-10">
          {arrivals ? arrivals.map((p) => <ProductTile key={p.product_id} product={p}
              picked={pickIds.has(p.product_id)} selected={p.product_id === selectedId} />)
            : Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="aspect-square" />)}
        </div>
        <p className="mt-10 text-xs text-faint flex items-center gap-1.5"><Sparkles size={12} />
          Offers and points shown on products are calculated by ShopSphere's servers for your account.</p>
      </section>
    </div>
  );
}
