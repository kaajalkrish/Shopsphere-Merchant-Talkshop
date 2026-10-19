# ShopSphere × Talkshop — Cardholder Current-State Audit (Phase 0)

> Branch audited: `demo-1-cardmember-vic` @ `1493937` ("Browsing for everyone…")
> Audit date: 2026-10-13. Read-only audit: **no implementation code was changed.**
> Companion plan: [`shopsphere-cardholder-execution-plan.md`](shopsphere-cardholder-execution-plan.md)

Everything Chase-, Visa- or issuer-related in this repository is a **local simulation**: no Chase, Visa,
Mastercard or Amex system is called anywhere. The code and UI already say so in most places, and this audit
does not change that.

---

## 0. Executive summary

| Question | Answer |
|---|---|
| What does the project do today? | ShopSphere is a merchant website (React + FastAPI + SQLite) with **Talkshop**, an embedded AI shopping assistant. Anyone can browse and chat. **Cart, checkout, offers, rewards, orders and payment are restricted to "eligible cardmembers"**: signed-in users holding an **active Chase credit card** fixture. |
| Is Chase the access gate? | **Yes.** `backend/shop/eligibility.py` hard-codes `ISSUER = "Chase"` and is enforced as a FastAPI dependency on every marketplace router, again inside `payment.confirm`, inside the Talkshop orchestrator, and in VIC control 1. |
| Is Visa hard-coded? | **Partly.** No prompt says "your Visa card". The prompts say "your **Chase** credit card". Visa is hard-coded as the **only network that gets the VIC (agent-payment) path**, as the default network for fixtures and tests, as the preferred default card at sign-up, and in the `visa_agent_pay` cashback offer. Non-Visa cards already route to a separate "standard" path, so Mastercard is **not** forced through VIC. |
| Is issuer separated from network? | **Only on `card_accounts`** (issuer + network columns). `payment_methods` (the saved card actually used to pay) has `brand` (= network) but **no issuer**. Issuer is inferred by joining to `card_accounts`, which only Chase cards have. |
| Are there "standard" vs "Chase-exclusive" offers? | **No.** All four offers are "member offers" with audience `all_cardmembers` or `visa_agent_pay`. Since only Chase cardholders are members, **every offer is effectively Chase-exclusive by access, not by entitlement**. |
| Is the benefit revalidated at checkout? | **Partly.** The best discount is recomputed by the server on every checkout change and again right before payment (`checkout._recalculate`). But eligibility is **not tied to the selected payment card**. Switching to a non-Chase card would not remove a Chase offer, because the offer rule doesn't look at the card. |
| Raw PAN/CVC? | **Yes, a gap.** `POST /api/me/payment-methods` takes the full card number + CVC. `CardForm` collects them in the ShopSphere UI (checkout page **and** inside the Talkshop panel). They are Luhn-checked and discarded, but the **ShopSphere backend receives them**. That conflicts with the new rule and must be replaced (see §16). |
| Consent bound to amount? | **VIC path: yes** (instruction binds checkout hash + amount). **Standard path: no.** `POST /api/checkouts/{id}/confirm` sends only `{consent: true}`, and the server re-prices and charges the *current* total. There is no check that it equals the total the customer saw. |
| Tests | Backend `pytest`: **390 passed** (baseline, §17). Frontend has no unit tests or lint. `tsc --noEmit` passes. |

---

## 1. Current architecture

```
Browser (React 19 + Vite, frontend/)
  ├─ ShopSphere website pages (pages/store/*)          ── REST ──┐
  └─ Talkshop panel (components/talkshop/*)             ── SSE ───┤
                                                                   ▼
FastAPI backend (backend/main.py)
  routers/ ── products, shop (cart/profile/checkout/confirm), member, auth, vic, talkshop, payments, authorizations …
  talkshop/orchestrator.py ── deterministic stage machine + LLM (brain.py) choosing only allowed actions
  shop/ ── merchant services (no AI): catalog, cart, checkout, offers, rewards, eligibility,
           cardholder_verification, payment (confirm), orders, profile, shipping, notifications, vic (path chooser)
  agents/ ── VibeCheck, SneakPeek, CartUp, GreenLight, PayIt, TrackIt (from the original v1/v2 pipeline)
  payment/ ── policy, DPAT signing, 12-check guardrail engine, processor_route (ContextVar), mock_processor
  vic/ ── VIC-*inspired* local simulator (agent token, simulated passkey, instruction, 12 controls,
          purchase credential, commerce signals)
  gateway/ ── mock PSP gateway (own FastAPI app; in-process or :8100) → simulated network → simulated issuer
  db/ ── SQLAlchemy schema + seed (init_db.py) on SQLite
```

Key principle already in place, and worth keeping: **the LLM never computes money or chooses to pay.**
Totals, offers, points and the payment path are computed in `backend/shop/*`. Payment happens only for an
explicit `go_ahead` action or a website "Approve and pay" tap.

---

