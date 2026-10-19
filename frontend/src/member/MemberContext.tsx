/**
 * Who the shopper is (GET /api/member). CH-2: "eligible" means a signed-in
 * ShopSphere customer (any card issuer); "chase" means Chase-exclusive benefits
 * apply. The server decides both; every API enforces them again on its own.
 */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { member as memberApi, type MemberInfo } from "../api/member";

interface MemberContextValue {
  info: MemberInfo | null;
  loading: boolean;
  /** Signed-in ShopSphere customer (checkout access). */
  eligible: boolean;
  /** Chase credit-card holder: Chase-exclusive offers apply. */
  chase: boolean;
  /** Has an active credit card on file. */
  cardHolder: boolean;
  refresh: () => Promise<void>;
}

const MemberContext = createContext<MemberContextValue | null>(null);

export function MemberProvider({ children }: { children: ReactNode }) {
  const { user, loading: authLoading } = useAuth();
  const [info, setInfo] = useState<MemberInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try { setInfo(await memberApi.me()); } catch { setInfo(null); }
  }, []);

  useEffect(() => {
    let live = true;
    if (!user) {                       // no session yet (or it failed): nothing to ask the server
      setInfo(null);
      setLoading(authLoading);
      return;
    }
    setLoading(true);
    memberApi.me().then((m) => { if (live) setInfo(m); }).catch(() => { if (live) setInfo(null); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [user?.user_id, authLoading]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <MemberContext.Provider value={{ info, loading, eligible: !!info?.eligible, chase: !!info?.chase_benefits,
      cardHolder: !!info?.credit_card_holder, refresh }}>
      {children}
    </MemberContext.Provider>
  );
}

export function useMember(): MemberContextValue {
  const ctx = useContext(MemberContext);
  if (!ctx) throw new Error("useMember must be used within MemberProvider");
  return ctx;
}
