# Protocols in each step — from a live run

This document shows **which protocol idea is used at every step** of a ShopSphere +
Talkshop purchase, and **what it actually did**. Everything here comes from a live run of
the app (frontend `localhost:5174` → backend `localhost:8010`) on 2026-10-18, with four
shoppers: a visitor, two existing users, and a brand-new user.

The lines in the tables marked *trace* are the real guard events Talkshop streamed during
the run (agent · check · result · protocol label). You can see the same lines in the
Talkshop panel's Presenter view.

> **POC note.** Protocol names are labels for ideas this demo borrows, always qualified:
> *MCP-style*, *UCP-inspired*, *ACP-style*, *AP2-style*, *VIC-inspired*. Nothing is
> certified, compliant or connected to a real bank, card network or payment provider.
> Lines marked **simulated** are stand-ins for external services.

---

## 1. The four test cases and their results

| # | Shopper | Payment | Result |
|---|---|---|---|
| 1 | **Visitor** (not signed in) | none | Searched, chose, added to cart ✅ · checkout asked to sign in ✅ · checkout API refused (`403 LOGIN_REQUIRED`) ✅ |
| 2 | **Existing user, not Chase** — Dana Brooks, Citi Mastercard •••• 5678 | Saved card | Order **SS-70491** ✅ · no Chase offer (correct) · 150 points |
| 3 | **Existing user, Chase** — Chris Park, Chase Visa •••• 1234 | Visa agent path (VIC-inspired, simulated passkey) | Chase offer −$20.85 applied · order **SS-22478** ✅ · 266 points (2× with the Chase card) · $10.00 cashback |
| 4 | **New user** — signs up inside Talkshop | New card on the Secure Payment Sheet (test card Visa •••• 4242) | "✓ Payment verified · Visa •••• 4242 · $150.47 authorized" · order **SS-73968** ✅ · card not saved (save unticked) |

Extra checks in the same run:
- A signed-in user who tries to type a card number into ShopSphere's API gets
  `410 PAN_NOT_ACCEPTED`.
- For the new user, the card number, the CVC field and the verification challenge were
  **not found** anywhere in Talkshop's events, the trace or the saved transcript.

---

## 2. The protocol map, step by step

```
 Step:      1 Intent      2 Discovery    3 Cart       4 Identity     5 Checkout      6 Mandate        7 Verify       8 Payment              9 Order
 Protocol:  MCP-style     UCP-inspired   ACP-style    UCP-inspired   ACP-style       AP2-style        AP2-style      Payment orchestration  ACP-style
            + guardrails                                             + entitlements  consent + DPAT   12 DPAT checks + saved card / VIC-     complete
                                                                                                                     inspired / provider    + TrackIt
 Agent:     VibeCheck     SneakPeek      CartUp       CartUp         CartUp          GreenLight       PayIt          PayIt (+ simulators)   TrackIt
```

Every step below has: **what the protocol does here**, **what the live run showed**, and
**where the code is**.

---

### Step 1 — Intent: the shopper's request

> Shopper (all four cases): *"I need running shoes under $150 for everyday running."*

| Protocol / idea | What it does here |
|---|---|
| **Security: sensitive-data mask** | Card numbers, CVC, expiry and typed addresses are masked before the message is shown, stored, guard-railed or read by the AI. Emails never go to the AI |
| **Guardrails (keyword rules + NeMo)** | Blocks off-topic or unsafe input |
| **MCP-style tool choice** | The AI (Gemini via LangChain) gets the current stage and the list of actions allowed there (`LLM_TOOLS`), and must pick one. It returned `search` with a structured intent (category running shoes, max price $150). An action that isn't allowed is rejected; any model failure falls back to plain code. No MCP server is involved |

Trace from the run (identical in all four cases):

| Agent | Check | Result | Protocol |
|---|---|---|---|
| VibeCheck | Sensitive-data mask | pass | Security |
| VibeCheck | Input guardrail (keywords + NeMo) | pass | Security |
| VibeCheck | Action allowed at this stage | pass | MCP-style tool choice |

Code: `backend/talkshop/orchestrator.py` (`_handle_text`), `backend/talkshop/brain.py`
(`decide`), `backend/talkshop/tools.py` (`LLM_TOOLS`), `backend/guardrails/`.

### Step 2 — Discovery: finding products

| Protocol / idea | What it does here |
|---|---|
| **UCP-inspired product discovery** | Talkshop searches ShopSphere's own catalog (local REST/JSON-shaped service). Prices, ratings and stock come from the catalog, never the AI. The AI only writes one-line reasons from those facts and checks the results are the kind of product asked for |

