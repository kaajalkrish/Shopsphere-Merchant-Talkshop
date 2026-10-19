# ShopSphere × Talkshop — General Cardholder Execution Plan

> Branch: `demo-1-cardmember-vic` · Plan written 2026-10-13 · Based on
> [`shopsphere-cardholder-current-state.md`](shopsphere-cardholder-current-state.md) (Phase 0 audit).
> These phases are numbered **CH-0 … CH-11** so they don't collide with the older Demo 1 Phases 1–19.

**Business message the finished POC must show:** ShopSphere supports credit-card holders generally. Chase
cardholder status adds eligible Chase-specific benefits; it does not control access to the marketplace.
Existing customers can pay with saved, tokenized payment methods. New customers are handed off to a
payment-provider-controlled experience, so Talkshop never sees raw card credentials.

**Working agreement:** one phase at a time. At the end of each phase: run tests and type checks, list the
files changed, explain what behaviour changed, list remaining issues, update the tracker below, then **stop
and wait for approval** before starting the next phase.

---

## Progress tracker

| Phase | Title | Status | Approved by / date | Notes |
|---|---|---|---|---|
| CH-0 | Repository audit and baseline | ✅ Done | User answered Q1/Q4/Q5/Q8/Q9, 2026-10-13 | Two docs written; no code changed. Baseline: pytest 390 passed; `tsc --noEmit` clean |
| CH-1 | Card model: issuer ≠ network (additive, no behaviour change) | ✅ Done (awaiting approval) | | pytest **417 passed** (390 existing unchanged + 27 new in `tests/test_ch1_card_model.py`); `tsc --noEmit` clean; migration verified on a scratch copy of `demo1.db`. Note: existing-DB persona cards backfill as `source=issuer_link`, a fresh reset seeds them as `seeded` |
| CH-1 approval | | | User, 2026-10-13 ("continue with next CH") | Plus refined Q1/Q3/Q4 (see Decisions) |
| CH-2 | Marketplace access for all cardholders + profiles/fixtures | ✅ Done (awaiting approval) | | pytest **433 passed** (16 new: `tests/test_ch2_access.py` ×14 + 2 replacement tests; 13 tests updated by design, listed below); `tsc` + `npm run build` clean; personas F/G/H verified on a scratch copy of `demo1.db` |
| CH-2 approval | | | User, 2026-10-13 ("approve ch-2") | |
| CH-3 | Chase-exclusive offer entitlement + Chase points boost | ✅ Done (awaiting approval) | | pytest **445 passed** (12 new in `tests/test_ch3_offers.py`; 5 tests updated by design, listed in CH-3 notes); `tsc` + `npm run build` clean; offer rules + enrollments verified on a scratch copy of `demo1.db` |
| CH-3 approval | | | User, 2026-10-13: "don't wait for the approval, complete all the CH and then say it" | CH-4…CH-11 run back-to-back; one report at the end |
| CH-4 | Checkout benefit revalidation + checkout version + bound consent | ✅ Done | (run-through) | pytest **454 passed** (9 new: `tests/test_ch4_revalidation.py`); API confirm calls in 6 test files now send scoped consent via `tests/conftest.py::scoped_consent`; `test_client_cannot_change_the_amount` now expects 409 STALE_CHECKOUT for a faked amount; Talkshop binds Approve to the invoice it last showed (24 Talkshop call sites unchanged) |
| CH-5 | Existing-customer payment with saved tokenized methods | ✅ Done | (run-through) | `backend/shop/payment_routing.py` is the one routing decision (`route`: wallet / saved_card / vic / vic_setup_required / provider_handoff / none; legacy `path` kept); issuer+network card labels; "Payment route" trace step. 7 new tests (`tests/test_ch5_saved_card_payment.py`); no expectation changes |
| CH-6 | Simulated Secure Payment Provider handoff (retire raw PAN/CVC) | ✅ Done | (run-through) | pytest **480 passed** (18 new: `tests/test_ch6_provider_handoff.py`). `POST /api/me/payment-methods` now always 410 `PAN_NOT_ACCEPTED`; the card-number form and `profile.add_card` are gone; tests that typed card numbers now use `tests/conftest.py::add_card` (a provider-saved token) or `pay_with_provider`; 4 PAN-validation tests rewritten to assert refusal |
| CH-7 | New ShopSphere user sign-up and checkout continuation | ✅ Done | (run-through) | Inline sign-up in Talkshop's sign-in card (posts straight to `/api/auth/register`, never into the chat; resumes checkout); register normalizes email, throttles duplicate probes (429), audits `CUSTOMER_SIGNUP`; login is case-insensitive. 4 new tests (`tests/test_ch7_signup_flow.py`) |
| CH-8 | Consent / GreenLight / PayIt hardening + error states | ✅ Done | (run-through) | pytest **490 passed** (6 new: `tests/test_ch8_consent.py`). `auto_countdown` removed end-to-end (refused: `EXPLICIT_APPROVAL_REQUIRED`); GreenLight trace states "Benefit eligibility" + "Payment readiness" before the DPAT; cancellations recorded as `APPROVAL_CANCELLED` (provider session released); "Approve & Pay $X" everywhere; invoice payment shows issuer + network. 2 tests changed by design (`test_demo1_every_shopper`: the auto-countdown test now expects refusal; the trace test expects "Approve & Pay tapped") |
| CH-9 | VIC / network decoupling | ✅ Done | (run-through) | pytest **497 passed** (7 new: `tests/test_ch9_vic_routing.py`). VIC route = Visa network + `card_accounts.agent_pay_capable` + active agent token (issuer never checked); enrollment, control 2 and `/api/vic/status` use the capability; non-Visa never VIC even if flagged; older DBs backfilled (Chase Visa → capable) |
| CH-10 | Technical trace + client-demo UX | ✅ Done | (run-through) | Trace adds *Customer access* + *Cardholder & benefits*; payment route names the page owner; 14 unqualified protocol labels now ACP-style / AP2-style / UCP-inspired / MCP-style; info view lists scoped consent, routing, provider page (PSP row: "not a PCI boundary"). 3-scenario demo script in README; workflow doc §17. 3 new tests (`tests/test_ch10_trace.py`) |
| CH-11 | End-to-end and regression testing | ✅ Done | (run-through) | pytest **510 passed** (10 new: `tests/test_ch11_e2e_scenarios.py` + `tests/test_zz_leak_scan.py`, which runs last and scans every table for card numbers/CVV columns); `tsc` + `npm run build` clean; two-service check on real HTTP (gateway/provider on :8101 as its own origin, backend on :8001 with `MOCK_GATEWAY_URL`, scratch DB): Scenario 3 handoff → provider page (no card fields) → return link → server-to-server completion → order, card saved as a token; provider merchant API without key → 401 |

### CH-2 notes

Tests whose expectations changed **by design** (old expectation → new):
`test_demo1_visitor::test_visitor_can_browse_but_not_fill_a_cart` (visitor cart 403 → visitors keep a cart; checkout
needs an account) · `test_part2_browse` ×3 (Chase sign-in/verify card → sign in at checkout only; offers only for
Chase holders) · `test_part2_cardmember_vic` ×3 (checking-only / guest / no-card signup denied everywhere → customers
have access, no Chase offers; cart open to visitors) · `test_part2_signup::test_existing_account_can_verify_its_card`
(`NO_ELIGIBLE_CARD` → a customer already; linking adds `chase_benefits`) · `test_demo1_every_shopper` ×3 (wallet
without a credit card → a credit card is required; points wording) · `test_demo1_secure_checkout::test_only_the_card_missing`
(card form wording → "never takes a card number").

Interim states until later phases: offers are Chase-only by customer (not yet by the **selected card**: CH-3/CH-4);
customers with no credit card can't pay until the provider handoff (CH-6); the legacy card-number endpoint is off by
default (`ALLOW_LEGACY_CARD_ENTRY`, on only in `tests/conftest.py` until CH-6 rewrites those tests).

---

### CH-3 notes

