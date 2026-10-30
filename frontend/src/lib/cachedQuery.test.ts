// @vitest-environment jsdom
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
// Plain `createElement` rather than JSX: a `.test.ts` file is a test, not an
// API consumer, to the SO-03 frontend inventory scanner.
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../auth/AuthContext";
import { api } from "./api";
import { useCachedGet, type CachedReadOptions } from "./cachedQuery";
import { createQueryClient, queryClient as appQueryClient } from "./queryCache";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Reply = { data: unknown } | { status: number };

let server: (url: string) => Reply;
let getCalls: string[];
/** When set, every GET waits for it: a request visibly in flight. */
let gate: Promise<void> | null = null;

function axiosError(status: number) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    response: { status, data: {} },
  });
}

beforeEach(() => {
  getCalls = [];
  gate = null;
  vi.spyOn(api, "get").mockImplementation((async (url: string) => {
    getCalls.push(url);
    if (gate) await gate;
    const reply = server(url);
    if ("status" in reply) throw axiosError(reply.status);
    return { data: reply.data };
  }) as unknown as typeof api.get);
});

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  appQueryClient.clear();
});

function mount(node: ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
}

function rerender(node: ReactNode) {
  act(() => root!.render(node));
}

function unmount() {
  act(() => root!.unmount());
  root = null;
}

async function settle() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

type Summary = { total: number };
type Result = ReturnType<typeof useCachedGet<Summary, number>>;

function Probe({
  url,
  options,
  seen,
}: {
  url: string | null;
  options?: CachedReadOptions<Summary, number>;
  seen: Result[];
}) {
  const result = useCachedGet<Summary, number>(url, { select: (r) => r.total, ...options });
  seen.push(result);
  return null;
}

/** A signed-in person in one store, for the scope part of the key. */
function as(userId: number, storeId: number, node: ReactNode) {
  const value = {
    user: { id: userId },
    session: { user: { id: userId }, assignments: [{ role_code: "store_manager" }] },
    activeStore: { id: storeId },
    activeBrand: null,
  } as unknown as React.ContextType<typeof AuthContext>;
  return h(AuthContext.Provider, { value }, node);
}

function withClient(client: QueryClient, node: ReactNode) {
  return h(QueryClientProvider, { client }, node);
}

