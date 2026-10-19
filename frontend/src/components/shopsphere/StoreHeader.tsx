import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { BadgeCheck, ChevronDown, CreditCard, Gift, LogOut, Menu, Moon, Package, Search, ShoppingBag, Sun, Truck,
  User as UserIcon, X } from "lucide-react";
import { useAuth } from "../../auth/AuthContext";
import { useCart } from "../../store/cart";
import { useMember } from "../../member/MemberContext";
import { DEPARTMENTS, money } from "../../api/shop";
import { cx } from "../ui";

export function Wordmark({ className }: { className?: string }) {
  return (
    <Link to="/" className={cx("flex items-center gap-2.5 shrink-0", className)} aria-label="ShopSphere home">
      {/* Original ShopSphere mark: a navy sphere with a blue orbit */}
      <span className="relative w-7 h-7 rounded-full bg-[radial-gradient(circle_at_32%_30%,#4f8fe8,#1f5fbf_45%,#0a2342_80%)] shadow-[inset_0_-2px_6px_rgba(0,0,0,0.3)]">
        <span className="absolute inset-[-3px] rounded-full border border-accent/40 rotate-[-20deg] scale-x-[1.25]" />
      </span>
      <span className="text-[15px] sm:text-[17px] font-bold tracking-[0.16em] sm:tracking-[0.22em] text-ink">SHOPSPHERE</span>
    </Link>
  );
}

function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => document.documentElement.getAttribute("data-theme") === "dark");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    try { localStorage.setItem("talkshop_theme", dark ? "dark" : "light"); } catch { /* private mode */ }
  }, [dark]);
  return [dark, () => setDark((d) => !d)];
}

const PUBLIC_NAV = [
  { to: "/", label: "Marketplace", end: true },
  { to: "/track", label: "Track order" },
  { to: "/membership", label: "Chase card benefits" },
];

const MEMBER_NAV = [
  { to: "/", label: "Marketplace", end: true },
  { to: "/offers", label: "Chase Offers" },
  { to: "/rewards", label: "Rewards" },
  { to: "/orders", label: "Orders" },
  { to: "/account", label: "Account" },
];

