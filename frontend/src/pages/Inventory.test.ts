import { describe, expect, it } from "vitest";
import {
  dailySections,
  foldTabsFor,
  INVENTORY_FOLD,
  sidebarRows,
  visibleSections,
} from "../shell/navConfig";
import { PANELS } from "./Inventory";

const manager = {
  role: { code: "store_person" },
  navigation: ["home", "stock", "stock_count", "transfer", "money", "reports", "hrms"],
  sections: ["home", "stock", "stock_count", "transfer", "money", "reports", "hrms"].map(
    (code) => ({ code, capability: "operate" }),
  ),
};

describe("daily stock and retained review destinations", () => {
  it("uses the canonical count entry only when the corresponding action is present", () => {
    const absent = foldTabsFor(INVENTORY_FOLD, manager, ["stock.view"]);
    expect(absent.some((tab) => tab.slug === "assigned-counts")).toBe(false);
    const allowed = foldTabsFor(INVENTORY_FOLD, manager, ["stock.view", "count.run"]);
    expect(allowed.find((tab) => tab.slug === "assigned-counts")).toMatchObject({
      entry: "/goods/counts",
      link: true,
      label: "Blind count records (non-trading)",
    });
    expect(allowed.find((tab) => tab.slug === "count")?.label).toBe("Earlier count records");
    expect(allowed.find((tab) => tab.slug === "ledger")?.entry).toBe("/stock/history");
    expect(allowed.some((tab) => tab.slug === "search")).toBe(false);
    expect(allowed.find((tab) => tab.slug === "damage")?.entry).toBe(
      "/goods/stock?view=quarantine",
    );
    for (const tab of allowed.filter((tab) => !tab.link)) expect(PANELS[tab.slug]).toBeDefined();
  });

  it("keeps the earlier quarantine projection behind its retained section gate", () => {
    const stockOnly = foldTabsFor(INVENTORY_FOLD, manager, ["stock.view"]);
    expect(stockOnly.some((tab) => tab.slug === "damage-history")).toBe(false);
    const history = foldTabsFor(
      {
        ...INVENTORY_FOLD,
      },
      {
        ...manager,
        navigation: [...manager.navigation, "return_to_brand"],
        sections: [...manager.sections, { code: "return_to_brand", capability: "operate" }],
      },
      ["stock.view"],
    );
    expect(history.find((tab) => tab.slug === "damage-history")?.entry).toBe(
      "/inventory?tab=damage-history",
    );
    expect(PANELS.damage!().type).not.toBe(PANELS["damage-history"]!().type);
  });

  it("keeps planned pages permission-filtered and reachable through the roadmap, outside daily manager navigation", () => {
    const sections = visibleSections(manager, ["stock.view"]);
    expect(sections.flatMap((section) => section.items).some((item) => item.planned)).toBe(true);
    expect(
      dailySections(sections, "store_person")
        .flatMap((section) => section.items)
        .some((item) => item.planned),
    ).toBe(false);
    expect(dailySections(sections, "owner")).toBe(sections);
    const rows = sidebarRows(manager, ["stock.view"]);
    for (const row of rows) {
      if (row.kind === "strip") expect(row.tabs.some((item) => item.planned)).toBe(false);
      if (row.kind === "section")
        expect(row.section.items.some((item) => item.planned)).toBe(false);
    }
    expect(
      visibleSections(
        { ...manager, navigation: ["stock"], sections: [{ code: "stock", capability: "view" }] },
        ["stock.view"],
      )
        .flatMap((section) => section.items)
        .some((item) => item.to === "/money/tally"),
    ).toBe(false);
  });
});