Trace from the run:

| Agent | Check | Result | Protocol |
|---|---|---|---|
| SneakPeek | Catalog facts only | pass | UCP-inspired product discovery |
| SneakPeek | AI relevance check | pass | UCP-inspired |

Result: top 3 running shoes; the shopper picked **FlexRun 5**.
Code: `talkshop/tools.py` (`search_products`), `backend/shop/catalog.py`, `brain.recommend()`.

### Step 3 — Choose and add to cart

| Protocol / idea | What it does here |
|---|---|
| **ACP-style / UCP-inspired** | Size and colour are checked against live stock; the item goes into the ShopSphere cart (the same cart the website shows) |

Trace from the run: **CartUp · Stock check · pass · ACP-style / UCP-inspired** →
"✓ FlexRun 5 · Size 9 · Orange is in stock. Added to your ShopSphere cart."

Visitors can do steps 1–3 too (case 1 did).
Code: `orchestrator.py` (`_choose_size`, `_choose_color`, `_confirm_variant`), `backend/shop/cart.py`.

### Step 4 — Identity and access

| Protocol / idea | What it does here |
|---|---|
| **UCP-inspired identity linking** | At checkout a visitor is asked to sign in or create an account **inside Talkshop**. The cart and the conversation carry on |
| **Access rules** | Browse = everyone · checkout = signed-in customers · pay = credit-card holders |
| **Mock entitlement rules** | Chase status is an extra benefit (offers, 2× points), not an entry rule |

What the run showed:

| Case | What happened | Trace / API |
|---|---|---|
| 1 Visitor | "To check out, sign in or create a ShopSphere account. Your cart and this conversation come with you." | CartUp · Customer access · info · *UCP-inspired identity linking* |
| 1 Visitor | Calling the checkout API directly | `403 LOGIN_REQUIRED` |
| 4 New user | Created an account (name, email, password, **no card**); the cart line was merged | `POST /api/auth/register → 200`, merged_lines: 1 |
| 2 Dana | Credit-card holder, **no** Chase benefits | `/api/member`: `credit_card_holder: true`, `chase_benefits: false` |
| 3 Chris | Credit-card holder **with** Chase benefits | `credit_card_holder: true`, `chase_benefits: true` |
| 4 New user | Signed in but no card yet | `credit_card_holder: false`: can pay with a new card at checkout |

Code: `backend/shop/eligibility.py`, `backend/routers/auth.py`, `backend/shop/entitlements.py`.

### Step 5 — Checkout: the merchant-owned order

| Protocol / idea | What it does here |
|---|---|
| **ACP-style checkout** | ShopSphere creates and owns the checkout: lines, address, payment choice, authoritative invoice (subtotal, tax, shipping, benefits, total), a SHA-256 fingerprint (`checkout_hash`) and a version number. The AI never builds or prices it |
| **ACP-style delegated details (secure forms)** | Missing address / payment are collected by secure forms that send them straight to ShopSphere; Talkshop only receives an ID |
| **Mock entitlement rules** | Chase offers are checked against the card selected for *this* payment |

What the run showed:

| Case | Trace | Outcome |
|---|---|---|
| 2 Dana | CartUp · Customer access · pass · *UCP-inspired identity*<br>CartUp · Cardholder & benefits · info · *Mock entitlement rules*<br>CartUp · Merchant-owned checkout · pass · *ACP-style create checkout session* | Review card ready; no Chase offer |
| 3 Chris | same three lines | "Your Chase card offer saves $20.85 … You'll confirm with a simulated passkey." |
| 4 New user | same three lines + CartUp · Secure forms · info · *ACP-style delegated payment*, then CartUp · Ownership check · pass · *ACP-style* (twice) | Address added via the secure form; "Pay with a new card" chosen |

Code: `backend/shop/checkout.py`, `backend/routers/shop.py` (`/api/checkouts`).

### Step 6 — The mandate: explicit, scoped approval (AP2-style)

The mandate is the proof that the shopper approved **this exact purchase**. It's built in
layers:

| Layer | What it is |
|---|---|
| **Consent gate** | Only the shopper's own tap counts. The AI can't trigger a payment; automatic or countdown approval is refused |
| **Scoped consent** | The approval names exactly what was shown: `action PAY`, `checkout_version`, `final_amount`, `currency` |
| **Live refresh** | Stock, prices and benefits are re-read; if anything changed, nothing is charged and the new total must be approved again |
| **Consent record** | `CHECKOUT_CONSENT` audit event (mode, fingerprint, total, version, route) |
| **Spending policy** | Allowed merchant, USD, above $0, at most $2,500 |
| **DPAT** (delegated payment token) | HMAC-SHA256 signed, single use, 15 minutes, bound to PayIt + ShopSphere + this order + this amount + the checkout fingerprint |