export default function StoreHeader() {
  const { user, logout } = useAuth();
  const { info, eligible, chase, refresh } = useMember();
  const location = useLocation();
  const { count, bumpKey } = useCart();
  const navigate = useNavigate();
  const [dark, toggleTheme] = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [q, setQ] = useState("");
  const accountRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  // The docked Talkshop panel sits right under the header, whatever its height.
  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const set = () => document.documentElement.style.setProperty("--ss-header-h", el.offsetHeight + "px");
    set();
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const card = info?.member?.primary_card;
  const points = info?.member?.rewards_balance ?? 0;
  const agentPay = info?.member?.agent_pay?.find((a) => a.card_account_id === card?.account_id);
  // Issuer and network are separate (CH-1): "Citi Mastercard", "Chase Visa"; Amex is both.
  const cardLabel = card ? (card.issuer_code === card.network_code ? card.issuer : `${card.issuer} ${card.network}`) : "";

  useEffect(() => { if (accountOpen) void refresh(); }, [accountOpen, refresh]);   // fresh points on open
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!accountRef.current?.contains(e.target as Node)) setAccountOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  useEffect(() => { setMenuOpen(false); }, [location.pathname]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    navigate(`/search?q=${encodeURIComponent(q.trim())}`);
    setMenuOpen(false);
  };
  const signOut = async () => { setAccountOpen(false); setMenuOpen(false); navigate("/", { replace: true }); await logout(); };

  const navLink = ({ isActive }: { isActive: boolean }) => cx(
    "relative py-3 text-[13px] font-medium whitespace-nowrap transition-colors",
    isActive ? "text-ink after:absolute after:left-0 after:right-0 after:bottom-0 after:h-[2px] after:bg-accent" : "text-muted hover:text-ink",
  );

  return (
    <header ref={headerRef} className="sticky top-0 z-[60] bg-canvas/92 backdrop-blur border-b border-line">
      <div className="mx-auto max-w-[1400px] px-3 sm:px-6 h-16 flex items-center gap-1.5 sm:gap-4">
        <button className="lg:hidden -ml-1 p-2 text-ink" aria-label="Menu" onClick={() => setMenuOpen((o) => !o)}>
          {menuOpen ? <X size={20} /> : <Menu size={20} />}
        </button>
        <Wordmark />
        {chase && (
          <span className="hidden sm:inline-flex items-center rounded-full border border-gold/50 bg-gold-soft px-2 py-0.5 text-[10px] font-semibold tracking-[0.14em] uppercase text-gold">
            Chase benefits
          </span>
        )}

        <div className="flex-1" />

        {(
          <form onSubmit={submit} className="hidden md:flex items-center gap-2 h-10 w-56 xl:w-72 rounded-full bg-panel px-4 border border-transparent focus-within:border-line-strong">
            <Search size={15} className="text-muted shrink-0" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the marketplace"
              className="w-full bg-transparent text-sm outline-none placeholder:text-faint" aria-label="Search products" />
          </form>
        )}

        {eligible && card && (
          <Link to="/account" title="Your card and rewards"
            className="hidden lg:flex items-center gap-2.5 h-10 pl-2 pr-3 rounded-full border border-line hover:border-line-strong shrink-0">
            <span className="w-7 h-7 rounded-full bg-navy text-white grid place-items-center"><CreditCard size={14} /></span>
            <span className="leading-tight">
              <span className="block text-[12px] font-semibold whitespace-nowrap">{cardLabel} •• {card.last4}</span>
              <span className="block text-[11px] text-muted whitespace-nowrap">
                {points.toLocaleString()} pts{agentPay?.status === "ACTIVE" ? " · agent pay on" : ""}
              </span>
            </span>
          </Link>
        )}

        <button onClick={toggleTheme} className="p-2 rounded-full text-muted hover:text-ink hover:bg-panel" title={dark ? "Light mode" : "Dark mode"}>
          {dark ? <Sun size={18} /> : <Moon size={18} />}
        </button>

        {(
          <Link to="/cart" className="relative p-2 rounded-full text-ink hover:bg-panel" aria-label={`Cart, ${count} items`}>
            <ShoppingBag size={20} />
            {count > 0 && (
              <span key={bumpKey} className="ss-bump absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-accent text-white text-[10px] font-bold grid place-items-center">
                {count}
              </span>
            )}
          </Link>
        )}

        {!info?.signed_in && (
          <Link to="/login" className="inline-flex items-center gap-2 h-10 px-4 shrink-0 whitespace-nowrap rounded-full bg-btn text-btn-ink text-sm font-medium hover:opacity-90">
            <UserIcon size={16} className="hidden sm:block" /> Sign in
          </Link>
        )}
        {info?.signed_in && user && (
          <div ref={accountRef} className="relative">
            <button onClick={() => setAccountOpen((o) => !o)} className="flex items-center gap-2 pl-1 pr-2 h-10 rounded-full hover:bg-panel"
              aria-label="Account menu">
              <span className="w-8 h-8 rounded-full bg-navy text-white grid place-items-center text-sm font-semibold">
                {user.name.charAt(0).toUpperCase()}
              </span>
              <span className="sr-only xl:not-sr-only text-sm font-medium max-w-[120px] truncate">{user.name.split(" ")[0]}</span>
              <ChevronDown size={14} className="text-muted" />
            </button>
            {accountOpen && (
              <div className="absolute right-0 mt-2 w-64 rounded-2xl border border-line bg-canvas shadow-float p-1.5">
                <div className="px-3 py-2.5 border-b border-line mb-1">
                  <p className="text-sm font-semibold">{user.name}</p>
                  <p className={cx("text-xs flex items-center gap-1 mt-0.5", info.credit_card_holder ? "text-good" : "text-gold")}>
                    <BadgeCheck size={13} /> {info.member?.status}
                  </p>
                  {eligible && card && (
                    <p className="text-xs text-muted mt-1">{card.product_name} •• {card.last4} · {points.toLocaleString()} pts
                      ({money(points / 100)})</p>
                  )}
                </div>
                {eligible && [
                  { to: "/account", icon: <UserIcon size={16} />, label: "Account & agent payments" },
                  { to: "/rewards", icon: <Gift size={16} />, label: "Rewards" },
                  { to: "/orders", icon: <Package size={16} />, label: "My orders" },
                  { to: "/track", icon: <Truck size={16} />, label: "Track order" },
                ].map((l) => (
                  <Link key={l.to} to={l.to} onClick={() => setAccountOpen(false)} className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm hover:bg-panel">
                    {l.icon} {l.label}
                  </Link>
                ))}
                <button onClick={signOut} className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm hover:bg-panel text-left">
                  <LogOut size={16} /> Sign out
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Customer navigation + departments (desktop). Visitors get the marketplace, tracking and Chase benefits info. */}
      {(
        <div className="hidden lg:block border-t border-line">
          <div className="mx-auto max-w-[1400px] px-6 flex items-center gap-6">
            <nav className="flex items-center gap-6" aria-label="Member">
              {(eligible ? MEMBER_NAV : PUBLIC_NAV).map((n) => <NavLink key={n.to} to={n.to} end={n.end} className={navLink}>{n.label}</NavLink>)}
            </nav>
            <div className="flex-1" />
            <nav className="hidden xl:flex items-center gap-5" aria-label="Departments">
              {DEPARTMENTS.map((d) => <NavLink key={d.slug} to={`/c/${d.slug}`} className={navLink}>{d.label}</NavLink>)}
            </nav>
          </div>
        </div>
      )}

      {menuOpen && (
        <div className="lg:hidden border-t border-line bg-canvas px-4 pb-4 max-h-[calc(100vh-4rem)] overflow-y-auto">
          <form onSubmit={submit} className="flex items-center gap-2 h-11 mt-3 rounded-full bg-panel px-4">
            <Search size={15} className="text-muted" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the marketplace" className="w-full bg-transparent text-sm outline-none" />
          </form>
          {card && (
            <p className="mt-3 text-xs text-muted">{cardLabel} •• {card.last4} · {points.toLocaleString()} pts
              {agentPay?.status === "ACTIVE" ? " · agent pay on" : ""}</p>
          )}
          <nav className="flex flex-col mt-2">
            {(eligible ? MEMBER_NAV : PUBLIC_NAV).map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end}
                className={({ isActive }) => cx("py-3 border-b border-line text-[15px]", isActive ? "font-semibold" : "text-ink-soft")}>{n.label}</NavLink>
            ))}
            {DEPARTMENTS.map((d) => (
              <NavLink key={d.slug} to={`/c/${d.slug}`}
                className={({ isActive }) => cx("py-2.5 border-b border-line text-[14px] pl-3", isActive ? "font-semibold" : "text-muted")}>{d.label}</NavLink>
            ))}
            {eligible && <Link to="/track" className="py-3 border-b border-line text-[15px] text-ink-soft">Track order</Link>}{/* visitors: in PUBLIC_NAV */}
            {info?.signed_in
              ? <button onClick={signOut} className="py-3 text-left text-[15px] text-ink-soft">Sign out</button>
              : <Link to="/login" className="py-3 text-[15px] font-medium">Sign in or create an account</Link>}
          </nav>
        </div>
      )}
    </header>
  );
}
