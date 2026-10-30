import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test, type Page } from "@playwright/test";

// The before/after picture for every Design System v2 phase: the cashier's screens at
// laptop, desktop and big-monitor widths, light and dark. Run with
// `yarn test:visual` (see playwright.visual.config.ts); compare the label folders.

const LABEL = process.env.KDPS_VISUAL_LABEL ?? "before";
const OUT = fileURLToPath(new URL(`../../.local/visual-baseline/${LABEL}/`, import.meta.url));
// The rehearsal tenant's "Cashier 1" demo chip (KDPS_DEMO_LOGINS); dev server only.
const CASHIER_CHIP = process.env.KDPS_VISUAL_CHIP ?? "demo-cashier1";

// Narrow a run to some of it, e.g. while building one screen:
//   KDPS_VISUAL_SCREENS=billing,bills   only these ids
//   KDPS_VISUAL_PATHS=cash:/sell/cash-count,cust:/sell/customers   extra screens
//   KDPS_VISUAL_WIDTHS=1440   KDPS_VISUAL_THEMES=light
const list = (name: string) =>
  (process.env[name] ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);

const ALL_WIDTHS = [
  { name: "1280", width: 1280, height: 800 },
  { name: "1440", width: 1440, height: 900 },
  { name: "1920", width: 1920, height: 1080 },
];
const WIDTHS = list("KDPS_VISUAL_WIDTHS").length
  ? ALL_WIDTHS.filter((w) => list("KDPS_VISUAL_WIDTHS").includes(w.name))
  : ALL_WIDTHS;
const THEMES = (
  list("KDPS_VISUAL_THEMES").length ? list("KDPS_VISUAL_THEMES") : ["light", "dark"]
) as ("light" | "dark")[];
const BASE_SCREENS = [
  { id: "home", path: "/" },
  { id: "action-needed", path: "/action-needed" },
  { id: "billing", path: "/sell" },
  { id: "bills", path: "/sell/bills" },
  { id: "till", path: "/sell/till" },
  // Cash count, Customers and the Sales report are switched off for this cashier on the
  // rehearsal tenant ("No access"); the record in <width>-shown.json would say so.
  { id: "day-summary", path: "/reports/day-summary" },
  { id: "return-to-brand", path: "/return-to-brand" },
  { id: "booking", path: "/booking" },
  { id: "receive", path: "/goods/receive" },
  { id: "transfers", path: "/goods/transfers" },
  { id: "stock", path: "/goods/stock" },
  { id: "counts", path: "/goods/counts" },
  { id: "movements", path: "/goods/movements" },
];
const EXTRA_SCREENS = list("KDPS_VISUAL_PATHS").map((entry) => {
  const [id = entry, path = "/"] = entry.split(/:(.*)/s);
  return { id, path };
});
const ONLY = list("KDPS_VISUAL_SCREENS");
const SCREENS = [
  ...(ONLY.length ? BASE_SCREENS.filter((s) => ONLY.includes(s.id)) : BASE_SCREENS),
  ...EXTRA_SCREENS,
];

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByTestId(CASHIER_CHIP).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"));
}

async function settle(page: Page) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  // Skeletons and fonts: give the page a moment past the network going quiet.
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(600);
}

for (const size of WIDTHS) {
  test(`cashier screens at ${size.name}`, async ({ page }) => {
    await mkdir(OUT, { recursive: true });
    // What each shot actually shows, so a "No access" or redirect is visible in review.
    const shown: Record<string, { url: string; heading: string }> = {};
    await page.setViewportSize({ width: size.width, height: size.height });
    await signIn(page);
    for (const theme of THEMES) {
      await page.evaluate((t) => window.localStorage.setItem("kdps-theme", t), theme);
      for (const screen of SCREENS) {
        await page.goto(screen.path);
        await settle(page);
        // The shell scrolls inside its main area, so each shot is what fits on one screen
        // at this size: the first view a cashier gets, before and after.
        const name = `${size.name}-${theme}-${screen.id}`;
        await page.screenshot({ path: `${OUT}${name}.png`, animations: "disabled" });
        // Read without waiting: workspaces such as Billing have no h1.
        const heading = await page.evaluate(() => document.querySelector("h1")?.textContent ?? "");
        shown[name] = { url: new URL(page.url()).pathname, heading: heading.trim() };
      }
    }
    await writeFile(`${OUT}${size.name}-shown.json`, JSON.stringify(shown, null, 2) + "\n");
  });
}