## 2. Current login / cardmember flow

| Path | Endpoint / file | Behaviour |
|---|---|---|
| Visitor (anonymous) | `POST /api/auth/visitor` — `backend/routers/auth.py:75` | Creates an `is_guest` user; can browse, chat with Talkshop. Cannot add to cart (blocked by the cardmember gate). |
| Persona sign-in | `POST /api/auth/login` — `auth.py:115`; persona buttons in `frontend/src/pages/Login.tsx:17-26` | Personas A–E (password `demo1234`). Visitor cart/conversation merged into the account (`_adopt_visitor`). |
| Cardmember sign-up | `POST /api/auth/register-cardmember` — `backend/routers/member.py:97` | Name + email + **last 4** must match a record in the **simulated Chase directory** (`issuer_cardholders`). On match: user created, all that person's Chase accounts linked as `card_accounts` + masked `payment_methods`. 5 failures / 15 min lockout. Checking-only is refused. |
| Verify card (existing account) | `POST /api/member/verify-card` — `member.py:124`; UI `MemberGate.tsx` | Same match using the account's own name/email + last 4. |
| Generic register | `POST /api/auth/register` — `auth.py:92` | Still exists and creates a plain user, but that user gets `NO_ELIGIBLE_CARD` everywhere (test `test_new_signup_without_a_card_is_not_eligible`). |
| Guest checkout | `POST /api/auth/guest`, `/api/checkouts/{id}/guest-details` | Phase 10 feature, **retired in Part 2**: the cardmember gate blocks it (test `test_guest_checkout_is_retired_in_part2`). Code is still present. |
| Who am I | `GET /api/member` — `member.py:62`; `frontend/src/member/MemberContext.tsx` | Returns `eligible`, `code` (`LOGIN_REQUIRED` / `NO_ELIGIBLE_CARD`), masked accounts, primary card, points, agent-pay status. The whole UI keys off `eligible`. |

**Last four digits are used as a matching factor at sign-up.** The code documents that this is not proof of
ownership (`cardholder_verification.py:18-19`). The new model treats last4 strictly as display metadata.

---

## 3. Where the Chase-only restriction exists

### Backend (enforcement)

| File:line | What |
|---|---|
| `backend/shop/eligibility.py:23` | `ISSUER = "Chase"` |
| `backend/shop/eligibility.py:26-28` | Messages: "marketplace is for eligible Chase credit-card members" |
| `backend/shop/eligibility.py:32-36` | `eligible_cards()` filters `issuer == "Chase"`, `credit_card`, `active`, `marketplace_eligible` |
| `backend/shop/eligibility.py:39-59` | `check()` / `require_cardmember` dependency: **the access gate** |
| `backend/routers/shop.py:46` | Router-level `Depends(require_cardmember)` → cart lines, profile (addresses, cards), checkouts, confirm, guest-details, **and order tracking** |
| `backend/routers/vic.py:33` | Router-level gate on all VIC endpoints |
| `backend/routers/member.py:68-77` | `/api/offers`, `/api/rewards` gated |
| `backend/routers/products.py:29-33` | Offer badges/points shown only if `eligibility.check()` passes |
| `backend/shop/payment.py:84-87` | Second gate inside `confirm()` |
| `backend/talkshop/orchestrator.py:632-634` | `s.member_ok = eligibility.check(...) is None` each turn |
| `backend/talkshop/orchestrator.py:334-336, 361-375, 380-381` | Add-to-cart and checkout stop at `_membership_needed` ("sign in as a Chase credit-card member") |
| `backend/talkshop/orchestrator.py:764` | Points question answered with "points are for cardmembers… eligible Chase credit card" |
| `backend/vic/credentials.py:46` | VIC control 1 calls `eligibility.check()` (Chase gate) |
| `backend/shop/cardholder_verification.py` | Whole module: the simulated **Chase** directory; `link()` hard-codes `issuer="Chase"` (`:148`) and `product_name="Chase …"` |
| `backend/routers/member.py:105, 117, 136` | "verify your Chase card", audit `issuer: "Chase (simulated)"` |
| `backend/db/init_db.py:181-245` | `PERSONAS`: every account seeded with `issuer="Chase"` |
| `backend/shop/eligibility.py:80-108` | `link_demo_card()` (seed/tests) always creates `issuer=ISSUER` (Chase) |
| `backend/gateway/issuer.py:17` | "Simulated Chase issuer" for any card with a `card_accounts` row, else generic |
| `backend/shop/vic.py:35, 39, 41` | Path labels say `issuer: "Chase (simulated)"` |
| `backend/shop/rewards.py`, `checkout.py` | Points work for any customer in code, but are only reachable by Chase members because of the gate |

### Frontend (presentation; the server is the real control)