* `backend/shop/entitlements.py` decides every offer: customer level (Chase credit card, dates, enrollment, limits,
  Chase Visa for the VIC cashback) → payment level (the **selected** card must be an eligible Chase credit card;
  another issuer's card, a typed-in card of unknown issuer, the wallet or no card → no offer) → order level.
* All five offers are Chase-exclusive and card-bound; new `OFF_CHASE2X` = 2× ShopSphere points with a Chase card.
  `OFF_STYLE3X` needs (mock) enrollment: A/B/E enrolled, Chris (H) not → `POST /api/offers/{id}/enroll`.
* Tests updated by design: `test_demo1_services` ×2, `test_demo1_secure_checkout` ×1, `test_talkshop_orchestrator`
  ×2 — they pay with a typed-in Visa 4242 (issuer unknown), so the 15% Chase offer no longer applies and the totals
  return to the Demo 1 spec ($129 + $10.64 tax = **$139.64**, was $118.70 with the offer).
* Deferred to CH-4 as planned: telling the shopper *why* a benefit disappeared on a card switch
  (`benefits.removed` + reasons in the invoice/UI), `checkout_version`, and binding confirm to it.

---

### Post-CH-11 feedback fixes (2026-10-13, after a hands-on demo)

* **Disabled "Approve & Pay" now explains itself.** Talkshop and the checkout page show "Why you can't approve yet"
  with the server's reasons (e.g. the Wallet needs a credit card on file) and a *Pay with a new card instead* button;
  the Wallet option says "needs a saved credit card" when there isn't one.
* **The provider step is now on one page and takes the card.** The provider's card form (name, number, expiry, CVC,
  optional save) is embedded as a cross-origin iframe in Talkshop and on the checkout page (no new tab). The form posts
  to the provider, which checks the card, keeps only network + last 4, and returns a token. **Test cards only**:
  every other number is refused and never echoed. Only ShopSphere's origins may embed it (`frame-ancestors`). When the
  provider finishes, it tells the embedding page (session id + status, sent only to the store's origin) and ShopSphere
  verifies server-to-server and places the order automatically ("I've finished paying" removed; a small "check
  status" fallback remains). The old synthetic-outcome buttons (`/simulate`) are gone.
* pytest **515 passed**; `tsc` clean.

### Final state and remaining limitations (after CH-11)

* **Simulation only.** No Chase, Citi, Amex, Visa or Mastercard system is called. The Simulated Secure Payment Provider
  shows the *shape* of a PSP handoff; it is not a PCI boundary. In the one-process demo it is served by the same backend
  under `/psp` (run `backend.gateway.app` separately with `MOCK_GATEWAY_URL` to give it its own origin). A real
  integration would use a PSP's hosted fields / iframe / redirect.
* **Last 4 digits** are used only to *match* a Chase directory record when linking Chase benefits (with a lockout);
  they are never an access or payment credential. A real issuer would add a one-time code.
* **Provider sessions** that are left open simply expire after 10 minutes. Cancelling the order releases them.
* **Not browser-tested by the assistant.** The UI changes are type-checked and build cleanly, and the APIs and
  Talkshop flows are covered by tests, but nobody has clicked through the pages in a browser yet.
* **Enrollment and offer dates** are mocked fixtures (`offer_enrollments`, `starts_at`/`ends_at`).
* **Sign-up throttling** is in memory (per email, per process).

---

## 1. Recommended order and why it differs from the suggested sequence

| Suggested | Recommended | Why |
|---|---|---|
| Phase 1 generalize model **and** remove Chase gate | **CH-1** model only; **CH-2** removes the gate | Phase 1 then becomes a purely additive schema/view change with zero behaviour change, which is easy to verify and roll back. Removing the gate rewrites ~10 denial tests, so it should ship together with the new fixtures and copy |
| Phase 6 new-user sign-up → Phase 7 secure handoff | **CH-6** secure handoff → **CH-7** sign-up | (a) The biggest security gap today is the ShopSphere-controlled PAN/CVC form (audit S1). Once CH-2 opens checkout to every customer, more people would reach that form, so it should be retired as early as possible. (b) The sign-up flow needs a payment path for users with no saved card, which is the provider handoff. Building the handoff first lets CH-7 be tested end-to-end |
| Phase 8 consent binding | Binding moves into **CH-4**. CH-8 keeps the UX, GreenLight/PayIt checks and error states | CH-4 introduces `checkout_version`. Binding confirm to that version and amount belongs in the same change (audit S2: today the standard path charges the refreshed total without comparing it to what the customer saw) |

Everything else keeps the suggested order.

## 2. Dependencies

```
CH-0 ─► CH-1 ─► CH-2 ─┬─► CH-3 ─► CH-4 ─┬─► CH-5 ─► CH-6 ─► CH-7 ─► CH-8 ─► CH-9 ─► CH-10 ─► CH-11
                      │                  │
                      └──────────────────┴─ (CH-9 only needs CH-1 + CH-5; it can run earlier if VIC work is urgent)
```

* CH-2 needs CH-1's `issuer`/`network` on cards (fixtures and Chase-entitlement lookup).
* CH-3 needs CH-2's split between access and entitlement.
* CH-4 needs CH-3's card-aware eligibility.
* CH-5 needs CH-4's version-bound confirm (it reworks the payment routing that confirm calls).
* CH-6 needs CH-5's generic routing (adds a `provider_handoff` route).
* CH-7 needs CH-2 (generic sign-up and access) and CH-6 (payment without a saved card).
* CH-8 polishes consent across all routes (needs CH-4..CH-7).
* CH-10 and CH-11 come last.

## 3. Target concepts (shared vocabulary for every phase)

| Concept | Meaning | Lives in |
|---|---|---|
| **MarketplaceAccess** ("CardholderEligibilityService" in the brief) | Three levels: **browse** (search, products, Talkshop, cart) = everyone; **checkout** = signed-in, active ShopSphere customer; **pay** = credit-card holder (active credit card on file, or provider card from CH-6). **Issuer is never consulted** | `backend/shop/eligibility.py` (refactored in CH-2) |
| **PaymentCard** | Network-neutral view of a payment method: `card_id`, `issuer` (CHASE/CITI/AMEX/BOFA/UNKNOWN), `network` (VISA/MASTERCARD/AMEX/DISCOVER), `last4`, opaque `token_ref` (never sent to the browser), `status`, `source`, `eligible_for_standard_checkout`, `agent_pay_capable`, `entitlements` | `payment_methods` columns (CH-1) + `backend/shop/cards.py` (new) |
| **ChaseRelationship** | A verified, active Chase credit-card account (`card_accounts.issuer = CHASE`) | `card_accounts` (existing table, made multi-issuer) |
| **OfferEligibilityService** | Which offers apply to *this* customer, *these* lines and *this* selected payment card, with reason codes | `backend/shop/entitlements.py` (new, CH-3) |
| **Invoice** | The authoritative checkout snapshot, plus `merchant_id`, `checkout_version`, applied/removed benefits and `final_amount` | `checkout.snapshot()` (CH-4) |
| **Scoped consent** | `{merchant_id, order_ref, final_amount, currency, checkout_version, action: "PAY"}` | `payment_authorizations` + audit (CH-4/CH-8) |
| **Payment route** | `wallet` · `saved_card` (card on file) · `vic` (Visa + agent-pay capable + active agent token) · `provider_handoff` (new card via the simulated provider) | `backend/shop/vic.py::payment_path` → generic routing (CH-5/CH-9) |

**Canonical codes:** issuer and network are stored and compared as upper-case codes (`CHASE`, `VISA`, …). The
UI shows them as display names ("Chase", "Visa"). Existing rows that store "Visa"/"Chase" are normalized by
a helper first (CH-1); a data migration follows only if needed.

---

## 4. Demo fixtures (deterministic, all simulated)

### Positive personas

| Id | Persona | Card(s) | Scenario | Expected |
|---|---|---|---|---|
| A | Kaajal (existing) | Chase Visa 4821 (agent token ACTIVE), Chase MC 0019 (decline card) | 2 + VIC | Standard + Chase offers; VIC path; switching to MC 0019 removes payment-bound Chase offers |
| B | Marcus (existing) | Chase Mastercard 5510 | 2 (non-Visa Chase) | Chase offers; standard card path (never VIC) |
| C | Priya (existing) | Chase checking 7702 only | 3-like (no credit card) | Can browse and check out; **can't pay until she adds a credit card** (provider handoff from CH-6). No Chase card benefits (checking isn't a credit card) |
| E | Elena (existing) | Chase Visa 4002 (no agent token) | 2 + VIC setup | Chase offers; VIC setup required, or switch card |
| **F** (new) | Dana Brooks, `dana@shopsphere.demo` | **Citi Mastercard 5678** (saved token, `CARD_002`) | **1** | Access YES · **no offers** (all offers are Chase-only) · wallet YES · 1× points · checkout via saved card |
| **G** (new) | Ravi Menon, `ravi@shopsphere.demo` | **Amex 9012** (issuer AMEX, network AMEX, `CARD_003`) | 1 | Same as F; Amex network on the saved-card path |
| **H** (new) | Chris Park, `chris@shopsphere.demo` | **Chase Visa 1234** (`CARD_001`), enrolled in Chase offers | **2** | Chase-exclusive offers + **2× points** when paying with the Chase card; revalidated at checkout |
| **N** (sign-up) | "New shopper": any new email, e.g. `new.shopper@example.com` | none | **3** | Sign-up → profile → no saved payment → provider handoff → optional save |

Personas A–E stay (existing tests depend on them). F, G, H are added in CH-2 (H's offer enrollment in CH-3).

### Negative fixtures / scenarios

| Negative case | How it's produced | Expected | Phase |
|---|---|---|---|
| Chase offer attempted with an ineligible card | Persona F checkout with running shoes | No Chase discount; reason `NOT_CHASE_CARDHOLDER` | CH-3 |
| User changes card after the offer was applied | Persona A or H switches to Citi MC/Chase MC decline card or wallet | Payment-bound Chase offer removed, total recalculated, version bumped, reason `PAYMENT_CARD_NOT_ELIGIBLE` | CH-4 |
| Chase cardholder not enrolled in an offer | Persona H not enrolled in `OFF_STYLE3X` (fixture) | Offer shown as "enroll to use", not applied | CH-3 |
| Payment declined | Persona A MC 0019, or scenario `decline`, or provider "Simulate decline" | No order; checkout reopened; nothing charged | CH-5/CH-6 |
| Payment timeout | Scenario `timeout` / provider "Simulate timeout" | No order; retry gets a new attempt reference | CH-5/CH-6 |
| Customer cancels approval | Cancel on the approval card / provider page | Consent record `APPROVAL_CANCELLED`; no DPAT; no charge | CH-8 |
| Stale checkout | Price or benefit changes after review; confirm with old version | `409 STALE_CHECKOUT` + fresh invoice; no charge | CH-4 |
| Duplicate Pay click | Two confirms for the same version | One authorization; the second gets `409 CHECKOUT_CLOSED`/replay | CH-4/CH-8 |
| Expired / invalid payment session | Provider session past TTL, wrong checkout, reused, or forged id | Refused; no order | CH-6 |
| Save card unchecked | Provider approval without save | Order paid; **no** reusable `payment_methods` row | CH-6 |
| Save card checked | Signed-in customer ticks save on the provider page | Stores only `pm_mock_…` + network + last4 (+ issuer hint); listed as a saved card | CH-6 |
| Non-Visa card when a VIC path exists | Persona A switches to MC; Persona G (Amex) | Never routed through VIC; standard path | CH-5/CH-9 |
| Visitor tries to pay | Visitor confirm | `LOGIN_REQUIRED` (sign-in/sign-up offered) | CH-2/CH-7 |

---

## 5. Phases

Every phase below lists: Goal · Current problem · Target behaviour · Modules · Files likely to change
(verified to exist; new files are marked *new*) · Frontend · Backend · Agents · Data model · API/event
contract · Security · Edge cases · Tests · Acceptance criteria · Dependencies · Rollback/regression risk ·
Copy-paste prompt.

Shared **prompt footer** (included in every phase prompt below):

```
Rules for this phase:
1. Read docs/shopsphere-cardholder-current-state.md and docs/shopsphere-cardholder-execution-plan.md first.
2. Inspect the actual code you will touch before editing; trust the code over the docs if they disagree, and say so.
3. Make the smallest safe implementation that meets this phase's acceptance criteria. Do not start later phases.
4. Preserve working functionality (Talkshop flow, VIC simulator, gateway idempotency, orders, tracking, email).
5. Run the existing tests: python -m pytest tests/ -q
6. Add tests for this phase's new behaviour (and update tests whose expectations change by design; list each one).
7. Run the frontend type check/build: cd frontend; npx tsc --noEmit (and npm run build if UI changed).
8. Update the progress tracker in docs/shopsphere-cardholder-execution-plan.md (status, notes, test counts).
9. Report: exact files changed, behaviour changed, tests added/changed with results, remaining issues.
10. STOP and wait for human approval. Do not commit unless asked.
Never: collect/store/log raw PAN or CVV; treat last4 as authentication; let the LLM compute amounts or
eligibility; claim real Chase/Visa/Mastercard/PCI/ACP/AP2/UCP/VIC compliance; route non-Visa cards through VIC.
```

---

### CH-0 — Repository audit and baseline ✅

* **Goal:** Understand the branch before changing anything.
* **Output:** `docs/shopsphere-cardholder-current-state.md`, this plan, and the baseline results (pytest + `tsc --noEmit`).
* **Acceptance:** Both docs exist; no implementation file changed; open questions listed (§6).

---

### CH-1 — Card model: separate issuer from network (additive, no behaviour change)

**GOAL.** Give every payment method an explicit issuer, network, status and source, and expose one
network-neutral `PaymentCard` view. Remove Visa-only *type* assumptions. Access rules stay exactly as they are.

**CURRENT PROBLEM.** `payment_methods` has `brand` (network) but no issuer or status. Issuer exists only on
`card_accounts` (Chase only). The frontend types allow only `"Visa" | "Mastercard"`. Fixtures default to Visa.

**TARGET BEHAVIOUR.** Same app behaviour. Card APIs additionally return `issuer`, `network` (canonical code),
`network_display`, `status` and `source`. All existing tests pass unchanged.

**MODULES.** DB schema + migration helper, profile/card views, eligibility card view, fixtures.

**FILES LIKELY TO CHANGE.** `backend/db/schema.py`, `backend/db/init_db.py`, `backend/shop/profile.py`,
`backend/shop/eligibility.py` (card_view, `link_demo_card` sets issuer on the card),
`backend/shop/cardholder_verification.py` (sets issuer on linked cards), `frontend/src/api/shop.ts`,
`frontend/src/api/member.ts`, *new* `backend/shop/cards.py`, *new* `tests/test_ch1_card_model.py`.

**FRONTEND.** Widen types (`network: string` with known codes; `issuer` field added). Display is unchanged.

**BACKEND.** `cards.py`: `normalize_network()`, `normalize_issuer()`, `display_name()`, `payment_card_view(pm, acct)`.
`card_dict()` and `card_view()` add the new fields (backward compatible: `brand` is kept).

**AGENTS.** None.

**DATA MODEL.** `payment_methods` += `issuer VARCHAR(30) NULL`, `status VARCHAR(20) DEFAULT 'active'`,
`source VARCHAR(20) NULL` (`seeded` | `issuer_link` | `manual_legacy` | `provider_token`). Added with the
existing `_ensure_column` pattern and backfilled: issuer from the linked `card_accounts.issuer`, else `UNKNOWN`;
source `manual_legacy` for rows created by `profile.add_card`. `card_accounts.issuer` default stays, but new
code always passes the issuer explicitly.

**API / EVENTS.** Additive fields only on `/api/me/payment-methods`, the checkout snapshot's `payment_method` /
`saved_payment_methods`, and `/api/member` accounts.

**SECURITY.** `token_ref` is never added to any view. Issuer/network are display metadata, not credentials.

**EDGE CASES.** Older DBs without the columns (migration); `brand="Card"` from unknown BINs → network `UNKNOWN`;
Amex network == Amex issuer.

**TESTS.** New: issuer/network present and correct for personas A, B and E; the network is normalized; `token_ref`
is absent from responses; migration on an old-shape table. All existing tests still pass.

**ACCEPTANCE.** Full suite green with no expectation changes; `tsc` clean; card views show issuer and network separately.

**DEPENDENCIES.** CH-0.

**ROLLBACK / RISK.** Low. Additive nullable columns; reverting the code leaves harmless columns behind.

**PROMPT (copy-paste):**
```
Implement Phase CH-1 of docs/shopsphere-cardholder-execution-plan.md: "Card model: separate issuer from network".
Add issuer/status/source to payment_methods (nullable, backfilled via init_db._ensure_column), create
backend/shop/cards.py with normalize/display helpers and a PaymentCard view, add the new fields to card_dict,
eligibility.card_view and member account views, set the issuer explicitly where Chase cards are linked/seeded,
and widen the frontend card/network types. This phase must NOT change any access, offer, checkout or payment
behaviour: every existing test must pass unchanged. Add tests/test_ch1_card_model.py.
<shared prompt footer>
```

---

### CH-2 — Marketplace access for every credit-card holder + profiles and fixtures (Scenario 1 entry)

**GOAL.** Stop using Chase status as the gate. Any signed-in ShopSphere customer can use cart, checkout,
orders, rewards and Talkshop buying. Chase status becomes an entitlement fact.

**CURRENT PROBLEM.** `eligibility.py` (`ISSUER = "Chase"`) is enforced on every marketplace router, in
`payment.confirm`, in the orchestrator, in product badges and in VIC control 1. The UI and Talkshop copy say
"for Chase credit-card members".

**TARGET BEHAVIOUR.**
* Signed-in customer (any issuer, or no card yet) → cart, checkout, orders, account, ShopSphere points.
* Visitor → browse + Talkshop. At checkout: sign in / sign up (cart kept if Q2 = yes).
* `/api/member` returns `access: {allowed, code}` and `chase: {linked, eligible_card_count}`. `eligible` keeps
  meaning *access* (the UI keys off it), and a new `chase_benefits: bool` is added.
* "Verify your Chase card" becomes the optional **"Link a Chase card for Chase-exclusive benefits"**. It never
  grants access.
* New personas F (Citi MC 5678), G (Amex 9012), H (Chase Visa 1234) seeded with issuer-side `card_accounts`
  rows for their issuers (the table becomes multi-issuer).
* Order tracking is no longer behind any cardholder gate.
* (If Q8 = fix) `reset-password` requires the signed-in user or is disabled outside demo mode.

**MODULES.** Eligibility/access, routers, orchestrator gate, product badges, VIC control 1 (switch to the
access check only; VIC still requires a Chase Visa until CH-9), fixtures, UI copy and routing.

**FILES LIKELY TO CHANGE.** `backend/shop/eligibility.py`, `backend/routers/shop.py`, `backend/routers/member.py`,
`backend/routers/products.py`, `backend/routers/vic.py`, `backend/routers/auth.py` (Q8), `backend/shop/payment.py`,
`backend/talkshop/orchestrator.py`, `backend/vic/credentials.py` (control 1 wording/check),
`backend/shop/cardholder_verification.py`, `backend/db/init_db.py`, `tests/conftest.py`,
`frontend/src/App.tsx`, `frontend/src/member/MemberContext.tsx`, `frontend/src/api/member.ts`,
`frontend/src/pages/Login.tsx`, `frontend/src/pages/store/MemberGate.tsx`, `frontend/src/pages/store/Home.tsx`,
`frontend/src/pages/store/Product.tsx`, `frontend/src/components/shopsphere/StoreHeader.tsx`,
`frontend/src/components/talkshop/cards.tsx`, `frontend/src/components/talkshop/TalkshopPanel.tsx`,
`frontend/src/talkshop/TalkshopContext.tsx`, `frontend/src/layouts/StoreLayout.tsx`;
tests whose expectations change: `tests/test_part2_cardmember_vic.py`, `tests/test_part2_browse.py`,
`tests/test_part2_signup.py`, `tests/test_demo1_every_shopper.py`, `tests/test_demo1_visitor.py`;
*new* `tests/test_ch2_access.py`.

**FRONTEND.** `MemberOnly` → `CustomerOnly` (signed-in). `MemberGate` becomes a sign-in/sign-up page plus an
optional "Link Chase card benefits" panel. Login: "Create account" (generic) is the default tab and
"I have a Chase card — link benefits" is secondary; the persona list gains F, G and H. Header shows the
customer's default card as "Citi · Mastercard ••5678". The Chase badge appears only if `chase_benefits`.

**BACKEND.** In `eligibility.py`: `check_access(user)` (visitor → `LOGIN_REQUIRED`; inactive → `ACCOUNT_INACTIVE`)
and `require_shopper`. Keep `require_cardmember` as a deprecated alias of `require_shopper` for this phase so
routers can switch in one place. Move the Chase logic into `chase_cards(db, user_id)` / `has_chase_relationship()`.
`payment.confirm` uses the access check. Talkshop `member_ok` → `access_ok`. `_membership_needed` → sign-in/sign-up copy.

**AGENTS.** CartUp's "Cardmember gate" trace step → "Customer access" (pass for any signed-in customer).
GreenLight is unchanged.

**DATA MODEL.** No new tables. `card_accounts` rows for CITI and AMEX fixtures. `marketplace_eligible` is no
longer read for access (kept, documented as legacy).

**API / EVENTS.** `/api/member` adds `access` and `chase`. Talkshop `login_required.reason` values become
`sign_in` | `checkout` (`not_eligible` retired). Error code `NO_ELIGIBLE_CARD` is no longer emitted by access checks.

**SECURITY.** Access is still enforced server-side. Last4 is never an access factor. The Chase link keeps its
lockout and generic errors, and its UI copy says it unlocks benefits only. Sign-up never auto-links Chase accounts.

**EDGE CASES.** Checking-only Priya (access yes, no Chase credit benefits); customer with zero cards (access yes,
the payment step will ask: wallet / provider from CH-6); suspended user; visitor adoption keeps the conversation.

**TESTS.** New: personas F, G and C can add to cart, create checkouts and pay with a saved card or wallet; a
visitor gets `LOGIN_REQUIRED` at checkout; the `/api/member` shape; Talkshop adds to cart for F; product
badges for F show no Chase offers (full offer split is CH-3). Update the denial tests (listed above) to the
new expectations.

**ACCEPTANCE.** Scenario 1 can buy end-to-end with saved card F (offers not yet split). No Chase wording on any
access gate. Suite green; `tsc`/build clean.

**DEPENDENCIES.** CH-1.

**ROLLBACK / RISK.** Medium. Widest test churn. Interim risk: until CH-3, non-Chase customers would receive
today's "member" offers, because `entitled()` doesn't check issuer. **Mitigation:** in CH-2, restrict
`offers.entitled()` to `has_chase_relationship()` (one line) so no Chase offer leaks before CH-3. Interim risk
2: brand-new customers without a card would see the legacy PAN form until CH-6. **Mitigation:** acceptable
locally, or hide the form (Q9).

**PROMPT (copy-paste):**
```
Implement Phase CH-2 of docs/shopsphere-cardholder-execution-plan.md: "Marketplace access for every
credit-card holder". Split backend/shop/eligibility.py into a generic access check (signed-in active customer;
issuer never consulted) and Chase-relationship helpers; switch every router/orchestrator/payment/VIC-control-1
call site from the Chase gate to the access check; keep require_cardmember as a deprecated alias; restrict
offers.entitled() to customers with a Chase relationship as an interim guard; seed personas F (Citi Mastercard
5678), G (Amex 9012), H (Chase Visa 1234) with issuer-correct card_accounts + saved card tokens; move order
tracking off the gate; extend /api/member with access + chase blocks; replace Chase-gate copy in the UI and
Talkshop with sign-in/sign-up copy and make "link a Chase card" an optional benefits step. Apply the human's
answers to the open questions (visitor cart, reset-password, PAN form interim). Update denial tests that change
by design and list each one. Add tests/test_ch2_access.py.
<shared prompt footer>
```

---

### CH-3 — Chase-exclusive offer entitlement + Chase points boost

**GOAL.** Offers are a Chase credit-card benefit only (decision Q4), decided by a deterministic,
card-aware eligibility service with reasons. Chase cardholders also earn more ShopSphere points (decision Q3).

**CURRENT PROBLEM.** Every offer is a "member offer" (audience `all_cardmembers`/`visa_agent_pay`) with no
issuer requirement, card binding, dates or enrollment. Eligibility isn't card-aware. Points are the same
for everyone. (CH-2 adds an interim guard: offers only for customers with a Chase relationship.)

**TARGET BEHAVIOUR.**
* `OfferEligibilityService.evaluate(customer, lines, selected_card)` returns, per offer: eligible?, amount and
  reason code. **Every offer requires an eligible Chase credit card**: a Chase relationship, and (for
  discounts, cashback and the points boost) the **selected payment card** is that Chase credit card. Enrollment,
  dates and limits are checked too.
* **There are no standard offers.** Non-Chase customers get ShopSphere pricing, the wallet and 1× points.
  The UI may show Chase offers to them as "Chase credit-card holders only", **never applied**.
* **Chase points boost:** a Chase-exclusive `OFF_CHASE2X` "2× ShopSphere points when you pay with your Chase
  credit card" (bonus_points, value 1.0 = +1 point per $1, every department). Stacks with `OFF_STYLE3X`
  (clothing becomes 1 base + 2 + 1 = 4 points per $1; documented). Earned only after an authorized payment, once
  per order (existing ledger keys).
* Wallet: same for every customer (no Chase difference in this phase; see risk note).

**MODULES.** Offers, entitlements, rewards ledger, products API, member API, UI offer pages and badges, Talkshop review text.

**FILES LIKELY TO CHANGE.** `backend/shop/offers.py`, `backend/db/schema.py`, `backend/db/init_db.py`,
`backend/routers/products.py`, `backend/routers/member.py`, `backend/shop/checkout.py` (call site + points
preview), `backend/shop/orders.py` (bonus/cashback via the new service), `backend/shop/rewards.py` (labels),
`backend/talkshop/orchestrator.py` (review/points copy), `frontend/src/api/member.ts`, `frontend/src/api/shop.ts`,
`frontend/src/pages/store/Member.tsx`, `frontend/src/pages/store/Home.tsx`,
`frontend/src/components/shopsphere/ProductTile.tsx`, `frontend/src/pages/store/Product.tsx`,
`frontend/src/pages/store/Checkout.tsx`, *new* `backend/shop/entitlements.py`, *new* `tests/test_ch3_offers.py`.

**FRONTEND.** One "Chase credit-card holder offers" page. For non-Chase customers it explains the offers are for
Chase credit-card holders. Badges carry the server's eligibility state and the reason. Checkout shows "2× points
with your Chase card" when it applies. No client-side eligibility logic.

**BACKEND.** `entitlements.py` with reason codes `NOT_CHASE_CARDHOLDER`, `PAYMENT_CARD_NOT_ELIGIBLE`,
`NOT_ENROLLED`, `OFFER_NOT_STARTED`, `OFFER_EXPIRED`, `LIMIT_REACHED`, `NO_MATCHING_ITEMS`, `BELOW_MINIMUM`,
`NETWORK_PATH_REQUIRED`. `offers.best_discount/bonus_points/cashback` delegate to it. A seed migration marks every
existing offer `requires_issuer=CHASE` and adds `OFF_CHASE2X`.

**AGENTS.** SneakPeek product results carry the server badges. CartUp's review line says "Your Chase card benefit
saves $X" only when the service says so. Talkshop answers "points" questions with 1× vs 2× from server facts.

**DATA MODEL.** `member_offers` += `requires_issuer` (`CHASE`), `requires_payment_with_issuer` (bool),
`requires_network` (e.g. `VISA`), `requires_payment_path` (e.g. `vic`), `starts_at`, `ends_at`,
`requires_enrollment` (bool). *New* table `offer_enrollments(user_id, offer_id, enrolled_at)` (mocked, seeded for
A and H; H deliberately not enrolled in one offer). `audience` kept for compatibility.

**API / EVENTS.** `/api/offers` is readable by any signed-in customer: every Chase offer with
`{eligible, reason, reason_text}`. Product `member` block → `offers` block (`member` kept as an alias for one phase).

**SECURITY.** All amounts and points are server-side. The LLM receives only server text. "Demo offer" labels stay.

**EDGE CASES.** Chase checking-only (not a credit card → not eligible); Chase Mastercard (eligible unless the offer
needs VISA/VIC); paying by wallet (no card-bound benefits, 1× points); once-per-member limits; an expired offer.

**TESTS.** F/G: no offer applied, `NOT_CHASE_CARDHOLDER`, 1× points. H: offers + 2× points with the Chase card.
H not enrolled → `NOT_ENROLLED`. Dates. Limits. Badges per persona. Talkshop text for F never mentions Chase savings.

**ACCEPTANCE.** Scenario 1 shows no offers and 1× points. Scenario 2 shows eligible Chase offers and 2× points.
No Chase offer is applied to a non-Chase customer or a non-Chase payment card. Suite green.

**DEPENDENCIES.** CH-2.

**ROLLBACK / RISK.** Medium. Offer math feeds totals and points. Persona A's demo points will rise (2× boost).
Documented. If a Chase **wallet** difference is wanted too (e.g. a Chase welcome top-up), add it here as a
separate, clearly labelled demo benefit.

**PROMPT (copy-paste):**
```
Implement Phase CH-3 of docs/shopsphere-cardholder-execution-plan.md: "Chase-exclusive offer entitlement + Chase
points boost". Every offer requires an eligible Chase credit card (no standard offers). Add the eligibility
columns and the offer_enrollments table, create backend/shop/entitlements.py (OfferEligibilityService with the
listed reason codes; deterministic, labelled mock), route offers.best_discount/bonus_points/cashback through it,
mark all existing offers requires_issuer=CHASE, add OFF_CHASE2X (2x ShopSphere points when paying with the Chase
card), return eligibility + reason from /api/offers and product APIs, and show "Chase credit-card holder offer"
labels in the UI. The selected payment card is an input; full checkout revalidation wiring is CH-4: here, evaluate
with the checkout's current card where one exists. Add tests/test_ch3_offers.py.
<shared prompt footer>
```

---

### CH-4 — Checkout benefit revalidation, checkout version, version-bound consent

**GOAL.** Benefits are revalidated against the selected payment card on every change and right before
payment. The invoice has a version, and payment refuses anything other than the exact version and amount the
customer approved.

**CURRENT PROBLEM.** `_recalculate` picks the best discount without looking at the card. Confirm sends only
`{consent: true}`, and the server charges the refreshed total even if it changed (audit S2).

**TARGET BEHAVIOUR.**
* `_recalculate` calls `entitlements.evaluate(..., selected_card)`. Switching card/wallet removes
  payment-bound Chase benefits and shows `benefits.removed[{offer_id, reason}]`.
* `checkouts.version` increments whenever `checkout_hash` changes. The snapshot exposes an **invoice** block:
  merchant id/name, order ref, items, subtotal, discounts, eligible benefits, tax, shipping, final amount,
  currency, version.
* Confirm requires `{consent: true, action: "PAY", checkout_version, final_amount, currency}`. A mismatch, or a
  version change during the pre-payment refresh, returns `409 STALE_CHECKOUT` with the fresh invoice and **no charge**.
* Duplicate confirm for a version already `consented`/`paid` → `409` (unchanged mechanism, clearer message).

**FILES LIKELY TO CHANGE.** `backend/shop/checkout.py`, `backend/shop/payment.py`, `backend/routers/shop.py`,
`backend/db/schema.py`, `backend/db/init_db.py`, `backend/talkshop/orchestrator.py`, `backend/talkshop/tools.py`,
`backend/vic/instructions.py` (store version alongside the hash), `frontend/src/api/shop.ts`,
`frontend/src/api/talkshop.ts`, `frontend/src/pages/store/Checkout.tsx`,
`frontend/src/components/talkshop/cards.tsx`, `frontend/src/components/talkshop/TalkshopPanel.tsx`,
*new* `tests/test_ch4_revalidation.py`.

**FRONTEND.** The review shows applied and removed benefits with reasons. "Approve & Pay $X" sends version +
amount + currency. On `STALE_CHECKOUT` it re-renders the new invoice and asks again.

**BACKEND.** As above. `refresh()` returns whether the version changed. `confirm()` compares before setting `consented`.

**AGENTS.** CartUp: "Benefit revalidated for Citi Mastercard ••5678: Chase offer removed". GreenLight: consent
gate checks version + amount. The Talkshop `go_ahead` action carries `checkout_version` from the server snapshot
(never from the LLM).

**DATA MODEL.** `checkouts` += `version INTEGER DEFAULT 1`, `benefits_json TEXT` (applied/removed with reasons).
`payment_instructions` += `checkout_version`. `payment_authorizations` += `checkout_version`, `action`.

**API / EVENTS.** `POST /api/checkouts/{id}/confirm` body extended (old body → `400 CONSENT_SCOPE_REQUIRED`).
Snapshot `invoice` block. Talkshop `checkout_ready`/`checkout_updated` include the invoice; the `go_ahead`
action includes `checkout_version`.

**SECURITY.** The client echo is only compared, never trusted for the amount. The server stays authoritative.
Consent is scoped to merchant + order ref + amount + currency + version + action.

**EDGE CASES.** Price change between review and tap; stock change; points cap shifting with the discount;
switching to wallet; switching back to the Chase card (benefit returns, version bumps); a VIC instruction
created at version n with the checkout now at n+1 (already invalidated by hash; also check the version).

**TESTS.** H applies the Chase offer → switches to the Citi card fixture (H may hold only Chase: use A with MC 0019
or give H a second non-Chase card) → offer removed, reason shown, total up, version +1. Confirm with the old
version → 409, no authorization rows. Duplicate confirm → one authorization. Price change → 409.

**ACCEPTANCE.** No payment is possible for an amount or version the customer didn't see. Benefits always match
the selected card. Suite green.

**DEPENDENCIES.** CH-3.

**ROLLBACK / RISK.** Medium-high. It touches the confirm contract on both UIs and all payment tests (they need
the new body). Mitigation: a test helper that reads the snapshot and confirms with its version.

**PROMPT (copy-paste):**
```
Implement Phase CH-4 of docs/shopsphere-cardholder-execution-plan.md: "Checkout benefit revalidation, checkout
version, version-bound consent". Make checkout._recalculate evaluate entitlements against the selected payment
card (wallet = no card), record applied/removed benefits with reasons, add a checkouts.version that increments
whenever checkout_hash changes, expose an invoice block in snapshot(), require confirm to carry
{consent, action:"PAY", checkout_version, final_amount, currency} and refuse with 409 STALE_CHECKOUT (no charge,
fresh invoice) on any mismatch or on a version change during the pre-payment refresh. Wire both the website
checkout and Talkshop (go_ahead carries the server's version). Update payment tests via a shared helper. Add
tests/test_ch4_revalidation.py.
<shared prompt footer>
```

---

### CH-5 — Existing-customer payment with saved tokenized methods (generic routing)

**GOAL.** Customers with saved cards of any issuer/network pay through one generic, network-aware routing
decision. Issuer and network simulators stop assuming Chase.

**CURRENT PROBLEM.** `shop/vic.py::payment_path` keys VIC on "has a Chase card_account + Visa". The simulated
issuer is named "Chase" whenever a `card_accounts` row exists. Card lists show the network only.

**TARGET BEHAVIOUR.** Routes: `wallet`, `saved_card`, `vic` (unchanged rule for now: Visa + active agent token
on a VIC-capable card), and a reserved `provider_handoff` (CH-6). The issuer simulator is labelled by
`acct.issuer` ("Simulated Citi issuer", "Simulated Amex issuer"). The card picker shows "Citi · Mastercard
••5678". Mastercard/Amex/Discover always use `saved_card`.

**FILES LIKELY TO CHANGE.** `backend/shop/vic.py`, `backend/shop/payment.py`, `backend/gateway/issuer.py`,
`backend/gateway/network.py`, `backend/shop/checkout.py` (snapshot `payment_path` shape),
`frontend/src/pages/store/Checkout.tsx`, `frontend/src/components/talkshop/cards.tsx`,
`frontend/src/api/shop.ts`, *new* `backend/shop/payment_routing.py`, *new* `tests/test_ch5_saved_card_payment.py`.

**FRONTEND.** Card selector with issuer + network. Path explainer text per route (no "Visa" fallback strings).

**BACKEND.** `payment_routing.route(db, checkout) -> {route, card: PaymentCard view, label, reasons}`.
`vic.payment_path` becomes a thin facade for compatibility. Issuer simulator decline/limit logic is unchanged
but issuer-neutral.

**AGENTS.** PayIt trace shows the route and network ("Mastercard card-on-file token → simulated Mastercard
network → simulated Citi issuer").

**DATA MODEL.** None beyond CH-1/CH-2 (uses `payment_methods.issuer`, `card_accounts.issuer`).

**API / EVENTS.** Snapshot `payment_path.path` values: `wallet|saved_card|vic|vic_setup_required|provider_handoff|none`.
`standard` is kept as an alias of `saved_card` for one phase.

**SECURITY.** Only `token_ref` crosses to the gateway. The idempotency key stays per attempt (`payment_ref`).
Customer ownership of the token is still checked at the network step.

**EDGE CASES.** A deleted card mid-checkout (existing `reassign`); expired card; insufficient simulated credit;
a decline then switching to another issuer's card on the same checkout.

**TESTS.** F (Citi MC) and G (Amex) pay via saved card with the correct trace labels. A on MC 0019 → decline,
no order. Timeout → no order, retry OK. Non-Visa never `vic`.

**ACCEPTANCE.** Scenario 1 completes with saved cards for Citi MC and Amex. Scenario 2 is unchanged (A on VIC,
B on saved card). Suite green.

**DEPENDENCIES.** CH-4.

**ROLLBACK / RISK.** Low-medium. Routing is localized; the facade keeps old callers working.

**PROMPT (copy-paste):**
```
Implement Phase CH-5 of docs/shopsphere-cardholder-execution-plan.md: "Existing-customer payment with saved
tokenized methods". Create backend/shop/payment_routing.py as the single routing decision (wallet | saved_card |
vic | vic_setup_required | provider_handoff (reserved) | none) using the PaymentCard view, keep
backend/shop/vic.payment_path as a compatibility facade, make the simulated issuer/network labels issuer- and
network-neutral, and show issuer + network in the card pickers and trace. Keep the VIC eligibility rule exactly
as today (decoupling is CH-9). Add tests/test_ch5_saved_card_payment.py covering Citi Mastercard, Amex,
decline, timeout and "non-Visa never VIC".
<shared prompt footer>
```

---

### CH-6 — Simulated Secure Payment Provider handoff (retire raw PAN/CVC)

**GOAL.** A customer without a saved card pays through a provider-controlled page. ShopSphere and Talkshop
receive only a safe token/status, and can optionally save it with explicit consent.

**CURRENT PROBLEM.** `CardForm` (website and Talkshop panel) posts the full number and CVC to
`POST /api/me/payment-methods` (audit S1). That is a ShopSphere-controlled raw-card form labelled "secure".

**TARGET BEHAVIOUR.**
1. Customer approves the exact invoice in ShopSphere (CH-4 scoped consent) → GreenLight DPAT.
2. The ShopSphere backend creates a **payment session** with the *Simulated Secure Payment Provider*
   (server-to-server): `{merchant_id, checkout_id, payment_ref, amount, currency, checkout_version, customer_ref, save_allowed}`.
3. The browser opens the provider's **hosted page**. It is served by the provider app (the mock gateway service,
   its own origin on :8100 when run separately) and is **not a React page of ShopSphere**. It has *no card
   fields*, only synthetic actions: *Simulate Visa approval · Simulate Mastercard approval · Simulate Amex
   approval · Simulate decline · Simulate timeout · Simulate issuer step-up (then approve/fail)*. The checkbox
   "[ ] Save this payment method for future purchases" is shown only when `save_allowed` (signed-in customer)
   and is unticked by default. The page is labelled "Simulated Secure Payment Provider — demo, no real card
   data".
4. The provider returns the browser with the session id only. ShopSphere **fetches the result server-to-server**:
   `{payment_session_id, status, payment_token: "pm_mock_…", network, last4, authorization_reference, issuer_hint?}`.
5. PayIt's charge step verifies the session (amount, currency, merchant, payment_ref and version bound; status
   AUTHORIZED; single use; not expired) instead of calling `authorize` again. Order finalize happens once.
6. Save checked → `payment_methods` row with `source=provider_token`, `token_ref=pm_mock_…`, network, last4 and
   `issuer` from the simulated BIN table or `UNKNOWN`. Unchecked → **no reusable row**; the order keeps
   network/last4/authorization reference only.
7. `POST /api/me/payment-methods` no longer accepts `number`/`cvc` (`410 PAN_NOT_ACCEPTED`). `CardForm` is
   removed and replaced by "Pay with a new card (secure provider)".

**FILES LIKELY TO CHANGE.** `backend/gateway/app.py`, `backend/gateway/adapter.py`, `backend/shop/payment.py`,
`backend/shop/payment_routing.py` (from CH-5), `backend/shop/profile.py`, `backend/routers/shop.py`,
`backend/shop/orders.py`, `backend/db/schema.py`, `backend/db/init_db.py`, `backend/talkshop/orchestrator.py`,
`frontend/src/components/shopsphere/CheckoutForms.tsx`, `frontend/src/pages/store/Checkout.tsx`,
`frontend/src/components/talkshop/cards.tsx`, `frontend/src/components/talkshop/TalkshopPanel.tsx`,
`frontend/src/api/shop.ts`, `frontend/src/api/talkshop.ts`, `tests/test_demo1_every_shopper.py` (test_3),
`tests/test_demo1_secure_checkout.py`, `tests/test_demo1_saved_details.py`,
*new* `backend/gateway/provider.py` (session store + hosted page), *new* `tests/test_ch6_provider_handoff.py`.

**FRONTEND.** A "Continue to secure payment provider" step after approval (popup/new tab, or an iframe of the
provider origin). Result polling via ShopSphere's API (never trusting postMessage contents for status).
Talkshop shows a `provider_handoff` card with "Opens the payment provider's page. Talkshop never sees your card".

**BACKEND.** Provider app endpoints: `POST /v1/payment_sessions` (merchant key), `GET /hosted/{session}` (HTML),
`POST /hosted/{session}/simulate` (browser action), `GET /v1/payment_sessions/{id}` (merchant key, safe result).
ShopSphere: `POST /api/checkouts/{id}/payment-session`, `GET /api/checkouts/{id}/payment-session/{sid}`.
Route `provider_handoff` in CH-5's router.

**AGENTS.** GreenLight: consent before the handoff. PayIt: "Provider session verified (status, amount, single
use)". Trace: "Payment page owner: Simulated Secure Payment Provider (not ShopSphere)".

