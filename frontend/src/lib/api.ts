import axios from "axios";
import type { AxiosRequestConfig, AxiosResponse } from "axios";

import type { components, paths } from "./api-schema";

// Same-origin when unset (platform proxy routes /api → backend on same host).
// Set REACT_APP_BACKEND_URL explicitly only for cross-origin dev (e.g. local FE → remote API).
const BASE = (import.meta.env.REACT_APP_BACKEND_URL as string) || "";

export const api = axios.create({ baseURL: `${BASE}/api`, withCredentials: true });

// The session is an opaque server-side record behind the HttpOnly `kdps_session`
// cookie (goods-v1 design §4.2); nothing token-shaped is readable here. Every
// write echoes the readable `kdps_csrf` cookie in `X-CSRF-Token`, which the
// server checks against the session. Bumped by `AuthContext` on every
// login/logout so a stale request's failed refresh (kicked off *before* a fresh
// login/logout) never clobbers the session that already replaced it.
let authEpoch = 0;
export const authSession = {
  bump() {
    authEpoch += 1;
  },
};

// The business unit — or, for a brand manager, the brand — the person picked in
// the top-bar switcher (#88). Sent on every call so the server narrows its answer
// to that unit. It can only ever *narrow*: the server intersects the header with
// what the caller is entitled to and refuses anything outside it, so nothing here
// is a permission. Held outside React because the interceptor needs it
// synchronously, before any screen's fetch runs.
let activeUnitCode = "";
let activeBrandId = "";

export const unitContext = {
  get unit() {
    return activeUnitCode;
  },
  get brand() {
    return activeBrandId;
  },
  /** Both are set together: choosing one clears the other. */
  set(next: { unit?: string; brand?: string }) {
    activeUnitCode = next.unit ?? "";
    activeBrandId = next.brand ?? "";
  },
};

/** The double-submit CSRF token the server set in the readable `kdps_csrf` cookie. */
export function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie.split("; ").find((part) => part.startsWith("kdps_csrf="));
  return match ? decodeURIComponent(match.slice("kdps_csrf=".length)) : "";
}

const UNSAFE = new Set(["post", "put", "patch", "delete"]);

api.interceptors.request.use((config) => {
  const method = (config.method ?? "get").toLowerCase();
  if (UNSAFE.has(method)) {
    const token = csrfToken();
    if (token) config.headers["X-CSRF-Token"] = token;
  }
  // Scoped readers may explicitly choose another authorised unit or their
  // whole scope. The server still intersects the header with assignments.
  if (unitContext.unit && !config.headers.has("X-KDPS-Unit"))
    config.headers["X-KDPS-Unit"] = unitContext.unit;
  if (unitContext.brand) config.headers["X-KDPS-Brand"] = unitContext.brand;
  return config;
});

// Single-flight refresh: N simultaneous 401s share one /auth/refresh call, so the
// session token rotates exactly once. The session cookie is HttpOnly - the
// browser attaches it and the rotated cookies land in the response.
let refreshPromise: Promise<void> | null = null;

function refreshAccessToken(): Promise<void> {
  refreshPromise ??= axios
    .post(
      `${BASE}/api/auth/refresh`,
      {},
      { withCredentials: true, headers: { "X-CSRF-Token": csrfToken() } },
    )
    .then(() => undefined)
    .finally(() => {
      refreshPromise = null;
    });
  return refreshPromise;
}

// Endpoints whose 401 judges the password just typed, not the session (ticket
// 03A review-fix). Refreshing and replaying one would count a single wrong
// password twice toward the 10-in-15-minutes lockout it shares with login, and
// rotate the session for nothing. Only a Refusal body (one with a `code`) is
// such a verdict: DRF's NotAuthenticated `{detail}` on the same URL means the
// session itself lapsed, and still goes through refresh so a failed one signs
// the person out (ticket 03A fix-round review, 03A-R1).
const PASSWORD_JUDGED = new Set(["/auth/change-password", "/auth/step-up"]);