| File | What |
|---|---|
| `frontend/src/App.tsx:38-43` | `MemberOnly` wrapper: non-eligible users see `MemberGate` on cart/checkout/orders/offers/rewards/account |
| `frontend/src/pages/store/MemberGate.tsx` | "Sign in as a cardmember to buy"; "Have a Chase credit card? Verify it"; eligibility table |
| `frontend/src/pages/Login.tsx:10-26, 76-135` | "For eligible Chase credit-card members"; sign-up fields "as on your Chase account", "Last 4 digits of your Chase credit card" |
| `frontend/src/pages/store/Home.tsx:52-123` | "Chase credit-card members get member offers…", "Verify a Chase card" |
| `frontend/src/pages/store/Product.tsx:149-156` | Buy button replaced with "Verify a Chase card to buy" / "Sign in as a cardmember to buy" |
| `frontend/src/components/shopsphere/StoreHeader.tsx` | Member nav vs public nav based on `eligible`; "Cardmember" badge |
| `frontend/src/components/talkshop/cards.tsx:514-549` | `SignInCard`: "ShopSphere checkout is for Chase credit-card members" |
| `frontend/src/components/talkshop/TalkshopPanel.tsx:81, 285-286` | "Your cardmember shopping assistant"; guardrail list "eligible Chase credit card" |
| `frontend/src/talkshop/TalkshopContext.tsx:47-95` | Resumes add-to-cart after sign-in only if `eligible` |
| `frontend/src/layouts/StoreLayout.tsx:42-53` | "The cardmember marketplace" |
| `frontend/src/index.css:36-55` | Colour token comments "Chase blue" (cosmetic only) |

---

## 4. Is Visa hard-coded?

No UI asks for "the last four digits of your **Visa**". It asks for the last four of your **Chase** credit card.
Visa assumptions that do exist:

| File:line | Assumption | Severity |
|---|---|---|
| `backend/shop/vic.py:30` | VIC path only when `acct.network == "Visa"` **and** a `card_accounts` row exists (i.e. **Chase** Visa). Issuer and network are conflated: a non-Chase Visa can never use VIC, and VIC is implicitly a Chase feature | Medium: correct routing for non-Visa, but couples VIC to Chase |
| `backend/vic/enrollment.py:24-30` | `_visa_account`: "Agent payments need an active, eligible **Chase Visa** credit card" | Medium |
| `backend/vic/credentials.py:25, 47-48` | Control 2 "Active Visa card (fixture)" checks `network == "Visa"` (correct for a Visa-specific path) | OK (VIC is Visa-specific by definition) |
| `backend/vic/instructions.py:92` | Log text "Chase Visa credit ending …" | Low |
| `backend/shop/offers.py:34-37, 50-52, 61-65, 121-131` | `OFF_AGENTPAY10` audience `visa_agent_pay`; `_has_visa()` | Medium: a network-specific offer mixed with "member" offers |
| `backend/shop/eligibility.py:80` | `link_demo_card(network="Visa")` default | Low (fixtures) |
| `backend/shop/cardholder_verification.py:139` | Sign-up makes the **Visa** the default card (`r.network != "Visa"` sort) | Low |
| `tests/conftest.py:44-63` | `register_member(network="Visa")`, `member_headers` = Chase Visa | Low (tests) |
| `frontend/src/api/member.ts:10` | `network: "Visa" \| "Mastercard" \| null`: type excludes Amex/Discover | Low |
| `backend/db/schema.py:158, 179` | Column comments "Visa \| Mastercard" | Cosmetic |
| `frontend/src/member/PasskeyContext.tsx:49`, `TalkshopPanel.tsx:259`, `cards.tsx:640` | Fallback display text `?? "Visa"` / "your Visa" | Low |
| `frontend/src/pages/store/Member.tsx:36`, `Account.tsx:60` | Agent-pay UI shown only for `network === "Visa"` | OK (VIC-specific UI) |

What is already network-neutral:
* `backend/shop/profile.py:118-127`: `_brand()` recognises Visa, Mastercard, Amex and Discover.
* `backend/gateway/network.py:50-59`: `card_on_file` labels the network by `card.brand` ("Simulated Mastercard network").
* The `standard` payment path (`shop/vic.py:40`) is used for every non-Visa card. **Mastercard is not forced through VIC** (test `test_mastercard_member_uses_standard_path`).

---

## 5. Existing card / payment data model

`backend/db/schema.py`

