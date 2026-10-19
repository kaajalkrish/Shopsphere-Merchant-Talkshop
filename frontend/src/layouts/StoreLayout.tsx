import { Link, Outlet } from "react-router-dom";
import StoreHeader, { Wordmark } from "../components/shopsphere/StoreHeader";
import { DEPARTMENTS } from "../api/shop";
import TalkshopPanel from "../components/talkshop/TalkshopPanel";
import { TalkshopProvider } from "../talkshop/TalkshopContext";
import { useAuth } from "../auth/AuthContext";
import { useMember } from "../member/MemberContext";
import { Spinner } from "../components/ui";

/**
 * Every ShopSphere page: header, the page itself, footer — with Talkshop AI
 * docked on the right. This is a layout route, so the panel (and its
 * conversation) stays mounted while the member moves between pages.
 *
 * Anyone can search, browse, use Talkshop and fill a cart. Checkout, orders and
 * rewards need a ShopSphere account (any credit-card holder; RequireCustomer in
 * App.tsx); offers are a Chase credit-card benefit. The server enforces the same.
 */
export default function StoreLayout() {
  const { user } = useAuth();
  const { loading } = useMember();
  if (loading) return <div className="ss-app min-h-screen"><Spinner /></div>;
  // Keyed by identity: signing in moves the conversation to the account.
  return (
    <TalkshopProvider key={user?.user_id ?? "anonymous"}>
      <div className="ss-app min-h-screen flex flex-col">
        <StoreHeader />
        <div className="flex-1 flex">
          <main className="flex-1 min-w-0">
            <Outlet />
            <StoreFooter />
          </main>
          <TalkshopPanel />
        </div>
      </div>
    </TalkshopProvider>
  );
}

function StoreFooter() {
  const { eligible } = useMember();
  return (
    <footer className="mt-24 border-t border-line bg-panel">
      <div className="mx-auto max-w-[1400px] px-4 sm:px-6 py-12 grid gap-10 sm:grid-cols-2 xl:grid-cols-4">
        <div className="flex flex-col gap-3">
          <Wordmark />
          <p className="text-sm text-muted leading-relaxed max-w-xs">
            The marketplace for credit-card holders. Ask <span className="text-talk font-medium">Talkshop AI</span>
            whenever you need a hand.
          </p>
        </div>
        {eligible && (
          <>
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-[0.14em] text-muted mb-3">Shop</h4>
              <ul className="flex flex-col gap-2 text-sm">
                {DEPARTMENTS.map((d) => <li key={d.slug}><Link to={`/c/${d.slug}`} className="hover:underline">{d.label}</Link></li>)}
              </ul>
            </div>
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-[0.14em] text-muted mb-3">Your account</h4>
              <ul className="flex flex-col gap-2 text-sm">
                <li><Link to="/offers" className="hover:underline">Chase offers</Link></li>
                <li><Link to="/rewards" className="hover:underline">Rewards</Link></li>
                <li><Link to="/account" className="hover:underline">Account &amp; agent payments</Link></li>
              </ul>
            </div>
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-[0.14em] text-muted mb-3">Orders</h4>
              <ul className="flex flex-col gap-2 text-sm">
                <li><Link to="/orders" className="hover:underline">My orders</Link></li>
                <li><Link to="/track" className="hover:underline">Track order</Link></li>
                <li><Link to="/cart" className="hover:underline">Cart</Link></li>
              </ul>
            </div>
          </>
        )}
      </div>
      {/* Extra room on phones so the floating "Ask Talkshop" button never covers content */}
      <div className="border-t border-line pt-5 pb-24 lg:pb-5 px-4 text-center text-xs text-faint leading-relaxed">
        © ShopSphere · Concept demo. Not affiliated with JPMorgan Chase or Visa. Identities, cards, rewards and payments
        are simulated.
      </div>
    </footer>
  );
}
