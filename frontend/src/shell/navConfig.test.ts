import { describe, expect, it } from "vitest";

import { headingOwning, litLine, menuSectionCode, NAV_ITEMS, sidebarRows } from "./navConfig";

/** A login holding `codes` at `capability`, across `stores` stores. */
function login(codes: string[], capability = "manage", stores = 2, role = "owner") {
  return {
    role: { code: role },
    navigation: codes,
    sections: codes.map((code) => ({ code, capability })),
    stores: Array.from({ length: stores }, (_, i) => ({ code: `S${i}` })),
  };
}

/** Every menu line the sidebar draws, by its `to`. */
function lines(rows: ReturnType<typeof sidebarRows>): string[] {
  return rows.flatMap((r) => (r.kind === "section" ? r.section.items.map((i) => i.to) : []));
}

const ALL_ON = new Set([
  "customer-rights",
  "customer-reservation",
  "special-orders",
  "alterations",
  "cash-count",
]);

describe("the one sidebar (desktop redesign, 29 Oct 2026)", () => {
  it("draws no planned screen as a menu line, and lists the roadmap instead", () => {
    const drawn = lines(sidebarRows(login(["home", "money", "reports", "hrms"])));
    expect(drawn).toContain("/roadmap");
    expect(drawn).not.toContain("/money/payments");
    expect(drawn).not.toContain("/reports/maker");
    expect(drawn).not.toContain("/staff/payroll");
  });

  it("reads Customer Orders under Customers, each line on its own section's gate", () => {
    const rows = sidebarRows(login(["customers", "customer_orders"]), [], ALL_ON);
    const customers = rows.find((r) => r.key === "customers");
    expect(customers?.kind === "section" && customers.section.items.map((i) => i.to)).toEqual([
      "/customers",
      "/orders/reservations",
      "/orders/special",
      "/orders/alterations",
    ]);
    expect(rows.some((r) => r.key === "customer_orders")).toBe(false);
    // Without the Customer Orders grant, the group draws Customers alone.
    expect(lines(sidebarRows(login(["customers"]), [], ALL_ON))).toEqual(["/customers"]);
    expect(headingOwning("/orders/special", "owner")).toBe("customers");
  });

  it("moves a setting-up screen to Setup only where Setup is drawn, so nobody loses it", () => {
    const withSetup = lines(sidebarRows(login(["money", "setup"])));
    expect(withSetup.filter((to) => to === "/money/partner-billing")).toHaveLength(1);
    const setup = sidebarRows(login(["money", "setup"])).find((r) => r.key === "setup");
    expect(
      setup?.kind === "section" &&
        setup.section.items.some((i) => i.to === "/money/partner-billing"),
    ).toBe(true);
    // No Setup: the line stays under Money.
    expect(lines(sidebarRows(login(["money"])))).toContain("/money/partner-billing");
    // A goods screen moves on its own grants, and stays put without Setup.
    const grants = ["opening.import.stage"];
    expect(lines(sidebarRows(login(["receive_goods", "setup"]), grants))).toContain(
      "/goods/opening",
    );
    const receiveOnly = sidebarRows(login(["receive_goods"]), grants);
    expect(lines(receiveOnly)).toContain("/goods/opening");
    expect(headingOwning("/goods/opening", "owner")).toBe("setup");
  });

  it("leaves the counter's own lines off a login that is not one store", () => {
    const owner = lines(sidebarRows(login(["sell"], "manage", 2), [], ALL_ON));
    expect(owner).not.toContain("/sell");
    expect(owner).not.toContain("/sell/cash-count");
    expect(owner).toContain("/sell/till");
    const counter = lines(sidebarRows(login(["sell"], "manage", 1, "store_person"), [], ALL_ON));
    expect(counter).toContain("/sell");
    expect(counter).toContain("/sell/cash-count");
  });

  it("keeps retiring legacy screens off the menu, still routed for bookmarks", () => {
    const grants = ["transfer.move"];
    const drawn = lines(sidebarRows(login(["transfer", "stock_count", "stock"]), grants));
    for (const to of ["/transfer", "/transfer/new", "/transfer/in-transit", "/stock-count"])
      expect(drawn).not.toContain(to);
  });

  it("keeps a legacy line wherever the page that now links to it is not drawn", () => {
    // Legacy grants, no goods ones: Transfers and Stock on hand are not drawn,
    // so the earlier records keep their own lines.
    const legacy = lines(sidebarRows(login(["transfer", "stock_count", "return_to_brand"])));
    expect(legacy).toContain("/transfer");
    expect(legacy).toContain("/stock-count");
    expect(legacy).toContain("/inventory?tab=damage-history");
    for (const to of ["/transfer/new", "/transfer/in-transit"]) expect(legacy).not.toContain(to);
  });

  it("lights the line that leads to a screen whose own line is not drawn", () => {
    const rows = sidebarRows(login(["stock"]), ["stock.view", "movement.draft"]);
    expect(lines(rows)).not.toContain("/goods/stock");
    expect(litLine(rows, "/inventory", "?tab=stock&view=location")).toBe("/stock");
    expect(litLine(rows, "/goods/stock", "")).toBe("/stock");
    expect(litLine(rows, "/stock/history", "")).toBe("/goods/movements");
    expect(headingOwning("/inventory", "store_person", rows, "?tab=stock")).toBe("stock");
  });

  it("captions a moved screen under Setup only where Setup draws it", () => {
    const item = NAV_ITEMS.find((i) => i.to === "/money/partner-billing")!;
    expect(menuSectionCode(item, sidebarRows(login(["money", "setup"])))).toBe("setup");
    expect(menuSectionCode(item, sidebarRows(login(["money"])))).toBe("money");
  });

  it("gives the store person the store's own shorter list under the one sidebar", () => {
    const grants = ["receive.arrival", "pt.prepare", "label.print"];
    const store = sidebarRows(
      login(["home", "receive_goods", "money", "reports"], "operate", 1, "store_person"),
      grants,
    );
    const drawn = lines(store);
    expect(drawn).toContain("/goods/receive");
    expect(drawn).toContain("/roadmap");
    for (const to of ["/goods/pt-work", "/goods/labels", "/money/partner-billing"])
      expect(drawn).not.toContain(to);
    // Another role with the same access keeps every line.
    const manager = lines(
      sidebarRows(login(["home", "receive_goods", "money"], "operate", 1, "store_manager"), grants),
    );
    expect(manager).toContain("/money/partner-billing");
    expect(manager).toContain("/goods/labels");
  });

  it("draws every role the same rows: no persona strips or folds", () => {
    const store = sidebarRows(login(["home", "sell", "stock"], "operate", 1, "store_person"));
    expect(store.every((r) => r.kind === "section")).toBe(true);
  });
});
