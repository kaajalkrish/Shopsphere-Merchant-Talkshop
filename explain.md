# How ShopSphere + Talkshop works

This document explains the technical working flow of the demo, end to end, and shows
**which protocol (or protocol idea) is used at each step, what it does there, and
where it lives in the code**.

> **Honesty note.** This is a proof of concept. The protocol names below are used as
> labels for *ideas we borrowed*, always qualified: **ACP-style**, **AP2-style**,
> **UCP-inspired**, **VIC-inspired**, **MCP-style**. Nothing here is certified,
> compliant or connected to Visa, Mastercard, Chase or a real payment provider. The
> payment provider, card network and issuer are **simulations** that accept test cards
> only. Nothing is "PCI compliant", "bank encrypted", "Visa secured" or "Chase secured".

---

## 1. The system in one paragraph

**ShopSphere** is a web store (React front end + FastAPI back end). **Talkshop** is
the AI shopping assistant that lives in a side panel of the store. A shopper can find
products, choose a size and colour, add to cart, review the order and pay — all inside
the conversation. Behind Talkshop, six small agents do specific jobs (find, cart,
consent, pay, track). The AI only *chooses what to do next*; every price, stock level,
total, discount and order number comes from ShopSphere's own services. A payment only
happens after the shopper explicitly approves the **exact** final invoice, and the
shopper's card details never pass through Talkshop or ShopSphere.

---

## 2. The pieces and where they run

```
 Browser (localhost:5174)                          Backend (localhost:8010, one FastAPI process)
 ┌───────────────────────────────┐                 ┌────────────────────────────────────────────────┐
 │ ShopSphere store (React/Vite) │ ── REST/JSON ─► │ /api/...   ShopSphere services (backend/shop)  │
 │                               │                 │            cart · checkout · offers · orders   │
 │ Talkshop panel                │ ── SSE ───────► │ /api/talkshop/turn  Talkshop orchestrator      │
 │   └ Secure Payment Sheet      │                 │            + agents (backend/agents)           │
 │       └ <iframe> provider page│ ── HTML form ─► │ /psp/hosted/...  Simulated Payment Provider    │
 └───────────────────────────────┘                 │ /psp/v1/...      (merchant API, server-only)   │
                                                   │ /api/vic/...     VIC-inspired simulator        │
                                                   │ mock gateway → simulated network → issuer      │
                                                   │ SQLite: backend/db/demo1.db                    │
                                                   └────────────────────────────────────────────────┘
                                                              │
                                                              └─► LLM (Gemini via LangChain) — only to
                                                                  understand free text and write
                                                                  product reasons; never for money
```

| Piece | What it does | Where |
|---|---|---|
| Store front end | Pages, cart, checkout, account, Talkshop panel | `frontend/src/` |
| Talkshop orchestrator | Runs one conversation turn as a state machine; streams events | `backend/talkshop/orchestrator.py`, `state.py` |
| Talkshop "brain" | LLM calls: pick an action from an allowed list; write product reasons | `backend/talkshop/brain.py`, `backend/config/llm.py` |
| Talkshop tools | The only functions the conversation may call; they call ShopSphere services | `backend/talkshop/tools.py` |
| Agents | VibeCheck, SneakPeek, CartUp, GreenLight, PayIt, TrackIt | `backend/agents/`, used by the services |
| ShopSphere services | Catalog, cart, checkout, eligibility, offers, payment, orders | `backend/shop/` |
| AP2-style consent + 12 checks | Consent record, signed one-time token, guardrail checks | `backend/routers/authorizations.py`, `backend/routers/payments.py`, `backend/payment/` |
| Payment gateway adapter | One interface for every payment call (in-process or HTTP) | `backend/gateway/adapter.py` |
| Mock gateway / network / issuer | Simulated card authorization for saved cards and VIC | `backend/gateway/app.py`, `network.py`, `issuer.py` |
| Simulated Secure Payment Provider | Hosted card page, tokenization, verification, authorization | `backend/gateway/provider.py` (mounted at `/psp`) |
| VIC-inspired simulator | Agent token enrollment, payment instruction, passkey, controls, credential, signals | `backend/vic/`, `backend/routers/vic.py` |
| Database | Users, products, carts, checkouts, orders, audit events, provider sessions … | `backend/db/schema.py`, `init_db.py` |

