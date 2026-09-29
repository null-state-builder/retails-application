import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { authApi, authSession, unitContext } from "../lib/api";
import type { operations } from "../lib/api-schema";
import { storeFeaturesOn } from "../shell/navConfig";

export interface Store {
  id: number;
  code: string;
  name: string;
  store_type: string;
  state_name: string;
  state_code: string;
  gstin_number: string;
}

export interface Brand {
  id: number;
  code: string;
  name: string;
}

export interface Role {
  code: string;
  name: string;
  landing_page: string;
  nav_groups: string[];
}

// SIDEBAR RBAC contract (issue #85): the server decides what each person may
// see and do. These describe the new authenticated-user payload; the shell
// re-housing (#87) consumes them. Optional here so nothing that reads the
// legacy `nav_groups` shell needs to change in this contract-only slice.
export interface NavSection {
  code: string;
  label: string;
  order: number;
  capability: "view" | "operate" | "approve" | "manage";
  scope_label: string; // exact RBAC-sheet wording, e.g. "Own store"
}

export interface User {
  id: number;
  username: string;
  full_name: string;
  is_superuser: boolean;
  role: Role | null;
  scope_type: string;
  scope_label: string;
  entity: number | null;
  entity_name?: string;
  stores: Store[];
  nav_groups: string[];
  landing_page: string;
  // New RBAC contract (may be absent when talking to an older backend).
  sections?: NavSection[];
  capabilities?: Record<string, NavSection["capability"]>;
  /** Stored actor policies this person satisfies — the questions the capability
   *  ladder cannot ask, because two roles can share a rung of one section and
   *  still differ on a single action inside it (#75). */
  actions?: string[];
  business_units?: Store[];
  all_business_units?: boolean;
  // What the top-bar switcher offers (issue #88). A brand manager works across
  // every store inside their own brands, so they get a brand filter where
  // everyone else gets a unit list. Both come from the server — never inferred
  // here from the role code.
  business_unit_mode?: "units" | "brands";
  assigned_brands?: Brand[];
  /** Whether this person has a counter PIN, and whether they are somebody who
   *  could hold one (#182). Never the hash: that goes to a till in its dataset
   *  and nowhere else. */
  has_till_pin?: boolean;
  may_hold_till_pin?: boolean;
}

/** E002's answer, straight from the generated schema (ticket 03A review-fix:
 *  `must_change_password` used to be declared here by hand). */
type SessionResponse =
  operations["auth_me_retrieve"]["responses"][200]["content"]["application/json"];

/** `SessionDTO` (goods-v1 design §6.1): who is signed in and what they may do.
 *  `user.must_change_password` (GSA-T03/ticket 03A): an administrator issued a
 *  temporary password that has not yet been replaced. Until it clears, the
 *  server refuses everything but this session's own lifecycle and
 *  `/auth/change-password` (`PASSWORD_CHANGE_REQUIRED`) — `ProtectedRoute`
 *  sends the person straight to `/change-password` on every route. */
export type GoodsSession = Omit<SessionResponse, "profile" | "csrf_token">;

/** The same answer with the legacy shell profile typed as this app reads it. */
interface SessionPayload extends GoodsSession {
  profile: User;
  csrf_token?: string;
}

interface AuthContextValue {
  user: User | null;
  /** The goods-v1 session (actions, field grants, sites) for the signed-in person. */
  session: GoodsSession | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<{ mustChangePassword: boolean }>;
  logout: () => void;
  /** Set when a request came back 401 while a session was already open —
   *  most often because a grant this person relied on was just revoked
   *  (goods-v1 design §4.2, GSA-T03: revocation ends the session at once,
   *  even inside a journey already under way). `Login` reads this to show
   *  "Your access changed. Sign in again." instead of a bare empty form;
   *  a normal, deliberate `logout()` never sets it. */
  sessionExpired: boolean;
  clearSessionExpired: () => void;
  /** Set once, by `ChangePassword` (E239), when this login just replaced its
   *  own password: the server already ended every session, so this only
   *  clears local state and gives `Login` the note to show. `Login` here
   *  rather than a react-router `location.state` — that state can race the
   *  redirect `ChangePassword`'s own `!user` guard fires as `endSession()`
   *  lands, same as `sessionExpired` already avoids that for revocation. */
  passwordChanged: boolean;
  completePasswordChange: () => void;
  clearPasswordChanged: () => void;
  /** Re-read the session from the server (E002) so navigation follows the
   *  person's *current* authority (GSA-T02). Throttled unless `immediate`; a
   *  revoked session answers 401 and ends here as any other request would. */
  refreshSession: (immediate?: boolean) => void;
  activeStore: Store | null;
  setActiveStore: (s: Store | null) => void;
  activeBrand: Brand | null;
  setActiveBrand: (b: Brand | null) => void;
  /** Store-operations features on where this person is working now (ST-OPS-6):
   *  the unit picked in the switcher, or anywhere they work in the all-units
   *  view. Menus and route guards hide a feature missing from it. */
  featuresOn: ReadonlySet<string>;
}

