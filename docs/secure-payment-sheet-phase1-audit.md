# Secure Payment Sheet — Phase 1 audit (new user / no saved card, inside Talkshop)

> Branch `demo-1-cardmember-vic`, after CH-11 and the follow-up provider-iframe fix (2026-10-13).
> **Audit only. No implementation changes.** (An additive schema edit started before the full brief arrived was reverted.)
> Everything payment-related here is a **simulation**. Nothing is PCI, bank, Visa or Chase secured.

## 0. Correction to the brief's "important observations"

The brief describes the code **before CH-6**. On the current branch:

| Brief says | Current code |
|---|---|
| `CardForm` collects number / expiry / CVC / name | `CardForm` is a **notice with a "Pay with a new card" button** (`frontend/src/components/shopsphere/CheckoutForms.tsx:44-70`); it has no card fields |
| `CardForm` calls `shop.addCard(...)` | `shop.addCard` was **removed** from `frontend/src/api/shop.ts` |
| `/api/me/payment-methods` accepts `CardRequest{number, cvc, …}` | `CardRequest` was removed; `POST /api/me/payment-methods` **always returns 410 `PAN_NOT_ACCEPTED`** and never reads the body (`backend/routers/shop.py`) |

The parts of the brief about `PasskeyContext.tsx`, `backend/vic/passkey.py` and the VIC tests are still accurate.

## 1. How card entry works today

```
Talkshop review card ── "Approve & Pay $X" (go_ahead, scoped to checkout_version + amount + currency + return_url)
   │  orchestrator._go_ahead → payment.confirm → path "provider_handoff"
   │  → _start_provider_session (backend/shop/payment.py:420): server-to-server POST /v1/payment_sessions
   │    bound to merchant + checkout + payment_ref + version + amount + currency; checkout → awaiting_payment
   ▼
Talkshop event `provider_handoff {checkout_id, payment_session_id, hosted_url, amount, currency}`
   ▼
ProviderHandoffCard (frontend/src/components/talkshop/cards.tsx:709): an <iframe src=hosted_url> INLINE in the chat
transcript (h-560). The iframe document is served by the provider app (backend/gateway/provider.py), origin
:8010/psp (one-process demo) or :8100 (separate service), so it is cross-origin to the store page at :5174.
   ▼
Provider page GET /hosted/{id} (provider.py:256) → card form (name, number, expiry, CVC, save; TEST cards only)
   → POST /hosted/{id}/pay (provider.py:278) → validated in memory → status AUTHORIZED immediately
     (or STEP_UP_REQUIRED for 4000 0027 6000 3184 → POST /hosted/{id}/challenge ok|fail, no nonce)
   → final page postMessage({type:'shopsphere-provider-result', session, status}) to the store origin only
   ▼
ProviderHandoffCard hears it → Talkshop action provider_complete → payment.complete_provider (payment.py:470):
GET /v1/payment_sessions/{id} (safe view) → bindings checked → confirm(... provider_session=…) → GreenLight DPAT
→ PayIt 12 checks → _provider_route (payment.py:534) "captures" the already-authorized session → order → optional save.
```

The website checkout embeds the same iframe on the page (`frontend/src/pages/store/Checkout.tsx:201`).

## 2. Where raw card data travels today

| Hop | PAN / CVC present? |
|---|---|
| Browser: the provider iframe's form fields | Yes, typed by the user. The store page (:5174) **cannot read them** (cross-origin iframe; browser same-origin policy) |
| HTTP POST → provider `/hosted/{id}/pay` | Yes. In the **one-process demo this reaches the same Python process** (the gateway app is mounted at `/psp`); with `MOCK_GATEWAY_URL` it is a separate process |
| Provider `pay()` handler | Used in memory to look up the test-card fixture and pick network + last4, then cleared. Never echoed back (tested) |
| `provider_payment_sessions` table | **No**: network, last4, opaque `pm_mock_…` token, auth reference only |
| ShopSphere tables, API responses, Talkshop events, LLM context, presenter trace, transcripts | **No** (tests: `test_ch6_provider_handoff`, `test_ch10_trace`, `test_zz_leak_scan`) |
| Uvicorn access log | Path only (`/hosted/pps_…/pay`), not the form body |
| Text typed into the Talkshop composer | Masked by `parse.redact_payment_data` (`backend/talkshop/parse.py:173`) and `frontend/src/talkshop/redact.ts` before display, storage or the LLM |

