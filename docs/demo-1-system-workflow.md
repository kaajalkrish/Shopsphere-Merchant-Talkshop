# ShopSphere + Talkshop: System Workflow

This document follows one purchase through the whole Demo 1 system, from the moment the store opens to the order confirmation. It also covers what happens in the backend at each step, and the safety checks along the way.

- **ShopSphere** is the merchant website: its catalog, cart, checkout and orders.
- **Talkshop** is ShopSphere's AI shopping assistant. It lives in the panel on the right of every page.

> The diagrams use Mermaid. They render on GitHub, and in VS Code's Markdown preview with a Mermaid extension.

---

## 1. The big picture

```mermaid
flowchart LR
    subgraph Browser["Browser (React 19 + Vite, port 5173)"]
        Store["ShopSphere store pages<br/>Home · Category · Product · Cart · Checkout · Orders"]
        Panel["Talkshop panel<br/>chat · cards · secure forms"]
        Auth["AuthContext<br/>visitor / customer token"]
    end

    subgraph API["FastAPI backend (port 8000)"]
        Routers["Routers<br/>/api/auth · /api/products · /api/cart<br/>/api/me · /api/checkouts · /api/orders<br/>/api/talkshop"]
        Orch["Talkshop orchestrator<br/>stage machine + parsers"]
        Brain["brain.py<br/>LLM calls"]
        Shop["ShopSphere services<br/>catalog · cart · profile · checkout · orders"]
        Pay["Payment pipeline<br/>GreenLight → PayIt → processor → TrackIt"]
    end

    DB[("SQLite<br/>demo1.db")]
    LLM["Gemini<br/>gemini-3.5-flash-lite"]
    Trace["OpenTelemetry → Phoenix<br/>(payment path only)"]

    Store -- "REST (JSON)" --> Routers
    Panel -- "POST /api/talkshop/turn<br/>(Server-Sent Events back)" --> Routers
    Auth -- "Bearer JWT on every call" --> Routers
    Routers --> Orch
    Routers --> Shop
    Orch --> Brain --> LLM
    Orch -- "tools.py" --> Shop
    Orch -- "only on GO AHEAD" --> Pay
    Shop --> DB
    Pay --> DB
    Pay -.-> Trace
```

### Who is responsible for what

| Layer | Owns | Never does |
|---|---|---|
| **Talkshop (LLM)** | Understanding what the shopper means, holding the conversation, choosing the next allowed step, writing short product reasons | Set prices, stock, tax or totals; see card numbers or addresses; start a payment |
| **ShopSphere (merchant backend)** | Catalog, inventory, cart, customer profile and addresses, pricing, tax (8.25%), delivery, checkout, orders | Make decisions from free text |
| **Payment pipeline** | Consent record, single-use payment token, the 12 security checks, charging the saved card, order creation | Run without an explicit GO AHEAD |

The LLM only ever **chooses** an action. The code checks that the action is allowed at this stage of the conversation, and the merchant services produce every number the shopper sees.

---

## 2. Step 0: the store opens (identity)

Anyone can browse, fill a cart and chat. An account is needed only to check out.

```mermaid
sequenceDiagram
    participant B as Browser (AuthContext)
    participant A as /api/auth
    participant DB as SQLite

    B->>B: read saved token (localStorage "talkshop_token")
    alt token present
        B->>A: GET /api/auth/me
        A-->>B: user (customer or visitor)
    else no token / invalid
        B->>A: POST /api/auth/visitor
        A->>DB: insert users row (VIS…, is_guest = true, no name/email)
        A-->>B: JWT (HS256, 24 h)
    end
    Note over B: Every request carries Authorization: Bearer <token>
```

**Technical details**
- **Tokens:** a JWT signed with HS256 that lasts 24 hours. `get_current_user` decodes it on every request.
- **Visitors** are real `users` rows with `is_guest = true`.
  - **Open to them:** browsing, the cart and Talkshop.
  - **Guest checkout:** they can **check out as a guest**. That checkout keeps a guest email and never becomes a customer account (see section 15).
  - **Customers only:** saved addresses and cards, My Orders, the wallet and loyalty points (`require_customer` → 403 `LOGIN_REQUIRED`).
- **Logging in or signing up as a visitor** sends the visitor's token along. The server then:
  - merges the visitor's cart into the account (`merge_carts`: the same SKU adds up, capped by stock);
  - moves the Talkshop conversation across (`talkshop_state.rekey`);
  - deletes the visitor record.