// Refresh the access token once on a 401, then replay the request.
api.interceptors.response.use(
  (r) => r,
  async (error) => {
    const original = error.config;
    const judgedCredential =
      PASSWORD_JUDGED.has(String(original?.url ?? "")) &&
      typeof error.response?.data?.code === "string";
    if (error.response?.status === 401 && original && !original._retry && !judgedCredential) {
      original._retry = true;
      const epochAtStart = authEpoch;
      try {
        await refreshAccessToken();
        return api(original);
      } catch {
        // Only fire the expiry event if a newer login/logout hasn't already
        // moved past this attempt — see the `authEpoch` note above.
        if (authEpoch === epochAtStart) {
          window.dispatchEvent(new Event("kdps:session-expired"));
        }
      }
    }
    // A goods request refused for authority: the menu this tab drew may be
    // older than the person's grants, so ask the session again (GSA-T02). The
    // refusal itself still reaches the screen unchanged.
    if (
      error.response?.status === 403 &&
      error.response?.data?.code !== "PASSWORD_CHANGE_REQUIRED"
    ) {
      window.dispatchEvent(new Event("kdps:authority-refused"));
    }
    // GSA-T03/ticket 03A: a temporary-password session was refused for reaching
    // business work, on any route - not only goods-v1 (`PASSWORD_CHANGE_REQUIRED`,
    // `accounts.authentication.enforce_password_change_restriction`). Ask the
    // session again so `ProtectedRoute` sees `must_change_password` and redirects.
    if (
      error.response?.status === 403 &&
      error.response?.data?.code === "PASSWORD_CHANGE_REQUIRED"
    ) {
      window.dispatchEvent(new Event("kdps:password-change-required"));
    }
    return Promise.reject(error);
  },
);

// E239's own request/response shapes, pulled straight from the generated schema
// (review-fix pass: the schema used to be unusable here - see the `request=`
// fix in `accounts/views.py` - so this used to be hand-written instead).
type ChangePasswordOperation = paths["/api/auth/change-password"]["post"];
type ChangePasswordRequest = ChangePasswordOperation extends {
  requestBody?: { content: { "application/json": infer Body } };
}
  ? Body
  : never;
type ChangePasswordResponse = ChangePasswordOperation extends {
  responses: { 200: { content: { "application/json": infer Body } } };
}
  ? Body
  : never;

export const authApi = {
  /** Mint the double-submit CSRF cookie a signed-out page needs before login. */
  csrf: () => api.get<{ csrf_token: string }>("/auth/csrf"),
  login: async (email: string, password: string) => {
    const { data } = await authApi.csrf();
    return api.post("/auth/login", { email, password, csrf_token: data.csrf_token });
  },
  me: () => api.get("/auth/me"),
  // The server revokes whatever session cookie this browser holds.
  logout: () => api.post("/auth/logout", {}),
  /** Re-enter the password for five minutes of privileged actions on this session. */
  stepUp: (password: string) => api.post<{ valid_until: string }>("/auth/step-up", { password }),
  /** E239 (GSA-T03/ticket 03A): replace a temporary or forgotten password. Ends
   *  every session this login holds, this one included - sign in again after. */
  changePassword: (current_password: string, new_password: string) =>
    api.post<ChangePasswordResponse>("/auth/change-password", {
      current_password,
      new_password,
    } satisfies ChangePasswordRequest),
};

export type ApiSchemas = components["schemas"];

/** A generated schema as the server actually *answers* it.
 *
 * Most of these components are one DRF serializer doing both directions, so a
 * field carrying a model default is optional *to send* - and `openapi-typescript`
 * can only render that as `?`, on the read shape as well as the write one. A
 * screen reading a list would then have to null-check a field the server writes
 * out every single time. What is optional there is the caller's obligation to
 * supply it, never the server's to answer, so a read shape says `ApiRead<…>` and
 * the field names, the enums and anything newly added still come straight from
 * the schema. A field that is *genuinely* nullable stays nullable: `Required<>`
 * removes `?`, not `| null`.
 *
 * Top level only, deliberately. A nested object keeps its own `?`, which is
 * right: the argument above is about one serializer's own fields, and a nested
 * component is a different serializer with its own answer. */
export type ApiRead<T> = Required<T>;
type ApiPath = keyof paths;
type PathWithoutPrefix<P extends ApiPath> = P extends `/api${infer Rest}` ? Rest : P;
type ApiRelativePath = PathWithoutPrefix<ApiPath>;
type FullApiPath<P extends ApiRelativePath> = `/api${P}` & ApiPath;
type ApiOperation<
  P extends ApiRelativePath,
  Method extends PropertyKey,
> = paths[FullApiPath<P>] extends infer Path
  ? Method extends keyof Path
    ? Path[Method]
    : never
  : never;
type JsonBody<Response> = Response extends {
  content: { "application/json": infer Body };
}
  ? Body
  : never;
type SuccessBody<Operation> = Operation extends { responses: infer Responses }
  ? Responses extends { 200: infer Ok }
    ? JsonBody<Ok>
    : Responses extends { 201: infer Created }
      ? JsonBody<Created>
      : never
  : never;