**Gaps relative to the new brief:**

1. The provider **authorizes at card entry**, so the order is: card → authorized → GreenLight → PayIt "capture". The brief wants: card → **tokenized** → **verified** → GreenLight → PayIt **authorization**.
2. There's no tokenization transition ("Securing your payment method…" → "✓ Card secured, Visa •••• 4242").
3. There's no generic verification step for every new card. Only the 3184 test card gets a two-button "step-up" page, with **no one-time challenge**.
4. The iframe sits **inline in the chat transcript**, so it doesn't read as a separate secure surface. There's no secure-session header, progress indicator or automatic collapse.
5. Closing or abandoning the payment can only **cancel the whole checkout**. There's no "close sheet → back to review".
6. The provider's `postMessage` carries only `{session, status}`. That's correct for safety, but the sheet can't show "Card secured, Visa •••• 4242" without a safe step message.

## 3. Passkey components that can be reused visually

`frontend/src/member/PasskeyContext.tsx:78` `PasskeyDialog`:

- **Generic presentation:** the navy header band with eyebrow/title/disclaimer, the merchant/amount/card `dl` grid, the large fingerprint button, the Cancel and "Simulate a failed check" links, and the green-shield footnote.
- **Visa-specific semantics:** the copy ("Simulated passkey authentication", "not a real Visa passkey", the enrollment purpose), and `approveAgentPayment` / `enrollAgentPay`, which call `/api/vic/*`.

`backend/vic/passkey.py` has a good **pattern**: a random nonce whose sha256 hash is stored, a short expiry, single use, and binding to a subject. But it lives in the VIC package, its `passkey_challenges` purposes are `enrollment|instruction`, and ShopSphere issues those challenges. For a new card, verification belongs to the **provider/issuer simulator**, not ShopSphere. **Recommendation:** copy the pattern into the provider module and don't import or extend `vic/passkey.py`.

## 4. Visa-specific pieces that must stay separate (untouched)

- `backend/vic/*`: enrollment, instructions, controls, credentials, signals, passkey.
- `backend/routers/vic.py`.
- `backend/shop/payment_routing.py::_vic_capable`, and the `vic` / `vic_setup_required` routes.
- Talkshop `approval_required` / `agent_pay_setup` events and their orchestrator branches. These are reached only on the VIC path, which a new card can never take.
- `PasskeyContext` (`approveAgentPayment`, `enrollAgentPay`) and its dialog copy.
- Tests: `test_part2_talkshop_vic.py`, `test_part2_cardmember_vic.py`, `test_ch9_vic_routing.py`, `test_ch5_saved_card_payment.py`.

A new card goes through the provider path only. Saved Mastercard and Amex cards keep the `saved_card` route.

## 5. Recommended modules to add or change

### Provider simulator (`backend/gateway/provider.py`): new state machine, still provider-owned

```
PENDING ──valid test card──► TOKENIZED (token + network + last4; verification_required from the fixture)
   │                            ├─ verification_required = false ─► VERIFIED (frictionless)
   │                            └─ true ─► VERIFICATION_REQUIRED (one-time challenge: nonce hash, 5-min expiry)
   │                                         ├─ verified ─► VERIFIED          ("payment credential ready")
   │                                         ├─ failed   ─► stays, retry (≤ 3 attempts) → then VERIFICATION_FAILED
   │                                         ├─ expired  ─► new challenge issued on the next view
   │                                         └─ cancel   ─► CANCELLED
   ├─ tokenization-failure test card ─► stays PENDING with an error (no payment attempt)
   ▼ (after GreenLight + PayIt's 12 checks)
PayIt: POST /v1/payment_sessions/{id}/authorize  (merchant key; idempotent on payment_ref; bound to amount/currency)
   ─► AUTHORIZED | DECLINED | TIMEOUT ─► (CAPTURED on order) | void on failure
```