---

## 3. Protocols at a glance

| Protocol | Real-world meaning | What this demo does with it | Where in the flow | Where in code |
|---|---|---|---|---|
| **UCP-inspired** | Universal Commerce Protocol: common commerce capabilities (discovery, identity, orders) | Product discovery, identity linking (sign up / sign in mid-conversation), order lookup — over local REST/JSON | Find, sign-in, track order | `orchestrator.py` (search, identity, order lookups), `backend/shop/catalog.py`, `routers/auth.py` |
| **MCP-style** | Model Context Protocol: an AI picks from declared tools | Talkshop's LLM chooses from a fixed, declared tool list (`LLM_TOOLS`); code enforces which tools are allowed at each stage. No MCP client/server is used by Talkshop | Every free-text turn | `talkshop/tools.py` (`LLM_TOOLS`), `brain.decide()`, `orchestrator.py` |
| **ACP-style** | Agentic Commerce Protocol: merchant-owned checkout (create → update → complete) | ShopSphere owns the checkout object. It is created, updated, revalidated and completed by ShopSphere, never by the AI | Review → order | `backend/shop/checkout.py`, `routers/shop.py` (`/api/checkouts…`) |
| **AP2-style** | Agent Payments Protocol: verifiable proof the user approved this exact purchase | Scoped consent (action + version + amount + currency), a consent record, an HMAC-signed single-use token (DPAT) and 12 checks before money moves | Approve → pay | `shop/payment.py` (`confirm`), `routers/authorizations.py`, `payment/signing.py`, `payment/guardrail_engine.py` |
| **VIC-inspired** | Visa Intelligent Commerce: agent tokens, authenticated instructions, controls, purchase credentials, commerce signals | A local simulation of that shape for Visa cards that are "agent-pay capable" | Pay (Visa agent path only) | `backend/vic/`, `routers/vic.py`, `shop/payment.py` (`_gateway_route`) |
| **Hosted payment page / PSP pattern** | A payment provider owns the card form; the merchant gets a token | Simulated Secure Payment Provider in a cross-origin iframe; tokenization, generic verification, authorization on PayIt's request | Pay (new card) | `gateway/provider.py`, `components/payments/SecurePaymentSheet.tsx` |
| **SSE** (Server-Sent Events) | One-way event stream over HTTP | Each Talkshop turn streams typed events (messages, cards, guard checks, `done`) | Every turn | `routers/talkshop.py`, `frontend/src/api/talkshop.ts` |
| **postMessage + CSP `frame-ancestors`** | Browser rules for cross-origin frames | Provider page may be embedded only by ShopSphere origins and tells the sheet only safe progress | New-card payment | `gateway/provider.py` (`_page`, `_post_to_store`), `SecurePaymentSheet.tsx` |
| **Idempotency** | Same request twice = same result once | Gateway idempotency key, provider authorization keyed on `payment_ref`, single-use tokens | Pay | `gateway/adapter.py`, `gateway/provider.py`, `guardrail_engine.py` |

**Not used (and why):** **VTS** (Visa Token Service — our agent tokens are random local
values); **A2A** (agent-to-agent across companies — all agents run in one app); **TAP**
(Visa Trusted Agent Protocol — relevant when an *external* agent visits a merchant, not
an embedded merchant assistant). The in-app "Protocols" panel in Talkshop shows the same
list.

---

## 4. The end-to-end flow

Talkshop's progress bar has five steps. Each maps to conversation stages
(`backend/talkshop/state.py`) and to protocols:

```
 Find ──────────► Choose ─────────────► Review ──────────────► Approve ─────────────► Done
 GREETING         RECOMMENDED           CHECKOUT_DETAILS        PAYING                 ORDER_CONFIRMED
 SEARCHING        PRODUCT_SELECTED      AWAITING_CONSENT
                  ASK_SIZE / ASK_COLOR
                  VARIANT_CONFIRMED
                  IN_CART / OFFER_CHECKOUT

 UCP-inspired     ACP-style (stock,     ACP-style checkout      AP2-style consent      ACP-style complete
 discovery        price from catalog)   + entitlements          + 12 checks            + TrackIt
 MCP-style tool                                                 + payment route:
 choice                                                           saved card / VIC /
                                                                  secure payment sheet
```

