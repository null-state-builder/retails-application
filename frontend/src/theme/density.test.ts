import { afterEach, describe, expect, it, vi } from "vitest";
import { applyDensity, roleDensity } from "./density";

function fakeRoot(): HTMLElement {
  return { dataset: {} } as unknown as HTMLElement;
}

function storeChoice(value: string | null) {
  vi.stubGlobal("window", { localStorage: { getItem: () => value } });
}

describe("density", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("gives cashiers Counter and everyone else Desk", () => {
    expect(roleDensity("store_person")).toBe("counter");
    expect(roleDensity("store_manager")).toBe("desk");
    expect(roleDensity("owner")).toBe("desk");
    expect(roleDensity("")).toBe("desk");
  });

  it("lets a person's own choice win and restores the root on cleanup", () => {
    storeChoice("compact");
    const root = fakeRoot();
    const restore = applyDensity(root, "store_person");
    expect(root.dataset.density).toBe("compact");
    restore();
    expect(root.dataset.density).toBeUndefined();
  });

  it("falls back to the role for an unknown or missing choice", () => {
    storeChoice("huge");
    const root = fakeRoot();
    applyDensity(root, "store_person");
    expect(root.dataset.density).toBe("counter");
    vi.unstubAllGlobals();
    const other = fakeRoot();
    applyDensity(other, "store_manager");
    expect(other.dataset.density).toBe("desk");
  });
});