Trace from the run (case 2 and case 4 after verification, case 3 after the passkey):

| Agent | Check | Result | Protocol |
|---|---|---|---|
| GreenLight | Consent gate | pass | AP2-style consent |
| GreenLight | Consent scope | pass | AP2-style consent |
| GreenLight | Live stock + price refresh | pass | ACP-style checkout |
| GreenLight | Benefit eligibility | pass | Mock entitlement rules |
| GreenLight | Payment readiness | pass | Payment orchestration |
| PayIt | Payment route | info | Payment orchestration |
| GreenLight | Spending policy | pass | AP2-style |
| GreenLight | DPAT issued | pass | AP2-style scoped token |

Extra layers by route:
- **Case 3 (Visa agent):** before paying, the mandate also needs a **passkey-authenticated
  payment instruction** (*VIC-inspired*, see 8b). Trace:
  GreenLight · Authenticated instruction needed · info → … → GreenLight · Payment instruction · pass.
- **Case 4 (new card):** the first approval ("Continue to Secure Payment") runs
  *Consent gate* + *Consent scope* and opens a provider session bound to the order. The
  full mandate (consent record, policy, DPAT) is completed once the card is verified.

Code: `backend/shop/payment.py` (`confirm`), `backend/routers/authorizations.py`,
`backend/payment/policy.py`, `backend/payment/signing.py`.

### Step 7 — Verification: the 12 DPAT checks (AP2-style)

PayIt verifies the token's signature, consumes it atomically (so it can never pay twice),
then runs 12 deterministic checks. All 12 passed in cases 2, 3 and 4:

| # | Check | # | Check |
|---|---|---|---|
| 1 | Token exists | 7 | Right order |
| 2 | Token is active | 8 | Right currency |
| 3 | Token not expired | 9 | Amount within authorized limit |
| 4 | Token not used before | 10 | Amount = checkout total |
| 5 | Right agent (PayIt) | 11 | Order fingerprint unchanged |
| 6 | Right merchant | 12 | Consent record exists |

Trace: **PayIt · DPAT check n/12 · … · pass · AP2-style verification** (12 lines).
Code: `backend/routers/payments.py`, `backend/payment/guardrail_engine.py`, `backend/payment/token_lookup.py`.

### Step 8 — Payment: three routes from the live run

PayIt's route is chosen by `backend/shop/payment_routing.py`. (The ShopSphere Wallet is a
fourth route, not used in this run.)

#### 8a. Saved card — case 2, Dana (Citi Mastercard)

| Protocol / idea | What it does here |
|---|---|
| **Payment gateway adapter + idempotency** | PayIt sends only the saved card's token (no card number) with an idempotency key |
| **Simulated network + issuer** | Routed by network and issuer: a Mastercard goes to the Mastercard network, never forced through Visa |

Trace:

| Agent | Check | Result | Protocol |
|---|---|---|---|
| PayIt | Gateway request sent | info | PaymentGatewayAdapter |
| Mock gateway / PSP | | pass | SIMULATED |
| Simulated Mastercard network | | pass | SIMULATED |
| Simulated Citi issuer | | pass | SIMULATED |

#### 8b. Visa agent path — case 3, Chris (Chase Visa) — VIC-inspired

| Protocol / idea | What it does here |
|---|---|
| **Agent token** | Chris's Visa already has an active (local, simulated) agent token linking it to Talkshop |
| **Payment instruction** | Created for this exact checkout: merchant SHOPSPHERE, amount $127.90, USD, 1 transaction. State `CHECKOUT_CREATED`, status `PENDING_AUTHENTICATION` |
| **Simulated passkey** | The shopper confirms the instruction directly with ShopSphere (Talkshop never sees the challenge). Status → `AUTHENTICATED`, state → `INSTRUCTION_AUTHENTICATED` |
| **12 VIC-inspired controls** | Run *after* the 12 DPAT checks |
| **Purchase credential** | Single use, 5 minutes, bound to merchant + checkout + amount |
| **Commerce signal** | Links the outcome to the instruction (local record; no Visa API) |

Trace:

