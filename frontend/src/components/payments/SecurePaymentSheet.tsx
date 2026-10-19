/**
 * Secure Payment Sheet — the new-card payment surface inside the Talkshop panel.
 *
 * It slides up over the panel (no new window, no navigation, the conversation stays
 * underneath) and is styled unlike the chat, so it's clear the customer is no longer
 * talking to the AI: they're on the payment provider's secure page. The card form
 * itself is the PROVIDER's page in a cross-origin iframe: Talkshop and ShopSphere
 * can't read what's typed into it. The provider tokenizes the card, runs the (simulated)
 * payment verification when the issuer asks for one, and tells this sheet only safe
 * progress (step, card network + last 4) and the final status. ShopSphere then checks
 * the result with the provider server-to-server, and PayIt asks for the authorization
 * after GreenLight's and PayIt's own checks: the sheet never authorizes anything.
 *
 * Simulation: the provider is a demo stand-in that accepts test cards only. Nothing
 * here is PCI, bank, Visa or Chase secured, and the UI never says so.
 */
import { useEffect, useRef, useState } from "react";
import { Check, Lock, ShieldCheck, X } from "lucide-react";
import { money } from "../../api/shop";
import { cx } from "../ui";

export type SheetStep = "card" | "verify" | "authorize";
const STEPS: { key: SheetStep; label: string }[] = [
  { key: "card", label: "Card details" }, { key: "verify", label: "Verify" }, { key: "authorize", label: "Authorize" },
];
const ENDED: Record<string, string> = {
  CANCELLED: "You cancelled the payment verification. Nothing was charged.",
  VERIFICATION_FAILED: "The payment couldn't be verified, so nothing was charged.",
  EXPIRED: "This payment session expired. Nothing was charged.",
};
const NETWORK_NAMES: Record<string, string> = { VISA: "Visa", MASTERCARD: "Mastercard", AMEX: "American Express", DISCOVER: "Discover" };

type Phase =
  | { kind: "card" }
  | { kind: "securing" }
  | { kind: "secured"; card: string }
  | { kind: "verify"; card?: string }
  | { kind: "verified"; card?: string }
  | { kind: "failed"; title: string; message: string };

export interface SecurePaymentSheetProps {
  merchant: string;
  amount: number;
  currency: string;
  hostedUrl: string;
  sessionId: string;
  /** The provider reported a final status (session + status only): let ShopSphere check it. */
  onResult: (status: string) => void;
  /** Collapse the sheet back to the conversation (after a result). */
  onClose: () => void;
  /** ✕ before finishing: release the payment session and go back to the review (nothing charged). */
  onAbandon: () => void;
}