### 4.1 One Talkshop turn

```
Browser                               Backend
  │ POST /api/talkshop/turn            │
  │ {session_id, text | action, page}  │
  ├───────────────────────────────────►│ orchestrator.run_turn()
  │                                    │  1. load session state (stage, cart lines, checkout id …)
  │                                    │  2. action?  → handled by code (buttons, cards)
  │                                    │     text?    → brain.decide(): LLM picks ONE action from the
  │                                    │                tools allowed in this stage (MCP-style)
  │                                    │  3. run the tool → ShopSphere service → real data
  │◄─── SSE: message / product_cards / │  4. stream events as they happen
  │     checkout_ready / guard / … ────┤
  │◄─── SSE: done {stage, step} ───────┤
```

Rules enforced in code, not by the AI:
- The LLM never sees or produces authoritative prices, stock, totals or order numbers.
- `confirm_and_pay` is **not** in `LLM_TOOLS`. Only the shopper's explicit button action
  (`go_ahead`) can start a payment; "auto" or countdown consent is refused.
- Every step that matters emits a **guard** event (agent, check, result, protocol label),
  which feeds the Presenter trace in the panel.

### 4.2 Find and choose (UCP-inspired discovery, MCP-style tool choice)

1. The shopper types "running shoes under $150". `brain.decide()` returns `search` with
   a structured intent (category, budget, colour …). A clear request takes a fast path
   without the LLM.
2. **SneakPeek** searches the ShopSphere catalog, filters and ranks; the top 3 are shown.
   `brain.recommend()` writes one-line reasons **only from the returned product facts**.
3. Choosing size and colour checks live stock (**CartUp**). Adding to cart uses the
   ShopSphere cart service (the same cart the website shows).

Everyone can do this — visitors included.

### 4.3 Access levels and entitlements

Three access levels (`backend/shop/eligibility.py`):

| Level | Who | Can do |
|---|---|---|
| Browse | Everyone, including visitors | Search, browse, use the cart, use Talkshop |
| Checkout | Signed-in customers (`require_shopper`) | Create a checkout, add address |
| Pay | Credit-card holders (`check_payment`) | Pay; the Wallet needs a saved credit card |

**Chase is an entitlement, not the gate.** Any issuer's cardholder can buy
(Citi, Amex, Chase …). Chase cardholders additionally get offers and 2× points
(`backend/shop/entitlements.py`, `offers.py`). Offers are **revalidated against the card
selected for this payment**: switch from a Chase card to a Citi card and the Chase offer
is removed, with a reason, and the total changes. Signing up mid-conversation is the
UCP-inspired identity-linking step: name, email and password only — never a card.

### 4.4 Review: the merchant-owned checkout (ACP-style)

`POST /api/checkouts` creates a ShopSphere checkout from cart lines
(`backend/shop/checkout.py`). It holds:

- lines, address, payment choice (`pay_with`: card / wallet / provider),
- **authoritative invoice**: subtotal, tax, shipping, benefits applied/removed, total,
- `checkout_hash` — SHA-256 fingerprint of everything that affects the price,
- `checkout_version` — goes up every time the hash changes.

`PATCH` updates it (address, card, points, offer). Right before paying, `refresh()`
re-reads live stock and prices and revalidates benefits. Talkshop renders the review card
from this object; it never calculates totals itself.

The review card's button reads **Approve & Pay $X** (saved card / VIC) or
**Continue to Secure Payment · $X** (new card). If it can't be approved, the card shows
**"Why you can't approve yet"** with the reasons.

### 4.5 Approve: scoped consent and the 12 checks (AP2-style)

Tapping the button sends a **scoped consent**:

```json
{ "consent": true, "action": "PAY", "checkout_version": 3, "final_amount": 150.47, "currency": "USD" }
```

`payment.confirm()` (`backend/shop/payment.py`) then:

1. **Access + payment eligibility** checked again (second line of defence).
2. **Scope check**: if the checkout's version, total or currency differ from what the
   shopper approved — before *or* after the live refresh — nothing is charged and
   **409 STALE_CHECKOUT** returns the new invoice to approve again.