Test-card fixtures (test numbers only):

| Card | Outcome |
|---|---|
| 4242 4242 4242 4242 | Verification required → authorized |
| 5555 5555 5555 4444 | Verification required → authorized |
| 3782 822463 10005 | Verification required → authorized |
| 4000 0000 0000 3055 | **Frictionless** (`verification_required = false`) |
| 4000 0000 0000 0101 | Tokenization failure |
| 4000 0000 0000 0002 | Declined at authorization |
| 4000 0000 0000 0119 | Timeout at authorization |

Safe `postMessage` events to the store page, sent to its origin only:

| Event | Payload |
|---|---|
| `{type:'shopsphere-provider-step'}` | `step`: card / securing / tokenized (+ network, last4) / verify / verified / failed / cancelled |
| `{type:'shopsphere-provider-result'}` | `session`, `status` |

**Never:** PAN, CVC, the token or the nonce. The token stays server-to-server; the brief's "Talkshop may receive a token reference" is met on the server through the provider's safe view.

### ShopSphere backend

- **`backend/shop/payment.py`**
  - `complete_provider` expects **VERIFIED** (credential ready).
  - `_provider_route` **authorizes** through the provider instead of capturing an authorization that already exists; a decline or timeout then happens after GreenLight, which is correct.
  - New `abandon_provider()`: void the session and put the checkout back to `open`, so the review returns.
  - Trace events: `SECURE_PAYMENT_SESSION_CREATED`, `PAYMENT_PROVIDER_OPENED`, `CARD_TOKENIZED (Visa •••• 4242)`, `ISSUER_VERIFICATION_REQUIRED`, `USER_VERIFIED`, `PAYMENT_CREDENTIAL_READY`, `GREENLIGHT_CONSENT_VALIDATED`, `DPAT_CHECKS_PASSED`, `PAYMENT_AUTHORIZED`, `ORDER_CONFIRMED`. No PAN, CVC, nonce or token.
- **`backend/gateway/adapter.py`:** add `authorize_payment_session()`.
- **`backend/routers/shop.py`:** add `POST /api/checkouts/{id}/payment-session/{sid}/abandon`.
- **`backend/talkshop/orchestrator.py` and `tools.py`**
  - Review wording for the new-card path: "Your order total is $X" + **Continue to Secure Payment**.
  - A `provider_abandon` action → back to review.
  - On success: "✓ Payment verified · Visa •••• 4242 · $X authorized · Creating your order…" → order confirmed.
- **`backend/db/schema.py` and `init_db.py`:** additive columns on `provider_payment_sessions`: `verification_required`, `challenge_hash`, `challenge_expires_at`, `verification_attempts`, `tokenized_at`, `verified_at`, `cardholder_display`.
- **`backend/shop/profile.py`:** the saved provider card keeps the cardholder display name and expiry metadata (never the CVV or PAN).

### Frontend

- **New `frontend/src/components/payments/SecurePaymentSheet.tsx`**
  - **Placement:** an overlay **inside the Talkshop `<aside>`** (absolute, slides up over the chat; the conversation stays mounted underneath).
  - **Header:** shield, "SECURE PAYMENT", "Simulated payment provider", ShopSphere, $X USD, and "Talkshop doesn't receive your card number or CVC".
  - **Progress:** 1 Card details · 2 Verify · 3 Authorized.
  - **Body:** the provider iframe.
  - **Footer:** "Demo payment environment — use test card details only".
  - **Behaviour:**
    - It listens to the provider's step and result messages and shows "Securing your payment method…" and then "✓ Card secured, Visa •••• 4242".
    - On VERIFIED it collapses by itself and tells Talkshop to complete.
    - "Close" means abandon: back to review, nothing charged.
- **`frontend/src/components/talkshop/TalkshopPanel.tsx`:** mount the sheet; it opens from the latest `provider_handoff`.
- **`frontend/src/components/talkshop/cards.tsx`**
  - Review button label for the new-card path: **Continue to Secure Payment · $X**. VIC and saved cards stay **Approve & Pay $X**.
  - `ProviderHandoffCard` becomes a compact transcript line, e.g. "🔒 Secure payment in progress · Reopen".
  - No card fields anywhere in the review card. This is already true.
