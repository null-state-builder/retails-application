import { describe, expect, it } from "vitest";

import { configurationPeriod } from "./Configuration";

describe("configuration effective dates", () => {
  it("accepts an open-ended period and a later exclusive end", () => {
    expect(configurationPeriod("2026-09-30T10:00:00Z", "")).toEqual({
      effective_from: "2026-09-30T10:00:00.000Z",
      effective_to: null,
    });
    expect(configurationPeriod("2026-09-30T10:00:00Z", "2026-10-01T10:00:00Z")).toEqual({
      effective_from: "2026-09-30T10:00:00.000Z",
      effective_to: "2026-10-01T10:00:00.000Z",
    });
  });

  it.each(["", " ", "invalid"])("refuses invalid start %j with an actionable message", (from) => {
    expect(() => configurationPeriod(from, "")).toThrow(
      "Choose a valid In force from date and time.",
    );
  });

  it("refuses a malformed optional end", () => {
    expect(() => configurationPeriod("2026-09-30T10:00:00Z", "invalid")).toThrow(
      "Choose a valid Until date and time, or leave it empty.",
    );
  });

  it.each(["2026-09-30T10:00:00Z", "2026-09-29T10:00:00Z"])(
    "refuses a non-positive effective period ending at %s",
    (until) => {
      expect(() => configurationPeriod("2026-09-30T10:00:00Z", until)).toThrow(
        "Until must be later than In force from.",
      );
    },
  );
});