describe("useCachedGet", () => {
  it("shows a payload it has seen before at once, with no loading flash", async () => {
    server = () => ({ data: { total: 7 } });
    const client = createQueryClient();
    const first: Result[] = [];
    mount(withClient(client, h(Probe, { url: "/stockledger/on-hand", seen: first })));
    expect(first[0]).toMatchObject({ loading: true, data: undefined, updatedAt: null });
    await settle();
    expect(first.at(-1)).toMatchObject({ loading: false, data: 7, error: null });
    expect(first.at(-1)?.updatedAt).toBeGreaterThan(0);
    unmount();

    const again: Result[] = [];
    mount(withClient(client, h(Probe, { url: "/stockledger/on-hand", seen: again })));
    expect(again[0]).toMatchObject({ loading: false, data: 7 });
    expect(again.every((r) => !r.loading)).toBe(true);
    await settle();
    expect(getCalls).toEqual(["/stockledger/on-hand"]);
  });

  it("refreshes a money read on every visit, behind the cached copy", async () => {
    let total = 1;
    server = () => ({ data: { total } });
    const client = createQueryClient();
    mount(withClient(client, h(Probe, { url: "/finledger/vendor/ageing", seen: [] })));
    await settle();
    unmount();

    total = 2;
    const again: Result[] = [];
    mount(withClient(client, h(Probe, { url: "/finledger/vendor/ageing", seen: again })));
    expect(again[0]).toMatchObject({ loading: false, data: 1, fetching: true });
    await settle();
    expect(again.at(-1)).toMatchObject({ loading: false, data: 2, fetching: false });
    expect(again.every((r) => !r.loading)).toBe(true);
    expect(getCalls).toHaveLength(2);
  });

  it("never shows one person's or store's answer to another", async () => {
    server = () => ({ data: { total: 5 } });
    const client = createQueryClient();
    mount(withClient(client, as(1, 10, h(Probe, { url: "/alerts/count", seen: [] }))));
    await settle();

    const other: Result[] = [];
    rerender(withClient(client, as(2, 10, h(Probe, { url: "/alerts/count", seen: other }))));
    // A different person: not the cached answer, and not a carried-over one.
    expect(other[0]).toMatchObject({ loading: true, data: undefined });
    await settle();

    const otherStore: Result[] = [];
    rerender(withClient(client, as(2, 11, h(Probe, { url: "/alerts/count", seen: otherStore }))));
    expect(otherStore[0]).toMatchObject({ loading: true, data: undefined });
    await settle();
    expect(getCalls).toEqual(["/alerts/count", "/alerts/count", "/alerts/count"]);
  });

  it("folds params into the URL and keeps the old filter's answer flagged as loading", async () => {
    server = (url) => ({ data: { total: url.includes("days=7") ? 7 : 30 } });
    const client = createQueryClient();
    const seen: Result[] = [];
    mount(
      withClient(
        client,
        h(Probe, { url: "/reports/sales", options: { params: { days: 30, q: " " } }, seen }),
      ),
    );
    await settle();
    rerender(
      withClient(
        client,
        h(Probe, { url: "/reports/sales", options: { params: { days: 7 } }, seen }),
      ),
    );
    expect(seen.at(-1)).toMatchObject({ data: 30, loading: true, updatedAt: null });
    await settle();
    expect(seen.at(-1)).toMatchObject({ data: 7, loading: false });
    expect(getCalls).toEqual(["/reports/sales?days=30", "/reports/sales?days=7"]);
  });

  it("polls with refetchInterval", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let total = 0;
    server = () => ({ data: { total: (total += 1) } });
    const seen: Result[] = [];
    mount(
      withClient(
        createQueryClient(),
        h(Probe, { url: "/approvals/count", options: { refetchInterval: 1_000 }, seen }),
      ),
    );
    await settle();
    expect(seen.at(-1)?.data).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_050);
    });
    await settle();
    expect(seen.at(-1)?.data).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_050);
    });
    await settle();
    expect(seen.at(-1)?.data).toBe(3);
    expect(seen.every((r, i) => i === 0 || !r.loading)).toBe(true);
  });

  it("returns a refusal as an error, not a spinner, and clears it on a good retry", async () => {
    let refuse = true;
    server = () => (refuse ? { status: 403 } : { data: { total: 3 } });
    const seen: Result[] = [];
    mount(withClient(createQueryClient(), h(Probe, { url: "/finledger/health", seen })));
    await settle();
    const refused = seen.at(-1)!;
    expect(refused.loading).toBe(false);
    expect(refused.data).toBeUndefined();
    expect((refused.error as { response?: { status: number } }).response?.status).toBe(403);
    expect(getCalls).toEqual(["/finledger/health"]); // no retries

    refuse = false;
    let open = () => {};
    gate = new Promise((resolve) => (open = resolve));
    act(() => refused.reload());
    await settle();
    // Asked again with nothing to show: loading, and the old refusal is not repeated.
    expect(seen.at(-1)).toMatchObject({ loading: true, error: null, fetching: true });
    open();
    await settle();
    expect(seen.at(-1)).toMatchObject({ loading: false, data: 3, error: null });
  });

  it("asks nothing for a null URL or while disabled, and does not claim to load", async () => {
    server = () => ({ data: { total: 1 } });
    const seen: Result[] = [];
    mount(withClient(createQueryClient(), h(Probe, { url: null, seen })));
    rerender(
      withClient(
        createQueryClient(),
        h(Probe, { url: "/alerts", options: { enabled: false }, seen }),
      ),
    );
    await settle();
    expect(getCalls).toEqual([]);
    expect(seen.at(-1)).toMatchObject({
      data: undefined,
      loading: false,
      fetching: false,
      error: null,
    });
  });
});