- **`frontend/src/api/talkshop.ts` and `api/shop.ts`:** types for the new events and actions, plus `abandonProvider`.
- **`frontend/src/member/PasskeyContext.tsx`:** **unchanged** (see §6).
- **`frontend/src/pages/store/Checkout.tsx`:** optional. It can reuse the sheet as a modal, or keep today's embedded section. The brief targets Talkshop, so I'd keep this out of scope unless you want consistency.

### Verification UI: where it lives

**Recommendation: inside the provider frame**, as provider-rendered HTML in the same design language as `PasskeyDialog`: navy band, "SIMULATED PAYMENT VERIFICATION", the merchant/amount/card/purpose grid, a fingerprint "Verify payment" button, Cancel, and "Simulate failed verification".

- **Why:** verification belongs to the issuer/provider. The one-time nonce then never enters ShopSphere's page, Talkshop's events or the trace. `PasskeyContext` (VIC) is untouched, so the risk to VIC is zero.
- **Alternative (B):** extract a presentational `VerificationDialog` React component from `PasskeyDialog` and render it in the store page. That would move the nonce into ShopSphere's front end, touch the VIC dialog, and blur whose step it is.

## 6. Embedded provider iframe or isolated component?

**Recommendation: keep the embedded provider iframe** (already built), presented inside the new sheet.

- **It isolates the fields.** It's a different origin from the store page, so ShopSphere's and Talkshop's JavaScript **cannot read the card fields**. A React component inside the app would hold the PAN in ShopSphere's own front-end state, which is exactly what the brief wants to avoid.
- **Embedding is restricted.** `frame-ancestors` already limits embedding to ShopSphere's origins, and `postMessage` already targets only the store origin.
- **It's cheap.** The plumbing exists; the work is presentation and the provider state machine.
- **Limitation (documented, never claimed as PCI):** in the one-process demo the provider runs **in the same backend process** (mounted at `/psp`). Running `backend.gateway.app` separately (`MOCK_GATEWAY_URL`) gives it its own process and origin. Either way it's a simulation that accepts test cards only.

## 7. Exact files that would change

| Area | Files |
|---|---|
| Backend | `backend/gateway/provider.py`, `backend/gateway/adapter.py`, `backend/shop/payment.py`, `backend/routers/shop.py`, `backend/talkshop/orchestrator.py`, `backend/talkshop/tools.py`, `backend/shop/profile.py`, `backend/db/schema.py`, `backend/db/init_db.py` |
| Frontend | **new** `frontend/src/components/payments/SecurePaymentSheet.tsx`, `frontend/src/components/talkshop/TalkshopPanel.tsx`, `frontend/src/components/talkshop/cards.tsx`, `frontend/src/api/talkshop.ts`, `frontend/src/api/shop.ts` (optionally `frontend/src/pages/store/Checkout.tsx`) |
| Tests | `tests/conftest.py` (`provider_pay` helper: card → verify), `tests/test_ch6_provider_handoff.py`, `tests/test_ch7_signup_flow.py`, `tests/test_ch9_vic_routing.py`, `tests/test_ch10_trace.py`; **new** `tests/test_secure_payment_sheet.py` (verification binding, cancel/fail/expire/retry, abandon → review, tokenization failure, decline/timeout after verification, duplicate submit, amount change, save on/off, PAN/CVC/nonce absent from events/trace/transcript); extend `tests/test_zz_leak_scan.py` |
| Unchanged | `frontend/src/member/PasskeyContext.tsx`, `backend/vic/*`, `backend/routers/vic.py`, `backend/shop/payment_routing.py` |
| Docs | `docs/shopsphere-cardholder-execution-plan.md` (new section), `README.md` (demo script) |

## 8. Regression risks

