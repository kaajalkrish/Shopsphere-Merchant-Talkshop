/**
 * Simulated passkey authentication (Part 2) — one dialog shared by the website
 * checkout, the Account page and Talkshop AI.
 *
 * This is NOT WebAuthn and NOT a Visa passkey. The server issues a random,
 * single-use challenge; the member's tap here returns it with
 * user_verified=true (or false for the "fail" demo). The dialog says so.
 *
 * Flows built on it:
 *   approveAgentPayment(checkoutId) → payment instruction AUTHENTICATED → instruction id
 *   enrollAgentPay(cardAccountId)   → agent token ACTIVE
 */
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { Fingerprint, ShieldCheck, X } from "lucide-react";
import { vic } from "../api/member";
import { money } from "../api/shop";

type Outcome = "verified" | "failed" | "cancelled";
interface Prompt { title: string; lines: [string, string][]; purpose: "payment" | "enrollment" }

interface PasskeyContextValue {
  /** Visa path: create the instruction for this exact checkout and authenticate it.
   *  Resolves to the instruction id, or throws (cancelled / failed / server refusal). */
  approveAgentPayment: (checkoutId: string, info: { amount: number; card?: string },
                        consentMode?: "button" | "typed") => Promise<string>;
  enrollAgentPay: (cardAccountId: string, card: string) => Promise<void>;
  open: boolean;
}

export class PasskeyCancelled extends Error {}

const PasskeyContext = createContext<PasskeyContextValue | null>(null);

export function PasskeyProvider({ children }: { children: ReactNode }) {
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const resolver = useRef<((o: Outcome) => void) | null>(null);

  const ask = useCallback((p: Prompt) => new Promise<Outcome>((resolve) => {
    resolver.current = resolve;
    setPrompt(p);
  }), []);
  const answer = (o: Outcome) => { setPrompt(null); resolver.current?.(o); resolver.current = null; };

  const approveAgentPayment = useCallback(async (checkoutId: string, info: { amount: number; card?: string },
                                                 consentMode: "button" | "typed" = "button") => {
    const { instruction, passkey } = await vic.createInstruction(checkoutId, consentMode);
    const outcome = await ask({
      title: "Approve this payment", purpose: "payment",
      lines: [["Merchant", "ShopSphere"], ["Amount", money(instruction.amount)], ["Card", info.card ?? "Visa"],
              ["Agent", "Talkshop AI (one purchase only)"]],
    });
    if (outcome === "cancelled") {
      await vic.cancel(instruction.instruction_id).catch(() => {});
      throw new PasskeyCancelled("Approval cancelled. Nothing was charged.");
    }
    await vic.authenticate(instruction.instruction_id, passkey, outcome === "verified");   // throws on failure
    return instruction.instruction_id;
  }, [ask]);

  const enrollAgentPay = useCallback(async (cardAccountId: string, card: string) => {
    const { passkey } = await vic.enrollStart(cardAccountId);
    const outcome = await ask({
      title: "Set up Visa agent payments", purpose: "enrollment",
      lines: [["Card", card], ["Agent", "Talkshop AI"], ["What it allows", "Purchases you approve, one at a time"]],
    });
    if (outcome === "cancelled") throw new PasskeyCancelled("Setup cancelled.");
    await vic.enrollComplete(cardAccountId, passkey, outcome === "verified");
  }, [ask]);

  return (
    <PasskeyContext.Provider value={{ approveAgentPayment, enrollAgentPay, open: !!prompt }}>
      {children}
      {prompt && <PasskeyDialog prompt={prompt} onAnswer={answer} />}
    </PasskeyContext.Provider>
  );
}

function PasskeyDialog({ prompt, onAnswer }: { prompt: Prompt; onAnswer: (o: Outcome) => void }) {
  return (
    <div className="fixed inset-0 z-[100] grid place-items-center bg-navy/60 backdrop-blur-sm p-4" role="dialog"
      aria-modal="true" aria-labelledby="passkey-title">
      <div className="w-full max-w-sm rounded-3xl bg-canvas shadow-float overflow-hidden">
        <div className="bg-navy text-white px-5 pt-5 pb-4 relative">
          <button onClick={() => onAnswer("cancelled")} aria-label="Close" className="absolute right-3 top-3 p-1.5 rounded-full hover:bg-white/10">
            <X size={16} />
          </button>
          <p className="text-[10px] font-semibold tracking-[0.18em] text-gold uppercase">Simulated passkey authentication</p>
          <h2 id="passkey-title" className="text-lg font-semibold mt-1">{prompt.title}</h2>
          <p className="text-xs text-white/70 mt-1">Demo only: not a real Visa passkey and not WebAuthn.</p>
        </div>
        <div className="p-5 flex flex-col gap-4">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
            {prompt.lines.map(([k, v]) => (
              <div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="font-medium text-right">{v}</dd></div>
            ))}
          </dl>
          <button autoFocus onClick={() => onAnswer("verified")}
            className="h-12 rounded-full bg-btn text-btn-ink font-medium inline-flex items-center justify-center gap-2 hover:opacity-90">
            <Fingerprint size={18} /> Verify with passkey (simulated)
          </button>
          <div className="flex items-center justify-between text-xs">
            <button onClick={() => onAnswer("cancelled")} className="text-muted hover:text-ink underline">Cancel</button>
            <button onClick={() => onAnswer("failed")} className="text-muted hover:text-bad underline" title="Demo: show a failed authentication">
              Simulate a failed check
            </button>
          </div>
          <p className="flex items-start gap-2 text-[11px] text-muted leading-snug">
            <ShieldCheck size={14} className="shrink-0 mt-0.5 text-good" />
            {prompt.purpose === "payment"
              ? "Approves this exact order only: same merchant, items and total. If anything changes, you'll be asked again."
              : "Links your Visa to Talkshop AI for purchases you approve. No card number is shared; you can suspend or revoke it any time."}
          </p>
        </div>
      </div>
    </div>
  );
}

export function usePasskey(): PasskeyContextValue {
  const ctx = useContext(PasskeyContext);
  if (!ctx) throw new Error("usePasskey must be used within PasskeyProvider");
  return ctx;
}