| Table | Purpose | Key columns | Notes |
|---|---|---|---|
| `payment_methods` (`PaymentMethod`, :123) | The saved / one-time card used to **pay** | `payment_method_id`, `user_id`, `brand` (=network), `last4`, `exp_month/year`, `cardholder_name`, `token_ref` (random `tok_…`, never derived from digits), `behaviour` (approve/decline demo), `is_default`, `saved` | **No `issuer`, no `status`**, no provider/source field |
| `card_accounts` (`CardAccount`, :145) | The **issuer-side relationship** (simulated Chase account) | `account_id`, `issuer` (default "Chase"), `account_type` (credit_card/checking), `product_name`, `network`, `last4`, `status`, `marketplace_eligible`, `payment_method_id` (link), `credit_limit`, `available_credit` | Only Chase rows exist. Doubles as the simulated issuer's ledger (available credit holds) |
| `issuer_cardholders` (:168) | Simulated **Chase** cardholder directory for sign-up | `record_id`, `full_name`, `email`, `account_type`, `network`, `last4`, `status`, `claimed_by` | **No issuer column**: implicitly Chase |
| `card_verification_attempts` (:185) | Lockout counter | `key` (email), `succeeded` | |
| `agent_tokens` (:482) | VIC-inspired agent token per card | `user_id`, `card_account_id`, `payment_method_id`, `agent_id`, `status` | Keyed on `card_account_id` → Chase only |
| `passkey_challenges`, `payment_instructions`, `payment_credentials`, `commerce_signals`, `vic_lifecycle_events` | VIC simulator | instruction binds `checkout_id`, `checkout_hash`, `amount`, `currency`, `merchant_id`, expiry | Good binding model; reusable concept |
| `gateway_authorizations` | Mock gateway idempotency store | `idempotency_key` (unique), `request_hash`, `response_json`, `instrument_type` (`vic_credential`/`card_on_file`) | Replay returns original response; key reuse with a different body → 409 |
| `payment_authorizations`, `delegated_tokens` | GreenLight consent + DPAT | `order_id` (=payment_ref), `max_amount`, `currency`, `checkout_hash`, HMAC `signature`, single use | |
| `wallets` | ShopSphere Wallet (stored value, $1000 demo) | | Internal, no network |

Mapping to the requested `PaymentCard` concept:

| Requested field | Today |
|---|---|
| `card_id` | `payment_methods.payment_method_id` |
| `issuer` | **missing** on payment_methods; only `card_accounts.issuer` |
| `network` | `payment_methods.brand` / `card_accounts.network` |
| `last4` | both |
| `token/reference` | `payment_methods.token_ref` (opaque) |
| `status` | **missing** on payment_methods; `card_accounts.status` |
| `eligible_for_standard_checkout` | **missing** (implicitly: any saved card) |
| benefit / entitlement metadata | `card_accounts.marketplace_eligible` (used as the *access* flag); agent-token status |

---

## 6. Existing customer / profile model

* `users` (`User`): `user_id`, `name`, `email` (unique), `password_hash` (bcrypt), `status`, `is_guest`.
  JWT sessions (`backend/auth/security.py`), `CurrentUser.is_visitor` = `is_guest`.
* `addresses`, `payment_methods`, `wallets`, `loyalty_points`, `loyalty_transactions` per user.
* Profile service: `backend/shop/profile.py` (addresses, cards; `add_card` takes PAN + CVC, §16).
* Personas (`backend/db/init_db.py:181-245`, `cardholder_verification.py:69-75`):

| Persona | user | Simulated accounts | Today |
|---|---|---|---|
| A Kaajal | USR001 | Chase Visa 4821 (agent token ACTIVE), Chase Mastercard 0019 (declines) | Eligible, VIC path |
| B Marcus | USR002 | Chase Mastercard 5510 | Eligible, standard path |
| C Priya | USR003 | Chase checking 7702 | **Not eligible** (no credit card) |
| D Guest | — | — | Visitor, browse only |
| E Elena | USR004 | Chase Visa 4002 (no agent token) | Eligible, VIC setup required |
| Sign-up directory | — | Alex (Visa 1188), Jordan (MC 2277), Sam (Visa 3366 + MC 3367), Taylor (checking 9900) + `.env` `DEMO_CARDHOLDERS` | All Chase |

**There is no non-Chase cardholder anywhere in the fixtures.**

---

## 7. Standard offer logic

**There is none.** No offer is available to a non-Chase shopper, because the shopper can't reach cart/checkout.
Non-member product cards get `member: null` (`products.py:36-40`). The closest things to "standard" are the
ShopSphere loyalty points (1 pt / $1, 100 pts = $1, `checkout.py:97-110`) and free standard shipping.
Both are reachable only by Chase members today.

## 8. Chase-specific offer logic (today's "member offers")

`backend/shop/offers.py`. Everything here is deterministic and server-side, and the LLM never supplies amounts.

| Offer | Kind | Scope | Audience | Limit |
|---|---|---|---|---|
| `OFF_RUN15` 15% off running shoes | discount_percent | shoes / running,trail | all_cardmembers | — |
| `OFF_AUDIO20` $20 off audio ≥ $150 | discount_amount | electronics / headphones,earbuds,speakers | all_cardmembers | 1 per member |
| `OFF_STYLE3X` 3× points on clothing | bonus_points | clothing | all_cardmembers | — |
| `OFF_AGENTPAY10` $10 back with Visa agent payments | cashback (simulated statement credit) | ≥ $50 | visa_agent_pay | 1 per member |

Rules: one best discount per checkout (`best_discount`), bonus points stack, cashback only on the VIC path,
`max_uses_per_member` counted from `offer_redemptions`. Redemptions are recorded idempotently at order finalize.

