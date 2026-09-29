import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import { authApi, authSession, unitContext } from "../lib/api";
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
  scope_label?: string;
}

export interface User {
  id: number;
  username: string;
  full_name: string;
  /** Presentation layout only. Assignments are the authority for every action. */
  role: Role | null;
  scope_type: string;
  scope_label: string;
  stores: Store[];
  sections: NavSection[];
  capabilities: Record<string, NavSection["capability"]>;
  navigation: string[];
  display_actions: string[];
  landing_page: string;
  business_units: Store[];
  all_business_units: boolean;
  business_unit_mode: "units" | "brands";
  assigned_brands: Brand[];
  /** Whether this person has a counter PIN, and whether they are somebody who
   *  could hold one (#182). Never the hash: that goes to a till in its dataset
   *  and nowhere else. */
  has_till_pin?: boolean;
  may_hold_till_pin?: boolean;
}

/** The sole session contract. Display hints shape the UI; the API still checks
 * each request against the scoped assignments and current policy. */
export interface GoodsSession {
  contract_version: "access-v2";
  policy_version: string;
  user: {
    id: string;
    human_id: string | null;
    display_name: string;
    email: string | null;
    username?: string;
    must_change_password: boolean;
    has_till_pin?: boolean;
    may_hold_till_pin?: boolean;
  };
  assignments: {
    id: string;
    role_code: string;
    role_name?: string;
    all_sites: boolean;
    site_ids: number[];
    all_brands: boolean;
    brand_ids: number[];
    effective_from: string;
    effective_to: string | null;
  }[];
  sections: NavSection[];
  capabilities: Record<string, NavSection["capability"]>;
  navigation: string[];
  /** Server-calculated hints for buttons. They never authorise a request. */
  display_actions: string[];
  sites: {
    id: string;
    code: string;
    name: string;
    type: string;
    stock_contract: "legacy" | "goods_v1";
  }[];
  context_choices: {
    mode: "units" | "brands";
    all_units: boolean;
    sites: Store[];
    brands: Brand[];
  };
  store_features: Record<string, string[]>;
  expires_at: string;
  step_up_valid_until: string | null;
}

interface SessionPayload extends GoodsSession {
  csrf_token?: string;
}

export class UnsupportedSessionError extends Error {}

/** Reject an old or partial session before it can draw an access menu. */
export function readSession(value: unknown): SessionPayload {
  if (!value || typeof value !== "object")
    throw new UnsupportedSessionError("Invalid session response");
  const session = value as Partial<SessionPayload>;
  if (
    session.contract_version !== "access-v2" ||
    typeof session.policy_version !== "string" ||
    !session.user ||
    !Array.isArray(session.assignments) ||
    !Array.isArray(session.sections) ||
    !session.capabilities ||
    !Array.isArray(session.navigation) ||
    !Array.isArray(session.display_actions) ||
    !Array.isArray(session.sites) ||
    !session.context_choices ||
    !Array.isArray(session.context_choices.sites) ||
    !Array.isArray(session.context_choices.brands)
  ) {
    throw new UnsupportedSessionError("Unsupported session response");
  }
  return session as SessionPayload;
}

interface AuthContextValue {
  user: User | null;
  /** The current unified server session and UI display hints. */
  session: GoodsSession | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<{ mustChangePassword: boolean }>;
  logout: () => Promise<void>;
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

/** The top-bar context is one filter axis at a time. A site and brand from
 * different assignments must never be presented as one combined selection. */
export function contextSelection(
  store: Store | null,
  brand: Brand | null,
): {
  unit?: string;
  brand?: string;
} {
  if (store && brand) throw new Error("A unit and brand cannot be selected together");
  return {
    ...(store ? { unit: String(store.id) } : {}),
    ...(brand ? { brand: String(brand.id) } : {}),
  };
}

/** The units this person may act in, supplied as server context choices. */
export function allowedUnits(u: User): Store[] {
  return u.business_units;
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
  if (!u.all_business_units && units.length === 1) return units[0] ?? null;
  return null; // network view (or nothing to act in at all)
}

function pickDefaultBrand(u: User): Brand | null {
  if (!isBrandScoped(u)) return null;
  const brands = u.assigned_brands ?? [];
  const saved = localStorage.getItem(BRAND_KEY);
  const found = saved ? brands.find((b) => b.code === saved) : undefined;
  if (found) return found;
  return brands.length === 1 ? (brands[0] ?? null) : null;
}

/** A shell view of the one session response. No second profile or role-scoped
 * permissions are fetched or merged into this object. */
export function shellUser(session: GoodsSession): User {
  const choices = session.context_choices;
  const roles = [...new Set(session.assignments.map((a) => a.role_code))];
  const singleRole = roles.length === 1 ? roles[0] : null;
  const roleAssignment = singleRole
    ? session.assignments.find((a) => a.role_code === singleRole)
    : undefined;
  const stores = choices.sites;
  return {
    id: Number(session.user.id),
    username: session.user.username ?? session.user.email ?? session.user.display_name,
    full_name: session.user.display_name,
    role: singleRole
      ? {
          code: singleRole,
          name: roleAssignment?.role_name ?? singleRole,
          landing_page: "",
          nav_groups: [],
        }
      : null,
    scope_type: !choices.all_units && stores.length === 1 ? "store" : "scoped",
    scope_label: roles.length
      ? roles.map((code) => code.replaceAll("_", " ")).join(", ")
      : "No role assigned",
    stores,
    sections: session.sections,
    capabilities: session.capabilities,
    navigation: session.navigation,
    display_actions: session.display_actions,
    landing_page:
      singleRole === "store_person" ? "store" : singleRole === "warehouse" ? "warehouse" : "owner",
    business_units: stores,
    all_business_units: choices.all_units,
    business_unit_mode: choices.mode,
    assigned_brands: choices.brands,
    has_till_pin: Boolean(session.user.has_till_pin),
    may_hold_till_pin: Boolean(session.user.may_hold_till_pin),
  };
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
    const selection = contextSelection(store, brand);
    setActiveStoreState((current) => (current?.id === store?.id ? current : store));
    setActiveBrandState((current) => (current?.id === brand?.id ? current : brand));
    unitContext.set(selection);
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
    const { csrf_token: _csrf, ...current } = payload;
    const u = shellUser(current);
    setUser(u);
    setSession(current);
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
        if (!cancelled && sessionEpoch.current === epoch) startSession(readSession(data));
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
        const { csrf_token: _csrf, ...next } = readSession(data);
        const view = shellUser(next);
        setUser((current) => (sameAuthority(current, view) ? current : view));
        setSession((current) => (sameAuthority(current, next) ? current : next));
        applyContext(pickDefaultStore(view), pickDefaultBrand(view));
      })
      .catch((reason) => {
        if (reason instanceof UnsupportedSessionError) {
          window.dispatchEvent(new Event("kdps:session-expired"));
        }
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
    const payload = readSession(data);
    startSession(payload);
    return { mustChangePassword: payload.user.must_change_password };
  }

  async function logout() {
    const epoch = ++sessionEpoch.current;
    authSession.bump();
    // Keep the session visible until the server confirms revocation. Leaving
    // the page sooner can cancel this request and restore the cookie on reload.
    await authApi.logout();
    if (sessionEpoch.current === epoch) {
      setSessionExpired(false);
      endSession();
    }
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