function toApiPath(path: ApiRelativePath): ApiPath {
  return `/api${path}` as ApiPath;
}

export const typedApi = {
  get<TPath extends ApiRelativePath>(
    path: TPath,
    config?: AxiosRequestConfig,
  ): Promise<AxiosResponse<SuccessBody<ApiOperation<TPath, "get">>>> {
    return api.get<SuccessBody<ApiOperation<TPath, "get">>>(
      toApiPath(path).replace("/api", ""),
      config,
    );
  },
  post<TPath extends ApiRelativePath>(path: TPath, data?: unknown, config?: AxiosRequestConfig) {
    return api.post(toApiPath(path).replace("/api", ""), data, config);
  },
  patch<TPath extends ApiRelativePath>(path: TPath, data?: unknown, config?: AxiosRequestConfig) {
    return api.patch(toApiPath(path).replace("/api", ""), data, config);
  },
  put<TPath extends ApiRelativePath>(path: TPath, data?: unknown, config?: AxiosRequestConfig) {
    return api.put(toApiPath(path).replace("/api", ""), data, config);
  },
};

/** Flatten one DRF error value — a string, or a list/dict of them — to a sentence. */
function firstSentence(value: unknown): string {
  if (typeof value === "string") {
    // A server crash page (Django's debug HTML) is not a sentence for a user.
    const text = value.trim();
    return text.startsWith("<") ? "" : text;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstSentence(item);
      if (found) return found;
    }
    return "";
  }
  if (value && typeof value === "object") {
    for (const nested of Object.values(value)) {
      const found = firstSentence(nested);
      if (found) return found;
    }
  }
  return "";
}

export function apiErrorMessage(e: unknown): string {
  const data = (e as { response?: { data?: unknown } })?.response?.data;
  // `detail` is what a refusal raised by a view or permission carries, and it
  // is the sentence written for the user, so it wins. A serializer refusal
  // arrives keyed by field instead (`{"roles": ["Floor rule: …"]}`) — the
  // message is just as deliberate, and falling through to "something went
  // wrong" would tell the user nothing about a rule they just hit.
  const detail = (data as { detail?: unknown })?.detail;
  // The D10 endpoints answer `{"error": "<sentence>", "code": "<CODE>"}` instead
  // (`core/refusals.py`), so `error` is read by name too. The fallback below
  // would find it by walking the object — but only while `error` is declared
  // before `code`, and a body whose message depends on its key order is one
  // reorder away from showing a store person "SCOPE_DENIED".
  const error = (data as { error?: unknown })?.error;
  return (
    firstSentence(detail) ||
    firstSentence(error) ||
    firstSentence(data) ||
    "Something went wrong. Please try again."
  );
}

/** The goods-v1 refusal's `code` (`core/refusals.py`), e.g. `STEP_UP_REQUIRED` or
 *  `REVISION_SUPERSEDED` — for a caller that needs to react to *which* refusal
 *  this was, not just show its sentence. Undefined for a non-goods-v1 error
 *  shape (a legacy serializer refusal has no such code). */
export function apiErrorCode(e: unknown): string | undefined {
  const data = (e as { response?: { data?: unknown } })?.response?.data;
  const code = (data as { code?: unknown })?.code;
  return typeof code === "string" ? code : undefined;
}

/** MutationMeta (design §6.1): every goods-v1 write names its own idempotency
 *  key and contract version, and a revision-bound one quotes the revision it
 *  read. Spread this into a write's body — never send `command_id` or
 *  `contract_version` by hand, or a retried click becomes two commands
 *  instead of one safe replay.
 *
 *  `commandId` is for the opposite case: a retry that *must* replay the exact
 *  command it is retrying, so the server returns the original outcome instead
 *  of making a second one. The caller holds that identity for as long as the
 *  retry is on offer (see the combination grid in `pages/ProductsParties.tsx`).
 *  Pass it here rather than overriding the generated key by spread order,
 *  which one reordered line would silently undo. */
export function goodsMeta(
  expectedRevision?: number,
  commandId?: string,
): {
  command_id: string;
  contract_version: "goods-v1";
  expected_revision?: number;
} {
  return {
    command_id: commandId ?? crypto.randomUUID(),
    contract_version: "goods-v1",
    ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
  };
}

/** The absolute address of a goods-v1 API path, for something that cannot go
 *  through the `api` axios instance — `EventSource` (E188's change stream)
 *  takes a bare URL, not a request config, and still needs the session cookie
 *  (`withCredentials`) and the same origin/base this instance already resolves. */
export function apiUrl(path: string): string {
  return `${BASE}/api${path}`;
}
