import { describe, expect, it } from "vitest";

import { PROTECTED_ROUTES, roomAt, routeWarmup } from "./routes";

describe("route code splitting", () => {
  it("loads every built screen on demand", () => {
    const eager = PROTECTED_ROUTES.filter(
      (r) =>
        !r.id.startsWith("planned:") &&
        !r.id.startsWith("legacy:") &&
        routeWarmup(r.path).preload === undefined,
    ).map((r) => r.id);
    expect(eager).toEqual([]);
  });

  it("keeps the counter room on the counter's screens only", () => {
    for (const path of [
      "/sell",
      "/sell/till",
      "/sell/cash-count",
      "/sell/day-close",
      "/sell/bills",
    ]) {
      expect(roomAt(path)).toBe("counter");
    }
    expect(roomAt("/sell/gift-vouchers")).toBeUndefined();
  });

  it("warms a screen's chunk, and its first list where it is plain", () => {
    const transfers = routeWarmup("/transfer");
    expect(typeof transfers.preload).toBe("function");
    expect(transfers.listUrl).toBe("/outbound/transfers?type=inter_store");

    // A wrapped screen (the till provider around it) still exposes its chunk.
    expect(typeof routeWarmup("/sell/till").preload).toBe("function");

    // A document has no list to warm; an unknown path warms nothing.
    expect(routeWarmup("/transfer/12").listUrl).toBeUndefined();
    expect(routeWarmup("/no/such/screen")).toEqual({});
  });
});