**DATA MODEL.** *New* `provider_payment_sessions(session_id, merchant_id, checkout_id, payment_ref,
checkout_version, amount, currency, customer_ref, status, network, last4, issuer_hint, payment_token,
authorization_reference, save_requested, expires_at, used_at)`. In a real system this table lives with the provider.

**API / EVENTS.** As above. Talkshop events `provider_handoff{checkout_id, session_id, hosted_url}` and
`provider_result{status}`.

**SECURITY.** No PAN/CVV fields anywhere. Server-to-server result fetch with the merchant key. Session TTL
(10 min) + single use + amount/version binding. The token is never derived from digits. A visitor/guest can't
save. Logging and trace carry only the shortened session ref, network and last4. Docs state plainly that this is a
**simulation of a PSP boundary, not a PCI scope reduction**.

**EDGE CASES.** Expired session; reused session; session for another checkout/amount/version; browser closes
mid-flow (session stays pending; retry creates a new session, same checkout); provider "approve" then order
creation fails (void the provider authorization); step-up failure; save checked by a visitor (ignored).

**TESTS.** Approve with Visa/MC/Amex simulations → order; decline/timeout → no order; expired/invalid/reused
session refused; save unchecked → no saved card; save checked → `pm_mock_` card listed, no PAN anywhere in DB
or responses (scan); `POST /api/me/payment-methods` with number → 410; the old test_3 rewritten to the provider flow.