- **Tab sync:** all tabs share one saved login. A `storage` listener reloads the other tabs when the login changes, and a request with no token reloads the page so it can recover.

---

## 3. Step 1: one chat turn, end to end

Every message or button tap in the panel is **one turn**: `POST /api/talkshop/turn`. The answer streams back as a series of **Server-Sent Events**, which draw the bubbles, cards and status lines one at a time.

```mermaid
flowchart TD
    In["Shopper types or taps<br/>(text or action)"] --> M1["Browser masks card data<br/>redact.ts"]
    M1 --> Turn["POST /api/talkshop/turn<br/>{session_id, text | action, page}"]
    Turn --> Act{"action or text?"}

    Act -- "action (Select, size chip,<br/>Checkout now, GO AHEAD…)" --> HA["_handle_action<br/>only if the stage allows it"]

    Act -- text --> R["Server masks card data again<br/>parse.redact_payment_data"]
    R -- "card details found" --> Warn["'I didn't keep that'<br/>(no LLM)"]
    R --> G["Input guardrail<br/>keyword layer + NeMo"]
    G -- blocked --> Refuse["Polite refusal"]
    G --> NS{"Something ShopSphere<br/>doesn't sell?<br/>parse.not_sold"}
    NS -- "yes, only that" --> NoSell["'ShopSphere doesn't sell …'<br/>(no product cards)"]
    NS -- "no / mixed request" --> Fast{"Quick, exact reply?<br/>yes · no · '10' · 'teal'<br/>· 'the first one'"}
    Fast -- yes --> Step["Run that step directly"]
    Fast -- no --> SI{"Clear product request?<br/>parse.search_intent"}
    SI -- yes --> Search["_search"]
    SI -- no --> Decide["LLM: brain.decide<br/>picks ONE allowed action"]
    Decide --> Step

    HA --> Out
    Step --> Out
    Search --> Out
    Out["Events streamed back (SSE)<br/>message · cards · status · stage · done"]
```

**Technical details**
- **Session state** is in memory, keyed by `(user_id, session_id)` (`talkshop/state.py`). It holds:
  - the stage and the product currently selected;
  - the size and colour wanted;
  - the cart lines Talkshop added;
  - the checkout id;
  - a transcript of every event sent.
- **Reloads:** after a page reload the panel rebuilds itself from the transcript (`GET /api/talkshop/sessions/{id}`).
- **Quick paths before the LLM:** short, unambiguous replies are matched exactly against the options ShopSphere offers. This keeps them fast, and they never depend on the model. Anything richer goes to the LLM.
- **Message names a product:** a message that names a product ("…can I get nike shoes in size 10") is never taken as a quick answer to the question on screen.
- **LLM calls** have a 10 s timeout and one retry. Each has a deterministic fallback, so a model hiccup never stalls the chat.
- **Status lines** name the step that is working: `VibeCheck` understands the request, `SneakPeek` searches, `CartUp` handles the cart, and `GreenLight`, `PayIt` and `TrackIt` handle payment.

---

## 4. Step 2: search and recommendations

```mermaid
sequenceDiagram
    participant O as Orchestrator (_search)
    participant C as catalog.search
    participant L as LLM (brain.recommend)
    participant P as Panel

    O->>C: filters (category, brand, max_price, gender, colour) + query words
    C-->>O: top 3 in-stock products (relevance, then rating)
    Note over O: relax one filter at a time if empty<br/>(colour, then budget); say which
    O->>L: request + product facts (name, tags, colours, sizes, category)
    L-->>O: intro, one reason per product,<br/>wants {size, colour}, fits true/false
    alt fits = false (none is what was asked for)
        O-->>P: "ShopSphere doesn't sell …" (no cards)
    else
        O->>O: remember wants (e.g. size 10, Teal)
        O-->>P: recommendations event (cards shown in the wanted colour)
    end
```

**Technical details**
- **Facts:** the LLM writes only the intro and the short reasons. Prices, ratings and stock always come from the catalog.
- **What you asked for:** the same LLM call reads typos and shades as a person would ("tale" means Teal, "sea green" means Teal). Code accepts only colours the products really come in.
- **Relevance:** in that call the LLM also judges whether the results are the kind of thing you asked for. Only an explicit "doesn't fit" hides the cards.
- **Not sold:** a fixed list of things ShopSphere doesn't sell (groceries, furniture, pet supplies, books, toys, beauty, appliances, medicine, vehicles) is caught before any search. "no"/"not" rules out only the next item.

---

