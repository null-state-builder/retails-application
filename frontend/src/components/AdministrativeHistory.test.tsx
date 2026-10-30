// @vitest-environment jsdom
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { createQueryClient } from "../lib/queryCache";
import { AdministrativeHistory } from "./AdministrativeHistory";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function event(id: string) {
  return {
    id,
    event_kind: "updated",
    outcome: "succeeded",
    revision: 1,
    recorded_at: "2026-10-30T10:00:00Z",
    actor_name: "Asha",
    reason_code: null,
    before: null,
    after: null,
  };
}

let calls: { url: string; cursor: string | null }[];
let fail: boolean;

beforeEach(() => {
  calls = [];
  fail = false;
  vi.spyOn(api, "get").mockImplementation((async (
    url: string,
    config?: { params?: { cursor?: string } },
  ) => {
    const cursor = config?.params?.cursor ?? null;
    calls.push({ url, cursor });
    if (fail) {
      throw Object.assign(new Error("503"), {
        response: { status: 503, data: { detail: "The server is busy." } },
      });
    }
    return cursor
      ? { data: { items: [event("e3")], next_cursor: null } }
      : { data: { items: [event("e1"), event("e2")], next_cursor: "c2" } };
  }) as unknown as typeof api.get);
});

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.restoreAllMocks();
});

function mount(client: QueryClient) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const node: ReactNode = (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AdministrativeHistory subjectKind="site" subjectId="s1" testId="hist" />
      </MemoryRouter>
    </QueryClientProvider>
  );
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

const events = () => [...(host?.querySelectorAll('[data-testid="hist-events"] > li') ?? [])].length;

describe("AdministrativeHistory over the shared cache", () => {
  it("shows a history seen before at once, without asking again while fresh", async () => {
    const client = createQueryClient();
    mount(client);
    expect(host?.textContent).toContain("Loading history…");
    await settle();
    expect(events()).toBe(2);
    unmount();

    mount(client);
    expect(host?.textContent).not.toContain("Loading history…");
    expect(events()).toBe(2);
    await settle();
    expect(calls).toEqual([{ url: "/goods-v1/history/site/s1", cursor: null }]);
  });

  it("adds older pages on demand", async () => {
    mount(createQueryClient());
    await settle();
    const more = host?.querySelector<HTMLButtonElement>('[data-testid="hist-more"]');
    expect(more).toBeTruthy();
    await act(async () => more!.click());
    await settle();
    expect(events()).toBe(3);
    expect(calls.at(-1)).toEqual({ url: "/goods-v1/history/site/s1", cursor: "c2" });
    expect(host?.querySelector('[data-testid="hist-more"]')).toBeNull();
  });

  it("keeps the events on screen and warns when a refresh fails", async () => {
    const client = createQueryClient();
    mount(client);
    await settle();
    fail = true;
    await act(async () => {
      await client.refetchQueries();
    });
    await settle();
    expect(events()).toBe(2);
    expect(host?.querySelector('[data-testid="hist-refresh-failed"]')?.textContent).toContain(
      "The server is busy.",
    );
  });
});