Gaps against the new requirements:
1. No `issuer` / `requires_issuer` / `requires_payment_card` field on `member_offers`; the audience is just a string.
2. `entitled()` doesn't consider **which card is selected for payment**. Only the cashback rule looks at the payment *path*.
3. No dates (`starts_at`/`ends_at`), no enrollment concept, no merchant field (single merchant today).
4. "Member" is used to mean both "allowed in" and "entitled to Chase benefits".

---

## 9. Existing checkout architecture

`backend/shop/checkout.py`: the **merchant-authoritative** checkout ("ACP-style").

* `create_checkout` snapshots cart lines at live prices; default address and default card.
* `_recalculate` (:64-94): live price/stock → subtotal → **best member offer** → points (capped) → tax 8.25%
  → shipping → total → **SHA-256 `checkout_hash`** over lines, address, delivery, `payment_method_id`,
  `pay_with`, points, offer, totals and currency. Runs on create, every update, and `refresh()` right before payment.
* `update_checkout` (:167): delivery, address, **payment card**, wallet, points, quantities → recalculate +
  `_invalidate_instructions` (cancels any open VIC instruction for the old hash).
* Status: `open → consented → paid` (or `cancelled`; back to `open` after a decline).
* `snapshot()` (:276) returns the review view including `offer`, `bonus_points`, `cashback`, `payment_path`,
  `issues`, `ready` and `checkout_hash`.

What exists vs. the target lifecycle:

| Target | Today |
|---|---|
| CART | `cart_items` (SKU lines) |
| CHECKOUT CALCULATED | `checkouts` row, server totals + hash |
| PENDING ORDER / INVOICE | **No separate invoice object.** The checkout snapshot is the invoice. An `orders` row is inserted by PayIt only after authorization |
| BENEFIT ELIGIBILITY VALIDATED | Implicit inside `_recalculate` (offer recomputed), **not card-aware** |
| CUSTOMER APPROVAL | `confirm(consent=True)`, or the VIC instruction + simulated passkey |
| PAYMENT AUTHORIZATION | Gateway → simulated network → simulated issuer |
| ORDER CONFIRMED | `orders.finalize` once, after authorization; voids the authorization if order creation fails |

`checkout_hash` already behaves like a `checkout_version` (it changes whenever anything that affects the charge
changes). There is no human-readable monotonic version number.

---

## 10. Existing payment implementation

`backend/shop/payment.py::confirm` (the only way money moves for ShopSphere):

1. `consent is True` required (else `CONSENT_REQUIRED`).
2. Chase eligibility gate (again).
3. Checkout must be `open`. Payment path chosen by the server: `shop/vic.py::payment_path`.
4. VIC path requires an `instruction_id` that belongs to this checkout.
5. `refresh()` (live price/stock), `snapshot.ready` must be true.
6. `payment_ref = {checkout_id}-{attempt}`; checkout → `consented`; audit `CHECKOUT_CONSENT`.
7. **GreenLight**: `approve_authorization` → policy (merchant allow-list, USD, ≤ $2,500) → signed single-use **DPAT** (15 min) bound to agent PayIt + merchant + order + amount + currency + checkout hash.
8. **PayIt**: `execute_payment_endpoint` → atomically claims the DPAT → **12 guardrail checks** → charge step via `processor_route` (ContextVar) → `_gateway_route`:
   * `vic`: 12 VIC controls → purchase credential `pcr_…` → gateway with `instrument=vic_credential`, idempotency key `{instruction}:1`.
   * `standard`: `instrument=card_on_file` (`token_ref`), idempotency key `payment_ref`.
   * `wallet`: no gateway; wallet balance deducted.
9. Gateway (`backend/gateway/app.py`): merchant key check, idempotency replay, demo scenarios (`decline`, `timeout`, `duplicate` set via `/api/vic/demo/scenario`), simulated network → simulated issuer (expiry, status, decline card, available credit hold).
10. Authorized → `orders.finalize` once (stock, points, offers, cashback, cart cleanup; checkout `paid`) → confirmation email. Declined/timeout → checkout reopened, no order kept.

Duplicate protection today: checkout status gate (second click → `409 CHECKOUT_CLOSED`), single-use DPAT
claimed atomically, gateway idempotency keys, `finalize` idempotent, offer/points ledger keys per order.

---

## 11. VIC implementation and dependencies

`backend/vic/*` + `backend/shop/vic.py` + `backend/routers/vic.py` + `backend/gateway/network.py::visa_agent_credential`.
Explicitly labelled "VIC-**inspired**, local, not connected to Visa, not VIC compliant" (`vic/__init__.py`).

