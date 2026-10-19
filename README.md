# Talkshop

**Talk to it. It shops for you.**

Talkshop is a multi-agent **agentic commerce** demo — a conversational shopping assistant that discovers products, manages your cart, authorizes payment, and confirms your order, entirely inside one chat window. No page navigation. No forms to fill. Just conversation.

It's a working prototype of the same problem Stripe's [Agentic Commerce Protocol](https://docs.stripe.com/agentic-commerce/acp), Google's [Agent Payments Protocol (AP2)](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol), and Visa/Mastercard's agent-payment frameworks are solving: how do you let an AI agent spend money on your behalf *without* ever handing it your actual card or credentials?

---

## Demo 1, Part 2 — ShopSphere cardmember marketplace + Visa Intelligent Commerce-inspired payments (branch: `demo-1-cardmember-vic`)

> **Concept demo.** ShopSphere is an original concept marketplace and is not affiliated with JPMorgan Chase or Visa.
> Every identity, account, card, reward, token and payment here is a **local simulation**. No Visa or Chase API is
> called, nothing is "VIC compliant", and the agent tokens are not Visa Token Service tokens.

**General cardholder update (CH-2, in progress — see
[`docs/shopsphere-cardholder-execution-plan.md`](docs/shopsphere-cardholder-execution-plan.md)):** ShopSphere is for
**every credit-card holder**. Anyone can search, browse, use Talkshop and fill a cart; checkout needs a signed-in
ShopSphere account (any card issuer); paying needs a credit card. **Chase status is an entitlement, not the gate:**
offers are exclusive to Chase credit-card holders **and apply only when the order is paid with the Chase card** (CH-3, `backend/shop/entitlements.py`); Chase cards also earn 2× ShopSphere points. The server enforces all of this on every request
(`backend/shop/eligibility.py`), not just the UI. **Talkshop AI** stays docked on the right. An eligible Visa member pays
through a **VIC-inspired flow**: approve the exact order → simulated passkey → GreenLight DPAT + PayIt's 12 checks →
12 VIC-inspired controls → one-time purchase credential → mock gateway → simulated Visa network → simulated Chase
issuer → commerce signal → order. Mastercard members use the standard card path through the same gateway.

Plan and phases (13–19): [`docs/demo-1-shopsphere-talkshop-plan.md`](docs/demo-1-shopsphere-talkshop-plan.md) · how
it all fits together: [`docs/demo-1-system-workflow.md`](docs/demo-1-system-workflow.md) §16.

### Run it (three services)

```powershell
pip install -r requirements.txt; cd frontend; npm install; cd ..     # once

# 1. Mock payment gateway / PSP (simulated network + issuer), its own HTTP service
$env:COMMERCE_DB_PATH = "backend/db/demo1.db"
python -m uvicorn backend.gateway.app:app --port 8100

# 2. ShopSphere backend (second terminal) — calls the gateway over HTTP
$env:COMMERCE_DB_PATH = "backend/db/demo1.db"; $env:MOCK_GATEWAY_URL = "http://127.0.0.1:8100"
python -m uvicorn backend.main:app --port 8000

# 3. Frontend (third terminal)
cd frontend; npm run dev                                              # http://localhost:5173
```

Without `MOCK_GATEWAY_URL` the backend calls the same gateway app in-process through the same HTTP client (the tests
do this). `python -m backend.db.reset_demo --yes` (backend stopped) gives a fresh database with all personas.

### Demo personas (password `demo1234`; one click on the sign-in page)

| | Persona | Simulated accounts | What happens |
|---|---|---|---|
| **A** | Kaajal · `kaajal@shopsphere.demo` | Chase Visa credit •••• 4821 (agent token **ACTIVE**), Chase Mastercard •••• 0019 (issuer declines) | Chase benefits (offers); Visa agent payments with passkey; 12,500 welcome points |
| **B** | Marcus · `marcus@shopsphere.demo` | Chase Mastercard credit •••• 5510 | Chase benefits; **standard** card path (no VIC token services) |
| **C** | Priya · `priya@shopsphere.demo` | Chase checking •••• 7702 only | A customer with **no credit card yet**: can shop and review a checkout, can't pay (`CREDIT_CARD_REQUIRED` / "add a credit card"); no Chase offers |
| **D** | Guest (not signed in) | — | Search, browse, Talkshop and a **cart**; checkout returns 403 `LOGIN_REQUIRED` and Talkshop shows a sign-in / sign-up card. Afterwards the conversation and cart carry on to checkout |
| **E** | Elena · `elena@shopsphere.demo` | Chase Visa credit •••• 4002 (agent token **NOT_ENROLLED**) | Chase benefits; must set up agent payments (simulated passkey) before a Visa agent payment |
| **F** | Dana · `dana@shopsphere.demo` | **Citi Mastercard** •••• 5678 | Scenario 1: buys with her saved card; ShopSphere pricing + points; **no** Chase offers |
| **G** | Ravi · `ravi@shopsphere.demo` | **American Express** •••• 9012 | Scenario 1 on the Amex network; standard card path |
| **H** | Chris · `chris@shopsphere.demo` | **Chase Visa** •••• 1234 (agent token **ACTIVE**) + Citi Mastercard •••• 5678 | Scenario 2: Chase offers + 2× points when paying with the Chase Visa; switching to the Citi card (or wallet) removes them; not enrolled in 3× clothing points (Enroll on the offers page) |

### Demo script: the three cardholder scenarios (CH-1 … CH-11)

> The business message: **ShopSphere supports credit-card holders generally. Chase cardholder status adds eligible
> Chase-specific benefits rather than controlling access to the marketplace. Existing customers can use saved,
> tokenized payment methods; new customers are handed off to a payment-provider-controlled page without exposing raw
> card credentials to Talkshop.** Everything below is simulated. Open Talkshop's info view (ⓘ) to show the live trace.

1. **Scenario 1: a non-Chase cardholder.** Sign in as **F · Dana** (Citi Mastercard ••5678) or **G · Ravi** (Amex
   ••9012). Ask Talkshop for "running shoes under $150", pick a size and colour, then check out. The review shows the
   regular price with **no Chase offer**, 1× points, and the saved card. Tap **Approve & Pay**. Trace: *Customer access*
   (any issuer) → *Cardholder & benefits* → *Payment route* (saved token → simulated Mastercard/Amex network → simulated
   Citi/Amex issuer) → *Consent scope* → 12 PayIt checks → order.
2. **Scenario 2: a Chase cardholder.** Sign in as **H · Chris** (Chase Visa ••1234 + Citi Mastercard ••5678). The same
   shoes now show **15% off with your Chase card** and **2× points**. Switch the payment card to the **Citi Mastercard**:
   the review says *Chase benefits not applied: pay with your Chase credit card* and the total goes up (a new checkout
   version). Switch back, tap **Approve & Pay**, and confirm with the simulated passkey (Visa agent payment, VIC-inspired).
   Offers page: Chris isn't enrolled in "3× points on clothing", so click **Enroll**.
3. **Scenario 3: a new ShopSphere user.** Start signed out. Find shoes with Talkshop and add them to the cart, then
   choose checkout. Talkshop's card offers **Create an account here** (name, email, password, never a card). The
   conversation and cart carry on. Add an address, choose **Pay with a new card**, and tap **Continue to Secure
   Payment**. The **Secure Payment Sheet** slides up over Talkshop (no new window, the conversation stays underneath).
   The card form inside it is the simulated payment provider's own page, which ShopSphere and Talkshop can't read. Enter
   a **test card**: `4242 4242 4242 4242` (Visa), `5555 5555 5555 4444` (Mastercard), `3782 822463 10005` (Amex), any
   future expiry, any CVC. The provider secures it ("✓ Card secured · Visa •••• 4242"), then asks for a **simulated
   payment verification** (Verify payment, Cancel, or *Simulate failed verification*). Once verified you're back in
   Talkshop: GreenLight and PayIt run their checks, PayIt asks the provider to authorize this exact amount, and you see
   "✓ Payment verified · Visa •••• 4242 · $X authorized · Creating your order…" and then the order confirmation.
   Other test cards: `4000 0000 0000 3055` needs no verification, `4000 0000 0000 0002` declines,
   `4000 0000 0000 9995` declines for insufficient funds, `4000 0000 0000 0119` times out, `4000 0000 0000 0101`
   can't be secured. Any other number is refused, so real cards are never used. *Save this payment method* is unticked by
   default; if ticked, ShopSphere keeps only a token, the network, the last 4, the expiry and the name on the card.
   Closing the sheet (✕) or cancelling verification takes you back to the review: nothing is charged.

Failure demos: Kaajal's Mastercard ••0019 (issuer decline); provider decline / timeout test cards or an expired session (no order); a new customer choosing the Wallet ("Why you can't approve yet": the Wallet needs a saved credit card, with a *Pay with a new card instead* button);
change the order in another tab before approving (**STALE_CHECKOUT**, nothing charged); tap Approve twice (one
payment); Account → demo outcomes for gateway decline/timeout/duplicate.

### New customers: create an account

**Sign in → Create account** with a name, email and password: a plain ShopSphere account (no card details asked).
**Optional:** tick "I have a Chase credit card" to also link Chase benefits: enter the **same name and email as your
Chase account** and the **last four digits** of your Chase credit card. ShopSphere checks all three against a
**simulated Chase cardholder directory** (`issuer_cardholders`); a match links the cardholder's Chase cards (Visa →
agent payments set up on first purchase; Mastercard → standard path). Typing a full card number into ShopSphere is
switched off (CH-2); adding a new card will go through a simulated secure payment provider (CH-6). A mismatch gets one generic message (no hint
which field was wrong), 5 failed tries lock that email for 15 minutes, a checking-only match is refused, and a card
can be linked to one account only. Never a full card number or CVV.