3. **Consent record** (`CHECKOUT_CONSENT` audit event) with mode, hash, total, version.
4. **GreenLight** checks the spending policy and issues a **DPAT** (delegated payment
   token): HMAC-SHA256 signed, single use, 15 minutes, bound to PayIt + ShopSphere +
   amount + checkout fingerprint (`routers/authorizations.py`, `payment/signing.py`).
5. **PayIt** runs the **12 DPAT checks** (`payment/guardrail_engine.py`):

| # | Check | # | Check |
|---|---|---|---|
| 1 | Token exists | 7 | Right order |
| 2 | Token is active | 8 | Right currency |
| 3 | Token not expired | 9 | Amount within authorized limit |
| 4 | Token not used before | 10 | Amount = checkout total |
| 5 | Right agent (PayIt) | 11 | Order fingerprint unchanged |
| 6 | Right merchant | 12 | Consent record exists |

6. Only then does PayIt call the **payment route**.

### 4.6 Payment routing

`backend/shop/payment_routing.py` decides how this checkout is paid:

| Route | When | What happens |
|---|---|---|
| `wallet` | Pay with ShopSphere Wallet (needs a saved credit card) | Internal stored value; no card network |
| `saved_card` | A saved card token | Gateway adapter → simulated network → simulated issuer |
| `vic` | Visa card, `agent_pay_capable`, active agent token | VIC-inspired path (4.8) |
| `vic_setup_required` | Visa agent-capable card, no active token yet | One-time simulated passkey enrollment first |
| `provider_handoff` | New card (no saved card, or the shopper chose a new one) | Secure Payment Sheet (4.9) |

Mastercard, Amex and Discover cards are **never** forced through the Visa/VIC path.

### 4.7 Saved card path

```
PayIt ── authorize(card token, amount, idempotency key = payment_ref) ──► Gateway adapter
      ◄─ approved / declined / timeout ─── simulated network ─── simulated issuer (from the card account)
```

The gateway gets only the PSP card token, never a card number
(`gateway/adapter.py`, `gateway/network.py`, `gateway/issuer.py`). The same request
sent twice returns the original result (idempotency).

### 4.8 Visa agent path (VIC-inspired)

```
 one time:   CARD_LINKED ─► AGENT_TOKEN_ACTIVE         (simulated passkey enrollment)
 per order:  CHECKOUT_CREATED ─► INSTRUCTION_AUTHENTICATED ─► CREDENTIAL_ISSUED
             ─► PAYMENT_AUTHORIZED ─► SIGNAL_RECORDED ─► ORDER_CONFIRMED
```

1. **Enrollment** (`vic/enrollment.py`): the member links their Visa to Talkshop with a
   simulated passkey; a local agent token (`vat_…`) becomes ACTIVE.
2. **Payment instruction** (`vic/instructions.py`): created for this exact checkout
   (merchant, checkout hash, amount, currency, expiry, max 1 transaction).
3. **Simulated passkey** (`vic/passkey.py`, `PasskeyContext.tsx`): the member confirms
   the instruction directly with ShopSphere; Talkshop never sees the challenge.
4. After GreenLight + the 12 DPAT checks, **12 VIC-inspired controls**
   (`vic/credentials.py`): customer access · active Visa credit card that is agent-pay
   capable · active agent token · instruction authenticated · merchant · checkout +
   fingerprint · amount + currency · not expired · transaction limit · not closed ·
   no duplicate attempt · checkout ready.
5. A single-use **purchase credential** (5 min, bound to merchant + checkout + amount) is
   issued and redeemed through the gateway.
6. A **commerce signal** links the outcome to the instruction (local record; no Visa API).

### 4.9 New card path: the Secure Payment Sheet

This is the flow for a new customer (or anyone paying with a new card).