| Question | Finding |
|---|---|
| What does it do? | Simulates: agent-token enrollment per card (with a simulated passkey), authenticated payment instruction per checkout (simulated passkey), 12 controls, a purchase-specific single-use credential, gateway redemption of that credential by a "simulated Visa network", commerce signals, and an explicit lifecycle state machine. |
| Coupled to authentication? | **Yes, to the Chase gate**: router-level `require_cardmember` (`routers/vic.py:33`) and VIC control 1 (`credentials.py:46`) call `eligibility.check()`. It is *not* coupled to login itself. |
| Coupled to checkout? | **Yes, appropriately**: instructions bind `checkout_id` + `checkout_hash` + amount. `checkout.update_checkout` cancels stale instructions. Clean coupling. |
| Coupled to payment? | **Yes**: `payment.confirm` branches on `path == "vic"`; `_gateway_route` runs the controls and issues the credential. Branching is localized in two places. |
| Only simulated? | **Yes**, entirely. |
| Assumes every card is Visa? | **No.** It only activates for Visa. It does assume VIC = **Chase** Visa, because it requires a `card_accounts` row and `enrollment._visa_account` requires the Chase account. |
| Can cardholder logic be separated from network routing? | **Yes, with modest change.** `shop/vic.py::payment_path` is already the single routing decision. It needs (a) the card's **network** from `payment_methods` rather than `card_accounts`, (b) a capability flag ("agent-pay capable") rather than "is a Chase account", and (c) VIC control 1 to check *marketplace access* (generic) rather than Chase eligibility. |

---

## 12. Existing Talkshop agent responsibilities (as actually implemented)

Six agents are defined in `backend/config/agents.py` and seeded into `agents`. In Demo 1 they appear as
**labels on trace steps** emitted by the orchestrator and `shop/payment.py`. Only GreenLight and PayIt run
their original agent code (`agents/greenlight.py::request_dpat`, `agents/payit.py::execute_payment`).
VibeCheck and SneakPeek functions are used for intent/LLM and search via `talkshop/brain.py` and `talkshop/tools.py`.

| Agent | What it really does in ShopSphere today |
|---|---|
| VibeCheck | Input guardrails, PAN/CVC/email masking, intent understanding (`brain.decide` picks only stage-allowed actions) |
| SneakPeek | Catalog search (ShopSphere only), ranking, stock/variant checks |
| CartUp | Add to cart, **cardmember gate** message, create/update checkout, missing-details forms, review card |
| GreenLight | Consent gate in the orchestrator (`_go_ahead`), VIC setup/approval prompts, policy + DPAT issuance |
| PayIt | 12 DPAT checks, gateway route, VIC controls/credential (labelled "VIC simulator"), idempotency |
| TrackIt | Order finalize, confirmation email, tracking/shipment |
| (pseudo) "Eligibility" | Shown in the info view's guardrail list: the Chase gate |

The suggested mapping in the brief matches this closely. **No agent needs replacing.**

## 13. GreenLight logic

* Orchestrator `_go_ahead` (`orchestrator.py:519`): only from an explicit `go_ahead` action while stage is `AWAITING_CONSENT` and the checkout id matches. VIC setup or approval is requested if needed.
* `agents/greenlight.py::request_dpat`: policy (`payment/policy.py`) → DPAT signed with HMAC (`payment/signing.py`).
* `routers/authorizations.py::approve_authorization`: persists `payment_authorizations` + `delegated_tokens` + audit `USER_APPROVED_PURCHASE`.
* **Not done by GreenLight today:** offer/benefit eligibility validation, payment-card readiness, comparing the approved amount/version with the current checkout (standard path).

## 14. PayIt logic

* `agents/payit.py::execute_payment`: 12 guardrails (`payment/guardrail_engine.py`) then the routed processor.
* `routers/payments.py::execute_payment_endpoint`: atomic DPAT claim, wallet debit, order row insert, points earn.
* `shop/payment.py::_gateway_route`: VIC vs card-on-file instrument, gateway call, trace, signals.
* Network-neutral at the gateway boundary. **No abstraction for "provider handoff" or new-card tokenization.**

## 15. Current consent implementation

| Surface | How consent is given | Bound to |
|---|---|---|
| Website checkout (`Checkout.tsx`) | "Approve and pay" / "Place order" → `shop.confirm(id, instructionId?)` sends `{consent: true}` | VIC: instruction (hash + amount). **Standard: nothing from the client.** The server charges the refreshed total |
| Talkshop | `go_ahead` action (button) or typed GO AHEAD (VIC: starts passkey approval only; standard: re-shows review) | Same |
| Auto checkout countdown (`cards.tsx:141`, 6 s) | Only moves to the **review card**; payment still needs a tap | n/a |
| `consent_mode="auto_countdown"` | Still accepted internally by `payment.confirm` / VIC instructions (HTTP layer allows only `button\|typed`). Leftover from the removed auto-pay | Remove (Phase 8) |

Consent records: `audit_events` `CHECKOUT_CONSENT` (mode, hash, total, path), `USER_APPROVED_PURCHASE`, VIC lifecycle.
Missing vs. the brief: explicit `action = PAY`, client echo of `checkout_version` + `final_amount` + `currency`
on the standard path, a rejection if they differ, and an explicit "approval cancelled" record on the standard path.

