# ShopSphere + Talkshop — end-to-end walkthrough

From the moment a shopper types what they want, to the order confirmation: every step,
which agent does it, which protocol idea it follows, what data is created, and where the
code lives.

> **POC note.** Protocol names are labels for ideas this demo borrows, always qualified:
> *ACP-style*, *AP2-style*, *UCP-inspired*, *VIC-inspired*, *MCP-style*. Nothing here is
> certified, compliant or connected to a real bank, card network or payment provider.
> The payment provider, network and issuer are simulations that accept test cards only.

**Contents**
1. [The big picture](#1-the-big-picture)
2. [The cast: actors and agents](#2-the-cast-actors-and-agents)
3. [The protocols, in one table](#3-the-protocols-in-one-table)
4. [The journey, step by step](#4-the-journey-step-by-step)
   - [Step 1 — Intent](#step-1--intent-the-shopper-says-what-they-want)
   - [Step 2 — Discovery](#step-2--discovery-finding-matching-products)
   - [Step 3 — Selection and cart](#step-3--selection-and-cart)
   - [Step 4 — Identity and access](#step-4--identity-and-access)
   - [Step 5 — Checkout](#step-5--checkout-the-merchant-owned-order)
   - [Step 6 — The mandate](#step-6--the-mandate-explicit-scoped-approval)
   - [Step 7 — Verification: the 12 checks](#step-7--verification-the-12-checks)
   - [Step 8 — Payment](#step-8--payment-one-of-four-routes)
   - [Step 9 — Order, confirmation, tracking](#step-9--order-confirmation-tracking)
5. [The whole flow in one diagram](#5-the-whole-flow-in-one-diagram)
6. [State machines](#6-state-machines)
7. [Safety rules that hold everywhere](#7-safety-rules-that-hold-everywhere)
8. [When something goes wrong](#8-when-something-goes-wrong)
9. [Glossary](#9-glossary)

---

## 1. The big picture

```
  Shopper ──► Talkshop (AI assistant in the store's side panel)
                 │  understands the request, guides the conversation,
                 │  but never decides prices, totals or whether to pay
                 ▼
              Six agents ──► ShopSphere services (catalog, cart, checkout, offers, orders)
                 │                    the single source of truth for every number
                 ▼
              Approval (the "mandate") ──► 12 checks ──► payment route ──► order
                                                           │
                         saved card · ShopSphere Wallet · Visa agent path · new card
                                                           │
                                     simulated gateway / network / issuer / payment provider
```

**Three ideas run through the whole system:**

1. **The AI chooses, the code decides.** The language model only interprets free text
   and writes short product reasons. Everything with money or stock comes from
   ShopSphere's services.
2. **Nothing is paid without the shopper's explicit approval of the exact invoice.**
   The approval is scoped (action, version, amount, currency), recorded, signed and
   single use.
3. **Card details never touch Talkshop or ShopSphere.** Saved cards are tokens; new
   cards are entered on the payment provider's own page.

**Where it runs:** React store on `localhost:5174`, one FastAPI backend on
`localhost:8010` (the simulated payment provider is mounted inside it at `/psp`),
SQLite database `backend/db/demo1.db`, and an LLM (Gemini via LangChain,
`backend/config/llm.py`).

---

## 2. The cast: actors and agents

### Actors

| Actor | Role |
|---|---|
| **Shopper** | Visitor (not signed in) or customer (signed in). Only a credit-card holder can pay. |
| **ShopSphere** | The merchant. Owns the catalog, cart, checkout, offers, orders and audit log. |
| **Talkshop** | The conversational front: a state machine (`backend/talkshop/orchestrator.py`) that streams events to the panel. |
| **LLM** | Reads free text and picks the next action from an allowed list; writes product reasons from catalog facts. |
| **Simulated Secure Payment Provider** | Hosted card page, tokenization, payment verification, authorization (`backend/gateway/provider.py`). |
| **Simulated gateway, network, issuer** | Authorize saved-card and Visa-agent payments (`backend/gateway/`). |
| **VIC-inspired simulator** | Agent token, payment instruction, passkey, controls, purchase credential, commerce signal (`backend/vic/`). |

### The six agents

Each agent has an ID and a role (`backend/config/agents.py`). In Talkshop every step
they take is reported as a **guard** line (agent, check, result, protocol), which you can
see in the Presenter trace.

| Agent | ID | Job | Uses the LLM? | What it reports in Talkshop | Code |
|---|---|---|---|---|---|
| **VibeCheck** | `VIBECHECK_001` | Orchestrator. Understands the request: masks sensitive data, runs input guardrails, checks the action is allowed at this stage, extracts the structured shopping intent | Yes, for intent only | *Sensitive-data mask*, *Input guardrail (keywords + NeMo)*, *Action allowed at this stage*, *Routed to order lookup*, *Address kept out of the chat* | `talkshop/orchestrator.py`, `talkshop/brain.py`, `agents/vibecheck.py`, `guardrails/` |
| **SneakPeek** | `SNEAKPEEK_001` | Product search. Searches the catalog with the intent, relaxes one filter at a time if nothing matches, ranks, checks relevance | Only to judge relevance and write reasons from facts | *Searching ShopSphere…*, *Catalog facts only*, *AI relevance check*, *Checking stock…* | `talkshop/tools.py` (`search_products`), `shop/catalog.py`, `agents/sneakpeek.py` |
| **CartUp** | `CARTUP_001` | Merchant/seller. Stock and price checks, cart, the merchant-owned checkout, access and cardholder checks, secure forms | No | *Stock check*, *Customer access*, *Cardholder & benefits*, *Merchant-owned checkout*, *Secure forms*, *Ownership check* | `shop/cart.py`, `shop/checkout.py`, `shop/eligibility.py`, `agents/cartup.py` |
| **GreenLight** | `GREENLIGHT_001` | Payment authorization. Consent gate, consent scope, spending policy, issues the signed one-time token (DPAT) | No | *Consent gate*, *Consent scope*, *Benefit eligibility*, *Payment readiness*, *Spending policy*, *DPAT issued* | `agents/greenlight.py`, `routers/authorizations.py`, `payment/policy.py`, `payment/signing.py` |
| **PayIt** | `PAYIT_001` | Payment execution. Runs the 12 DPAT checks, picks the payment route, asks for the authorization | No | *DPAT check 1/12 … 12/12*, *Payment route*, *Gateway request sent*, *Provider authorization* | `agents/payit.py`, `payment/guardrail_engine.py`, `routers/payments.py`, `shop/payment.py` |
| **TrackIt** | `TRACKIT_001` | Order and audit. Confirms the order, confirmation email, tracking, audit trail | No | *Order confirmed*, *Confirmation email*, *Order ID + email check*, *Logged-in owner* | `agents/trackit.py`, `shop/orders.py`, `shop/notifications.py` |

---

## 3. The protocols, in one table

| Protocol idea | What it means here | Steps |
|---|---|---|
| **MCP-style tool choice** | The LLM picks from a declared tool list (`LLM_TOOLS`); code allows only the tools valid at the current stage. Talkshop does not run an MCP client/server | 1, 2, 3 |
| **UCP-inspired** | Common commerce capabilities over local REST/JSON: product discovery, identity linking (sign up / sign in mid-conversation), order lookup | 2, 4, 9 |
| **ACP-style checkout** | ShopSphere owns the checkout object (create → update → complete); the AI never builds or prices it | 3, 5, 9 |
| **AP2-style mandate** | Scoped consent, a consent record, an HMAC-signed single-use token (DPAT) bound to agent + merchant + order + amount + checkout fingerprint, and 12 checks | 6, 7 |
| **VIC-inspired agent payment** | Visa agent token, authenticated payment instruction (passkey), 12 controls, single-use purchase credential, commerce signal | 8 (Visa agent route) |
| **Hosted payment page (PSP pattern)** | The provider owns the card form in a cross-origin iframe; tokenization, payment verification, authorization on PayIt's request | 8 (new card route) |
| **SSE** | Each Talkshop turn streams typed events to the browser | every step |
| **Idempotency** | Single-use tokens, idempotency keys, authorization keyed on the payment reference | 7, 8 |

Not used: **VTS** (our agent tokens are local values), **A2A** (all agents run in one
app), **TAP** (for external agents visiting a merchant, not an embedded assistant).

---

## 4. The journey, step by step

Talkshop shows five progress steps: **Find → Choose → Review → Approve → Done.**

### Step 1 — Intent: the shopper says what they want

> Shopper: *"I need running shoes under $150 for everyday running."*

Every Talkshop turn is a `POST /api/talkshop/turn` with `{session_id, text | action, page}`;
the answer is a stream of events (SSE) ending in `done`.

| # | What happens | Agent | Protocol idea |
|---|---|---|---|
| 1.1 | **Sensitive-data mask.** Card numbers, CVC and expiry are masked *before* the text is shown, stored, guard-railed or read by the AI. Typed addresses are withheld at checkout. Emails never go to the LLM | VibeCheck | Security |
| 1.2 | **Order questions** ("where is SS-12345?") are routed to code, never to the AI | VibeCheck | UCP-inspired order management |
| 1.3 | **Input guardrail** (keyword rules + NeMo Guardrails) blocks off-topic or unsafe input | VibeCheck | Guardrails |
| 1.4 | **Stage check.** Each stage has an allowed action list (`state.py`), e.g. no `checkout` before something is in the cart | VibeCheck | MCP-style tool choice |
| 1.5 | **Intent extraction.** `brain.decide()` sends the LLM the stage, the allowed actions, safe context and recent history. It returns one action plus arguments, e.g. `search {query, category: running_shoes, max_price: 150}`. If the model's action isn't allowed it is rejected; on any model failure a deterministic fallback runs | VibeCheck | MCP-style tool choice |

**Output:** a structured intent (category, brand, size, colour, max price, use case…),
the same shape as `ShoppingIntent` (`backend/models/intent.py`). Nothing about money is
decided here.

### Step 2 — Discovery: finding matching products

| # | What happens | Agent | Protocol idea |
|---|---|---|---|
| 2.1 | `search_products` queries the ShopSphere catalog with the intent. Unknown brand → "we don't carry X, here are similar ones". No match → relax colour, then price, and say which was relaxed | SneakPeek | UCP-inspired discovery |
| 2.2 | The top 3 are shown as product cards. Prices, ratings and stock come from the catalog (**Catalog facts only**) | SneakPeek | — |
| 2.3 | `brain.recommend()` writes one-line reasons **only from the returned facts**, reads the requested size/colour (typos too), and checks the products are the kind of thing asked for (**AI relevance check**) | SneakPeek | — |

Everyone can do this, including visitors. Photo search works the same way after the
image is described.

### Step 3 — Selection and cart

| # | What happens | Agent | Protocol idea |
|---|---|---|---|
| 3.1 | Shopper picks a product, then size and colour (tapped or typed). The variant is checked against live stock | CartUp | ACP-style |
| 3.2 | "Add to cart" uses the ShopSphere cart service: the same cart the website shows | CartUp | ACP-style |
| 3.3 | Talkshop offers checkout or keep shopping | — | — |

### Step 4 — Identity and access

When the shopper chooses checkout, three access levels apply (`shop/eligibility.py`):

| Level | Who | Allowed |
|---|---|---|
| Browse | Everyone | Search, browse, cart, Talkshop |
| Checkout | Signed-in customers | Create a checkout, give an address |
| Pay | Credit-card holders | Pay (the Wallet needs a saved credit card) |

- A visitor gets a **sign-in / create-account card inside Talkshop** (name, email,
  password; never a card). The cart and the conversation carry on afterwards
  (*UCP-inspired identity linking*). Sign-up is throttled and audited (`CUSTOMER_SIGNUP`).
- **Chase is an entitlement, not the gate.** Any issuer's cardholder can buy. Chase
  cardholders also get Chase offers and 2× points, but only when they pay with that
  Chase card (`shop/entitlements.py`, `shop/offers.py`).

### Step 5 — Checkout: the merchant-owned order

`POST /api/checkouts` (CartUp, *ACP-style*) creates the checkout from the cart lines
(`shop/checkout.py`).

- **Missing details** (address, payment) are collected on **secure forms** that send them
  straight to ShopSphere; Talkshop receives only an ID.
- **The invoice is authoritative:** subtotal, 8.25% tax, shipping (free over $50,
  otherwise $5.99), points, benefits applied and removed (with reasons), total.
- **`checkout_hash`** is a SHA-256 fingerprint of everything that affects the price.
  **`checkout_version`** increases whenever the hash changes.
- **Benefits are revalidated against the selected card.** Switching from a Chase card to a
  Citi card removes the Chase offer, says why, and changes the total.
- Talkshop shows the **review card** built from this object. Its button reads
  **Approve & Pay $X** (saved card, Wallet, Visa agent) or
  **Continue to Secure Payment · $X** (new card). If it can't be approved yet, the card
  explains **"Why you can't approve yet"**.

### Step 6 — The mandate: explicit, scoped approval

In AP2 terms, a *mandate* is the verifiable proof that the user authorized this agent to
make this purchase. This demo builds an AP2-style equivalent in three layers:

| AP2 concept | What this demo has | Where |
|---|---|---|
| **Intent mandate** (what the user wants) | The structured shopping intent from Step 1. It is not signed and pre-authorizes nothing: the shopper is present and approves every purchase themselves | `brain.decide()` |
| **Cart mandate** (approval of this exact cart and price) | The merchant-owned checkout + the **scoped consent** + the **consent record** | `shop/payment.py` `confirm()`, `CHECKOUT_CONSENT` audit |
| **Payment mandate** (authority to pay this exact amount) | The **DPAT**, an HMAC-signed single-use token bound to PayIt, ShopSphere, this order, this amount and this checkout fingerprint. On the Visa agent route, also the passkey-authenticated payment instruction; on the new-card route, also the provider's one-time payment verification | `routers/authorizations.py`, `payment/signing.py`, `vic/instructions.py`, `gateway/provider.py` |

**How the mandate is created** (`payment.confirm()`):

1. **Only an explicit tap counts.** The `go_ahead` action comes from the shopper's
   button. `confirm_and_pay` is not in the LLM's tool list, and automatic or countdown
   consent is refused (*Consent gate*, GreenLight).
2. **Scoped consent.** The approval names exactly what was shown:
   ```json
   { "consent": true, "action": "PAY", "checkout_version": 3, "final_amount": 150.47, "currency": "USD" }
   ```
3. **Access and payment eligibility** are checked again (second line of defence).
4. **Scope check, before and after a live refresh.** Stock, prices and benefits are
   re-read. If version, total or currency differ from what was approved →
   **409 STALE_CHECKOUT**, nothing charged, and the new invoice is shown to approve again
   (*Consent scope*, GreenLight).
5. **Consent record.** `CHECKOUT_CONSENT` is written with mode, checkout hash, total,
   action, merchant, currency, version and payment route.
6. **Spending policy** (`payment/policy.py`): allowed merchant, USD only, amount above
   zero, at most $2,500. Anything else is refused with a reason and no token is issued
   (the policy marks over-limit purchases as "step-up required", but this demo has no
   step-up flow yet, so they are refused).
7. **DPAT issued.** A `PaymentAuthorization` row is stored with an **HMAC-SHA256
   signature** over `{token_id, agent_id, merchant_id, order_id, currency, max_amount,
   checkout_hash}`, plus a `DelegatedToken` (single use, 15 minutes). Audit:
   `USER_APPROVED_PURCHASE`, `DPAT_CREATED`. PayIt receives only the token ID.

### Step 7 — Verification: the 12 checks

Before any money moves, PayIt verifies the mandate (`routers/payments.py`,
`payment/guardrail_engine.py`):

- The stored **signature is verified** first. Any tampered field blocks the payment.
- The token is **consumed atomically** (an UPDATE that only succeeds while it is unused),
  so two concurrent requests can't both pay.
- Then the **12 deterministic checks** (no LLM):

| # | Check | # | Check |
|---|---|---|---|
| 1 | Token exists | 7 | Right order |
| 2 | Token is active | 8 | Right currency |
| 3 | Token not expired | 9 | Amount within the authorized limit |
| 4 | Token not used before | 10 | Amount = checkout total |
| 5 | Right agent (PayIt) | 11 | Order fingerprint unchanged |
| 6 | Right merchant | 12 | Consent record exists |

A failed check stops everything: nothing is charged, the checkout reopens, and the reason
is shown.

### Step 8 — Payment: one of four routes

`shop/payment_routing.py` picks the route from the checkout's payment choice and the card:

| Route | When |
|---|---|
| **Wallet** | ShopSphere Wallet chosen (needs a saved credit card) |
| **Saved card** | A saved card token (any network) |
| **Visa agent (VIC-inspired)** | A Visa card that is agent-pay capable with an active agent token; if the token isn't set up yet, a one-time passkey setup comes first |
| **New card → Secure Payment Sheet** | No saved card, or the shopper chose a new one |

Mastercard, Amex and Discover are never forced through the Visa route.

#### 8a. Wallet
Internal stored value: the balance is debited, no card network involved.

#### 8b. Saved card
```
PayIt ── authorize(card token, amount, idempotency key) ──► gateway adapter
                                                            └► simulated network ─► simulated issuer
      ◄── approved / declined / timeout ─────────────────────┘
```
Only the provider token travels, never a card number. The same request twice returns the
original answer.

#### 8c. Visa agent route (VIC-inspired)
1. **Agent token** (one time): the shopper links their Visa to Talkshop with a simulated
   passkey; a local token (`vat_…`) becomes ACTIVE (`vic/enrollment.py`).
2. **Payment instruction** for this exact checkout: merchant, fingerprint, amount,
   currency, expiry, one transaction (`vic/instructions.py`).
3. **Passkey confirmation** of the instruction, directly with ShopSphere; Talkshop never
   sees the challenge (`vic/passkey.py`).
4. After the 12 DPAT checks, **12 VIC-inspired controls** (`vic/credentials.py`): customer
   access · active, agent-pay capable Visa credit card · active agent token · instruction
   authenticated · merchant · checkout + fingerprint · amount + currency · not expired ·
   transaction limit · instruction not closed · no duplicate attempt · checkout ready.
5. A **single-use purchase credential** (5 minutes, bound to merchant + checkout + amount)
   goes to the gateway.
6. A **commerce signal** links the outcome to the instruction (a local record; no Visa API).

Lifecycle: `CARD_LINKED → AGENT_TOKEN_ACTIVE → CHECKOUT_CREATED → INSTRUCTION_AUTHENTICATED
→ CREDENTIAL_ISSUED → PAYMENT_AUTHORIZED → SIGNAL_RECORDED → ORDER_CONFIRMED`.

#### 8d. New card: the Secure Payment Sheet
For new customers, or anyone paying with a new card. With this route the mandate's
consent step (Step 6) **opens a provider session first**; the DPAT and the 12 checks run
once the card is verified.

1. **Session.** ShopSphere asks the provider (server-to-server) for a payment session
   bound to merchant, checkout, payment reference, version, amount and currency
   (10 minutes, single use).
2. **The sheet opens** over the Talkshop panel: no new window, no redirect, the
   conversation stays underneath. The card form inside is the **provider's page** in a
   cross-origin iframe; the store and Talkshop cannot read it. Only test cards are accepted.
3. **Tokenization.** "Securing your payment method…" → "✓ Card secured · Visa •••• 4242".
   The provider keeps a token, network and last 4; the number and CVC are dropped.
4. **Payment verification (simulated, generic).** If the issuer asks for it: "Verify your
   payment" with Merchant, Amount, Card and Purpose *One-time purchase*, plus
   **Verify payment**, Cancel and *Simulate failed verification*. It approves this exact
   ShopSphere purchase only. Single-use challenge, 5 minutes, 3 attempts. Some cards are
   frictionless (no extra step).
5. **Back to Talkshop.** ShopSphere fetches the provider's safe result itself (it never
   trusts the browser) and checks the binding. Then the Step 6 mandate (consent record,
   policy, DPAT), the Step 7 checks, and **PayIt asks the provider to authorize** the
   verified credential: once, for exactly this amount.
6. Talkshop says **"✓ Payment verified · Visa •••• 4242 · $X authorized. Creating your
   order…"**.
7. **Save card** is unticked by default. If ticked, ShopSphere keeps only the token,
   network, last 4, expiry and name on the card.

```
Talkshop sheet         Provider page (iframe)          ShopSphere                Provider API
     │ Continue to Secure Payment ──────────────────────► confirm(): scope check ──► create session (PENDING)
     │◄── provider_handoff ─────────────────────────────────┤
     │ ── iframe ──► card form ── tokenize ─────────────────────────────────────────► TOKENIZED
     │◄─ step ──────│ verify (challenge) ── ok ──────────────────────────────────────► VERIFIED
     │◄─ VERIFIED ──│                                       │
     │ provider_complete ──────────────────────────────────► safe result + binding ◄─┤
     │                                                      │ GreenLight + 12 checks
     │                                                      │ PayIt ── authorize ────► CAPTURED
     │◄── "✓ Payment verified … authorized" + order ────────┤
```

Trace events for this route, in order: `SECURE_PAYMENT_SESSION_CREATED`,
`PAYMENT_PROVIDER_OPENED`, `CARD_TOKENIZED`, `ISSUER_VERIFICATION_REQUIRED`,
`USER_VERIFIED`, `PAYMENT_CREDENTIAL_READY`, `GREENLIGHT_CONSENT_VALIDATED`,
`DPAT_CHECKS_PASSED`, `PAYMENT_AUTHORIZED`, `ORDER_CONFIRMED`.

### Step 9 — Order, confirmation, tracking

| # | What happens | Agent | Protocol idea |
|---|---|---|---|
| 9.1 | The order is created **once, only after authorization**; stock is committed; points (2× with a Chase card), offers and cashback are applied. Order IDs look like `SS-…` | TrackIt | ACP-style complete checkout |
| 9.2 | If the order can't be created, the authorization is released and the shopper is told nothing was charged | TrackIt / PayIt | — |
| 9.3 | Confirmation email: Order ID, items, total, tracking link, no card data. Real SMTP if configured, otherwise the demo outbox. An email problem never undoes the order | TrackIt | — |
| 9.4 | **Track Order** with Order ID + email (or as the signed-in owner), from the website or by asking Talkshop | TrackIt | UCP-inspired order management |

---

## 5. The whole flow in one diagram

```
 SHOPPER          TALKSHOP / VIBECHECK     SNEAKPEEK      CARTUP           GREENLIGHT        PAYIT                    TRACKIT
   │ "running shoes   │                       │              │                 │                 │                        │
   │  under $150" ───►│ mask · guardrail ·    │              │                 │                 │                        │
   │                  │ stage · LLM intent ──►│ catalog      │                 │                 │                        │
   │◄── top 3 + reasons ──────────────────────┤ search       │                 │                 │                        │
   │ pick, size, colour ─────────────────────────────────────► stock · cart    │                 │                        │
   │ checkout ───────────────────────────────────────────────► access · card · │                 │                        │
   │                                                          │ checkout +      │                 │                        │
   │◄── review card (authoritative invoice, version, hash) ───┤ benefits        │                 │                        │
   │ Approve & Pay (scoped consent) ───────────────────────────────────────────► scope · record · │                        │
   │                                                                            │ policy · DPAT ─►│ signature · 12 checks  │
   │                                     (new card: Secure Payment Sheet → tokenize → verify)     │ route → authorize ────►│ order once
   │◄── "Order confirmed SS-…" + email ───────────────────────────────────────────────────────────────────────────────────┤ email · track
```

---

## 6. State machines

**Talkshop stages** (`talkshop/state.py`)
```
GREETING → SEARCHING → RECOMMENDED → PRODUCT_SELECTED → ASK_SIZE → ASK_COLOR → VARIANT_CONFIRMED
→ IN_CART → OFFER_CHECKOUT → CHECKOUT_DETAILS → AWAITING_CONSENT → PAYING → ORDER_CONFIRMED
```

**Checkout** (`shop/checkout.py`)
```
open ──approve──► consented ──paid──► paid
  │                    └─ failure → open (retry or change payment)
  └─ new card ──► awaiting_payment ──verified + authorized──► paid
                       └─ closed / cancelled / declined → open
open | consented | awaiting_payment ──cancel──► cancelled
```

**Provider payment session** (`gateway/provider.py`)
```
PENDING ─card─► TOKENIZED ─┬─ verification needed ─► VERIFICATION_REQUIRED ─ok─► VERIFIED
                           └─ frictionless ───────────────────────────────────► VERIFIED
VERIFIED ─PayIt authorize─► CAPTURED | DECLINED | TIMEOUT
open states ─► CANCELLED · EXPIRED (10 min) · VERIFICATION_FAILED (3 failed checks)
```

**VIC-inspired instruction** (`vic/common.py`): see 8c.

---

## 7. Safety rules that hold everywhere

- The LLM never produces authoritative prices, stock, totals, discounts or order numbers.
- The LLM cannot start a payment; only the shopper's own tap can.
- Every approval is scoped to action, checkout version, amount and currency; any change
  means approve again.
- Every payment token is signed, single use, short-lived and bound to agent, merchant,
  order, amount and checkout fingerprint.
- Card numbers, CVC and verification challenges never reach Talkshop, the LLM, traces,
  logs or ShopSphere's database. Typing a card into ShopSphere is disabled
  (`410 PAN_NOT_ACCEPTED`); card data typed into the chat is masked.
- Reusable payment credentials are saved only when the shopper ticks *save*.
- Retries never double charge (single-use tokens, idempotency keys, authorization keyed
  on the payment reference).
- An order exists only after an authorization; if the order fails, the authorization is
  released.

The test suite (`tests/`) checks these rules, including scans for card data and secrets in
events, transcripts, traces and audit records.

---

## 8. When something goes wrong

| Situation | Result |
|---|---|
| Order changed after approval (price, stock, benefit, card) | 409 STALE_CHECKOUT, nothing charged, new total shown, approve again |
| Policy refuses (merchant, currency, amount) | Not approved, reason shown, nothing charged |
| A DPAT check fails / signature invalid / token reused | Blocked, nothing charged, checkout reopens |
| Card declined / insufficient funds | Message shown, review shown again to choose another payment method |
| Network or provider timeout | "Nothing was charged"; a retry can't double charge |
| Secure Payment Sheet closed (✕) | Session released, back to the review, nothing charged (`APPROVAL_CANCELLED / PAYMENT_SHEET_CLOSED`) |
| Payment verification cancelled / failed / expired | Back to review / retry (3 attempts) / a new verification request |
| Passkey cancelled (Visa route) | Nothing approved, recorded as `APPROVAL_CANCELLED` |
| Approve tapped twice | One payment |
| Order creation fails after authorization | Authorization released, shopper told nothing was charged |

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **Checkout** | ShopSphere's order-in-progress object: lines, address, payment choice, invoice |
| **Checkout hash** | SHA-256 fingerprint of everything that affects the price |
| **Checkout version** | Counter that increases whenever the checkout hash changes |
| **Scoped consent** | The approval naming action PAY, version, amount and currency |
| **Mandate** | AP2 term for verifiable proof of the user's authorization; here the consent record + signed DPAT (+ passkey instruction or provider verification) |
| **DPAT** | Delegated Payment Authorization Token: HMAC-signed, single use, 15 minutes, bound to PayIt + merchant + order + amount + fingerprint |
| **Guard** | One reported check in the Talkshop trace (agent, check, result, protocol) |
| **Payment route** | Wallet, saved card, Visa agent or new card |
| **Provider token** | Opaque `pm_mock_…` reference to a card held by the simulated provider |
| **Agent token** | Local `vat_…` value that links a Visa card to Talkshop (VIC-inspired, not VTS) |
| **Payment instruction** | VIC-inspired record of one approved purchase, authenticated with a passkey |
| **Purchase credential** | VIC-inspired single-use credential for one authenticated instruction |
| **Entitlement** | A benefit (Chase offers, 2× points) granted by the card used, not an access rule |

For a deeper look at each protocol and the code map, see [explain.md](explain.md).
