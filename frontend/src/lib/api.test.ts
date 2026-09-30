import { describe, expect, it } from "vitest";
import { api, apiErrorCode, apiErrorMessage, unitContext } from "./api";

describe("API refusals", () => {
  it("preserves a deliberate server refusal and its code", () => {
    const refusal = {
      response: { data: { error: "This store is outside your grant.", code: "SCOPE_DENIED" } },
    };
    expect(apiErrorMessage(refusal)).toBe("This store is outside your grant.");
    expect(apiErrorCode(refusal)).toBe("SCOPE_DENIED");
  });

  it("shows the first field error without exposing a debug HTML response", () => {
    expect(apiErrorMessage({ response: { data: { roles: ["Choose one role."] } } })).toBe(
      "Choose one role.",
    );
    expect(apiErrorMessage({ response: { data: "<html>server crash</html>" } })).toBe(
      "Something went wrong. Please try again.",
    );
  });

  it("does not claim a code when the server sent none", () => {
    expect(apiErrorCode({ response: { data: { detail: "Sign in." } } })).toBeUndefined();
  });
});

describe("explicit scoped stock reads", () => {
  it("keeps top-bar defaults while permitting an explicit all-authorised-stores read", async () => {
    unitContext.set({ unit: "12", brand: "7" });
    try {
      const defaults = await api.get("/proof/stock", {
        adapter: async (config) => ({
          config,
          status: 200,
          statusText: "OK",
          headers: {},
          data: config.headers.toJSON(),
        }),
      });
      expect(defaults.data["X-KDPS-Unit"]).toBe("12");
      const all = await api.get("/proof/stock", {
        headers: { "X-KDPS-Unit": "" },
        adapter: async (config) => ({
          config,
          status: 200,
          statusText: "OK",
          headers: {},
          data: config.headers.toJSON(),
        }),
      });
      expect(all.data["X-KDPS-Unit"]).toBe("");
      expect(all.data["X-KDPS-Brand"]).toBe("7");
      const bookmarked = await api.get("/proof/stock", {
        headers: { "X-KDPS-Unit": "27" },
        adapter: async (config) => ({
          config,
          status: 200,
          statusText: "OK",
          headers: {},
          data: config.headers.toJSON(),
        }),
      });
      expect(bookmarked.data["X-KDPS-Unit"]).toBe("27");
      expect(unitContext.unit).toBe("12");
    } finally {
      unitContext.set({});
    }
  });
});