| Risk | Mitigation |
|---|---|
| Moving authorization from card entry to PayIt changes what "AUTHORIZED" means at the provider; about 30 tests use the provider helper | Change the helper once (`provider_pay`: card → verify); keep `complete_provider`'s public results (`authorized` / `declined` / `pending`) |
| Decline and timeout now happen after GreenLight/DPAT: a single-use DPAT is consumed and the checkout reopens | Same as the saved-card path today (`reopen`); the retry gets a new payment_ref, and the provider's authorize is idempotent on payment_ref |
| A double click on "Continue Securely" or "Verify payment" | Provider state machine, single-use nonce, and the existing `PAYMENT_IN_PROGRESS` and idempotency keys |
| Closing the sheet now means "abandon → review", not "cancel checkout" | New abandon endpoint; cancelling the order stays separate |
| `postMessage` origin / port mismatch (for example, the frontend falls to :5174) | `FRONTEND_ORIGINS` (defaults already include 5173–5175); a "check status" fallback stays |
| Mobile: the sheet height inside the 88vh panel | Sheet uses the panel's full height with a scrollable body |
| VIC / passkey | No VIC file changes; VIC tests act as the regression net |
| Trace label changes | `test_ch10_trace.py` is updated deliberately; `assert_honest` stays (no PAN, nonce or token) |

## Decisions needed before Phase 2

1. **Verification UI inside the provider frame** (recommended), or the extracted React dialog (option B)?
2. **The token stays server-to-server**: the browser `postMessage` carries only session, step, network and last 4. Is that OK? The brief allows Talkshop a token reference, but nothing needs it in the browser.
3. **Declines and timeouts happen at PayIt authorization** (after verification), not at card entry. Is that OK?
4. **Website checkout:** reuse the sheet there too, or keep the change to Talkshop only, as the brief says?

---

## Progress