export default function SecurePaymentSheet({ merchant, amount, currency, hostedUrl, sessionId, onResult, onClose, onAbandon }: SecurePaymentSheetProps) {
  const [phase, setPhase] = useState<Phase>({ kind: "card" });
  const [shown, setShown] = useState(false);
  const reported = useRef(false);
  useEffect(() => { const f = requestAnimationFrame(() => setShown(true)); return () => cancelAnimationFrame(f); }, []);

  let providerOrigin = "", providerHost = "";
  try { const u = new URL(hostedUrl); providerOrigin = u.origin; providerHost = u.host; } catch { /* bad url */ }

  useEffect(() => {
    if (!providerOrigin) return;
    const onMessage = (e: MessageEvent) => {
      // Only the provider's own frame, only this payment session, only safe fields.
      if (e.origin !== providerOrigin || e.data?.session !== sessionId) return;
      const cardOf = () => e.data?.network && /^\d{4}$/.test(String(e.data?.last4 ?? ""))
        ? `${NETWORK_NAMES[String(e.data.network)] ?? "Card"} •••• ${e.data.last4}` : undefined;
      if (e.data?.type === "shopsphere-provider-step") {
        const step = String(e.data.step ?? "");
        if (step === "card") setPhase({ kind: "card" });           // (back to the form, e.g. a card it refused)
        else if (step === "securing") setPhase({ kind: "securing" });
        else if (step === "tokenized") setPhase({ kind: "secured", card: cardOf() ?? "Card" });
        else if (step === "verify") setPhase((p) => ({ kind: "verify", card: cardOf() ?? ("card" in p ? p.card : undefined) }));
        return;
      }
      if (e.data?.type !== "shopsphere-provider-result" || reported.current) return;
      const status = String(e.data.status ?? "");
      reported.current = true;
      if (status === "VERIFIED") {          // card secured + verified: PayIt authorizes next, back in Talkshop
        setPhase((p) => ({ kind: "verified", card: cardOf() ?? ("card" in p ? p.card : undefined) }));
        window.setTimeout(() => { onResult(status); onClose(); }, 1400);
      } else {
        setPhase({ kind: "failed", title: status === "CANCELLED" ? "Verification cancelled" : "Payment not completed",
          message: ENDED[status] ?? "The payment wasn't completed. Nothing was charged." });
        window.setTimeout(() => { onResult(status); onClose(); }, 1800);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [providerOrigin, sessionId, onResult, onClose]);

  const current: SheetStep = phase.kind === "verified" ? "authorize"
    : phase.kind === "verify" || phase.kind === "secured" ? "verify" : "card";
  const doneIdx = STEPS.findIndex((s) => s.key === current);
  const finished = phase.kind === "verified" || phase.kind === "failed";

  return (
    <div className="absolute inset-0 z-[60] flex flex-col pointer-events-auto" role="dialog" aria-modal="true"
      aria-labelledby="secure-payment-title">
      <div className={cx("absolute inset-0 bg-navy/55 backdrop-blur-[2px] transition-opacity duration-300", shown ? "opacity-100" : "opacity-0")}
 />
      <div className={cx("absolute inset-x-0 bottom-0 top-6 flex flex-col rounded-t-3xl bg-canvas shadow-float overflow-hidden",
        "transition-transform duration-300 ease-out", shown ? "translate-y-0" : "translate-y-full")}>
        {/* Secure-session header: unlike the chat, on purpose */}
        <div className="bg-navy text-white px-5 pt-4 pb-4 relative shrink-0">
          <button onClick={finished ? onClose : onAbandon} aria-label="Close secure payment" title="Close (nothing is charged)"
            className="absolute right-3 top-3 p-1.5 rounded-full hover:bg-white/10"><X size={16} /></button>
          <p className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.18em] text-gold uppercase">
            <ShieldCheck size={15} /> Secure payment</p>
          <p className="text-[11px] text-white/60 mt-0.5">Simulated payment provider{providerHost ? ` · ${providerHost}` : ""}</p>
          <div className="mt-3 flex items-end justify-between gap-3">
            <div>
              <p id="secure-payment-title" className="text-sm text-white/80">{merchant}</p>
              <p className="text-[26px] leading-tight font-semibold tabular-nums">{money(amount)} <span className="text-sm font-medium text-white/70">{currency}</span></p>
            </div>
            <Lock size={22} className="text-white/40 mb-1" />
          </div>
          <ol className="mt-4 grid grid-cols-3 gap-2" aria-label="Payment progress">
            {STEPS.map((s, i) => {
              const done = i < doneIdx, active = i === doneIdx && phase.kind !== "failed";
              return (
                <li key={s.key} className="flex flex-col gap-1.5">
                  <span className={cx("h-1 rounded-full", done ? "bg-[#86e0ad]" : active ? "bg-white" : "bg-white/20")} />
                  <span className={cx("text-[11px] flex items-center gap-1", done ? "text-[#86e0ad]" : active ? "text-white" : "text-white/50")}>
                    {done && <Check size={11} />}{i + 1}. {s.label}</span>
                </li>
              );
            })}
          </ol>
        </div>

        <p className="shrink-0 flex items-start gap-2 px-5 py-2.5 text-[11px] text-ink-soft bg-panel border-b border-line leading-snug">
          <Lock size={13} className="shrink-0 mt-px text-talk" />
          Payment details are handled inside the secure payment experience. Talkshop receives only the payment result
          and masked card information, never your card number or CVC.
        </p>

        <div className="relative flex-1 min-h-0">
          <iframe src={hostedUrl} title="Simulated payment provider: secure card entry"
            className={cx("absolute inset-0 w-full h-full border-0 bg-white", finished && "invisible")}
            sandbox="allow-forms allow-scripts allow-same-origin allow-top-navigation-by-user-activation" />
          {phase.kind === "securing" && <Overlay><Spinner /> Securing your payment method…</Overlay>}
          {phase.kind === "verified" && (
            <Overlay>
              <Ok>Card secured</Ok>{phase.card && <p className="text-sm text-muted -mt-1">{phase.card}</p>}
              <Ok>Payment verified</Ok>
              <p className="text-sm text-muted mt-1 max-w-[280px]">Returning to Talkshop: ShopSphere checks the order, then
                authorizes {money(amount)} and creates your order…</p>
            </Overlay>
          )}
          {phase.kind === "failed" && (
            <Overlay><p className="text-base font-semibold text-bad">{phase.title}</p>
              <p className="text-sm text-muted text-center max-w-[260px]">{phase.message}</p>
              <p className="text-sm text-muted mt-1">Returning to Talkshop…</p></Overlay>
          )}
        </div>

        <div className="shrink-0 px-5 py-2.5 border-t border-line flex items-center justify-between gap-3 text-[11px] text-muted">
          <span className="flex items-center gap-1.5"><ShieldCheck size={13} className="text-good" /> Protected payment session</span>
          <span className="rounded-full bg-gold-soft text-gold px-2 py-0.5 font-semibold tracking-wide">SIMULATED FOR DEMO</span>
        </div>
        <p className="shrink-0 px-5 pb-3 -mt-1 text-[11px] text-faint">Demo payment environment: use test card details only.</p>
      </div>
    </div>
  );
}

function Overlay({ children }: { children: React.ReactNode }) {
  return <div className="absolute inset-0 bg-canvas flex flex-col items-center justify-center gap-2 px-6 text-center">{children}</div>;
}

function Ok({ children }: { children: React.ReactNode }) {
  return <p className="flex items-center gap-2 text-base font-semibold text-good"><span className="w-6 h-6 rounded-full bg-good-soft grid place-items-center"><Check size={14} /></span>{children}</p>;
}

function Spinner() {
  return <span className="w-8 h-8 rounded-full border-[3px] border-line border-t-talk animate-spin" aria-hidden />;
}