| Agent | Check | Result | Protocol |
|---|---|---|---|
| VIC simulator | VIC control 1/12 · Customer session + access | pass | VIC-inspired controls |
| VIC simulator | VIC control 2/12 · Active agent-pay capable Visa card (fixture) | pass | VIC-inspired controls |
| VIC simulator | VIC control 3/12 · Active agent token | pass | VIC-inspired controls |
| VIC simulator | VIC control 4/12 · Instruction authenticated | pass | VIC-inspired controls |
| VIC simulator | VIC control 5/12 · Merchant matches | pass | VIC-inspired controls |
| VIC simulator | VIC control 6/12 · Checkout id + hash match | pass | VIC-inspired controls |
| VIC simulator | VIC control 7/12 · Amount + currency match | pass | VIC-inspired controls |
| VIC simulator | VIC control 8/12 · Instruction not expired | pass | VIC-inspired controls |
| VIC simulator | VIC control 9/12 · Transaction count + spending scope | pass | VIC-inspired controls |
| VIC simulator | VIC control 10/12 · Not cancelled or consumed | pass | VIC-inspired controls |
| VIC simulator | VIC control 11/12 · No duplicate payment attempt | pass | VIC-inspired controls |
| VIC simulator | VIC control 12/12 · Inventory + final checkout state | pass | VIC-inspired controls |
| VIC simulator | Purchase credential issued | pass | VIC-inspired credential |
| PayIt | Gateway request sent | info | PaymentGatewayAdapter |
| Mock gateway / PSP · Simulated Visa network · Simulated Chase issuer | | pass | SIMULATED |
| VIC simulator | Commerce signal recorded | pass | VIC-inspired commerce signal |

Code: `backend/vic/` (enrollment, instructions, passkey, credentials, signals), `backend/routers/vic.py`.

#### 8c. New card on the Secure Payment Sheet — case 4, new user

| Protocol / idea | What it does here |
|---|---|
| **Hosted payment page (PSP pattern)** | The card form belongs to the simulated payment provider and sits in a cross-origin iframe inside the Talkshop panel: no new window, no redirect. Talkshop and ShopSphere can't read it. Test cards only |
| **Tokenization** | The provider turns the card into an opaque token and keeps only network + last 4 |
| **Simulated payment verification** | The (simulated) issuer asks the shopper to verify this exact purchase: one-time, single-use challenge |
| **Server-to-server result** | ShopSphere asks the provider directly for the safe result and checks it's bound to this merchant, order, version, amount and currency |
| **AP2-style mandate + 12 checks** | As in steps 6–7 |
| **Provider authorization** | PayIt asks the provider to authorize the verified card, once, for exactly this amount |

What the run showed:

| Where | What happened |
|---|---|
| Talkshop | "Opening secure payment. Your card details go to the simulated payment provider, not to me…" Trace: Simulated Secure Payment Provider · Payment session created · pass · *SIMULATED*; Secure payment sheet opened · info |
| Provider page (`localhost:8010/psp`) | "Enter test card details" → **Continue securely** |
| Provider page | Test card 4242 4242 4242 4242 → "✓ Card secured · Visa •••• 4242" |
| Provider page | "Verify your payment" · One-time purchase · Verify payment / Cancel / *Simulate failed verification* |
| Provider page | **Verify payment** → "Payment verified" |
| Back in Talkshop | "✓ Payment verified · Visa •••• 4242 · $150.47 authorized. Creating your order…" |

Trace after returning to Talkshop:

| Agent | Check | Result | Protocol |
|---|---|---|---|
| Simulated Secure Payment Provider | Card tokenized | pass | SIMULATED |
| Simulated Secure Payment Provider | Issuer verification required | info | SIMULATED payment verification |
| Simulated Secure Payment Provider | Customer verified | pass | SIMULATED payment verification |
| Simulated Secure Payment Provider | Payment credential ready | pass | SIMULATED |
| GreenLight / PayIt | Steps 6 and 7 (consent, policy, DPAT, 12/12 checks) | pass | AP2-style |
| PayIt | Provider authorization | pass | SIMULATED |
| PayIt | Save payment method | info | Simulated provider token: *not saved, "save" wasn't ticked* |

Code: `backend/gateway/provider.py`, `frontend/src/components/payments/SecurePaymentSheet.tsx`,
`backend/shop/payment.py` (`complete_provider`, `_provider_route`).

### Step 9 — Order, confirmation, tracking

| Protocol / idea | What it does here |
|---|---|
| **ACP-style complete checkout** | The order is created once, only after the payment is authorized; stock is committed; points, offers and cashback are applied |
| **Order updates** | Confirmation email (Order ID, items, total, tracking link, no card data) |
| **UCP-inspired order management** | Track the order with Order ID + email, on the website or by asking Talkshop |

