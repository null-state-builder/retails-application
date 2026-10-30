# App loading and caching plan

Status: Phases 1–2 built (branch `caching-phase-1-2`, PR #2). Phase 3 built on
branch `caching-phase-3` (2026-10-30) to the narrowed scope below: conditional
GET (ETag/304) and device data minimisation. Delta sync and server push are
deferred by owner decision.

## Owner decisions (2026-10-30)
- Freshness stays **refresh on focus/open**: the current staleTimes and
  `refetchOnWindowFocus`. No polling is added, no server push (SSE/WebSocket),
  no `since`-cursor delta sync.
- Back-office devices are **personal**. The logout wipe and the 24 h maximum
  age of the device copy stay as they are.
- **Vendor and legal-entity PAN and payment terms are never saved on the
  device.** Names, codes, GSTIN and addresses may be. Those fields may still be
  shown on screen from memory, as before.

## Problem
The counter (till) already keeps its own per-store copy of items, stock and
offers in Dexie (`frontend/src/till/db.ts`) and opens instantly. The back office
does not: `useList` / `useDoc` in `frontend/src/lib/hooks.ts` keep data in
component state, remember nothing between visits and set `loading` on every
mount, so every page shows a loading screen every time. 24 files use them.
There is no route-level code splitting.

Goal: after login a user should almost never wait on a loading screen. Show
what we already have at once, refresh it quietly, and load what each role needs
once at login.

## What the market and the industry do (research, 2026-10-29)
- Retail POS/ERP (Shopify POS, Odoo POS, Dynamics 365 Commerce, Ginesys,
  Gofrugal) bulk-load master data when a session opens, keep it in a local
  store filtered to one store/user, and keep it fresh with delta sync. Odoo
  loads up to 20,000 products by default; our ~6,300 barcodes fits easily.
- Money, gift cards, close-of-day and cross-store stock are always live, never
  served from a cache (D365, Square, Shopify).
- Local-first business apps (Linear, Notion) bootstrap once, persist locally,
  then apply deltas; Notion measured 20% faster navigation (33% in India).
- Engineering practice: a server-state cache (TanStack Query v5) with
  stale-while-revalidate, persistence of an allowlisted subset to IndexedDB,
  cache keys scoped by user+store+role, full wipe on logout, intent prefetch,
  per-route code splitting, ETag/304 on hot endpoints. The service worker must
  not cache authenticated API responses.

## Plan

### Phase 1: pages open instantly after the first visit (frontend only)
1. Add TanStack Query v5 with one `QueryClient`.
2. Reimplement `useList` / `useDoc` over `useQuery`, keeping their signatures
   and return shapes, so the 24 call sites do not change. Query key =
   `[scope, url]` where scope = user, store, role.
3. Stale-while-revalidate: cached data renders immediately; `loading` is true
   only when there is no data yet. Filtered/paged lists keep the previous
   result while a new filter loads.
4. Invalidation: every existing `reload()` and every mutation invalidates the
   document and the lists it appears in, so the screen ends on what the server
   says.
5. Route code splitting with `React.lazy`; sidebar hover/focus prefetches the
   target page's data and code chunk.
6. Logout, store or role change, and session-expiry 401: cancel queries and
   clear the client. Nothing is persisted to disk in Phase 1.
7. Default staleness: reference data 1 h, lists 30 s, open documents and
   action-needed counts 0–10 s with refetch on window focus. Cost/financial
   reports: staleTime 0.

### Phase 2: load at login
- One versioned `/bootstrap` endpoint: profile, permissions, stores, tax,
  seasons/categories, suppliers, action-needed counts, `ref_version`.
- Then prefetch each role's main screen in idle time.
- Persist an explicit allowlist of reference queries to IndexedDB, keyed and
  busted by `appVersion:user:store:role`, max age 24 h, deleted on logout.

### Phase 3: cheap refreshes, less on the device (built 2026-10-30)
Scope, after the owner decisions above:

1. **Device data minimisation** (`frontend/src/lib/deviceCache.ts`). On the way
   to IndexedDB every persisted entry loses `pan`, `pan_number`, `pan_no`,
   `payment_terms`, `payment_terms_days`, `bank_account`,
   `bank_account_number`, `bank_account_no`, `account_number`, `ifsc` and
   `ifsc_code`, at any depth (today only `pan` on `/vendors` and
   `/masters/entities`, and `payment_terms` on `/vendors`, exist). Memory keeps
   the full answer. An entry that lost a field is saved already invalidated, so
   after a restore its screen renders the rest at once and refetches the whole
   answer. The device stamp gained a format (`d2:<build>:<scope>`), so copies
   saved before this rule are refused and deleted. The vendor form leaves PAN
   and terms untouched when it edits a restored row that does not carry them
   yet, so a save can never blank them.
2. **Conditional GET** (`backend/core/conditional_get.py`, a middleware
   innermost in the stack). On `GET /api/masters/{stores,locations,brands,
   seasons,gstins,entities}`, `/api/vendors`, `/api/auth/bootstrap`,
   `/api/approvals/inbox`, `/api/alerts`, `/api/stockledger/on-hand` and
   `/api/stockledger/summary`, a 200 JSON answer gets a strong ETag: a SHA-256
   over the exact body the view rendered for this caller, mixed with tenant,
   user and the `X-KDPS-Unit`/`X-KDPS-Brand` headers. It is computed after the
   view, so authorisation and scoping always run; a refusal is never touched.
   An `If-None-Match` that still matches gets a 304 with no body. `Vary:
   Cookie, X-KDPS-Unit, X-KDPS-Brand`. The goods-v1 stock readers are left out:
   their body carries the read moment (`as_of`), so it never repeats.
3. **`no-store`, not `no-cache`, and revalidation from memory**
   (`frontend/src/lib/conditionalGet.ts`). The plan first assumed
   `Cache-Control: private, no-cache` and letting the browser revalidate. That
   works (the browser answers the XHR from its HTTP cache after a 304), but it
   needs the browser to keep every body in its HTTP disk cache, which outlives
   a logout and would hold vendor PAN, payment terms and stock values on the
   device, against the owner decision and the fixed rules. So these responses
   stay `no-store, private`, and the app remembers each ETag'd body in memory,
   sends `If-None-Match` itself (a request header set by the page puts the
   fetch in `no-store` mode, so the browser hands the 304 through), and turns a
   304 back into the remembered body for every `api.get` caller. That memory is
   emptied with the query cache on logout and every scope change. The service
   worker stays shell-only (`runtimeCaching: []`, `/api` denied).
4. **Build stamp**: `KDPS_BUILD_ID`, then `VERCEL_GIT_COMMIT_SHA`, then git,
   then the package version. Server-side or Docker frontend builds must set
   `KDPS_BUILD_ID` (`deploy/README.md`).

Deferred: `since`-cursor delta sync for stock and server-push invalidation
(owner decision); revisit only if refresh-on-open measurably misses the
targets. No endpoint uses a cheap pre-query ETag (`max(updated_at)` + count):
none could be shown to cover every field and the caller's scope that way, so
every ETag is a body digest. The view still runs; the saving is the transfer
and the client's re-render work. Not yet measured on a deployed revision.

## Fixed rules
- Cost, margin and financial reports are never persisted on the device.
- Actions always go to the server; the cache only renders.
- Stock and money screens show when the data was last updated.
- No cross-user, cross-store or cross-role leakage; proven by a denial test
  (log in as A, log out, log in as B).
- The service worker stays shell-only (`runtimeCaching: []`).
- Vendor and legal-entity PAN, payment terms and bank details are never
  written to the device (memory only).
- ETag'd reads stay `no-store, private` and are revalidated from memory, never
  through the browser's HTTP cache.
- The till's Dexie database keeps its own rules; unsent bills are never wiped.
- Logout deletes everything cached for the back office. Counter PIN/lock-screen
  is out of scope.

## Targets
- Revisit of a page: cached content in < 100 ms.
- First login: main screen usable in < 2.5 s.
- Measured before and after on the exact revision.