| Phase | Status | Notes |
|---|---|---|
| 1 Audit | ✅ Approved 2026-10-13 | Recommendations used for decisions 1–4 (user: "approve phase 2") |
| 2 Secure Payment Sheet UI | ✅ Done, approved | New `frontend/src/components/payments/SecurePaymentSheet.tsx`, an overlay over the whole Talkshop panel (no new window or navigation): shield + "SECURE PAYMENT", "Simulated payment provider · host", merchant, amount, progress *Card details · Verify · Authorized*, the provider iframe, "Talkshop receives only the result and masked card information", "Protected payment session · SIMULATED FOR DEMO", "use test card details only". Opens by itself on a new hand-off; collapse/reopen; on the provider's final status it shows "✓ Payment verified · $X authorized · Returning to Talkshop…" and closes. The transcript card is now a compact "Open secure payment" line (no iframe in the chat). The review button for a new card reads **Continue to Secure Payment · $X** (VIC and saved cards unchanged). The Talkshop hand-off message was reworded. **No payment logic changed.** pytest 515 passed; `tsc` + build clean |
| 2 approval | | User, 2026-10-13 ("approved") |
| 3 Provider session + tokenization | ✅ Done (user: "don't wait for my approval, complete all the phases") | Provider status machine now starts **PENDING → TOKENIZED** (`/hosted/{id}/pay` tokenizes, nothing charged) → `/hosted/{id}/continue`. Safe result via the merchant API: `payment_session_id`, `status: TOKENIZED`, opaque `payment_token` (`pm_mock_…`), `network`, `last4`, `verification_required` (per test-card fixture; `4000 0000 0000 3055` = frictionless). The store page hears only `step` (card / securing / tokenized + network/last4 / verify) and the final status, never the token. New "can't be secured" test card `4000 0000 0000 0101` stops before any payment attempt; a double submit after tokenization doesn't tokenize again. Provider's own header is hidden inside the sheet (the sheet shows merchant + amount); button reads **Continue securely** → "Securing your payment method…" → "✓ Card secured · Visa •••• 4242". **Interim until Phases 4–5:** after tokenization the provider still applies the card's outcome itself (approve / decline / timeout / old step-up). pytest **522 passed** (7 new: `tests/test_payment_provider_tokenization.py`); `tsc` + build clean |
| 4 Generic payment verification | ✅ Done | After tokenization the provider decides per card: **VERIFICATION_REQUIRED** shows a generic screen inside the provider frame ("SIMULATED PAYMENT VERIFICATION · Verify your payment", Merchant / Amount / Card / Purpose *One-time purchase*, fingerprint **Verify payment**, Cancel, *Simulate failed verification*, the "this exact ShopSphere purchase only" footnote, and a note that real issuers may skip it or use OTP / app / biometric / 3DS). Works for any network (no Visa wording on a Mastercard). One-time challenge per view: only its SHA-256 hash is stored, 5-minute expiry, single use; a stale or forged one is refused and an expired one issues a new request. Fail → retry (3 attempts) → **VERIFICATION_FAILED**; Cancel → **CANCELLED** (`CUSTOMER_CANCELLED`). Frictionless card `4000 0000 0000 3055` goes straight to **VERIFIED**. The old step-up card `…3184` is now an ordinary verified approval. The challenge never reaches the store page, Talkshop, the trace or the merchant API |
| 5 Authorization moves to PayIt | ✅ Done | Provider no longer authorizes on its own: merchant endpoint `POST /v1/payment_sessions/{id}/authorize` (replaces `/consume`) accepts only a **VERIFIED** credential, bound to merchant + payment_ref + amount + currency (409 `SESSION_BINDING_MISMATCH` / `CREDENTIAL_NOT_READY`), idempotent on payment_ref (`duplicate: true`, same reference). `complete_provider` waits for VERIFIED (pending = PENDING / TOKENIZED / VERIFICATION_REQUIRED), then GreenLight → 12 DPAT checks → PayIt *Provider authorization* → order. Decline reasons from the card (`CARD_DECLINED`, new `INSUFFICIENT_FUNDS` for `4000 0000 0000 9995`), timeout → `PAYMENT_TIMEOUT`; nothing charged, session can't be reused. Talkshop says **"✓ Payment verified · Visa •••• 4242 · $X authorized. Creating your order…"** before the order card; on a decline or cancelled verification it shows the review again (`checkout_ready`) so another method can be chosen. Sheet: VERIFIED = success ("✓ Card secured · ✓ Payment verified · Returning to Talkshop…"), progress *Card details · Verify · Authorize* |
| 6 Save card metadata | ✅ Done | Save stays unticked by default. When ticked, the provider hands over (and ShopSphere stores) only the opaque token, network, last 4, expiry month/year and the cardholder display name; unticked, the provider doesn't even keep the name/expiry. New provider columns `cardholder_display`, `card_exp_month`, `card_exp_year` (migrated with `_ensure_column`) |
| 7 Error flows, trace, tests | ✅ Done | **Close (✕) = abandon**: `POST /api/checkouts/{id}/payment-session/{sid}/abandon` + Talkshop action `provider_abandon` release the session (CANCELLED), reopen the checkout, audit `APPROVAL_CANCELLED` reason `PAYMENT_SHEET_CLOSED`, and show the review again; after a paid order it is a no-op. The store checkout page got a *Back to review (nothing charged)* link. Trace rows carry `event`: SECURE_PAYMENT_SESSION_CREATED, PAYMENT_PROVIDER_OPENED, CARD_TOKENIZED, ISSUER_VERIFICATION_REQUIRED, USER_VERIFIED, PAYMENT_CREDENTIAL_READY, GREENLIGHT_CONSENT_VALIDATED, DPAT_CHECKS_PASSED, PAYMENT_AUTHORIZED, ORDER_CONFIRMED (audit `PAYMENT_CREDENTIAL_READY` too: network, last 4, verification flag only). New `tests/test_secure_payment_sheet.py` (23): generic screen (Visa + Mastercard), challenge single use / stale / expired, retry, 3 failures, cancel, frictionless, verified ≠ authorized, authorize binding + idempotency, insufficient funds then another card, timeout, duplicate completion, amount change → new approval + new verification, save on/off, close sheet (API + Talkshop), trace order, Talkshop end-to-end redaction (no card number, CVC field, challenge or token in events, transcript or audit), saved-Mastercard regression. Existing provider tests updated for the new statuses |
