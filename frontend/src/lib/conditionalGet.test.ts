import axios, {
  AxiosError,
  AxiosHeaders,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";
import { afterEach, describe, expect, it } from "vitest";

import {
  forgetRevalidation,
  installConditionalGet,
  revalidationBytes,
  revalidationSize,
  setRevalidationLimits,
} from "./conditionalGet";

/** A pretend server: answers the current body with a strong ETag, and 304
 *  when the request already holds it. Honours `validateStatus` like axios's
 *  own adapters do. */
function server() {
  const state = {
    body: [{ id: 1, name: "Store" }] as unknown,
    version: 1,
    etag: true,
    /** Runs while a request is "on the wire", before it is answered. */
    inFlight: null as null | ((url: string) => Promise<void> | void),
  };
  const seen: { url: string; ifNoneMatch: string | null; method: string }[] = [];
  const adapter = async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
    const headers = AxiosHeaders.from(config.headers);
    const ifNoneMatch = (headers.get("If-None-Match") as string | undefined) ?? null;
    seen.push({ url: String(config.url), ifNoneMatch, method: String(config.method) });
    if (state.inFlight) await state.inFlight(String(config.url));
    const etag = `"v${state.version}"`;
    const notModified = state.etag && ifNoneMatch === etag;
    const response: AxiosResponse = {
      data: notModified ? "" : state.body,
      status: notModified ? 304 : 200,
      statusText: notModified ? "Not Modified" : "OK",
      headers: state.etag ? { etag } : {},
      config,
    };
    const accept = config.validateStatus ?? ((s: number) => s >= 200 && s < 300);
    if (!accept(response.status)) {
      throw new AxiosError("refused", "ERR_BAD_RESPONSE", config, null, response);
    }
    return response;
  };
  const client = axios.create({ baseURL: "/api", adapter });
  installConditionalGet(client);
  return { client, state, seen };
}

afterEach(() => {
  forgetRevalidation();
  setRevalidationLimits();
});

describe("revalidating from memory", () => {
  it("sends If-None-Match from the last 200 and turns a 304 into the remembered body", async () => {
    const { client, state, seen } = server();
    const first = await client.get("/masters/stores");
    expect(first.status).toBe(200);
    expect(seen[0]?.ifNoneMatch).toBeNull();

    const second = await client.get("/masters/stores");
    expect(seen[1]?.ifNoneMatch).toBe('"v1"');
    expect(second.status).toBe(200);
    expect(second.data).toEqual(first.data);
    expect(second.data).not.toBe(first.data);

    state.body = [{ id: 1, name: "Store renamed" }];
    state.version = 2;
    const third = await client.get("/masters/stores");
    expect(seen[2]?.ifNoneMatch).toBe('"v1"');
    expect(third.data).toEqual([{ id: 1, name: "Store renamed" }]);
    await client.get("/masters/stores");
    expect(seen[3]?.ifNoneMatch).toBe('"v2"');
  });

  it("keeps each URL, query string included, apart", async () => {
    const { client, seen } = server();
    await client.get("/vendors");
    await client.get("/vendors", { params: { include_inactive: 1 } });
    expect(seen[1]?.ifNoneMatch).toBeNull();
    await client.get("/vendors?include_inactive=1");
    expect(seen[2]?.ifNoneMatch).toBe('"v1"');
  });

  it("remembers nothing without an ETag, and never revalidates a write", async () => {
    const { client, state, seen } = server();
    state.etag = false;
    await client.get("/outbound/transfers");
    await client.get("/outbound/transfers");
    expect(seen[1]?.ifNoneMatch).toBeNull();
    expect(revalidationSize()).toBe(0);

    state.etag = true;
    await client.get("/vendors");
    await client.post("/vendors", { name: "x" });
    expect(seen[3]?.ifNoneMatch).toBeNull();
  });

  it("forgets everything on logout or a scope change", async () => {
    const { client, seen } = server();
    await client.get("/masters/stores");
    expect(revalidationSize()).toBe(1);
    forgetRevalidation();
    expect(revalidationSize()).toBe(0);
    await client.get("/masters/stores");
    expect(seen[1]?.ifNoneMatch).toBeNull();
  });

  it("never invents a body for a 304 it did not ask for", async () => {
    const adapterless = axios.create({
      baseURL: "/api",
      adapter: async (config) => {
        throw new AxiosError("refused", "ERR_BAD_RESPONSE", config, null, {
          data: "",
          status: 304,
          statusText: "Not Modified",
          headers: {},
          config,
        });
      },
    });
    installConditionalGet(adapterless);
    await expect(adapterless.get("/alerts")).rejects.toBeInstanceOf(AxiosError);
  });
});