const AuthContext = createContext<AuthContextValue>(null as unknown as AuthContextValue);

export const useAuth = () => useContext(AuthContext);

const STORE_KEY = "kdps_store";

/** The least time between two throttled session re-reads. A route change and a
 *  refused goods request ask often; one answer covers a burst of them. Coming
 *  back to the tab always asks. */
export const SESSION_REFRESH_MIN_MS = 5_000;

/** Same authority, same object: a re-read that changed nothing must not hand
 *  every screen a new `session`/`user` and re-run their effects. The session's
 *  own sliding expiry is not authority, so it does not count as a change. */
export function sameAuthority(a: unknown, b: unknown): boolean {
  const strip = (v: unknown) =>
    v && typeof v === "object" ? { ...(v as Record<string, unknown>), expires_at: undefined } : v;
  return JSON.stringify(strip(a)) === JSON.stringify(strip(b));
}
const BRAND_KEY = "kdps_brand";

/** The units this person may act in — server payload only, with the legacy
 *  `stores` field as the fallback for an older backend. */
export function allowedUnits(u: User): Store[] {
  return u.business_units ?? u.stores ?? [];
}

/** Is the switcher a brand filter rather than a unit list? */
export function isBrandScoped(u: User): boolean {
  return u.business_unit_mode === "brands";
}

/** The unit to open in. A remembered choice counts only if the server still
 *  lists it; someone with exactly one unit and no network view is locked into
 *  it, so the context is never ambiguous. */
function pickDefaultStore(u: User): Store | null {
  if (isBrandScoped(u)) return null;
  const units = allowedUnits(u);
  const saved = localStorage.getItem(STORE_KEY);
  if (saved) {
    const found = units.find((s) => s.code === saved);
    if (found) return found;
  }
  if (!u.all_business_units && units.length === 1) return units[0];
  return null; // network view (or nothing to act in at all)
}