**ACCEPTANCE.** No code path accepts or stores PAN/CVV. Scenario 3's payment works with optional save. Suite green.

**DEPENDENCIES.** CH-5.

**ROLLBACK / RISK.** Medium-high: new subsystem and removal of a working form. Mitigation: build the provider
path first, switch the UI, then remove the PAN endpoint in the same phase with tests proving no regression.

**PROMPT (copy-paste):**
```
Implement Phase CH-6 of docs/shopsphere-cardholder-execution-plan.md: "Simulated Secure Payment Provider
handoff". Add a provider session subsystem to the mock gateway service (backend/gateway/provider.py): server-to-
server session create + result fetch with the merchant key, and a provider-served hosted HTML page with ONLY
synthetic actions (approve as Visa/Mastercard/Amex, decline, timeout, step-up) and an optional, unticked
"save this payment method" box shown only for signed-in customers. No card number/expiry/CVV fields anywhere.
Bind sessions to merchant, checkout, payment_ref, checkout_version, amount, currency; single use; 10-minute TTL.
Add the provider_handoff route so consent (CH-4) happens before the handoff and PayIt verifies the provider
session instead of charging again. Save → payment_methods(source=provider_token, token_ref=pm_mock_…, network,
last4); no save → no reusable row. Remove CardForm and make POST /api/me/payment-methods refuse number/cvc (410).
Label everything "Simulated Secure Payment Provider"; do not claim PCI compliance. Add
tests/test_ch6_provider_handoff.py and rewrite tests that posted raw card numbers.
<shared prompt footer>
```