```
Talkshop panel           Provider page (iframe)       ShopSphere backend            Provider merchant API
(store origin)           (provider origin)            (GreenLight / PayIt)          (server-to-server)
     │ Continue to Secure Payment                             │                              │
     ├───────────────────────────────────────────────────────►│ confirm(): scope + checks     │
     │                                                        ├─ POST /v1/payment_sessions ──►│ PENDING
     │◄── provider_handoff (session id, hosted URL) ──────────┤  bound to merchant, checkout, │
     │ sheet opens over the panel                             │  version, amount, currency    │
     │──── loads iframe ──►│                                  │                              │
     │                     │ card form (test cards only)      │                              │
     │◄─ step: securing ───│ POST /hosted/{id}/pay ─────────────────────────────────────────►│ TOKENIZED
     │◄─ step: tokenized ──│ "✓ Card secured · Visa •••• 4242"                                │ (token, network, last 4)
     │                     │ POST /hosted/{id}/continue ────────────────────────────────────►│ VERIFICATION_REQUIRED
     │◄─ step: verify ─────│ "Verify your payment" (one-time challenge)                      │  (or VERIFIED: frictionless)
     │                     │ POST /hosted/{id}/challenge ok ────────────────────────────────►│ VERIFIED
     │◄─ result: VERIFIED ─│                                  │                              │
     │ sheet closes; action provider_complete ───────────────►│ GET /v1/payment_sessions/{id}►│ safe result
     │                                                        │ check binding; GreenLight;    │
     │                                                        │ 12 DPAT checks; PayIt:        │
     │                                                        ├─ POST …/authorize ───────────►│ CAPTURED / DECLINED /
     │◄── "✓ Payment verified · Visa •••• 4242 · $X authorized. Creating your order…" ────────┤ TIMEOUT
     │◄── order_confirmed SS-… ────────────────────────────────┤                              │
```

**Provider session states** (`backend/gateway/provider.py`):

```
PENDING ──card──► TOKENIZED ─┬─ verification needed ─► VERIFICATION_REQUIRED ─ok─► VERIFIED
                             └─ frictionless ────────────────────────────────────► VERIFIED
VERIFIED ──PayIt authorize──► CAPTURED | DECLINED | TIMEOUT
open states ─► CANCELLED (customer/store) · EXPIRED (10 min) · VERIFICATION_FAILED (3 failed checks)
```

What each protocol idea does here:

- **Hosted payment page pattern.** The card form is the provider's page on the
  provider's origin, inside a sandboxed iframe. The store's and Talkshop's JavaScript
  cannot read it. Only well-known test card numbers are accepted; anything else is
  refused so real cards are never used. The number and CVC are dropped after
  tokenization; the provider keeps only network + last 4 + an opaque token (`pm_mock_…`).
- **CSP `frame-ancestors`** lets only ShopSphere's origins embed the page.
  **postMessage** sends the sheet only safe progress — `step` (card / securing /
  tokenized / verify), network, last 4, final `status` — and only to the store's origin.
  Never the token, never the challenge.
- **Generic payment verification (simulated).** The issuer decides per card whether a
  check is needed. The screen names Merchant, Amount, Card and Purpose
  ("One-time purchase") and says it approves *this exact ShopSphere purchase only*. It is
  not Visa-specific. Each challenge is single use, expires in 5 minutes, and only its
  hash is stored. Real issuers may use OTP, app approval, biometrics or 3DS; this demo
  shows one passkey-style example.
- **Server-to-server result.** ShopSphere never trusts the browser: it asks the
  provider's merchant API (with `X-Gateway-Key`) for the safe result and checks it is bound
  to this merchant, checkout, payment reference, version, amount and currency.
- **AP2-style consent + 12 checks** run as in 4.5, *then* **PayIt** asks the provider to
  authorize. Authorization accepts only a VERIFIED credential, only for exactly what the
  session was created for, and only once (idempotent on `payment_ref`).
- **Save card** is unticked by default. If ticked, ShopSphere stores only the token,
  network, last 4, expiry and cardholder display name (`profile.save_provider_card`).

### 4.10 Done: order and tracking (ACP-style complete + TrackIt)

`orders.finalize()` creates the order **once, only after authorization** and commits
stock. Points (2× with a Chase card), offers and cashback are applied. **TrackIt** writes
the confirmation email (SMTP if configured, otherwise the demo outbox) and powers Track
Order (Order ID + email). If the order can't be created, the authorization is released
and the shopper is told nothing was charged.

---

## 5. The data boundary