Trace (cases 2, 3, 4): **TrackIt · Order confirmed · pass · ACP-style complete checkout**
and **TrackIt · Confirmation email · pass · Order updates**.

| Case | Talkshop said |
|---|---|
| 2 | "Your order is confirmed! Order SS-70491 arrives Wed, October 21 … You earned 150 points." |
| 3 | "… Order SS-22478 … You earned 266 points. $10.00 cashback is on its way as a (simulated) statement credit." |
| 4 | "… Order SS-73968 … You earned 150 points." |

Code: `backend/shop/orders.py`, `backend/agents/trackit.py`, `backend/shop/notifications.py`.

---

## 3. The path each shopper took

```
Case 1  Visitor          MCP-style → UCP discovery → ACP cart → UCP identity: "sign in to check out"  (stops; 403 if forced)

Case 2  Dana (Citi MC)   MCP-style → UCP discovery → ACP cart → identity ✓ → ACP checkout
                         → AP2 consent + DPAT → 12 DPAT checks → gateway → Mastercard network → Citi issuer → ACP complete

Case 3  Chris (Chase)    MCP-style → UCP discovery → ACP cart → identity ✓ → ACP checkout + Chase offer
                         → VIC instruction + passkey → AP2 consent + DPAT → 12 DPAT checks → 12 VIC controls
                         → purchase credential → Visa network → Chase issuer → commerce signal → ACP complete

Case 4  New user         MCP-style → UCP discovery → ACP cart → UCP identity (sign up) → ACP checkout + secure forms
                         → AP2 consent scope → provider session → tokenize → payment verification
                         → AP2 consent + DPAT → 12 DPAT checks → provider authorization → ACP complete
```

---

## 4. Where each protocol shows up

| Protocol | Steps | Cases | Code |
|---|---|---|---|
| **MCP-style tool choice** | 1 | all | `talkshop/brain.py`, `talkshop/tools.py`, `orchestrator.py` |
| **UCP-inspired** (discovery, identity, orders) | 2, 4, 9 | all | `shop/catalog.py`, `routers/auth.py`, `shop/orders.py` |
| **ACP-style** (cart, checkout, delegated forms, complete) | 3, 5, 9 | all (case 1 stops at cart) | `shop/cart.py`, `shop/checkout.py`, `routers/shop.py` |
| **AP2-style** (consent, scope, DPAT, 12 checks) | 6, 7 | 2, 3, 4 | `shop/payment.py`, `routers/authorizations.py`, `routers/payments.py`, `payment/` |
| **VIC-inspired** (agent token, instruction, passkey, controls, credential, signal) | 6, 8b | 3 | `vic/`, `routers/vic.py` |
| **Hosted payment page / payment verification** | 8c | 4 | `gateway/provider.py`, `SecurePaymentSheet.tsx` |
| **Gateway adapter + simulated network/issuer** | 8a, 8b | 2, 3 | `gateway/adapter.py`, `gateway/network.py`, `gateway/issuer.py` |
| **Mock entitlement rules** (Chase offers, 2× points) | 4, 5, 6 | 2 (none), 3 (applied) | `shop/entitlements.py`, `shop/offers.py` |
| **Security + guardrails** | 1, throughout | all | `orchestrator.py`, `talkshop/parse.py`, `guardrails/` |

**Not used:** VTS (agent tokens are local values), A2A (all agents run in one app), TAP
(for external agents visiting a merchant).

---

## 5. How to run it yourself

1. Start the backend and frontend (see `README.md`), then open `http://localhost:5174`.
2. **Visitor:** don't sign in; ask Talkshop for running shoes, add a pair, choose checkout.
3. **Existing users** (password `demo1234`): `dana@shopsphere.demo` (Citi Mastercard, saved
   card), `chris@shopsphere.demo` (Chase Visa, Visa agent path with a simulated passkey).
4. **New user:** start signed out, create an account in Talkshop at checkout, add an
   address, choose **Pay with a new card**, tap **Continue to Secure Payment**, enter
   `4242 4242 4242 4242` with any future expiry and any CVC, then **Verify payment**.
5. Open Talkshop's Presenter view to watch each guard line and protocol label as it runs.

See also: [PROJECT_WALKTHROUGH.md](PROJECT_WALKTHROUGH.md) (the full flow and agents) and
[explain.md](explain.md) (protocol details and code map).
