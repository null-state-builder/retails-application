# App loading and caching plan

Status: Phase 1 approved for build (2026-10-29). Phases 2–3 planned, not started.

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

### Phase 3: freshness without full reloads
- `@condition` ETags and `Cache-Control: private, no-cache` on hot list and
  reference endpoints (304 when nothing changed).
- `since`-cursor delta sync for large sets (stock), reusing the till dataset
  pattern.
- Refetch on focus; server-push invalidation hints only if that proves too slow.

## Fixed rules
- Cost, margin and financial reports are never persisted on the device.
- Actions always go to the server; the cache only renders.
- Stock and money screens show when the data was last updated.
- No cross-user, cross-store or cross-role leakage; proven by a denial test
  (log in as A, log out, log in as B).
- The service worker stays shell-only (`runtimeCaching: []`).
- The till's Dexie database keeps its own rules; unsent bills are never wiped.
- Logout deletes everything cached for the back office. Counter PIN/lock-screen
  is out of scope.

## Targets
- Revisit of a page: cached content in < 100 ms.
- First login: main screen usable in < 2.5 s.
- Measured before and after on the exact revision.
