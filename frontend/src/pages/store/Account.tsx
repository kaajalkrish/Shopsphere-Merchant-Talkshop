import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BadgeCheck, Bot, CreditCard, FlaskConical, Landmark, Package, ShieldCheck } from "lucide-react";
import { vic, type AgentPayStatus } from "../../api/member";
import { useMember } from "../../member/MemberContext";
import { PasskeyCancelled, usePasskey } from "../../member/PasskeyContext";
import { Button, Notice, Skeleton, cx } from "../../components/ui";

const STATUS_TONE: Record<string, string> = {
  ACTIVE: "bg-good-soft text-good", SUSPENDED: "bg-gold-soft text-gold", PENDING_AUTHENTICATION: "bg-gold-soft text-gold",
  NOT_ENROLLED: "bg-panel text-muted", REVOKED: "bg-bad-soft text-bad",
};

/** Account (CH-2): profile, masked cards (issuer + network), Chase benefits, agent payments, demo controls. */
export default function AccountPage() {
  const { info, refresh } = useMember();
  const { enrollAgentPay } = usePasskey();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const [scenario, setScenario] = useState<string>("none");
  const m = info?.member;
  useEffect(() => { void refresh(); }, [refresh]);
  if (!m) return <div className="mx-auto max-w-[1100px] px-4 sm:px-6 py-10"><Skeleton className="h-64" /></div>;

  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key); setNote(null);
    try { await fn(); await refresh(); setNote({ tone: "good", text: ok }); }
    catch (e) { setNote({ tone: "bad", text: e instanceof PasskeyCancelled ? e.message : (e as Error).message }); }
    finally { setBusy(null); }
  };
  const agentFor = (accountId: string): AgentPayStatus | undefined => m.agent_pay?.find((a) => a.card_account_id === accountId);

  return (
    <div className="mx-auto max-w-[1100px] px-4 sm:px-6 py-10 flex flex-col gap-8">
      <div>
        <p className="text-xs font-semibold tracking-[0.2em] text-gold uppercase">Account</p>
        <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight mt-1">{m.name}</h1>
        <p className="mt-2 inline-flex items-center gap-1.5 text-sm text-good"><BadgeCheck size={16} /> {m.status}
          {m.member_since && <span className="text-muted">· member since {m.member_since}</span>}</p>
      </div>
      {note && <Notice tone={note.tone === "good" ? "good" : undefined}>{note.text}</Notice>}

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold flex items-center gap-2"><Landmark size={18} /> Accounts on file (simulated)</h2>
        <div className="grid md:grid-cols-2 gap-4">
          {m.accounts.map((a) => {
            const agent = agentFor(a.account_id);
            return (
              <div key={a.account_id} className="rounded-2xl border border-line p-5 flex flex-col gap-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-semibold flex items-center gap-2"><CreditCard size={16} /> {a.product_name}</p>
                    <p className="text-sm text-muted mt-0.5">•••• {a.last4} · {a.status}</p>
                  </div>
                  <span className={cx("text-[11px] font-semibold rounded-full px-2.5 py-1",
                    a.marketplace_eligible ? "bg-good-soft text-good" : "bg-panel text-muted")}>
                    {a.marketplace_eligible ? "Chase benefits" : a.account_type === "credit_card" ? "Credit card" : "No card benefits"}
                  </span>
                </div>
                {a.network === "Visa" && a.agent_pay_capable && (
                  <div className="rounded-xl bg-panel p-4 flex flex-col gap-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-medium flex items-center gap-2"><Bot size={16} className="text-talk" /> Visa agent payments</p>
                      <span className={cx("text-[11px] font-semibold rounded-full px-2.5 py-1", STATUS_TONE[agent?.status ?? "NOT_ENROLLED"])}>
                        {(agent?.status ?? "NOT_ENROLLED").replace("_", " ")}
                      </span>
                    </div>
                    <p className="text-xs text-muted leading-relaxed">
                      Lets Talkshop AI pay with this card, only for orders you approve with a passkey.
                      {agent?.agent_token && <> Agent token <span className="font-mono">{agent.agent_token}</span>.</>}
                      {" "}{agent?.label ?? "Simulated agent token (not a Visa Token Service token)"}.
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {(!agent || agent.status === "NOT_ENROLLED" || agent.status === "REVOKED" || agent.status === "PENDING_AUTHENTICATION") && (
                        <Button size="sm" loading={busy === `enroll-${a.account_id}`}
                          onClick={() => run(`enroll-${a.account_id}`, () => enrollAgentPay(a.account_id, `Visa •••• ${a.last4}`),
                            "Visa agent payments are active for this card.")}>
                          Set up agent payments
                        </Button>
                      )}
                      {agent?.status === "ACTIVE" && (
                        <Button size="sm" variant="secondary" loading={busy === `suspend-${a.account_id}`}
                          onClick={() => run(`suspend-${a.account_id}`, () => vic.tokenAction("suspend", a.account_id), "Agent payments suspended.")}>
                          Suspend
                        </Button>
                      )}
                      {agent?.status === "SUSPENDED" && (
                        <Button size="sm" loading={busy === `resume-${a.account_id}`}
                          onClick={() => run(`resume-${a.account_id}`, () => vic.tokenAction("resume", a.account_id), "Agent payments resumed.")}>
                          Resume
                        </Button>
                      )}
                      {agent && ["ACTIVE", "SUSPENDED"].includes(agent.status) && (
                        <Button size="sm" variant="ghost" loading={busy === `revoke-${a.account_id}`}
                          onClick={() => run(`revoke-${a.account_id}`, () => vic.tokenAction("revoke", a.account_id),
                            "Agent token revoked. Open approvals for it were cancelled.")}>
                          Revoke
                        </Button>
                      )}
                    </div>
                  </div>
                )}
                {a.network === "Mastercard" && (
                  <p className="text-xs text-muted">Pays through the standard card path (no Visa agent token).</p>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section className="grid md:grid-cols-2 gap-4">
        <Link to="/orders" className="rounded-2xl border border-line p-5 hover:border-line-strong flex items-center gap-3">
          <Package size={20} /> <span><span className="block font-semibold">Orders and purchase history</span>
          <span className="text-sm text-muted">Every order, with tracking</span></span>
        </Link>
        <Link to="/rewards" className="rounded-2xl border border-line p-5 hover:border-line-strong flex items-center gap-3">
          <ShieldCheck size={20} /> <span><span className="block font-semibold">{m.rewards_balance.toLocaleString()} rewards points</span>
          <span className="text-sm text-muted">Ledger of points, offers and cashback</span></span>
        </Link>
      </section>

      <section className="rounded-2xl border border-dashed border-line-strong p-5 flex flex-col gap-3">
        <h2 className="text-base font-semibold flex items-center gap-2"><FlaskConical size={17} /> Demo controls: next payment outcome</h2>
        <p className="text-xs text-muted">Sets how the simulated gateway / issuer answers your next card payment (used once).</p>
        <div className="flex flex-wrap gap-2">
          {(["none", "decline", "timeout", "duplicate"] as const).map((o) => (
            <button key={o} onClick={() => run(`scn-${o}`, async () => { await vic.scenario(o); setScenario(o); },
                o === "none" ? "Next payment: normal." : `Next payment: ${o} (simulated).`)}
              className={cx("h-9 px-4 rounded-full border text-sm", scenario === o ? "border-ink bg-panel font-medium" : "border-line hover:border-ink")}>
              {{ none: "Normal", decline: "Issuer declines", timeout: "Network timeout", duplicate: "Duplicate submission" }[o]}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