---

### CH-7 — New ShopSphere user sign-up and checkout continuation (Scenario 3)

**GOAL.** A brand-new person signs up from the website or from inside Talkshop, keeps their cart and
conversation, and checks out with no saved payment method.

**CURRENT PROBLEM.** The default sign-up is the Chase-directory match. Generic `/api/auth/register` exists but
isn't offered as the main path. Talkshop's sign-in card points to "cardmember" sign-in.

**TARGET BEHAVIOUR.** Visitor → "Buy" → Talkshop `signup_required` card (name, email, password; or "I already
have an account") → `POST /api/auth/register` adopts the visitor cart/conversation → Talkshop resumes →
address form → invoice → approve → provider handoff (CH-6) → optional save → order. The website Login page
gets the same generic sign-up. "Link a Chase card" stays optional afterwards.

**FILES LIKELY TO CHANGE.** `backend/routers/auth.py`, `backend/talkshop/orchestrator.py`,
`backend/talkshop/state.py`, `frontend/src/pages/Login.tsx`, `frontend/src/auth/AuthContext.tsx`,
`frontend/src/auth/afterSignIn.ts`, `frontend/src/components/talkshop/cards.tsx`,
`frontend/src/talkshop/TalkshopContext.tsx`, `frontend/src/api/talkshop.ts`, *new* `tests/test_ch7_signup_flow.py`.

**FRONTEND.** Sign-up card in Talkshop; resume after sign-up (the existing `resume` mechanism); no card fields at sign-up.

**BACKEND.** Register: email normalization, duplicate → "sign in instead", wallet/points created as today
(Q3), audit `CUSTOMER_SIGNUP`. Orchestrator resumes the add-to-cart/checkout intent.

**AGENTS.** CartUp: "Customer profile created" trace step. VibeCheck: passwords and emails are never put in the LLM context.

**DATA MODEL.** None (existing `users`, `wallets`, `loyalty_points`).

**API / EVENTS.** Talkshop `login_required{reason:"checkout"}` → `signup_required{options:["sign_up","sign_in"]}`.

**SECURITY.** Sign-up does not link or imply Chase status. Passwords go only to the auth endpoint, never into
the chat transcript (the existing redaction rules are extended to cover them). Rate-limit sign-up attempts per
email/IP (simple in-memory or DB counter).

**EDGE CASES.** Email already exists; visitor cart lines out of stock after sign-up; a new user who later links a
Chase card (benefits appear on the next checkout recalculation).

**TESTS.** Visitor → sign up in Talkshop → resumed checkout → provider approval → order; no saved card
afterwards unless save was ticked; duplicate email; the conversation is preserved.

**ACCEPTANCE.** Scenario 3 completes end-to-end from both the website and Talkshop. Suite green.

**DEPENDENCIES.** CH-2, CH-6.

**ROLLBACK / RISK.** Medium (Talkshop stage machine). Mitigation: reuse the existing `login_required`/`resume` path.

**PROMPT (copy-paste):**
```
Implement Phase CH-7 of docs/shopsphere-cardholder-execution-plan.md: "New ShopSphere user sign-up and checkout
continuation". Make generic sign-up (POST /api/auth/register) the primary path on the Login page and add an
inline sign-up option in Talkshop when a visitor wants to buy; adopt the visitor's cart and conversation, resume
the purchase, collect the address, show the invoice, and pay through the CH-6 provider handoff with optional
save. Sign-up must not imply or link Chase status. Keep passwords out of the transcript/LLM context. Add
tests/test_ch7_signup_flow.py.
<shared prompt footer>
```

---

### CH-8 — Consent / GreenLight / PayIt hardening and error states

**GOAL.** One consistent, explicit approval experience across routes, with clear error states.

**CURRENT PROBLEM.** Approval UX differs by path. `auto_countdown` is still an internal consent mode. The
standard path has no recorded "approval cancelled". GreenLight doesn't re-check benefit eligibility or payment
readiness itself.

**TARGET BEHAVIOUR.** The approval card/page always shows Merchant, Order ref, Items, Subtotal, Discounts,
Eligible benefits, Tax, Shipping, **Final amount**, Currency and the payment method or "new card via secure
provider", with the button **"Approve & Pay $X.XX"**. GreenLight checks: scoped consent matches the current
invoice, benefits revalidated (calls `entitlements.evaluate` again), payment readiness by route. PayIt maps
every outcome to a friendly, specific state: declined, timeout, stale, duplicate, cancelled, session expired,
order-creation failure (voided). `auto_countdown` is removed from backend and types.

**FILES LIKELY TO CHANGE.** `backend/shop/payment.py`, `backend/routers/authorizations.py`,
`backend/agents/greenlight.py`, `backend/payment/guardrail_engine.py` (fold the version into the fingerprint
check; keep 12 checks), `backend/vic/instructions.py`, `backend/talkshop/orchestrator.py`,
`frontend/src/components/talkshop/cards.tsx`, `frontend/src/pages/store/Checkout.tsx`,
`frontend/src/member/PasskeyContext.tsx`, `frontend/src/api/member.ts`, `frontend/src/api/talkshop.ts`,
`tests/test_demo1_every_shopper.py` (`test_auto_countdown_payment_records_how_consent_was_given`),
*new* `tests/test_ch8_consent.py`.

**DATA MODEL.** `payment_authorizations` += `consent_status` (`approved|cancelled`) if not already added in CH-4.
Audit `APPROVAL_CANCELLED`.

**SECURITY.** No charge without an explicit, scoped approval. Cancel is final for that version. Typed GO AHEAD
on the standard path still only re-shows the approval card (unchanged rule).

**TESTS.** Each error state; cancel → no DPAT; the auto_countdown mode is refused; invoice fields present in the
approval event; GreenLight refuses when a benefit became ineligible between invoice and approval.

**ACCEPTANCE.** All three scenarios show the same explicit approval. No silent or automatic payment path exists. Suite green.

**DEPENDENCIES.** CH-4..CH-7.

**ROLLBACK / RISK.** Medium (UI-visible). The guardrail count stays at 12 to avoid UI/test churn.

**PROMPT (copy-paste):**
```
Implement Phase CH-8 of docs/shopsphere-cardholder-execution-plan.md: "Consent / GreenLight / PayIt hardening".
Unify the approval card/page for all routes (full invoice + "Approve & Pay $X"), make GreenLight re-check scoped
consent, benefit eligibility and route readiness before issuing the DPAT, record explicit approval cancellation,
remove the auto_countdown consent mode end-to-end, and map every failure to a specific customer-facing state.
Keep PayIt at 12 checks (fold checkout_version into the fingerprint check). Add tests/test_ch8_consent.py.
<shared prompt footer>
```

---

### CH-9 — VIC / network decoupling

**GOAL.** VIC is a Visa-network capability chosen by network + agent-pay capability. It is not a Chase
feature and not an access check. Everything else uses generic orchestration.

**CURRENT PROBLEM.** VIC requires a Chase `card_accounts` row (`shop/vic.py:30`, `vic/enrollment.py:24-30`).
Control 1 used the Chase gate (switched to access in CH-2). Labels say "Chase Visa".

**TARGET BEHAVIOUR.** `vic` route iff `network == VISA` **and** the card is `agent_pay_capable` (issuer-side
fixture flag; in fixtures only participating issuers' Visa cards, per Q5) **and** the card has an active agent
token. Mastercard/Amex/Discover → `saved_card` or `provider_handoff`, never VIC. A provider-tokenized new
Visa → not VIC (no agent token). VIC internals (controls 3–12, credential, signals) unchanged; control 2 reads
`PaymentCard.network`.

**FILES LIKELY TO CHANGE.** `backend/shop/payment_routing.py`, `backend/shop/vic.py`, `backend/vic/enrollment.py`,
`backend/vic/credentials.py`, `backend/vic/instructions.py`, `backend/shop/offers.py`/`entitlements.py` (the
`visa_agent_pay` cashback uses `requires_network=VISA` + `requires_payment_path=vic`), `backend/db/schema.py`,
`backend/db/init_db.py`, `frontend/src/pages/store/Account.tsx`, `frontend/src/pages/store/Member.tsx`,
`frontend/src/components/talkshop/cards.tsx`, *new* `tests/test_ch9_vic_routing.py`.

**DATA MODEL.** `card_accounts` (or `payment_methods`) += `agent_pay_capable BOOLEAN DEFAULT false`, backfilled true for Chase Visa A/E/H.

**SECURITY.** Unchanged VIC bindings. No new external claims ("VIC-inspired, simulated").

**TESTS.** A (Chase Visa) still VIC; B (Chase MC) never VIC; G (Amex) never VIC; a Visa from a
non-participating issuer → saved_card; a provider Visa → not VIC; all existing VIC tests still pass.

**ACCEPTANCE.** No non-Visa card can reach VIC code. VIC eligibility is independent of the Chase entitlement
logic. Suite green.

**DEPENDENCIES.** CH-5 (CH-6 for the provider-card case).

**ROLLBACK / RISK.** Low-medium. The VIC tests are a strong regression net.

**PROMPT (copy-paste):**
```
Implement Phase CH-9 of docs/shopsphere-cardholder-execution-plan.md: "VIC / network decoupling". Route to VIC
only when the selected PaymentCard's network is VISA, the card is agent_pay_capable, and an active agent token
exists; remove the "must be a Chase card_account" coupling from routing and enrollment; keep VIC internals and
labels honest (VIC-inspired, simulated). Prove with tests that Mastercard/Amex/Discover and provider-tokenized
cards never reach VIC code and that existing VIC tests still pass. Add tests/test_ch9_vic_routing.py.
<shared prompt footer>
```

---

### CH-10 — Technical trace and client-demo UX

**GOAL.** The presenter view tells the business story honestly.

**TARGET BEHAVIOUR.** The trace shows, in order: customer access (type: new / existing; any cardholder) →
card (issuer · network · last4) → offer eligibility (standard ✓, Chase-exclusive ✓/✗ with reason) → benefit
revalidation at checkout → invoice version → scoped consent → payment ownership ("ShopSphere saved token" /
"VIC-inspired" / "Simulated Secure Payment Provider page") → authorization ref → order. The static GUARDRAILS
and PROTOCOLS text is updated (the Eligibility row becomes "Access: any signed-in customer; Chase benefits:
entitlement check"). Protocol labels without a qualifier get one (`ACP`→`ACP-style`, `UCP …`→`UCP-inspired …`,
`AP2`→`AP2-style`). A short demo script for the 3 scenarios goes into the docs.

**FILES LIKELY TO CHANGE.** `backend/talkshop/orchestrator.py`, `backend/shop/payment.py`,
`frontend/src/components/talkshop/TalkshopPanel.tsx`, `frontend/src/components/talkshop/cards.tsx`,
`README.md`, `docs/demo-1-system-workflow.md`, *new* `tests/test_ch10_trace.py`.

**SECURITY.** No secrets, tokens (beyond shortened refs), PAN or email in the trace (existing test extended).

**ACCEPTANCE.** Each scenario's trace reads as the business message. No unqualified protocol claims.

**DEPENDENCIES.** CH-9. **RISK.** Low.

**PROMPT (copy-paste):**
```
Implement Phase CH-10 of docs/shopsphere-cardholder-execution-plan.md: "Technical trace and client-demo UX".
Add trace steps for access, card (issuer/network), offer eligibility with reasons, benefit revalidation, invoice
version, scoped consent, payment-page ownership, authorization and order; qualify every protocol label
(ACP-style, AP2-style, UCP-inspired, VIC-inspired); update the info view's guardrail/protocol text; add a 3-
scenario demo script to README/docs. Extend the "no secrets in trace" test. Add tests/test_ch10_trace.py.
<shared prompt footer>
```

---

### CH-11 — End-to-end and regression testing

**GOAL.** Prove all three scenarios and every negative fixture, and that nothing old broke.

**SCOPE.** API-level E2E tests per scenario (personas F/G, H/A, N), the negative fixture table (§4), a
security scan test (no PAN/CVV patterns in DB dumps, responses, logs, trace, LLM context), the full legacy
suite, `npm run build`, and a manual demo checklist (website + Talkshop, gateway in-process and as a separate
service on :8100).

**FILES LIKELY TO CHANGE.** *new* `tests/test_ch11_e2e_scenarios.py`, `tests/conftest.py` (persona helpers),
`docs/shopsphere-cardholder-execution-plan.md` (final tracker), `README.md`.

**ACCEPTANCE.** Definition of Done met for all three scenarios. Suite + build green. Remaining limitations documented.

**PROMPT (copy-paste):**
```
Implement Phase CH-11 of docs/shopsphere-cardholder-execution-plan.md: "End-to-end and regression testing".
Write tests/test_ch11_e2e_scenarios.py covering Scenario 1 (Citi Mastercard 5678, Amex 9012), Scenario 2
(Chase Visa 1234 and Persona A incl. VIC), Scenario 3 (new sign-up + provider handoff, save on/off) and every
negative fixture in §4; add a PAN/CVV leak scan across DB, responses, trace and Talkshop transcripts; run the full
suite and npm run build; run the gateway as a separate service once and record the manual demo checklist results.
<shared prompt footer>
```

---

## 6. Risks and questions that need your confirmation

| # | Question | My recommendation |
|---|---|---|
| Q1 | **What does "credit-card holder" mean for access?** Must a customer already have a credit card on file to enter, or is any signed-in ShopSphere customer allowed (with the card supplied at payment, as in Scenario 3)? | Any signed-in customer. The credit card is required **at payment** (saved token or provider handoff). Otherwise Scenario 3 contradicts the gate |
| Q2 | Can visitors keep a cart again (as before Part 2), signing in/up only at checkout? | Yes. It makes Scenario 3's "wants to purchase → signup" natural, and the cart-merge code already exists |
| Q3 | ShopSphere Wallet and points: for every customer, or Chase-only? | Points + wallet for every customer (standard ShopSphere features). Chase gets the *bonus* offers |
| Q4 | Offer classification | *(superseded: see Decisions — all offers Chase-exclusive)* |
| Q5 | Do Chase-exclusive benefits require **paying with** the Chase card, or just holding one? | Paying with an eligible Chase credit card for discounts/cashback (matches the brief's "user changes card" rule). Holding one is enough to *see* them |
| Q6 | VIC eligibility for Visa cards from other issuers? | Keep only Chase Visa fixtures VIC-capable, but key the logic on `network=VISA + agent_pay_capable`, not "issuer = Chase" (CH-9) |
| Q7 | Keep personas A–E and add F/G/H, or replace them with the brief's exact `CARD_001/002/003`? | Keep A–E (tests + existing demo script) and **add** F (Citi MC 5678), G (Amex 9012), H (Chase Visa 1234) |
| Q8 | `POST /api/auth/reset-password` is unauthenticated (anyone can reset anyone's password). Fix in CH-2? | Yes. Saving payment methods relies on account identity. Restrict it to the signed-in user, or to a demo-only flag |
| Q9 | Between CH-2 and CH-6, new customers without a card would still see the legacy PAN form. Hide it immediately in CH-2? | Hide it behind a "legacy demo" flag in CH-2. Wallet is the interim payment for card-less customers until CH-6 |
| Q10 | A new user who tokenizes a Chase-issued Visa through the provider: Chase benefits? | No. Chase benefits require a verified Chase relationship (linked account), not a provider issuer hint. They appear after "Link a Chase card" |
| Q11 | Guest checkout (Phase 10, retired in Part 2): revive it, or keep Scenario 3 as sign-up only? | Keep it retired. Scenario 3 is sign-up. Fewer identity paths to secure |
| Q12 | Where the provider hosted page runs | Inside the existing mock gateway service (own origin on :8100 when run separately; in-process for tests). No new service to operate |

### Decisions recorded (2026-10-13)

| # | Decision |
|---|---|
| Q1 | ✅ **Refined (2026-10-13, second answer):** three levels. **Browse** (search, product pages, Talkshop, cart) is open to everyone, visitors included. **Checkout** needs a signed-up / signed-in ShopSphere account. **Paying** needs a **credit-card holder**: an active credit card on file (CH-2), or a card supplied through the secure provider (from CH-6). Issuer is never part of this check |
| Q2 | ✅ Visitors search, browse and keep a cart; sign-up / sign-in happens at checkout |
| Q3 | ✅ Wallet and ShopSphere points are for **every** customer, **but Chase credit-card holders earn more**: 2× ShopSphere points when they pay with an eligible Chase credit card (CH-3; payment-bound like other Chase benefits) |
| Q4 | ✅ **Changed: every offer is Chase-exclusive.** There are **no standard offers** for non-Chase customers (no `OFF_WELCOME10`). `OFF_RUN15`, `OFF_AUDIO20`, `OFF_STYLE3X`, `OFF_AGENTPAY10` (+ Visa + VIC path) all require an eligible Chase credit card. Non-Chase customers get standard ShopSphere pricing, wallet and 1× points |
| Q5 | ✅ **Yes**: Chase-exclusive discounts/cashback/points boost require paying with an eligible Chase credit card |
| Q6, Q7, Q10, Q11, Q12 | ✅ As recommended (user: "everything else is perfect") |
| Q8 | ✅ **Yes**: fix the unauthenticated password reset in CH-2 |
| Q9 | ✅ **Yes**: hide the legacy PAN/CVC form in CH-2. Until CH-6, a customer with no credit card on file can review a checkout but can't pay ("add a credit card"), consistent with the credit-card-holder rule |

**Top risks:** (1) CH-2 and CH-4 test churn (mitigated by helpers and by listing every changed expectation);
(2) the CH-6 subsystem size (mitigated by reusing the gateway app and PayIt's processor route); (3) offer
re-classification changing persona A's demo totals (documented per phase); (4) the interim states between
phases (mitigated by the CH-2 offer guard and the PAN-form flag).