## 16. Existing security controls (and gaps)

Present and worth keeping:
* Server-side gates (not just hidden UI); JWT + bcrypt; visitor adoption on login.
* Money is computed only on the server; SHA-256 checkout fingerprint; DPAT HMAC, single use, atomic claim; 12 PayIt checks; 12 VIC controls.
* Opaque random tokens (`tok_…`, `vat_…`, `pcr_…`); test `test_tokens_are_random_not_derived_from_card_digits`.
* PAN/CVC/email masking in chat (`frontend/src/talkshop/redact.ts` + server); test `test_no_pan_cvv_or_secrets_in_responses_or_trace`.
* Gateway idempotency + request-hash conflict; void on order-creation failure.
* Sign-up lockout and generic errors; one claim per directory record.

Gaps relevant to this work:

| # | Gap | Where | Plan phase |
|---|---|---|---|
| S1 | **Raw PAN + CVC posted to ShopSphere** (`CardRequest.number/cvc`) and collected by a ShopSphere-controlled form, also embedded inside the Talkshop panel and labelled "Secure forms" | `routers/shop.py:113-150`, `shop/profile.py:130-159`, `components/shopsphere/CheckoutForms.tsx:43-90`, `components/talkshop/cards.tsx:328, 492`, `pages/store/Checkout.tsx:227, 262` | 6 (secure provider) |
| S2 | Standard-path consent not bound to the amount/version the customer saw | `routers/shop.py:206-210`, `shop/payment.py:70-132`, `api/shop.ts:176` | 4 |
| S3 | Chase offers not revalidated against the **selected card** | `shop/offers.py::entitled` | 3–4 |
| S4 | `POST /api/auth/reset-password` is **unauthenticated**: anyone can set any account's password by email | `routers/auth.py:179-187` | 2 (needs your decision, see the plan's questions) |
| S5 | Last-4 used as a sign-up matching factor (documented as not proof) | `cardholder_verification.py` | 2 (reposition as "link Chase benefits", never as access auth) |
| S6 | `auto_countdown` consent mode still accepted internally | `shop/payment.py:123`, `vic/instructions.py:56` | 8 |
| S7 | Order tracking endpoints sit behind the Chase gate (router-level dependency) | `routers/shop.py:46, 287-310` | 2 |

## 17. Existing tests

`tests/` (pytest, run with `python -m pytest tests/`; uses a throwaway SQLite DB per run, `tests/conftest.py`).

* Part 2 suites: `test_part2_cardmember_vic.py` (28), `test_part2_signup.py` (9), `test_part2_talkshop_vic.py` (6), `test_part2_browse.py` (5).
* Demo 1 suites: `test_demo1_every_shopper.py`, `test_demo1_secure_checkout.py`, `test_demo1_saved_details.py`, `test_demo1_services.py`, `test_demo1_visitor.py`, `test_demo1_catalog.py`, `test_talkshop_orchestrator.py`.
* Older v1/v2 suites: `test_phase1-5.py`, `test_auth.py`, `test_cart.py`, `test_chat_history.py`, `test_dashboard.py`, `test_shopkeeper.py`, `test_reset_demo.py`, `test_catalog_sources.py`.
* Fixture helpers `register_member()` / `member_headers` assume **Chase Visa**. Many suites depend on "non-Chase ⇒ 403", which will change by design in Phase 2 (`test_checking_only_customer_is_denied_everywhere`, `test_new_signup_without_a_card_is_not_eligible`, `test_signed_in_without_a_card_is_asked_to_verify`, `test_member_extras_only_for_members`, …).
* Test `test_3_no_saved_card_uses_secure_payment_and_saves_only_if_asked` posts a raw card number. It changes in Phase 6.
* Frontend: no unit tests, no lint script. `npm run build` = `tsc && vite build`. **`tsc --noEmit` passes on this branch.**

**Baseline result (2026-10-13, system `python -m pytest tests/ -q`):** **390 passed**, 0 failed (≈2 m 50 s;
deprecation warnings only, plus harmless "can't export spans to localhost:6006" messages from the tracing exporter).
Frontend `npx tsc --noEmit`: **clean**. Note: the `.venv-qa` virtualenv has no pytest; the system Python has the deps.

## 18. Current technical trace / UI