## 5. Step 3: choose size and colour, add to cart

```mermaid
sequenceDiagram
    participant S as Shopper
    participant O as Orchestrator
    participant Cat as catalog.availability
    participant Cart as cart service

    S->>O: Select (product_id)
    O->>O: pre-fill size/colour remembered from the request (if in stock)
    O->>Cat: which sizes/colours are in stock?
    Cat-->>O: availability per size and colour
    O-->>S: ask only what is missing (size chips, then colour chips)
    S->>O: "8" / "Black" (or a chip)
    O->>Cat: exact SKU in stock?
    O->>Cart: add_line(user, SKU, 1)
    Cart-->>O: cart + line_id
    O-->>S: "✓ Runner Pro X · Size 8 · Black is in stock" + cart card
    O-->>S: offer_checkout → panel shows a 10 s countdown
```

**Technical details**
- **Sold out:** sold-out sizes and colours are shown crossed out and can't be chosen. A single in-stock option is picked automatically.
- **Cart lines:** Talkshop records which cart lines it added (`line_ids`, `added_qty`). Checkout from the chat then covers only those, even if the cart holds other items.
- **Countdown:** after 6 seconds the panel sends the `checkout` action by itself.
  - It pauses if the shopper clicks into the chat box.
  - It never runs while the panel is minimised, or for a conversation restored after a reload.
  - Moving to checkout never pays.

---

## 6. Step 4: checkout (login, missing details, review)

```mermaid
flowchart TD
    CO["checkout action"] --> V{"Visitor?"}
    V -- yes --> SignIn["login_required →<br/>sign-in card in the chat"]
    SignIn --> Merge["login / sign up:<br/>merge cart + conversation"]
    Merge --> CO
    V -- no --> Create["checkout.create_checkout<br/>snapshot: lines, prices, tax 8.25%,<br/>delivery options/date, SHA-256 hash"]
    Create --> Missing{"Address and card<br/>on file?"}
    Missing -- "address missing" --> FA["SECURE SHOPSHERE CHECKOUT form<br/>→ POST /api/me/addresses"]
    Missing -- "card missing" --> FC["SECURE PAYMENT form<br/>→ POST /api/me/payment-methods<br/>(tokenized: tok_…)"]
    FA -- "details_added {address_id}" --> Re["update_checkout<br/>(ownership checked) + recalculate"]
    FC -- "details_added {payment_method_id}" --> Re
    Re --> Missing
    Missing -- both --> Review["checkout_ready →<br/>REVIEW YOUR ORDER + one GO AHEAD<br/>stage AWAITING_CONSENT"]
```

**Technical details**
- **The checkout snapshot is authoritative.** It records the lines, unit prices, subtotal, tax, shipping, total, delivery date and a **SHA-256 `checkout_hash`** over all of them. These amounts are what get charged. The browser never sends a price.
- **Secure forms are ShopSphere's, not the chat's.**
  - The address goes straight to the profile API, which checks the required fields, a 2-letter state code and the ZIP format.
  - The card goes straight to the card endpoint, which checks the Luhn checksum, the expiry and the CVC. It stores only the brand, the last 4 digits, the expiry and a vault token.
  - Talkshop receives **only the id**, and the LLM never sees the details.
- **Ownership:** an address or card id that isn't the customer's own is refused (`UNKNOWN_ADDRESS` / `UNKNOWN_CARD`).
- **On the Review card** the shopper can change the quantity, delivery (standard free, express $9.99), address or card. Each change goes through `update_checkout`, which recalculates everything and the hash.

---

## 7. Step 5: GO AHEAD → payment → order

