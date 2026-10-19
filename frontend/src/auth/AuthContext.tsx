/**
 * Who is shopping. Everyone gets an identity straight away: a browser with no
 * login becomes an anonymous *visitor* (Demo 1, Phase 8), so the cart and
 * Talkshop work without an account. Logging in or signing up turns the
 * visitor into a customer — the server moves their cart and Talkshop
 * conversation into the account and reports how cart line ids changed.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { TOKEN_KEY, clearToken, getToken, setToken } from "../api/client";

export interface User {
  user_id: string;
  name: string;
  email: string;
  is_visitor: boolean;
}

interface AuthResult { merged_lines: Record<string, string> }

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  isCustomer: boolean;
  login: (email: string, password: string) => Promise<AuthResult>;
  register: (name: string, email: string, password: string) => Promise<AuthResult>;
  /** Optional: sign up AND link Chase benefits — name + email + last 4 must match the (simulated) Chase records. */
  registerCardmember: (fullName: string, email: string, password: string, last4: string) => Promise<AuthResult>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Pages that need an account (see RequireCustomer in App.tsx). */
const CUSTOMER_ONLY = /^\/(checkout|orders)(\/|$)/;

/** Per-tab shopping state (the Talkshop conversation, a pending checkout). */
function clearTabState() {
  try {
    Object.keys(sessionStorage)
      .filter((k) => k.startsWith("talkshop_") || k.startsWith("ss_"))
      .forEach((k) => sessionStorage.removeItem(k));
  } catch { /* private mode */ }
}

async function authCall(url: string, body?: unknown): Promise<{ access_token: string; user: User; merged_lines: Record<string, string> }> {
  const token = getToken();
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail
      : typeof data.detail?.message === "string" ? data.detail.message : "Something went wrong — please try again.");
  }
  return res.json();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  // Bumped on every login/logout. A visitor session that comes back after the
  // shopper has already logged in is stale and must not replace the login.
  const identity = useRef(0);
  const started = useRef(false);

  const startVisitor = useCallback(async () => {
    const mine = identity.current;
    const data = await authCall("/api/auth/visitor");
    if (identity.current !== mine) return;
    setToken(data.access_token);
    setUser(data.user);
  }, []);

  // Restore the saved session, or start a visitor one — never a login wall.
  useEffect(() => {
    if (started.current) return;          // once per page load (dev mode runs effects twice)
    started.current = true;
    const token = getToken();
    const restore = token
      ? fetch("/api/auth/me", { headers: { Authorization: `Bearer ${token}` } })
          .then((r) => (r.ok ? r.json() : Promise.reject()))
          .then((u: User) => setUser(u))
      : Promise.reject();
    // The backend may be briefly unavailable (e.g. restarting): keep trying
    // rather than leave the page with nobody signed in.
    const visitorWithRetry = async () => {
      for (let attempt = 0; ; attempt++) {
        try { return await startVisitor(); } catch (e) {
          if (attempt >= 5) throw e;
          await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
        }
      }
    };
    restore.catch(() => visitorWithRetry()).catch(() => setUser(null)).finally(() => setLoading(false));
  }, [startVisitor]);

  // Every tab shares one saved login. When another tab logs in, logs out or
  // loses the session, follow it, so no tab shows a stale name while sending
  // someone else's (or no) token.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== TOKEN_KEY && e.key !== null) return;
      // Logged out (or lost) elsewhere: a page that needs an account goes to
      // Home, like the tab that logged out, not to "log in to come back here".
      const loggedOut = e.key === null || e.newValue === null;
      if (loggedOut && CUSTOMER_ONLY.test(window.location.pathname)) window.location.replace("/");
      else window.location.reload();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const signIn = useCallback(async (url: string, body: unknown): Promise<AuthResult> => {
    const wasCustomer = user && !user.is_visitor;
    const data = await authCall(url, body);
    identity.current += 1;
    // Same shopper going from visitor → customer keeps their conversation;
    // switching accounts starts clean.
    if (wasCustomer) clearTabState();
    setToken(data.access_token);
    setUser(data.user);
    return { merged_lines: data.merged_lines ?? {} };
  }, [user]);

  const login = useCallback((email: string, password: string) => signIn("/api/auth/login", { email, password }), [signIn]);
  const register = useCallback((name: string, email: string, password: string) =>
    signIn("/api/auth/register", { name, email, password }), [signIn]);
  const registerCardmember = useCallback((full_name: string, email: string, password: string, last4: string) =>
    signIn("/api/auth/register-cardmember", { full_name, email, password, last4 }), [signIn]);

  const logout = useCallback(async () => {
    clearTabState();                 // a fresh chat and an empty visitor cart
    identity.current += 1;
    clearToken();
    await startVisitor().catch(() => { clearToken(); setUser(null); });
  }, [startVisitor]);

  return (
    <AuthContext.Provider value={{ user, loading, isCustomer: !!user && !user.is_visitor, login, register, registerCardmember, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