Free demo cardholders: Alex Rivera · `alex.rivera@cardmember.demo` · 1188 (Visa) · Jordan Lee ·
`jordan.lee@cardmember.demo` · 2277 (Mastercard) · Sam Patel · `sam.patel@cardmember.demo` · 3366 / 3367 (Visa +
Mastercard) · Taylor Kim · `taylor.kim@cardmember.demo` · 9900 (checking only → refused). Add your own (simulated)
record in `.env`, which git ignores:
```
DEMO_CARDHOLDERS=Your Name|you@gmail.com|4417|Visa;Your Name|you@gmail.com|5562|Mastercard
```
Signed-in customers can link a Chase card later on the **Chase card benefits** page (`/membership`) with the last four
digits; their account name and email must match. A real bank would add a second factor (a one-time code to the
email or phone on file); the last four digits alone don't prove ownership.

### Walkthrough (Persona A)

1. Sign in → pick **A · Kaajal**. The landing shows the member card (•••• 4821, points, agent pay on), member offers and
   offer badges/points on every product (all computed by the server).
2. In Talkshop AI: *"I need running shoes under $150 for everyday running."* → **Select** FlexRun 5 → **9** → **Orange**.
3. The review card shows the **15% member offer**, points to earn, **$10 simulated cashback** (Visa agent-pay offer)
   and "Visa agent payment: you'll confirm with a simulated passkey".