describe("a 304 with nothing remembered", () => {
  it("replays the request once, unconditionally, when the body was evicted in flight", async () => {
    setRevalidationLimits({ count: 2 });
    const { client, state, seen } = server();
    await client.get("/masters/stores");
    let evicting = false;
    state.inFlight = async (url) => {
      if (url !== "/masters/stores" || evicting) return;
      evicting = true;
      // Two other reads land while this one is on the wire and push it out.
      await client.get("/masters/brands");
      await client.get("/masters/seasons");
    };
    const second = await client.get("/masters/stores");
    expect(second.status).toBe(200);
    expect(second.data).toEqual([{ id: 1, name: "Store" }]);
    const stores = seen.filter((r) => r.url === "/masters/stores");
    expect(stores.map((r) => r.ifNoneMatch)).toEqual([null, '"v1"', null]);
    state.inFlight = null;
    // The replay's answer is remembered again, so the next read is cheap.
    await client.get("/masters/stores");
    expect(seen.at(-1)?.ifNoneMatch).toBe('"v1"');
  });

  it("replays when the memory was forgotten while the request was in flight", async () => {
    const { client, state, seen } = server();
    await client.get("/vendors");
    state.inFlight = () => {
      state.inFlight = null;
      forgetRevalidation();
    };
    const second = await client.get("/vendors");
    expect(second.status).toBe(200);
    expect(second.data).toEqual([{ id: 1, name: "Store" }]);
    expect(seen.map((r) => r.ifNoneMatch)).toEqual([null, '"v1"', null]);
  });

  it("still refuses what the caller would refuse on the replay", async () => {
    const { client, state } = server();
    await client.get("/alerts");
    state.inFlight = () => {
      state.inFlight = () => {
        state.etag = false;
        state.body = { detail: "gone" };
      };
      forgetRevalidation();
    };
    const answer = await client.get("/alerts");
    expect(answer.status).toBe(200);
    expect(answer.data).toEqual({ detail: "gone" });
  });

  it("does not remember an answer to a request sent before the memory was forgotten", async () => {
    const { client, state } = server();
    state.inFlight = () => {
      state.inFlight = null;
      forgetRevalidation();
    };
    await client.get("/masters/stores");
    expect(revalidationSize()).toBe(0);
  });
});

describe("what is handed out", () => {
  it("gives every caller its own copy, so sorting the rows changes nothing kept", async () => {
    const { client, state } = server();
    state.body = [
      { id: 2, name: "B" },
      { id: 1, name: "A" },
    ];
    const first = await client.get<{ id: number; name: string }[]>("/masters/stores");
    first.data.sort((a, b) => a.id - b.id);
    first.data[0]!.name = "edited";
    const second = await client.get<{ id: number; name: string }[]>("/masters/stores");
    expect(second.data).toEqual([
      { id: 2, name: "B" },
      { id: 1, name: "A" },
    ]);
    second.data.pop();
    const third = await client.get<unknown[]>("/masters/stores");
    expect(third.data).toHaveLength(2);
    expect(third.data).not.toBe(second.data);
  });

  it("caps the remembered bodies by size, oldest first", async () => {
    const { client, state, seen } = server();
    state.body = { text: "x".repeat(100) };
    const size = JSON.stringify(state.body).length;
    setRevalidationLimits({ bytes: size * 2 + 1 });
    await client.get("/masters/stores");
    await client.get("/masters/brands");
    expect(revalidationSize()).toBe(2);
    await client.get("/masters/seasons");
    expect(revalidationSize()).toBe(2);
    expect(revalidationBytes()).toBe(size * 2);
    await client.get("/masters/stores");
    expect(seen.at(-1)?.ifNoneMatch).toBeNull();
    await client.get("/masters/seasons");
    expect(seen.at(-1)?.ifNoneMatch).toBe('"v1"');
  });

  it("never remembers a body bigger than the whole cap", async () => {
    const { client, state } = server();
    state.body = { text: "x".repeat(500) };
    setRevalidationLimits({ bytes: 100 });
    await client.get("/masters/stores");
    expect(revalidationSize()).toBe(0);
    expect(revalidationBytes()).toBe(0);
  });
});