| Data | Provider page | ShopSphere backend | Talkshop (events, LLM, transcript) | Browser store page |
|---|---|---|---|---|
| Card number (PAN) | Seen, then dropped | Never | Never | Never (cross-origin iframe) |
| CVC | Checked, then dropped | Never | Never | Never |
| Verification challenge | Hash only | Never | Never | Never |
| Provider token | Created | Stored only if "save" ticked | Never | Never |
| Network + last 4 | Yes | Yes | Yes ("Visa •••• 4242") | Yes |
| Expiry + name | Kept only if "save" ticked | Stored only if "save" ticked | Never | — |
| Totals, prices, stock | — | Authoritative source | Shown, never computed | Shown |

`POST /api/me/payment-methods` (typing a card into ShopSphere) is permanently disabled
(**410 PAN_NOT_ACCEPTED**). Tests scan events, transcripts, traces and audit records for
card numbers, CVC fields, challenges and tokens (`tests/test_secure_payment_sheet.py`,
`tests/test_zz_leak_scan.py`).

---

## 6. Trace and audit

Every payment returns a **trace** — the real backend steps in the order they ran, shown
in Talkshop's Presenter view. New-card steps carry named events:

```
SECURE_PAYMENT_SESSION_CREATED → PAYMENT_PROVIDER_OPENED → CARD_TOKENIZED
→ ISSUER_VERIFICATION_REQUIRED → USER_VERIFIED → PAYMENT_CREDENTIAL_READY
→ GREENLIGHT_CONSENT_VALIDATED → DPAT_CHECKS_PASSED → PAYMENT_AUTHORIZED → ORDER_CONFIRMED
```

The **audit log** (`audit_events` table) records `CUSTOMER_SIGNUP`, `CHECKOUT_CONSENT`,
`PAYMENT_CREDENTIAL_READY`, `APPROVAL_CANCELLED` (with reason, e.g.
`PAYMENT_SHEET_CLOSED`), payment outcomes and more. References are shortened; no card data.

---

## 7. What happens when things go wrong

| Situation | Result |
|---|---|
| Shopper closes the sheet (✕) | Session released (CANCELLED), back to the review, nothing charged, `APPROVAL_CANCELLED / PAYMENT_SHEET_CLOSED` |
| Not a test card / bad expiry / bad CVC | Stays on the provider form with the error; the number is never echoed back |
| Card can't be secured (`…0101`) | No payment attempted; try another card |
| Verification cancelled | Back to the review, nothing charged |
| Verification failed | Retry (3 attempts), then VERIFICATION_FAILED, nothing charged |
| Verification expired | A new verification request is shown |
| Declined (`…0002`, `…9995` insufficient funds) | Message shown, review shown again to choose another payment method |
| Timeout (`…0119`) | "Nothing was charged"; the session can't be reused, so a retry can't double charge |
| Approve tapped twice / complete called twice | One authorization, one order (single-use token, idempotent authorize) |
| Price or benefit changed after approval | 409 STALE_CHECKOUT, old verification voided, new total shown, approve and verify again |
| Order creation fails after authorization | Authorization released, shopper told nothing was charged |

---

## 8. Code map (quick reference)

```
backend/
  main.py                    FastAPI app; mounts the provider at /psp
  talkshop/                  orchestrator (state machine), brain (LLM), tools, state
  agents/                    VibeCheck, SneakPeek, CartUp, GreenLight, PayIt, TrackIt
  shop/
    eligibility.py           access levels (browse / checkout / pay)
    entitlements.py, offers.py   Chase offers and 2× points, revalidated per card
    checkout.py              ACP-style checkout, version, hash, invoice
    payment.py               confirm (AP2-style scope), routes, provider completion/abandon
    payment_routing.py       wallet / saved_card / vic / provider_handoff
    orders.py, profile.py    order creation, saved provider tokens
  routers/                   REST endpoints (shop, talkshop, auth, member, vic, payments …)
  payment/                   DPAT signing (HMAC) and the 12 guardrail checks
  gateway/
    adapter.py               one interface for all payment calls
    provider.py              Simulated Secure Payment Provider (hosted page + merchant API)
    app.py, network.py, issuer.py   mock gateway, simulated network and issuer
  vic/                       VIC-inspired enrollment, instructions, passkey, controls, signals
frontend/src/
  components/talkshop/       Talkshop panel, cards, Presenter trace, protocols panel
  components/payments/SecurePaymentSheet.tsx   the sheet around the provider iframe
  pages/store/               store pages incl. Checkout
  api/                       typed REST + SSE clients
tests/                       pytest suite (545 tests)
```