4. **Approve and pay** (or type *"go ahead"*) → the **Simulated passkey authentication** dialog (clearly labelled) →
   **Verify** → ORDER CONFIRMED.
5. ⓘ **Developer trace → Payment**: the 12-step lifecycle from real backend calls — eligibility, agent token,
   checkout, instruction authenticated, DPAT 12/12, VIC controls 12/12, credential issued, gateway request (over HTTP to
   :8100), simulated Visa network, simulated Chase issuer (approved), commerce signal, order confirmed. **Live trace**
   labels each step *Local* or *Simulated*.

Negative paths to show: **Account → Demo controls** (issuer declines / network timeout / duplicate submission) ·
change delivery after approving (instruction cancelled) · **Cancel** or **Simulate a failed check** in the passkey
dialog · **Revoke** the agent token on the Account page · sign in as **C** or open the site signed out.

### Implemented vs simulated

| Real, local code | Simulated (stand-ins) | Would need Visa / Chase access |
|---|---|---|
| Eligibility gate on every marketplace API; offers, rewards ledger, totals, checkout hash; GreenLight DPAT + PayIt 12 checks; payment instructions, controls, credential binding, idempotency, lifecycle, order-once; HTTP call to a separate gateway service | Chase accounts, cards and eligibility (fixtures); passkey (not WebAuthn); agent tokens (not VTS); purchase credential + cryptogram; gateway/PSP, Visa/Mastercard networks, Chase issuer; commerce signals; cashback | Real cardholder authentication, VIC/VTS APIs and credentials, real network tokens and passkeys, real PSP/acquirer, real commerce-signal API, real Chase eligibility data |

