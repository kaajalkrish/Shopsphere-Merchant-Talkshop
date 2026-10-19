/**
 * Talkshop client — one conversational turn = POST /api/talkshop/turn,
 * answered as a stream of structured events (backend/talkshop/orchestrator.py).
 */
import { authFetch } from "./client";
import type { Cart, Checkout, Order, Product, ProductDetail, TrackedOrder } from "./shop";

export type Stage =
  | "GREETING" | "SEARCHING" | "RECOMMENDED" | "PRODUCT_SELECTED" | "ASK_SIZE" | "ASK_COLOR" | "VARIANT_CONFIRMED"
  | "IN_CART" | "OFFER_CHECKOUT" | "CHECKOUT_DETAILS" | "AWAITING_CONSENT" | "PAYING" | "ORDER_CONFIRMED";

export interface OptionChoice { value: string; available: boolean; hex?: string; image_url?: string | null }

export type TalkEvent =
  | { type: "user_message"; text: string; image?: boolean }
  | { type: "message"; role: "assistant"; text: string }
  | { type: "suggestions"; chips: string[]; chip_actions?: Record<string, TalkAction> }
  | { type: "recommendations"; intro: string; products: (Product & { reason?: string | null })[] }
  | { type: "product_selected"; product: ProductDetail; from_page: boolean }
  | { type: "ask_option"; option: "size" | "color"; label: string; choices: OptionChoice[] }
  | { type: "variant_confirmed"; sku: string; product_id: string; name: string; size: string | null; color: string; option_label: string; image_url: string | null; price: number }
  | { type: "cart_updated"; cart: Cart; added_line_id: string }
  | { type: "offer_checkout"; choices: { value: string; label: string }[] }
  | { type: "checkout_ready" | "checkout_updated"; checkout: Checkout }
  | { type: "payment_status"; state: "processing" | "authorizing" | "authorized" | "declined" | "failed" | "handoff" | "cancelled"; payment: string | null; reason?: string; message?: string }
  /** CH-6: continue on the Simulated Secure Payment Provider's own page (Talkshop never sees the card). */
  | { type: "provider_handoff"; checkout_id: string; payment_session_id: string; hosted_url: string; amount: number; currency: string }
  | { type: "order_confirmed"; order: Order; email?: string | null }
  | { type: "track_order_form"; order_id: string | null }
  | { type: "order_tracking"; order: TrackedOrder }
  | { type: "login_required"; reason: string }
  | { type: "checkout_details_needed"; checkout_id: string; needs: { guest?: boolean; address: boolean; payment: boolean };
      guest?: boolean; wallet?: { balance: number; enough: boolean } | null }
  | { type: "stage"; stage: Stage; step: string }
  | { type: "status"; agent: string; message: string }
  /** One guardrail decision for the live trace (info view). */
  | { type: "guardrail"; agent: string; check: string; result: "pass" | "blocked" | "info"; detail: string; protocol: string;
      layer?: "local" | "simulated" }
  /** Visa agent-payment path: the member must confirm this exact order with a simulated passkey (Part 2). */
  | { type: "approval_required"; checkout_id: string; consent_mode: "button" | "typed";
      amount: number; currency: string; merchant: string; payment: string | null }
  /** Persona E: agent payments must be set up for this Visa before paying through Talkshop. */
  | { type: "agent_pay_setup"; checkout_id: string; card_account_id: string }
  | { type: "done"; stage: Stage; step: string };

export type TalkAction =
  | { type: "greet" }
  | { type: "select" | "ask_about"; product_id: string }
  | { type: "choose_size" | "choose_color"; value: string }
  | { type: "checkout"; guest?: boolean } | { type: "keep_shopping" } | { type: "cancel_checkout" }
  | { type: "update_checkout"; delivery_method?: string; address_id?: string; payment_method_id?: string; pay_with?: "card" | "wallet" | "provider"; points_used?: number; quantities?: Record<string, number> }
  // Only from the member's Approve and pay tap (consent "typed": GO AHEAD typed on the Visa path, still needs the passkey)
  | { type: "go_ahead"; checkout_id: string; consent?: "typed"; instruction_id?: string;
      /** CH-4: the invoice the shopper approved (the server compares; nothing is charged if it changed) */
      checkout_version?: number; final_amount?: number; currency?: string;
      /** CH-6: this page's address, so the payment provider's embedded form can tell it when it's done */
      return_url?: string }
  // Part 2: the member closed the passkey prompt (nothing approved); agent payments were just set up
  | { type: "approval_cancelled" } | { type: "agent_pay_ready" }
  // Back after signing in / verifying a card: add the item chosen before
  | { type: "add_to_cart" }
  // Order ID + email typed into the secure lookup card (not chat text)
  | { type: "track_order"; order_id: string; email: string }
  // A secure form saved the detail on ShopSphere; Talkshop gets only the id.
  | { type: "details_added"; address_id?: string; payment_method_id?: string; pay_with?: "wallet" | "provider" }
  // CH-6: the shopper finished on the provider's page; ShopSphere asks the provider for the result
  | { type: "provider_complete"; checkout_id: string; payment_session_id: string }
  // The shopper closed the secure payment sheet before finishing: back to review, nothing charged
  | { type: "provider_abandon"; checkout_id: string; payment_session_id: string };

export interface TurnInput {
  text?: string;
  action?: TalkAction & { label?: string };
  image_base64?: string;
  page?: Record<string, unknown>;
}

export async function streamTurn(sessionId: string, input: TurnInput, onEvent: (e: TalkEvent) => void): Promise<void> {
  const res = await authFetch("/api/talkshop/turn", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, ...input }),
  });
  if (!res.ok || !res.body) throw new Error(`Talkshop is unavailable (${res.status})`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const data = block.split("\n").find((l) => l.startsWith("data: "));
      if (data) onEvent(JSON.parse(data.slice(6)) as TalkEvent);
    }
  }
}

export async function getSession(sessionId: string): Promise<{ stage: Stage; step: string; transcript: TalkEvent[] }> {
  const res = await authFetch(`/api/talkshop/sessions/${sessionId}`);
  if (!res.ok) throw new Error("Couldn't load the conversation");
  return res.json();
}

export async function resetSession(sessionId: string): Promise<void> {
  await authFetch(`/api/talkshop/sessions/${sessionId}`, { method: "DELETE" });
}