* Live trace events come from `Turn.guard(agent, check, result, detail, protocol, layer)` in the orchestrator, plus the `trace` list returned by `payment.confirm` (GreenLight → PayIt 12 checks → VIC controls → gateway/network/issuer steps → TrackIt).
* `TalkshopPanel.tsx` info view: Live trace, Guardrails (static list per agent), Protocols (VIC/VTS/AP2/ACP/UCP/MCP/A2A/TAP with honest "inspired / not used" notes), Email, and a 12-milestone Visa payment lifecycle.
* Labels are mostly honest ("AP2-style", "ACP-style", "VIC-inspired", "SIMULATED"). A few trace labels drop the qualifier: `"AP2"` (`orchestrator.py:553`), `"ACP"` (`:471, :475`), `"ACP create checkout session"`, `"ACP delegated payment"`, `"UCP identity"`, `"UCP identity linking"`, `"ACP / UCP"`, `"UCP product discovery"`, `"UCP order management"`. Phase 10 adds the qualifiers.
* Not shown today: cardholder type (issuer vs network), access decision vs entitlement decision, offer eligibility reason, who owns the payment page (merchant vs provider).

## 19. Exact files / modules involved

**Backend:** `backend/shop/eligibility.py`, `backend/shop/cardholder_verification.py`, `backend/shop/offers.py`,
`backend/shop/checkout.py`, `backend/shop/payment.py`, `backend/shop/vic.py`, `backend/shop/profile.py`,
`backend/shop/orders.py`, `backend/shop/rewards.py`, `backend/routers/shop.py`, `backend/routers/member.py`,
`backend/routers/products.py`, `backend/routers/vic.py`, `backend/routers/auth.py`, `backend/talkshop/orchestrator.py`,
`backend/talkshop/state.py`, `backend/talkshop/tools.py`, `backend/vic/enrollment.py`, `backend/vic/credentials.py`,
`backend/vic/instructions.py`, `backend/gateway/app.py`, `backend/gateway/network.py`, `backend/gateway/issuer.py`,
`backend/db/schema.py`, `backend/db/init_db.py`.

**Frontend:** `frontend/src/App.tsx`, `frontend/src/member/MemberContext.tsx`, `frontend/src/member/PasskeyContext.tsx`,
`frontend/src/api/member.ts`, `frontend/src/api/shop.ts`, `frontend/src/api/talkshop.ts`, `frontend/src/auth/AuthContext.tsx`,
`frontend/src/pages/Login.tsx`, `frontend/src/pages/store/{MemberGate,Home,Product,Checkout,Account,Member,Cart,Orders}.tsx`,
`frontend/src/components/shopsphere/{CheckoutForms,StoreHeader,ProductTile}.tsx`,
`frontend/src/components/talkshop/{TalkshopPanel,cards}.tsx`, `frontend/src/talkshop/TalkshopContext.tsx`,
`frontend/src/layouts/StoreLayout.tsx`.

**Tests:** `tests/conftest.py`, `tests/test_part2_*.py`, `tests/test_demo1_every_shopper.py`, `tests/test_demo1_visitor.py`,
`tests/test_demo1_secure_checkout.py`, `tests/test_demo1_saved_details.py`.

**Docs:** `README.md`, `docs/demo-1-system-workflow.md` (§16), `docs/demo-1-shopsphere-talkshop-plan.md` (Phases 13–19).

## 20. What can remain unchanged

* Catalog, search, Talkshop conversation stages, variant/stock checks, cart service, shipping, email, tracking.
* Merchant-authoritative checkout and `checkout_hash` mechanics (extended, not replaced).
* GreenLight DPAT + policy, PayIt 12 checks, `processor_route`, gateway adapter + mock gateway + idempotency, void-on-failure, `orders.finalize` idempotency, rewards ledger.
* VIC simulator internals (instruction, passkey, controls 3–12, credential, signals, lifecycle). They stay the Visa-specific path.
* Six-agent roster and trace format.
* Masking/redaction, random tokens, audit events.

## 21. What needs modification

| Area | Change |
|---|---|
| Access gate | Split `eligibility.py` into a generic **marketplace access** check (signed-in ShopSphere customer) and a **Chase entitlement** check |
| Card model | Add `issuer`, `status` (and a source/provider field) to `payment_methods`. Remove "Chase" defaults from fixtures. Widen network types |
| Fixtures | Add non-Chase cardholders (Citi MC 5678, Amex 9012) and a Chase Visa 1234 persona. Keep A–E |
| Offers | Add an offer **class** (standard vs Chase-exclusive) and a "requires Chase card at payment" rule. Make `entitled()` card-aware |
| Checkout | Revalidate entitlements against the selected card. Expose `checkout_version`, applied/removed benefits and reasons |
| Consent | Bind confirm to `checkout_version` + amount + currency + action. Remove `auto_countdown` |
| New-card payment | Replace the PAN/CVC form with a **Simulated Secure Payment Provider** (synthetic outcomes only). Add a save-for-future consent |
| Sign-up | A general ShopSphere sign-up (name/email/password) is the default. "Link Chase card benefits" becomes optional |
| VIC | Route on network + agent-pay capability, not "is a Chase account". Control 1 checks generic access |
| UI copy | Remove "Chase members only" gate language. Label Chase benefits as Chase-exclusive. Make issuer and network visible separately |
| Trace | Show access vs entitlement decisions, benefit revalidation, payment ownership (merchant vs provider), and fully qualified protocol labels |