---

## Demo 1, Part 1 — ShopSphere × Talkshop (snapshot: `DEMO-1-Shopsphere-3ways`)

> Part 2 replaces Part 1's open store and guest checkout with the cardmember gate. The section below describes Part 1.

### Part 1 overview

> **Real confirmation emails (optional).** Add your mail account to `.env`, restart the backend, and every order's confirmation is emailed for real. Without these settings, emails stay in the demo outbox, viewable on the Track Order page.
> ```
> SMTP_HOST=smtp.gmail.com
> SMTP_PORT=587
> SMTP_SECURITY=starttls
> SMTP_USERNAME=your.address@gmail.com
> SMTP_PASSWORD=your-16-character-app-password
> ```
> For Gmail, use an **App Password** (Google Account → Security → 2-Step Verification → App passwords), not your normal password. Test the settings with `python -m backend.shop.mailer you@example.com`.

**ShopSphere** is a merchant website; **Talkshop** is its built-in shopping assistant, docked on the right of every page. Anyone can browse, fill a cart and chat with Talkshop without an account. Logging in (or signing up) is needed only to check out, and the visitor's cart and conversation come along. Talkshop understands the request and decides what should happen; ShopSphere's own services (catalog, inventory, cart, checkout, payment, orders) do it. Payment only ever runs when the customer taps **GO AHEAD**.

Full plan, phases, decisions and checklist: [`docs/demo-1-shopsphere-talkshop-plan.md`](docs/demo-1-shopsphere-talkshop-plan.md).

### Run it

```powershell
pip install -r requirements.txt            # once
cd frontend; npm install; cd ..            # once
# .env needs GOOGLE_API_KEY (see .env.example)

# Backend, on its own demo database (your other local data is untouched)
$env:COMMERCE_DB_PATH = "backend/db/demo1.db"
python -m backend.db.reset_demo --yes      # fresh catalog, Kaajal's saved details, no orders
python -m uvicorn backend.main:app --port 8000

# Frontend, in a second terminal
cd frontend; npm run dev                   # http://localhost:5173
```

Open http://localhost:5173: the store opens straight away, no login needed. Log in from the header, or at checkout, with the demo account (**Use demo account**): `kaajal@shopsphere.demo` / `demo1234`. Kaajal has two saved addresses and two cards: **Visa •••• 4821** (approves) and **Mastercard •••• 0019** (always declines, for the decline demo).

To reset between demos: stop the backend, run `python -m backend.db.reset_demo --yes` with the same `COMMERCE_DB_PATH`, start it again.

### Demo scripts