Payment starts only from the GO AHEAD / **Approve and pay** action (`{type: "go_ahead", checkout_id}`), tapped by
the shopper. (The Review card's 3-second auto-pay countdown was removed on Oct 9; paying always needs a tap.)

The audit log records how consent was given (`CHECKOUT_CONSENT`). Typed text such as "pay now" never pays, and the LLM's list of actions doesn't include payment.

```mermaid
sequenceDiagram
    autonumber
    participant S as Shopper
    participant O as Orchestrator (_go_ahead)
    participant Pmt as payment.confirm
    participant GL as GreenLight (consent + token)
    participant PI as PayIt (12 checks + policy)
    participant PR as Mock processor
    participant OS as orders.finalize
    participant DB as SQLite

    S->>O: GO AHEAD (checkout_id)
    O->>O: stage must be AWAITING_CONSENT and the id must match
    O->>Pmt: confirm(consent = true)
    Pmt->>DB: refresh live stock and prices → must be "ready"
    Pmt->>DB: checkout.status = consented
    Pmt->>GL: approve_authorization(amounts, checkout_hash)
    GL->>DB: payment_authorizations (consent record)<br/>+ delegated_tokens (HMAC-SHA256 signed, 15 min, single use)
    GL-->>Pmt: token_id only
    Pmt->>PI: execute_payment(token_id, amounts, payment_method_id)
    PI->>PI: 12 checks (token exists, active, not expired, not used,<br/>agent, merchant, order, currency, amount ≤ authorized,<br/>amount = checkout total, hash matches, consent exists)
    PI->>PI: policy: allowed merchant, currency, 0 < amount ≤ $2,500
    PI->>PR: charge the saved card (by id)
    alt approved
        PR-->>PI: transaction_id + auth code
        Pmt->>OS: finalize
        OS->>DB: order SS-#####, order_lines, address + masked-card snapshot,<br/>stock decrement (guarded), purchased cart lines removed
        OS-->>O: order
        O-->>S: payment_status: authorized → ORDER CONFIRMED card
    else declined / expired / blocked
        PR-->>PI: decline reason
        Pmt->>DB: checkout back to open, no order
        O-->>S: "Payment wasn't authorized … No order was created"
    else order creation fails after approval
        Pmt->>PR: void_authorization(transaction_id)
        O-->>S: "payment released, you haven't been charged"
    end
```

**Technical details**
- **The token is scoped.** It is bound to this checkout, merchant, amount, currency and checkout hash. It expires after 15 minutes and is consumed on use. Any change to the order after consent breaks the hash check.
- **Tracing:** the payment path (PayIt, the guardrail engine, the processor and TrackIt) is traced with OpenTelemetry and exported to Phoenix when it's running. Spans record ids and outcomes, never card data.
- **Audit:** consent and payment events are written to `audit_events`.

---

## 8. Conversation stages

The orchestrator only allows actions that make sense at the current stage (`state.ALLOWED`), so the LLM can't skip ahead.

```mermaid
stateDiagram-v2
    [*] --> GREETING
    GREETING --> SEARCHING : request
    SEARCHING --> RECOMMENDED : top 3 shown
    SEARCHING --> GREETING : nothing found / not sold
    RECOMMENDED --> PRODUCT_SELECTED : Select
    PRODUCT_SELECTED --> ASK_SIZE
    ASK_SIZE --> ASK_COLOR
    ASK_COLOR --> VARIANT_CONFIRMED
    VARIANT_CONFIRMED --> OFFER_CHECKOUT : added to cart
    OFFER_CHECKOUT --> IN_CART : Keep shopping
    OFFER_CHECKOUT --> CHECKOUT_DETAILS : no address / card
    OFFER_CHECKOUT --> AWAITING_CONSENT : review shown
    IN_CART --> AWAITING_CONSENT : checkout
    CHECKOUT_DETAILS --> AWAITING_CONSENT : details saved
    AWAITING_CONSENT --> PAYING : GO AHEAD
    PAYING --> ORDER_CONFIRMED : authorized
    PAYING --> AWAITING_CONSENT : declined
    AWAITING_CONSENT --> IN_CART : Cancel
    ORDER_CONFIRMED --> SEARCHING : new request
```

| Stage | Allowed next |
|---|---|
| GREETING | search, answer, select |
| RECOMMENDED | select, search, answer |
| PRODUCT_SELECTED / ASK_SIZE / ASK_COLOR | choose option, search, answer, select |
| IN_CART / OFFER_CHECKOUT | checkout, keep shopping, search, answer, select |
| CHECKOUT_DETAILS | cancel, answer, search (GO AHEAD impossible) |
| AWAITING_CONSENT | update checkout, cancel, answer, search; **GO AHEAD button only** |
| PAYING | nothing |
| ORDER_CONFIRMED | search, answer, select |

---

## 9. Events streamed to the panel (SSE)

| Event | Draws |
|---|---|
| `user_message` | The shopper's bubble (already masked) |
| `message` | Talkshop's bubble |
| `status` | "VibeCheck · Understanding your request…" line + presenter trace |
| `stage` / `done` | The progress stepper (Search → Choose → Size → Colour → Cart → Review → Done) |
| `suggestions` | Greeting chips |
| `recommendations` | Top 3 product cards (+ "Talkshop picks" on the store page) |
| `product_selected` | Selected-product card |
| `ask_option` | Size or colour chips (sold out = crossed out) |
| `variant_confirmed` | (internal) the exact SKU |
| `cart_updated` | "Added to your ShopSphere cart" card; header cart badge refreshes |
| `offer_checkout` | 6-second countdown card (Checkout now / Keep shopping) |
| `login_required` | Sign-in card (visitors) |
| `checkout_details_needed` | Secure ShopSphere Checkout / Secure Payment form |
| `checkout_ready` / `checkout_updated` | Review Your Order card with GO AHEAD |
| `payment_status` | Processing → authorizing → authorized / declined / failed |
| `order_confirmed` | Order confirmed card (SS-#####, delivery date, total) |

---

## 10. Key API endpoints

| Area | Endpoints |
|---|---|
| Identity | `POST /api/auth/visitor` · `POST /api/auth/register` · `POST /api/auth/login` · `GET /api/auth/me` · `POST /api/auth/reset-password` |
| Catalog | `GET /api/products` · `GET /api/products/{id}` · `GET /api/products/{id}/availability` |
| Cart | `GET/POST /api/cart/lines` · `PATCH/DELETE /api/cart/lines/{line_id}` |
| Profile | `GET/POST /api/me/addresses` · `DELETE /api/me/addresses/{id}` · `GET/POST /api/me/payment-methods` · `DELETE /api/me/payment-methods/{id}` |
| Checkout | `POST /api/checkouts` · `GET/PATCH /api/checkouts/{id}` · `POST /api/checkouts/{id}/cancel` · `POST /api/checkouts/{id}/confirm` |
| Orders | `GET /api/orders` · `GET /api/orders/{id}` |
| Talkshop | `POST /api/talkshop/turn` (SSE) · `GET/DELETE /api/talkshop/sessions/{id}` |

The website's own checkout page uses the same Checkout and Profile endpoints as Talkshop. **Place order** on the website calls `/confirm` with `consent: true`, which is the same payment pipeline as GO AHEAD.

---

## 11. Data (SQLite tables)

| Group | Tables |
|---|---|
| People | `users` (customers + visitors), `addresses`, `payment_methods` (brand, last 4, expiry, vault token, never the number/CVC) |
| Catalog | `merchants`, `products`, `product_variants` (SKU, size, colour, stock, image) |
| Shopping | `cart_items`, `checkouts` (authoritative snapshot + hash) |
| Orders | `orders` (SS-#####, address + card snapshots), `order_lines` |
| Payment | `payment_authorizations` (consent), `delegated_tokens` (single-use), `audit_events` |
| Other | `wallets`, `loyalty_points`, `loyalty_transactions`, `chat_sessions`, `chat_messages`, `agents` |

Demo data: about 60 products, plus the demo customer Kaajal (`kaajal@shopsphere.demo` / `demo1234`) with two saved addresses and two cards: a Visa •••• 4821 that is approved, and a Mastercard •••• 0019 that is always declined. Reset it with `python -m backend.db.reset_demo --yes` (backend stopped).

---

## 12. Safety checks, in one place

| Where | Check |
|---|---|
| Identity | JWT on every call; visitors check out only as guests (no account created); saved details, My Orders, wallet and points are customer-only |
| Conversation | Stage machine (`ALLOWED`); LLM output is only a choice from allowed actions; payment isn't one of them |
| Chat input | Card numbers, CVC and expiry masked in the browser **and** the server, before display, storage, guardrails or the LLM. Keyword + NeMo input guardrail. Addresses typed during checkout are withheld |
| Recommendations | Not-sold blocker; LLM relevance check; facts only from the catalog |
| Profile | Secure forms post straight to ShopSphere; server-side validation; ownership checks on every id; tokenized cards |
| Checkout | Authoritative snapshot; live stock/price refresh before paying; SHA-256 tamper hash |
| Payment | Explicit GO AHEAD; consent record; signed single-use 15-minute token; 12 checks; $2,500 policy limit |
| After payment | Order only if authorized; guarded stock decrement; void the authorization if the order can't be created |

---

## 13. Failure paths (what the shopper sees)

| Situation | Result |
|---|---|
| Card declined (e.g. Mastercard •••• 0019) | "Payment wasn't authorized … No order was created". Change card, GO AHEAD again |
| Invalid card / expired / bad CVC | Error in the Secure Payment form, nothing saved |
| Bad ZIP or state | Error in the address form |
| Item sold out in that size/colour | Crossed out; Talkshop lists what's in stock |
| Asked for something not sold | "ShopSphere doesn't sell …", no product cards |
| Brand not carried (e.g. Gucci) | "ShopSphere doesn't carry Gucci, but here are similar options" |
| Typed "go ahead" / "pay now" | Nothing charged; points to the GO AHEAD button |
| Card typed into the chat | Masked; "For your security I didn't keep that" |
| Visitor tries to check out | Sign-in card; cart and chat carry over |
| Order creation fails after approval | Payment voided; "you haven't been charged" |
| LLM slow or unavailable | Deterministic fallback: plain wording, quick paths and the blocker list still work |

---

## 14. Where the code lives

| Piece | Files |
|---|---|
| Talkshop orchestrator, stages | `backend/talkshop/orchestrator.py`, `backend/talkshop/state.py` |
| Quick replies, blockers, masking | `backend/talkshop/parse.py` |
| LLM calls | `backend/talkshop/brain.py` (decide, recommend, describe_image) |
| Merchant tools used by Talkshop | `backend/talkshop/tools.py` |
| ShopSphere services | `backend/shop/catalog.py`, `cart.py`, `profile.py`, `checkout.py`, `payment.py`, `orders.py` |
| Payment pipeline | `backend/routers/authorizations.py`, `backend/agents/greenlight.py`, `backend/agents/payit.py`, `backend/payment/guardrail_engine.py`, `policy.py`, `mock_processor.py`, `signing.py` |
| Identity | `backend/auth/*`, `backend/routers/auth.py`, `frontend/src/auth/AuthContext.tsx` |
| Panel | `frontend/src/components/talkshop/TalkshopPanel.tsx`, `cards.tsx`, `frontend/src/talkshop/*` |
| Store pages | `frontend/src/pages/store/*`, `frontend/src/components/shopsphere/*` |
| Plan and decisions | `docs/demo-1-shopsphere-talkshop-plan.md` |

---

## 15. Added later (Phases 10–12 and follow-ups)

| Feature | How it works |
|---|---|
| **Guest checkout** | Log in / Sign up / **Continue as guest**. `POST /api/checkouts` with `guest: true`; `POST /api/checkouts/{id}/guest-details` (name, email, address). The card is tokenized for that one order only. The order stores `guest_email` and never links to a customer account |
| **Saved-card choice and wallet** | New cards are saved only when **Save this card for next time** is ticked. Customers can pay from the **ShopSphere Wallet** (`pay_with: wallet`, balance-checked) |
| **Confirmation email** | Written to the `email_outbox` table after the order exists. Real addresses are sent in the background over **Gmail SMTP** (`.env` `SMTP_*`, App Password, TLS); made-up addresses stay in the outbox. Status: `sent` / `failed` / `outbox`. An email problem never undoes an order |
| **Shipping and tracking** | Simulated carrier (`shipping.py`). Steps: confirmed → payment completed → processing → packed → shipped (`TRK######`) → out for delivery → delivered |
| **Track order** | Header button. Logged-in customers see their own orders. Guests use **Order ID + email** (`POST /api/orders/track`); any mismatch gets one neutral error |
| **Tracking in Talkshop** | "Where is my order?" / "I placed an order of nike shoes". Logged in: matched against the customer's own orders. Guest: a secure lookup card (Order ID + email). Answered by code, never the AI |
| **Automatic payment** | ~~The Review card placed the order after 3 s unless Stop was pressed~~. Removed on Oct 9: paying always needs a tap on Approve and pay |
| **Loyalty points** | Customers only: 1 point per $1. **100 points = $1 off** the products before tax, with at least $1 payable. Points are spent only when the payment is authorized. Shown in the account menu, at checkout, on the confirmation and in the email; Talkshop answers "how many points do I have?" |
| **"We don't sell that"** | A list of product types ShopSphere doesn't sell, plus an AI relevance check. No unrelated cards are shown |
| **Live guardrail trace** | Every guardrail decision streams as a `guardrail` event (agent, check, pass/blocked/info, protocol). It's shown in Talkshop's info view (ⓘ) along with the Guardrails, Protocols and Email tabs. No card data or emails appear in the trace |

### Protocol mapping (inspired by, not certified)

| Step | Protocol |
|---|---|
| The AI picks a tool from a fixed list | **MCP** |
| Product discovery, identity (log in / guest), order tracking | **UCP** |
| Merchant-owned checkout: create / update / complete; card → token | **ACP** |
| Consent record, signed single-use token, 12 checks, audit log | **AP2** |
| Agents from different companies talking | **A2A**: not implemented (the agents hand off inside one app) |

---

## 16. Part 2: cardmember marketplace + VIC-inspired payments

Branch `demo-1-cardmember-vic`. Everything Visa / Chase below is a **local simulation**: no Visa or Chase API is
called, nothing is VIC compliant, agent tokens are not Visa Token Service tokens.

### 16.1 Who can enter

| Who | Server answer on every marketplace API |
|---|---|
| Signed-in member with an active, eligible Chase **credit card** (Visa or Mastercard fixture) | ✅ everything |
| Signed in, checking/savings only | Browse + Talkshop; buying → 403 `NO_ELIGIBLE_CARD` |
| Not signed in (visitor session) | Browse + Talkshop; buying → 403 `LOGIN_REQUIRED` |

Since Oct 11 the catalog (`/api/products`) and Talkshop are open to every session; member extras (offers, points)
appear only for eligible cardmembers. Talkshop's add-to-cart and checkout steps, the cart/checkout/profile/offers/
rewards/VIC APIs and `payment.confirm` all check eligibility.

`require_cardmember` (`backend/shop/eligibility.py`) guards the catalog, cart, profile, checkout, Talkshop, offers,
rewards and `/api/vic/*` routers. `GET /api/member` is open so the gate page can say *why*. Demo 2's APIs
(`/api/chat`, `/api/authorizations`, `/api/payments`, `/api/cart`, `/api/orders`) are unchanged.

**Sign-up (new customers).** `POST /api/auth/register-cardmember {full_name, email, password, last4}`: name
(case/spacing ignored) + email (case ignored) + last four digits must match one active, unlinked **credit-card**
record in the simulated Chase directory (`issuer_cardholders`, `backend/shop/cardholder_verification.py`). Then the
user is created and all of that cardholder's accounts are linked. Mismatch → one generic `NO_MATCH` message;
checking-only → `NOT_ELIGIBLE`; 5 failures in 15 min → `TOO_MANY_ATTEMPTS`. Existing accounts without a card use
`POST /api/member/verify-card {last4}` with their own name + email. Extra records: `DEMO_CARDHOLDERS` in `.env`.

### 16.2 Offers and rewards (server-authoritative)

- **One discount offer** per checkout, the best one (`offers.best_discount`); applied before points and tax; offer id
  and amount are inside the checkout hash.
- **Bonus points** add up; **cashback** is a simulated statement credit, only after an approved **Visa agent payment**.
- Ledger rows have one id per order (`EARN-`, `BONUS-`, `RDM-`) and `offer_redemptions` is unique per (offer, order):
  a retry can't earn, redeem or credit twice. `orders.finalize` returns an already-finalized order unchanged.

### 16.3 Payment paths (decided by the server from the card on the checkout)

| Path | When | Steps |
|---|---|---|
| `vic` | Chase Visa credit card with an ACTIVE agent token | instruction + simulated passkey → GreenLight DPAT → PayIt 12 checks → 12 VIC controls → purchase credential → gateway → simulated Visa network → simulated Chase issuer → commerce signal → order |
| `vic_setup_required` | Chase Visa, agent payments not active | enrollment first (simulated passkey) |
| `standard` | Any other card (e.g. Chase Mastercard) | GreenLight DPAT → PayIt 12 checks → gateway (card-on-file token) → simulated card network → simulated issuer → order |
| `wallet` | ShopSphere Wallet | internal balance (no gateway) |

### 16.4 The Visa path, step by step

```
Approve and pay (tap, or typed GO AHEAD; no automatic payment timer)
 → POST /api/vic/instructions            instruction pin_… PENDING_AUTHENTICATION + passkey challenge chl_…
 → simulated passkey dialog (browser)    POST /api/vic/instructions/{id}/authenticate → AUTHENTICATED
 → POST /api/checkouts/{id}/confirm {instruction_id}
     GreenLight: consent audit + DPAT (HMAC, single use, 15 min)
     PayIt: 12 DPAT checks
     processor route (ContextVar) → ShopSphere gateway route:
        12 VIC controls → credential pcr_… (single use, 5 min, bound to member/merchant/checkout/amount)
        PaymentGatewayAdapter ──HTTP (Idempotency-Key)──► mock gateway :8100
            simulated Visa network: redeem credential once, check bindings, agent token active
            simulated Chase issuer: account status, available credit, demo decline/timeout
        commerce signal (APPROVED / DECLINED / TIMEOUT) linked to the instruction
     TrackIt: order once → instruction CONSUMED → ORDER_CONFIRMED
```

Lifecycle (`vic_lifecycle_events`): CARD_LINKED → AGENT_TOKEN_ACTIVE → CHECKOUT_CREATED → INSTRUCTION_AUTHENTICATED →
CREDENTIAL_ISSUED → PAYMENT_AUTHORIZED → SIGNAL_RECORDED → ORDER_CONFIRMED; failures: INSTRUCTION_CANCELLED,
INSTRUCTION_EXPIRED, AUTHENTICATION_FAILED, CONTROLS_FAILED, PAYMENT_DECLINED, PAYMENT_TIMEOUT, ORDER_FAILED. Any
other transition is refused.

**Six things kept separate:** consent evidence (audit event) · authenticated instruction (`pin_`) · DPAT (`DPAT_`) ·
agent token (`vat_`) · purchase credential (`pcr_`) · gateway authorization (`gw_` + auth code).

### 16.5 The 12 VIC-inspired controls

1 member session + eligibility · 2 active Visa card fixture · 3 active agent token (same one as the instruction) ·
4 instruction authenticated · 5 merchant matches · 6 checkout id + hash match · 7 amount + currency match ·
8 instruction not expired · 9 transaction count + spending scope · 10 not cancelled/consumed · 11 no duplicate attempt
(no other credential, no paid order) · 12 inventory + final checkout state.

### 16.6 What blocks a payment

| Situation | Result |
|---|---|
| Visa card, no approval yet | 409 `INSTRUCTION_REQUIRED` |
| Agent payments not set up / token revoked | 409 `AGENT_PAY_NOT_ENROLLED` (open instructions cancelled) |
| Passkey cancelled / failed | nothing approved; instruction FAILED; confirm blocked |
| Order changed after approval | instruction CANCELLED (`CHECKOUT_CHANGED`) |
| Tampered merchant / amount, expired instruction | VIC control fails → blocked, no credential |
| Credential replayed / other merchant / other amount / other member / expired | simulated network refuses |
| Issuer decline / network timeout | no order, points kept, signal DECLINED / TIMEOUT, credential revoked on timeout |
| Same request sent twice | gateway returns the original result (`duplicate=true`), one authorization |

### 16.7 Protocol story (accurate)

| | Demo 1 |
|---|---|
| **VIC** | Inspired by its lifecycle; local simulation only |
| **VTS** | Not used; agent tokens are random local values |
| **AP2** | AP2-style consent + DPAT; local HMAC, not production-compliant |
| **ACP** | ACP-style merchant-owned checkout; separate from the PSP credential |
| **UCP** | UCP-inspired discovery/identity/orders over local REST; not full UCP |
| **MCP** | Not used in Demo 1 (fixed in-process tool list) |
| **A2A** | Not needed between Demo 1's six internal agents |
| **Visa Trusted Agent Protocol** | Not used; relevant to an independent external agent (Demo 2) |

---

## 17. General credit-card holders (CH-1 … CH-11)

Plan and progress: [`shopsphere-cardholder-execution-plan.md`](shopsphere-cardholder-execution-plan.md) · audit:
[`shopsphere-cardholder-current-state.md`](shopsphere-cardholder-current-state.md).

| Concern | Where | Rule |
|---|---|---|
| Access | `backend/shop/eligibility.py` | Browse + cart: everyone · checkout: signed-in customer (any issuer) · pay: a credit-card holder |
| Card model | `backend/shop/cards.py` | Issuer (CHASE, CITI, AMEX …) is separate from network (VISA, MASTERCARD, AMEX, DISCOVER) |
| Offers | `backend/shop/entitlements.py` | Every offer is Chase-exclusive and applies only when the order is **paid with** an eligible Chase credit card; 2× points with a Chase card; reasons for every "no" |
| Checkout | `backend/shop/checkout.py` | Benefits revalidated on every change and before paying; `checkout_version`; authoritative `invoice` |
| Consent | `backend/shop/payment.py::confirm` | action PAY + merchant + order + amount + currency + version; stale → 409, nothing charged; no automatic consent; cancellations recorded |
| Routing | `backend/shop/payment_routing.py` | wallet · saved_card (any network) · vic (Visa + agent-pay capable + active token) · provider_handoff |
| New card | `backend/gateway/provider.py` | Simulated Secure Payment Provider: provider-owned page, synthetic outcomes, no card fields; safe result fetched server-to-server; single-use session; save only on request |

