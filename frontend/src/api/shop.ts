/**
 * ShopSphere merchant services (Demo 1) — typed client for backend/routers/shop.py
 * and products.py. The website and the Talkshop panel both use these.
 */
import { authFetch } from "./client";

export interface ColorOption { name: string; hex: string; image_url: string | null; in_stock: boolean }
export interface Product {
  product_id: string; slug: string; merchant_id: string; name: string; brand: string;
  department: string; category: string; subcategory: string; gender: "women" | "men" | "unisex";
  description: string; tags: string[]; price: number; currency: string; rating: number; review_count: number;
  is_new: boolean; delivery_days: number; option_label: string; image_url: string | null;
  colors: ColorOption[]; sizes: string[]; in_stock: boolean;
  /** Extras computed by the server for a signed-in customer: Chase offers that apply (Chase credit-card holders only) and points at list price. */
  member?: { offers: OfferBadge[]; points: number };
}
export interface OfferBadge { offer_id: string; kind: string; title: string; short: string; pay_with_chase_card?: boolean }
/** Which payment path the server chose for this checkout (Part 2, D38). */
export interface PaymentPath {
  path: "vic" | "vic_setup_required" | "standard" | "provider_handoff" | "wallet" | "none"; label?: string;
  /** CH-5: generic route name (saved_card = the older "standard"). */
  route?: "vic" | "vic_setup_required" | "saved_card" | "provider_handoff" | "wallet" | "none";
  network?: string; page_owner?: string;
  card?: { brand: string; last4: string; issuer?: string; network?: string; display?: string }; card_account_id?: string; agent_token?: string;
  agent_token_status?: string; issuer?: string;
}
export interface Variant { sku: string; size: string | null; color: string; color_hex: string; stock: number; in_stock: boolean; image_url: string | null }
export interface ProductDetail extends Product { variants: Variant[] }
export interface Availability {
  product_id: string; option_label: string; has_sizes: boolean;
  sizes: { size: string; available: boolean }[];
  colors: { name: string; hex: string; image_url: string | null; available: boolean }[];
  selected: { sku: string | null; size: string | null; color: string; stock: number; available: boolean; image_url: string | null } | null;
}
export interface CartLine {
  line_id: string; sku: string; product_id: string; name: string; brand: string; size: string | null; color: string;
  option_label: string; quantity: number; unit_price: number; line_total: number; image_url: string | null;
  delivery_days: number; stock: number; in_stock: boolean;
}
export interface Cart { lines: CartLine[]; item_count: number; subtotal: number; currency: string }
export interface Address {
  address_id: string; label: string; full_name: string; line1: string; line2: string | null; city: string;
  state: string; postal_code: string; country: string; is_default: boolean; display: string;
}
/** Card network and issuer are separate (CH-1): a credit card isn't automatically Visa, and a
 *  Chase card isn't automatically Visa. Codes are upper case; "UNKNOWN" when not known. */