| Script | What to do | What it shows |
|---|---|---|
| **A: the spec** | In Talkshop: *"I need running shoes under $150 for everyday running."* → **Select** Runner Pro X → size **8** → **Black** → **Yes, checkout** → **GO AHEAD** | Top 3 on the chat and the page, size/colour checked against stock, ShopSphere cart, review card ($129 + $10.64 = **$139.64**), authorized, order **SS-#####** |
| **B: declined card** | As A, then on the review card **Change** payment → Mastercard •••• 0019 → GO AHEAD → change back to Visa → GO AHEAD | Payment not authorized, **no order created**, retry on the same checkout |
| **D: no account yet** | Open the store logged out → add a wallet → in Talkshop pick Runner Pro X, 8, Black → **Yes, checkout** → sign-in card → **Use demo account** → **Log in & continue** → GO AHEAD | Browsing, cart and chat without an account; login only at checkout; the cart and conversation carry over and Talkshop continues straight to Review |
| **E: guest checkout** | Logged out → add an item → Cart → **Checkout selected** → **Continue as guest** → name, email, address → card (used once) → **Place order** (or in Talkshop: sign-in card → **Continue as guest**) | No account created; Order **SS-#####**; "confirmation sent to …"; Track Order opens with the order |
| **F: track an order** | Footer → **Track order** → Order ID + checkout email → **View confirmation email** → **Next shipping step** (demo control) until Shipped | Wrong email → "We could not find an order matching…"; steps Confirmed → … → Delivered; tracking ID **TRK######** appears at Shipped (simulated shipping) |
| **H: track in Talkshop** | Not logged in, tell Talkshop *"I placed order SS-12345, show me the order details and tracking"* → enter the email in the secure card → **Show my order** | Order details + shipping progress in the chat; a wrong email is refused; a logged-in customer's own order shows straight away |
| **G: wallet / no saved card** | Kaajal at checkout: Payment → **Change** → **ShopSphere Wallet** → Place order. A new account with no card: the card form shows **Save this card for next time** (unticked) and **Or pay with your ShopSphere Wallet** | Pay with wallet balance; cards are saved only when ticked |
| **C: site + Talkshop** | Shoes → FlexRun 5 → **Ask Talkshop about this** → size/colour in chat → Keep shopping → add a wallet on the website → Cart: select only the wallet → Checkout selected → Place order | One cart for both, page-aware chat, website checkout of selected items, both orders in My Orders |

Things worth pointing out: typing *"go ahead"* never pays (only the button does) · the header cart badge moves when Talkshop adds items · **ⓘ How Talkshop works** in the panel shows the live agent trace (VibeCheck → SneakPeek → CartUp → GreenLight → PayIt → TrackIt) · light/dark toggle in the header.

### Notes

- Catalog: 60 products / 385 SKUs in `data/shopsphere_catalog.json`, built by `scripts/build_shopsphere_catalog.py`; one studio photo per colour in `frontend/public/catalog/` (Pixabay licence, sources in `data/catalog_images.json`). Regenerating photos needs its own virtualenv — see `scripts/fetch_catalog_photos.py`.
- Product search reads only the ShopSphere catalog (`CATALOG_SOURCES=shopsphere`); the Shopify/BestBuy/DummyJSON adapters are kept but switched off.
- Tests run on their own temporary database: `python -m pytest tests/`.

---

## Version 2 — Conversational Commerce (current branch: `v2-upgrade`)

Version 2 transforms TalkShop into a fully conversational single-screen experience. The entire purchase flow — search → add to cart → checkout → pay → order confirmation — happens inline in the chat. The right panel shows a live real-time trace of the 6-agent pipeline as it runs.

### What's new in v2

**LLM-driven everything**
- Every message — shopping queries, follow-ups, general questions, cart commands — is handled by the LLM
- "which one is better for trail running?" → answered from the products already shown in the session
- "add the first one to cart" / "remove it" / "show me my cart" → LLM understands and executes the action
- "what else do you have?" → surfaces products 6–10 from the original search without a new query
- Any off-topic question is answered naturally ("who invented the internet?" → real answer)

**Inline purchase flow — zero page navigation**
1. Agent recommends a product and asks "Want me to add it to your cart?"
2. Say "yes" or "add it" → inline cart card appears: added product, full cart list, total, action buttons
3. "Proceed to Checkout →" → order summary card appears in chat with product details, tax, shipping
4. "Confirm & Pay" → live pipeline streams GreenLight authorization → PayIt execution → TrackIt confirmation
5. "Order Confirmed" card shows inline with order ID, amount, loyalty points earned

**Live agent pipeline panel (right)**
- 6-node horizontal flowchart: VibeCheck → SneakPeek → CartUp → GreenLight → PayIt → TrackIt
- Each node animates live: idle (grey) → running (amber pulse) → done (green) → error (red)
- Click any agent to expand: key-value output table + step-by-step timeline
- Idle agents show their role description when expanded — the panel explains itself
- Accumulates all agent steps across the full session (search + checkout + payment in one view)

