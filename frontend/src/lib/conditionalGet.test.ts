import axios, {
  AxiosError,
  AxiosHeaders,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";
import { afterEach, describe, expect, it } from "vitest";

import { forgetRevalidation, installConditionalGet, revalidationSize } from "./conditionalGet";

/** A pretend server: answers the current body with a strong ETag, and 304
 *  when the request already holds it. Honours `validateStatus` like axios's
 *  own adapters do. */
function server() {
  const state = { body: [{ id: 1, name: "Store" }] as unknown, version: 1, etag: true };
  const seen: { url: string; ifNoneMatch: string | null; method: string }[] = [];
  const adapter = async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
    const headers = AxiosHeaders.from(config.headers);
    const ifNoneMatch = (headers.get("If-None-Match") as string | undefined) ?? null;
    seen.push({ url: String(config.url), ifNoneMatch, method: String(config.method) });
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

afterEach(() => forgetRevalidation());

describe("revalidating from memory", () => {
  it("sends If-None-Match from the last 200 and turns a 304 into the remembered body", async () => {
    const { client, state, seen } = server();
    const first = await client.get("/masters/stores");
    expect(first.status).toBe(200);
    expect(seen[0]?.ifNoneMatch).toBeNull();

    const second = await client.get("/masters/stores");
    expect(seen[1]?.ifNoneMatch).toBe('"v1"');
    expect(second.status).toBe(200);
    expect(second.data).toBe(first.data);

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
