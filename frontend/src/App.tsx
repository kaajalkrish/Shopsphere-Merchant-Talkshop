import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { CartProvider } from "./store/cart";
import StoreLayout from "./layouts/StoreLayout";
import { Spinner } from "./components/ui";
import Login from "./pages/Login";
import StoreHome from "./pages/store/Home";
import Category from "./pages/store/Category";
import ProductPage from "./pages/store/Product";
import CartPage from "./pages/store/Cart";
import CheckoutPage from "./pages/store/Checkout";
import { OrderDetailPage, OrdersPage } from "./pages/store/Orders";
import TrackOrderPage from "./pages/store/Track";
import { OffersPage, RewardsPage } from "./pages/store/Member";
import AccountPage from "./pages/store/Account";
import { MemberProvider } from "./member/MemberContext";
import MemberGate from "./pages/store/MemberGate";
import { PasskeyProvider } from "./member/PasskeyContext";

/** Waits until the shopper has an identity (a visitor session at least). */
function AuthReady({ children }: { children: ReactNode }) {
  const { loading } = useAuth();
  if (loading) return <div className="ss-app min-h-screen"><Spinner /></div>;
  return <>{children}</>;
}

/** Checkout, orders, offers, rewards and account need a ShopSphere account (any
 *  credit-card holder, CH-2): visitors sign in or sign up, then come back. Search,
 *  products, Talkshop, the cart and order tracking are open to everyone. The
 *  server enforces the same rules. */
function RequireCustomer({ children }: { children: ReactNode }) {
  const { isCustomer } = useAuth();
  const location = useLocation();
  if (!isCustomer) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <MemberProvider>
        <PasskeyProvider>
        <CartProvider>
          <AuthReady>
            <Routes>
              <Route path="/login" element={<Login />} />
              {/* One layout keeps Talkshop mounted across pages */}
              <Route element={<StoreLayout />}>
                <Route path="/" element={<StoreHome />} />
                <Route path="/c/:slug" element={<Category />} />
                <Route path="/search" element={<Category />} />
                <Route path="/p/:productId" element={<ProductPage />} />
                <Route path="/membership" element={<MemberGate />} />
                <Route path="/cart" element={<CartPage />} />
                <Route path="/track" element={<TrackOrderPage />} />
                <Route path="/checkout/:checkoutId" element={<RequireCustomer><CheckoutPage /></RequireCustomer>} />
                <Route path="/offers" element={<RequireCustomer><OffersPage /></RequireCustomer>} />
                <Route path="/rewards" element={<RequireCustomer><RewardsPage /></RequireCustomer>} />
                <Route path="/account" element={<RequireCustomer><AccountPage /></RequireCustomer>} />
                <Route path="/orders" element={<RequireCustomer><OrdersPage /></RequireCustomer>} />
                <Route path="/orders/:orderId" element={<RequireCustomer><OrderDetailPage /></RequireCustomer>} />
              </Route>
              <Route path="/dashboard" element={<Navigate to="/orders" replace />} />
              <Route path="/assistant" element={<Navigate to="/" replace />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </AuthReady>
        </CartProvider>
        </PasskeyProvider>
        </MemberProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