**Session persistence**
- Navigate to /cart or /dashboard and return — the conversation is exactly where you left it
- In-progress checkout survives navigation; order summary card re-appears, card details pre-filled
- Completed payment shows the order confirmation card, not the original product results

---

## How it works

A user's message moves through six named agents, each with one job:

| # | Agent | Role | Uses an LLM? |
|---|-------|------|---------------|
| 1 | **VibeCheck** | Classifies your message, answers any question, extracts shopping intent, writes recommendations | Yes — intent, prose, action routing |
| 2 | **SneakPeek** | Searches 3 merchant catalogs in parallel, filters and ranks results | No — deterministic |
| 3 | **CartUp** | Adds items, builds the checkout (subtotal, tax, shipping, tamper hash) | No — deterministic |
| 4 | **GreenLight** | Issues a scoped, single-use DPAT payment token after 12 guardrail checks | No — deterministic |
| 5 | **PayIt** | Independently re-verifies the token and executes the charge | No — deterministic |
| 6 | **TrackIt** | Confirms the order, updates loyalty balance | No — deterministic |

**Core design rule:** anything involving money, inventory, or a guardrail decision is plain, testable Python — never the LLM. The LLM classifies intent, routes actions, and writes prose. It never invents a price and never authorizes a payment.

**Trust boundary:** agents never see a card number, CVV, or bank credential — only an opaque DPAT token. GreenLight issues a token scoped to one merchant, one order, one amount, with a 15-minute TTL and single-use flag. PayIt does **not** trust that token just because GreenLight issued it — it independently re-runs 12 guardrail checks (merchant match, amount ceiling, expiry, single-use enforcement, tamper-hash verification, and more) before any charge happens. Every step is written to an audit trail.