export type CardNetwork = "VISA" | "MASTERCARD" | "AMEX" | "DISCOVER" | "UNKNOWN";
export type CardIssuer = "CHASE" | "CITI" | "BOFA" | "AMEX" | "CAPITAL_ONE" | "DISCOVER" | "UNKNOWN";
export interface Card {
  payment_method_id: string; brand: string; last4: string; exp_month: number; exp_year: number;
  cardholder_name: string; is_default: boolean; display: string; saved?: boolean;
  issuer: CardIssuer; issuer_display: string | null; network: CardNetwork; network_display: string;
  status: string; source: "seeded" | "issuer_link" | "manual_legacy" | "provider_token" | null;
  eligible_for_standard_checkout: boolean;
  /** "Chase Visa •••• 1234", "Citi Mastercard •••• 5678" (CH-5) */
  label?: string;
}
/** The ShopSphere Wallet shown as a way to pay (customers only, Phase 10). */
export interface WalletOption { payment_method_id: "wallet"; type: "wallet"; display: string; balance: number; enough: boolean }
/** CH-6: a new card through the Simulated Secure Payment Provider. */
export interface ProviderOption { payment_method_id: "provider"; type: "provider"; display: string }
export interface CheckoutLine {
  line_id: string; sku: string; product_id: string; name: string; brand: string; size: string | null; color: string;
  option_label: string; quantity: number; unit_price: number; line_total: number; image_url: string | null; in_stock: boolean;
}
export interface DeliveryOption { method: "standard" | "express"; label: string; price: number; date: string }
export interface CheckoutIssue { code: string; message?: string; name?: string; available?: number }
export interface Checkout {
  checkout_id: string; status: "open" | "consented" | "awaiting_payment" | "paid" | "cancelled"; lines: CheckoutLine[]; item_count: number;
  subtotal: number; tax: number; tax_rate: number; shipping: number; total: number; currency: string;
  delivery: { method: "standard" | "express"; date: string; options: DeliveryOption[] };
  address: Address | null; payment_method: (Card | WalletOption | ProviderOption) | null; saved_addresses: Address[]; saved_payment_methods: Card[];
  pay_with: "card" | "wallet" | "provider"; wallet: WalletOption | null;
  /** CH-6: paying with a new card on the Simulated Secure Payment Provider's page */
  provider_option?: { available: boolean; label: string } | null;
  provider_session?: { payment_session_id: string; status: string; hosted_url: string } | null;
  /** Loyalty points (customers only; null for guests). 100 points = $1 off before tax. */
  points: { balance: number; used: number; discount: number; max_usable: number; value_per_point: number } | null;
  points_discount: number;
  /** Points this order earns (guests: what they'd earn with an account). */
  earn_points: number;
  /** Guest checkout (no account): the email and name once given. */
  guest: { email: string | null; name: string | null } | null;
  issues: CheckoutIssue[]; ready: boolean; checkout_hash: string;
  /** Chase card offer (one discount, server-chosen; CH-3: only when paying with an eligible Chase card), bonus points incl. 2× with a Chase card, simulated cashback, payment path */
  offer?: { offer_id: string; title: string; kind: string; discount: number } | null;
  offer_discount?: number; base_points?: number;
  bonus_points?: { offer_id: string; title: string; points: number }[];
  cashback?: { offer_id: string; title: string; amount: number }[];
  payment_path?: PaymentPath;
  /** CH-4: the version "Approve & Pay" approves; it changes whenever anything that affects the charge changes. */
  checkout_version: number;
  /** CH-4: Chase benefits on this order with the selected card, and those it would only get with a Chase card. */
  benefits: { applied: Benefit[]; removed: (Benefit & { reason: string; reason_text: string | null })[] };
  /** CH-4: the merchant's authoritative final invoice (what the consent is scoped to). */
  invoice: Invoice;
}
export interface Benefit { offer_id: string; title: string; kind: string; amount: number; points: number }
export interface Invoice {
  merchant_id: string; merchant_name: string; order_ref: string; checkout_version: number; action: "PAY";
  items: { name: string; sku: string; quantity: number; unit_price: number; line_total: number }[];
  subtotal: number; discounts: { label: string; amount: number }[]; eligible_benefits: Benefit[];
  tax: number; shipping: number; final_amount: number; currency: string;
  payment: { type: "card" | "wallet"; display: string; issuer?: string; network?: string; route?: string } | null;
}
/** CH-4 consent scope sent with "Approve & Pay": exactly what the customer saw. */
export function consentScope(co: Pick<Checkout, "checkout_version" | "total" | "currency">) {
  return { action: "PAY" as const, checkout_version: co.checkout_version, final_amount: co.total, currency: co.currency };
}
export interface ShipmentStep { key: string; label: string; done: boolean; at: string | null }
export interface Shipment {
  status: string; status_label: string; steps: ShipmentStep[]; tracking_number: string | null;
  carrier: string | null; estimated_delivery: string | null; simulated: boolean;
}
/** What Track Order shows: no full address, no card details, no internal ids. */
export interface TrackedOrder {
  order_id: string; status: string; payment_status: string | null; created_at: string | null;
  lines: { name: string; size: string | null; color: string; quantity: number; line_total: number; image_url: string | null }[];
  subtotal: number; tax: number; shipping: number; total: number; delivery_method: string | null; delivery_date: string | null;
  ship_to: { city: string | null; state: string | null }; payment: { display: string } | null; guest: boolean; shipment: Shipment;
}
export interface ConfirmationEmail {
  to: string; subject: string; body: string; sent_at: string | null; demo_outbox: boolean;
  /** outbox (no email server set) | sending | sent | failed */
  delivery?: "outbox" | "sending" | "sent" | "failed";
}
export interface GuestDetails { full_name: string; email: string; line1: string; line2?: string; city: string; state: string; postal_code: string }
export interface OrderLine { sku: string; product_id: string; name: string; size: string | null; color: string; quantity: number; unit_price: number; line_total: number; image_url: string | null }
export interface Order {
  order_id: string; internal_order_id: string; status: string; payment_status: string | null; lines: OrderLine[];
  subtotal: number; tax: number; shipping: number; total: number; currency: string;
  delivery_method: string | null; delivery_date: string | null; ship_to: Address | null;
  payment: { brand: string; last4: string | null; display: string } | null; tracking_number: string | null; created_at: string | null;
  payment_method_type?: "card" | "wallet"; guest?: boolean; guest_email?: string | null; shipment?: Shipment;
  points?: { used: number; discount: number; earned: number; bonus?: number };
  offer?: { offer_id: string; title: string; discount: number } | null; cashback?: number;
  payment_path?: "vic" | "standard" | "wallet" | null;
}
export type ConfirmResult =
  | { status: "authorized"; order: Order; transaction_id: string; confirmation_email?: string | null; trace?: TraceEntry[];
      saved_payment_method?: Card | null; duplicate?: boolean }
  /** CH-6: approved; the customer now gives the card on the provider's own page */
  | { status: "provider_handoff"; payment_session_id: string; hosted_url: string; expires_at: string; checkout: Checkout; trace?: TraceEntry[] }
  | { status: "pending"; payment_session_id: string; hosted_url: string; message: string }
  | { status: "declined" | "blocked" | "order_failed" | "cancelled"; reason: string; message: string; checkout: Checkout; trace?: TraceEntry[] };
