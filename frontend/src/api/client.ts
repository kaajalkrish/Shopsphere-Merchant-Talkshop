/**
 * authFetch() — attaches the bearer token to every request.
 * Token is persisted in localStorage so a page refresh doesn't log the user out.
 */
export const TOKEN_KEY = "talkshop_token";

/** Backend origin for production builds (e.g. https://shopsphere-api.onrender.com),
 *  set as VITE_API_BASE_URL in Vercel. Unset locally: paths stay relative and the
 *  Vite dev proxy forwards /api to localhost:8000. */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");

/** "/api/products" → "<API_BASE>/api/products". Absolute URLs pass through unchanged. */
export function apiUrl(path: string): string {
  if (!API_BASE || /^[a-z][a-z\d+.-]*:/i.test(path)) return path;
  return `${API_BASE}/${path.replace(/^\/+/, "")}`;
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Reload, but never more than once in a few seconds (no reload loops). */
function reloadOnce() {
  const KEY = "ss_reloaded_at";
  try {
    const last = Number(sessionStorage.getItem(KEY) || 0);
    if (Date.now() - last < 5000) return;
    sessionStorage.setItem(KEY, String(Date.now()));
  } catch { /* private mode: still reload */ }
  window.location.reload();
}

export async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(apiUrl(url), { ...options, headers });

  // A lost session — expired/invalid token, or no token at all (e.g. another
  // tab cleared the shared one): reload so the page re-establishes who is
  // shopping (the saved login, or a fresh visitor) instead of showing a stale
  // name and failing every request. (403 LOGIN_REQUIRED, for a visitor
  // reaching a customer-only action, is handled by the caller.)
  if (res.status === 401 && !window.location.pathname.startsWith("/login")) {
    if (token) clearToken();
    reloadOnce();
  }

  return res;
}