Discovery (VibeCheck → SneakPeek) runs as a [LangGraph](https://github.com/langchain-ai/langgraph) state machine, streamed live to the UI over SSE. Checkout and payment (CartUp → GreenLight → PayIt → TrackIt) run as direct, auditable function calls behind REST endpoints.

---

## Features

### Conversation & intelligence
- **Any question answered** — LLM replies naturally to general knowledge, advice, or chitchat, not a scripted fallback
- **Natural language intent extraction** — "blue Nike size 10 under $100" → structured `ShoppingIntent`
- **Product follow-up** — "which one is better?", "is the first one waterproof?", "compare the top two" answered from session context
- **LLM-driven cart operations** — "add the first one", "remove it", "show me my cart", all understood by the LLM
- **Affirmative replies** — "yes", "ok", "sure" after the agent's CTA triggers the correct action
- **Context merging** — "make it Nike" updates the previous search without restarting
- **Show more** — "what else?" returns products 6–10 from the original search, no new query
- **Streaming text** — recommendation appears word-by-word at 40 ms/word with a live cursor
- **Voice input** — real audio waveform level meter (32-bar visualizer) while mic is active
- **Image-paste search** — paste an image; AI infers category and attributes to find similar items

### Product display
- **137 products** across 11 categories, 73 brands, 3 merchants, $14.99–$1,399
- **Match checkmarks** — every card shows which constraints were verified (✓ blue ✓ size 10 ✓ under $100)
- **Sort bar** — Best Match · ↓ Price · Top Rated · Fastest — client-side re-sort per turn
- **Same-model deduplication** — highest-scored variant shown when multiple sizes/colors match
- **Follow-up chips** — auto-generated smart suggestions after each result set
- **Delivery urgency** — ⚡ Arrives tomorrow, computed from today's date
- **Merchant trust badges** — Verified / Premium tier, ships-in time, return policy

### Compare mode
- Select 2–4 products → Compare button in floating bar
- Side-by-side spec table with best price / top rating / fastest delivery highlighted
- AI Recommendation panel — LLM picks the winner with numbered reasoning and trade-off bullets

### Cart & checkout (inline in chat)
- **Inline cart card** — added product + full cart list + total + View Cart / Proceed to Checkout buttons
- **Inline order summary** — product, subtotal, tax, shipping, card selection, Confirm & Pay
- **Animated payment pipeline** — live GreenLight → PayIt → TrackIt progress in the chat turn
- **Inline order confirmation** — Order ID, amount, loyalty points, collapsible order tracker
- **Guest checkout** — secure card entry modal; card details never appear in chat
- **Session card memory** — guest card pre-filled for repeat checkouts in the same session
- **Enter key submits** — the card details form responds to Enter

### Payment security (DPAT model)
- Token scoped to one merchant, one amount, one checkout hash, one use, 15-minute TTL
- Agent holds only the token ID — card number and CVV never enter the AI pipeline
- 12 deterministic guardrail checks before payment executes
- Any check failure → `PAYMENT_BLOCKED` with reason code, logged to audit trail

### Session & persistence
- **Full conversation history** — ChatGPT-style session sidebar, resumable
- **Cross-navigation restore** — returning from /cart or /dashboard shows the exact conversation state
- **Checkout state persisted** — in-progress checkout survives tab navigation
- **Order confirmation persisted** — completed payment shows the order card, not raw product cards

### Agent pipeline panel
- **6-agent horizontal flowchart** with animated arrows and status badges
- **Per-agent detail** — click to expand; shows key-value output table + step trace
- **All agents always visible** — idle agents show their role description when expanded
- **Session accumulation** — complete step history across all turns: search + payment in one view
- **Pipeline progress bar** — N / 6 with gradient fill

### Auth & identity
- Known Customer / Guest two-CTA landing page
- JWT + bcrypt registered accounts
- Guest checkout with session-scoped card memory
- Loyalty points: 1 pt per $1 on every confirmed order

---

## Tech stack

**Backend:** FastAPI · Python 3.12 · SQLAlchemy · SQLite · Pydantic v2 · LangGraph · Gemini 2.0 Flash Lite (+ Ollama fallback) · NeMo Guardrails · Guardrails AI · PyJWT · bcrypt · Arize Phoenix

**Frontend:** React 18 · TypeScript · Vite · Tailwind CSS · Framer Motion · React Router v6 · Lucide React · SSE (EventSource)

---

## Project structure

```
backend/
  agents/        VibeCheck, SneakPeek, CartUp, GreenLight, PayIt, TrackIt
  graph/         LangGraph discovery workflow + per-session state
  payment/       Guardrail engine, HMAC signing, mock processor
  routers/       FastAPI endpoints (auth, chat, cart, checkout, payments, ...)
  db/            SQLAlchemy schema + seeding
  merchants/     Local catalog + Shopify/DummyJSON adapter stubs
frontend/
  src/pages/       Chat (main), Cart, Dashboard, Login
  src/components/  AgentTrailPanel, InlineCheckout, CartDrawer, ProductCard, ...
  src/api/         Typed fetch clients per backend router
data/            Seed catalog (products.json, merchants.json)
tests/           pytest suite
docs/product.md  Full product document with v2 feature details
status.md        Version-by-version build log
plan.md          v2 task tracker and build diary
```

---

## Getting started

### Backend

```bash
pip install -r requirements.txt
cp .env.example .env        # set GOOGLE_API_KEY (see .env.example)
python -m backend.db.init_db
uvicorn backend.main:app --reload --port 8000
```

LLM priority: Gemini 2.0 Flash Lite if `GOOGLE_API_KEY` is set → Ollama + Llama 3.2 if running locally → setup error.

### Frontend

```bash
cd frontend
npm install
npm run dev                 # http://localhost:5175
```

### Try it

```
email:    demo@talkshop.io
password: demo1234
```

Then ask: *"Find running shoes, size 10, under $100"* — or anything else.

---

## Running tests

```bash
python -m pytest tests/ -v
```

---

## Limitations

This is a proof of concept, not a production payment system:

- **Payment is simulated** — no real card network or bank connection; no real money moves
- **Merchant search is a seeded local catalog** — the Shopify/DummyJSON adapters exist but have no live credentials
- **The MCP tool layer** is structured correctly but called in-process, not over a real MCP transport
- **Multi-item checkout** processes each item as its own order rather than one combined order

See [`docs/product.md`](docs/product.md) for the full product document, [`status.md`](status.md) for the detailed build log, and [`plan.md`](plan.md) for the v2 task tracker.