/** One backend step of a payment, as it ran (safe: shortened refs, no card data). */
export interface TraceEntry {
  agent: string; check: string; result: "pass" | "blocked" | "info"; detail?: string | null; protocol?: string;
  layer?: "local" | "simulated";
  /** Named step of the secure-payment flow, e.g. CARD_TOKENIZED, USER_VERIFIED, PAYMENT_AUTHORIZED. */
  event?: string;
}

export class ShopError extends Error {
  code: string; status: number; detail: Record<string, unknown>;
  constructor(status: number, detail: Record<string, unknown>) {
    super(String(detail.message ?? "Something went wrong"));
    this.code = String(detail.code ?? "ERROR"); this.status = status; this.detail = detail;
  }
}

export async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(url, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "object" && body.detail ? body.detail : { message: body.detail ?? res.statusText };
    throw new ShopError(res.status, detail);
  }
  return res.json();
}

export const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

export interface ProductQuery {
  q?: string; department?: string; category?: string; subcategory?: string; gender?: string; brand?: string;
  color?: string; size?: string; min_price?: number; max_price?: number; new?: boolean;
  sort?: "relevance" | "price_asc" | "price_desc" | "rating" | "new"; limit?: number;
}

export const shop = {
  products: (query: ProductQuery = {}) => {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") params.set(k, String(v)); });
    return call<Product[]>(`/api/products?${params}`);
  },
  product: (id: string) => call<ProductDetail>(`/api/products/${id}`),
  availability: (id: string, size?: string | null, color?: string | null) => {
    const params = new URLSearchParams();
    if (size) params.set("size", size);
    if (color) params.set("color", color);
    return call<Availability>(`/api/products/${id}/availability?${params}`);
  },

  cart: () => call<Cart>("/api/cart/lines"),
  addToCart: (sku: string, quantity = 1) => call<Cart & { added_line_id: string }>("/api/cart/lines", json("POST", { sku, quantity })),
  setQuantity: (lineId: string, quantity: number) => call<Cart>(`/api/cart/lines/${lineId}`, json("PATCH", { quantity })),
  removeLine: (lineId: string) => call<Cart>(`/api/cart/lines/${lineId}`, json("DELETE")),

  addresses: () => call<Address[]>("/api/me/addresses"),
  addAddress: (a: Omit<Address, "address_id" | "is_default" | "display" | "country" | "label" | "line2"> & { line2?: string; label?: string; make_default?: boolean }) =>
    call<Address>("/api/me/addresses", json("POST", a)),
  deleteAddress: (id: string) => call<{ deleted: string }>(`/api/me/addresses/${id}`, json("DELETE")),
  cards: () => call<Card[]>("/api/me/payment-methods"),
  deleteCard: (id: string) => call<{ deleted: string }>(`/api/me/payment-methods/${id}`, json("DELETE")),

  createCheckout: (lineIds: string[], guest = false) => call<Checkout>("/api/checkouts", json("POST", { line_ids: lineIds, guest })),
  guestDetails: (id: string, details: GuestDetails) => call<Checkout>(`/api/checkouts/${id}/guest-details`, json("POST", details)),
  checkout: (id: string) => call<Checkout>(`/api/checkouts/${id}`),
  updateCheckout: (id: string, changes: { delivery_method?: string; address_id?: string; payment_method_id?: string; pay_with?: "card" | "wallet" | "provider"; points_used?: number; quantities?: Record<string, number> }) =>
    call<Checkout>(`/api/checkouts/${id}`, json("PATCH", changes)),
  loyalty: () => call<{ balance: number; lifetime_points: number }>("/api/loyalty"),
  cancelCheckout: (id: string) => call<Checkout>(`/api/checkouts/${id}/cancel`, json("POST")),
  /** Approve & Pay (CH-4): scoped to the invoice the customer saw. Visa agent-payment path: also the
   *  member's AUTHENTICATED instruction (simulated passkey). A changed order → 409 STALE_CHECKOUT, nothing charged. */
  confirm: (co: Pick<Checkout, "checkout_id" | "checkout_version" | "total" | "currency">, instructionId?: string) =>
    call<ConfirmResult>(`/api/checkouts/${co.checkout_id}/confirm`,
      json("POST", { consent: true, instruction_id: instructionId, ...consentScope(co),
        return_url: `${window.location.origin}/checkout/${co.checkout_id}` })),
  /** CH-6: back from the payment provider's page — ShopSphere checks the result with the provider itself. */
  completeProvider: (checkoutId: string, sessionId: string) =>
    call<ConfirmResult>(`/api/checkouts/${checkoutId}/payment-session/${encodeURIComponent(sessionId)}/complete`, json("POST", {})),
  /** The customer closed the secure payment page: release the provider session, back to review. Nothing charged. */
  abandonProvider: (checkoutId: string, sessionId: string) =>
    call<{ status: "cancelled" | "noop"; checkout: Checkout }>(
      `/api/checkouts/${checkoutId}/payment-session/${encodeURIComponent(sessionId)}/abandon`, json("POST", {})),

  orders: () => call<(Record<string, unknown> & { display_id?: string })[]>("/api/orders"),
  order: (id: string) => call<Record<string, unknown> & Partial<Order> & { display_id?: string; amount?: number }>(`/api/orders/${id}`),

  // Track Order: Order ID + the email used for the order (guests and customers)
  track: (order_id: string, email: string) => call<TrackedOrder>("/api/orders/track", json("POST", { order_id, email })),
  trackEmail: (order_id: string, email: string) => call<ConfirmationEmail>("/api/orders/track/email", json("POST", { order_id, email })),
  /** Demo control: move the simulated shipment one step. */
  trackAdvance: (order_id: string, email: string) => call<TrackedOrder>("/api/orders/track/advance", json("POST", { order_id, email })),
};

export const DEPARTMENTS = [
  { slug: "women", label: "Women", query: { gender: "women" } },
  { slug: "men", label: "Men", query: { gender: "men" } },
  { slug: "shoes", label: "Shoes", query: { department: "shoes" } },
  { slug: "accessories", label: "Accessories", query: { department: "accessories" } },
  { slug: "electronics", label: "Electronics", query: { department: "electronics" } },
] as const;

export function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function niceDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`);
  return d.toLocaleDateString("en-US", { weekday: "short", month: "long", day: "numeric" });
}