function pickDefaultBrand(u: User): Brand | null {
  if (!isBrandScoped(u)) return null;
  const brands = u.assigned_brands ?? [];
  const saved = localStorage.getItem(BRAND_KEY);
  const found = saved ? brands.find((b) => b.code === saved) : undefined;
  if (found) return found;
  return brands.length === 1 ? brands[0] : null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [session, setSession] = useState<GoodsSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [passwordChanged, setPasswordChanged] = useState(false);
  const [activeStore, setActiveStoreState] = useState<Store | null>(null);
  const [activeBrand, setActiveBrandState] = useState<Brand | null>(null);
  // Bumped on every login/logout/forced-expiry; a slow prior-session bootstrap
  // that resolves after the epoch changed must not clobber the new session.
  const sessionEpoch = useRef(0);
  // Whether a real session is (or was, until just now) open — the effect
  // below runs once, so `user` in its own closure would always read the
  // mount-time `null`. Kept in a ref instead of read from state: the initial
  // unauthenticated `/auth/me` probe also raises a 401 and must never be
  // mistaken for a revoked session.
  const loggedInRef = useRef(false);
  const lastRefresh = useRef(0);

  /** Set the context React renders *and* the one axios sends, together. The
   *  header is read synchronously by the request interceptor, so it must not
   *  wait for an effect — a screen's fetch can fire before a parent effect runs
   *  and would otherwise carry the previous unit. */
  function applyContext(store: Store | null, brand: Brand | null) {
    setActiveStoreState(store);
    setActiveBrandState(brand);
    unitContext.set({ unit: store?.code, brand: brand?.name });
  }

  function setActiveStore(s: Store | null) {
    applyContext(s, null);
    if (s) localStorage.setItem(STORE_KEY, s.code);
    else localStorage.removeItem(STORE_KEY);
  }

  function setActiveBrand(b: Brand | null) {
    applyContext(null, b);
    if (b) localStorage.setItem(BRAND_KEY, b.code);
    else localStorage.removeItem(BRAND_KEY);
  }

  function startSession(payload: SessionPayload) {
    const u = payload.profile;
    setUser(u);
    const { profile: _profile, csrf_token: _csrf, ...goods } = payload;
    setSession(goods);
    applyContext(pickDefaultStore(u), pickDefaultBrand(u));
    loggedInRef.current = true;
    lastRefresh.current = Date.now();
  }

  function endSession() {
    setUser(null);
    setSession(null);
    applyContext(null, null);
    loggedInRef.current = false;
  }

  useEffect(() => {
    let cancelled = false;
    const epoch = sessionEpoch.current;
    (async () => {
      // The session lives in an httpOnly cookie this code can't read, so the
      // only way to learn "is anyone logged in" is to ask — a 401 here just
      // means no one is, not an error.
      try {
        const { data } = await authApi.me();
        if (!cancelled && sessionEpoch.current === epoch) startSession(data);
      } catch {
        // Stay logged out.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Navigation is drawn from this session's actions, so it is re-read whenever
  // the person comes back to the tab, moves between screens, or is refused by
  // a goods request (GSA-T02). Focus and visibility both fire when a tab
  // regains attention; visibility always asks immediately and focus is
  // throttled, so the pair costs one request, not two. There is no
  // idle-tab timer: `resolve_session` already slides the server's own idle
  // window on every request that reaches it (accounts/sessions.py), so
  // polling merely to notice a lapsed grant would also keep that idle clock
  // from ever running out. An access change ends the server session (the
  // security epoch moves), so the re-read answers 401 and the interceptor
  // signs the tab out with "Your access changed."; a grant that merely
  // lapsed comes back as a smaller action list and the menu shrinks with it.
  const refreshSession = useCallback((immediate = false) => {
    if (!loggedInRef.current) return;
    const now = Date.now();
    if (!immediate && now - lastRefresh.current < SESSION_REFRESH_MIN_MS) return;
    lastRefresh.current = now;
    const epoch = sessionEpoch.current;
    authApi
      .me()
      .then(({ data }) => {
        if (sessionEpoch.current !== epoch || !loggedInRef.current) return;
        const { profile, csrf_token: _csrf, ...goods } = data as SessionPayload;
        setUser((current) => (sameAuthority(current, profile) ? current : profile));
        setSession((current) => (sameAuthority(current, goods) ? current : goods));
      })
      .catch(() => {
        // A 401 has already been turned into the session-expired event by the
        // interceptor; anything else (offline, a blip) keeps the current view
        // until the next re-read. The server gates every request regardless.
      });
  }, []);

  useEffect(() => {
    // Throttled: visibilitychange usually fires alongside focus when a tab
    // regains attention, and already asked immediately, so this one lands
    // inside its 5s window and costs nothing extra.
    const onFocus = () => refreshSession();
    const onRefused = () => refreshSession();
    // GSA-T03/ticket 03A: an assisted reset (or the temp-password flag itself)
    // may have landed after this tab already drew a screen; re-read immediately
    // so `ProtectedRoute` sees `must_change_password` and redirects at once.
    const onPasswordChangeRequired = () => refreshSession(true);
    function onVisible() {
      if (document.visibilityState === "visible") refreshSession(true);
    }
    window.addEventListener("focus", onFocus);
    window.addEventListener("kdps:authority-refused", onRefused);
    window.addEventListener("kdps:password-change-required", onPasswordChangeRequired);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("kdps:authority-refused", onRefused);
      window.removeEventListener("kdps:password-change-required", onPasswordChangeRequired);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refreshSession]);

  useEffect(() => {
    function onExpired() {
      // A session that was never open (the unauthenticated bootstrap probe,
      // or a stale retry racing a deliberate logout) is not a revocation —
      // only flag it when a real session was open a moment ago.
      if (loggedInRef.current) setSessionExpired(true);
      sessionEpoch.current += 1;
      endSession();
    }
    window.addEventListener("kdps:session-expired", onExpired);
    return () => window.removeEventListener("kdps:session-expired", onExpired);
  }, []);

  async function login(email: string, password: string) {
    sessionEpoch.current += 1;
    authSession.bump();
    setSessionExpired(false);
    setPasswordChanged(false);
    const { data } = await authApi.login(email, password);
    const payload = data as SessionPayload;
    startSession(payload);
    return { mustChangePassword: payload.user.must_change_password };
  }

  function logout() {
    sessionEpoch.current += 1;
    authSession.bump();
    setSessionExpired(false);
    authApi.logout().catch(() => undefined);
    endSession();
  }

  /** GSA-T03/ticket 03A: `ChangePassword` calls this instead of `logout()` on
   *  E239 success — the server already ended every session for this login, so
   *  there is nothing left to revoke; this only clears local state and sets
   *  the note `Login` shows. */
  function completePasswordChange() {
    sessionEpoch.current += 1;
    authSession.bump();
    setSessionExpired(false);
    setPasswordChanged(true);
    endSession();
  }

  const featuresOn = useMemo(
    () => storeFeaturesOn(session?.store_features, activeStore?.id),
    [session, activeStore],
  );

  return (
    <AuthContext.Provider
      value={{
        user,
        session,
        loading,
        login,
        logout,
        sessionExpired,
        clearSessionExpired: () => setSessionExpired(false),
        passwordChanged,
        completePasswordChange,
        clearPasswordChanged: () => setPasswordChanged(false),
        refreshSession,
        activeStore,
        setActiveStore,
        activeBrand,
        setActiveBrand,
        featuresOn,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
