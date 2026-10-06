import { expect, test } from "@playwright/test";

import { loginProof, pairProof } from "./firstStoreProof";

// Adapted from the older counter-look journey: use trusted accepted proof stock,
// never preview customer/loyalty/credit data or an unowned manual tag.
test("stock bookmarks keep filters and explicit authorised scope", async ({ page }) => {
  await loginProof(page, "manager");
  await page.goto("/stock/search?q=ALPHA000123&sku=ALPHA000123");
  await expect(page).toHaveURL(/tab=stock/);
  await expect(page).toHaveURL(/scope=all/);
  await expect(page.getByTestId("stock-workspace-store")).toHaveValue("all");
  await expect(page.getByTestId("stock-workspace-search")).toHaveValue("ALPHA000123");
  await expect(page.getByTestId("stock-workspace-scope")).toContainText("All authorised stores");
  const site = await page
    .getByTestId("stock-workspace-store")
    .locator("option")
    .last()
    .getAttribute("value");
  await page.getByTestId("stock-workspace-store").selectOption(site!);
  await expect(page).toHaveURL(new RegExp(`site=${site}`));
  await expect(page).toHaveURL(/sku=ALPHA000123/);
  await page.reload();
  await expect(page.getByTestId("stock-workspace-store")).toHaveValue(site!);
  await page.goto("/inventory?tab=stock&site=999999999&q=ALPHA000123");
  await expect(page.getByRole("alert")).toContainText("no longer in your available store choices");
  await expect(page.getByTestId("onhand-table")).toHaveCount(0);
});

for (const width of [1440, 1366, 768, 375]) {
  test(`bills, drawer and daily report reconcile at ${width}px`, async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 1000 });
    await loginProof(page, "manager");
    await pairProof(page);
    await page.goto("/sell/bills");
    await page.getByTestId("bills-from").fill("2026-04-01");
    await page.getByTestId("bills-to").fill("2027-03-31");
    await page.getByTestId("bills-term").fill("26-27/FIRST/SAL/1");
    await page.getByTestId("bills-search").click();
    await expect(page.getByTestId("bills-search")).toBeEnabled();
    await expect(page.getByTestId("bills-row-26-27/FIRST/SAL/1")).toContainText("900");
    await page.getByTestId("bills-open-26-27/FIRST/SAL/1").click();
    await expect(page.getByTestId("bills-detail-line-1")).toContainText("ALPHA000123", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("bills-detail-tenders")).toContainText("Cash");
    const receipt = await page.request.get("/api/sell/sales/26-27/FIRST/SAL/1");
    expect(receipt.status()).toBe(200);
    const bill = await receipt.json();
    expect(bill.doc_number).toBe("26-27/FIRST/SAL/1");
    expect(Number(bill.net_paise)).toBe(90_000);

    await page.goto("/sell/cash-count");
    await expect(page.getByTestId("cash-switched-off")).toHaveCount(0);
    const cash = await page.request.get("/api/sell/cash-count");
    expect(cash.status()).toBe(200);
    const position = await cash.json();
    if (!position.counted) {
      expect(position.bills).toBe(1);
      expect(Number(position.cash_sales_paise)).toBe(90_000);
      await expect(page.getByTestId("cash-count-form")).toBeVisible();
      if (position.opening_declared) await page.getByTestId("cash-opening").fill("0");
      await page.getByTestId("cash-note-500").fill("1");
      await page.getByTestId("cash-note-200").fill("2");
      await expect(page.getByTestId("cash-variance")).toHaveAttribute("data-variance", "0");
      await page.getByTestId("cash-save").click();
    } else {
      // A saved count closes its window. A repeat visit sees no uncounted
      // bills, while the immutable saved count retains the original amount.
      expect(position.bills).toBe(0);
      expect(Number(position.cash_sales_paise)).toBe(0);
      expect(Number(position.counted.cash_sales_paise)).toBe(90_000);
      expect(Number(position.counted.counted_paise)).toBe(90_000);
      expect(Number(position.counted.variance_paise)).toBe(0);
    }
    await expect(page.getByTestId("cash-counted-today")).toBeVisible();
    await page.goto("/reports/day-summary");
    await expect(page.getByTestId("day-mode-cash")).toContainText("900");
    await expect(page.getByTestId("day-counts")).toContainText("1");
    const geometry = await page.locator(".operations-page").evaluate((element) => ({
      width: element.clientWidth,
      scroll: element.scrollWidth,
    }));
    expect(geometry.scroll).toBeLessThanOrEqual(geometry.width + 1);
  });
}

test("a delayed reprint refuses the logged-out session and creates no receipt frame", async ({
  page,
}) => {
  await loginProof(page, "manager");
  await page.goto("/sell/bills");
  await page.getByTestId("bills-from").fill("2026-04-01");
  await page.getByTestId("bills-to").fill("2027-03-31");
  await page.getByTestId("bills-term").fill("26-27/FIRST/SAL/1");
  await page.getByTestId("bills-search").click();
  await page.getByTestId("bills-open-26-27/FIRST/SAL/1").click();
  await expect(page.getByTestId("bills-detail")).toBeVisible();
  let release!: () => void;
  let intercepted!: () => void;
  const paused = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    intercepted = resolve;
  });
  await page.route(
    (url) => decodeURIComponent(url.pathname) === "/api/sell/sales/26-27/FIRST/SAL/1",
    async (route) => {
      intercepted();
      await paused;
      await route.continue();
    },
  );
  await page.getByTestId("bills-reprint").click();
  await started;
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  const logout = await page.request.post("/api/auth/logout", {
    headers: { "X-CSRF-Token": cookie!.value },
  });
  expect(logout.status()).toBe(200);
  const refusal = page.waitForResponse(
    (response) =>
      decodeURIComponent(new URL(response.url()).pathname) === "/api/sell/sales/26-27/FIRST/SAL/1",
  );
  release();
  expect((await refusal).status()).toBe(401);
  await expect(page.locator('iframe[title="Receipt"]')).toHaveCount(0);
  const denied = await page.request.get("/api/sell/sales/26-27/FIRST/SAL/1");
  expect(denied.status()).toBe(401);
  expect(await denied.text()).not.toContain("ALPHA000123");
});

test("customer search reprints the current authorised bill", async ({ page }) => {
  await page.addInitScript(() => {
    window.print = () => {
      if (window.parent !== window) {
        window.parent.document.documentElement.dataset.proofPrintedReceipt =
          document.body.innerText;
      }
    };
  });
  await loginProof(page, "manager");
  await page.goto("/sell/customers?doc=26-27%2FFIRST%2FSAL%2F1");
  await page.getByTestId("find-rows").getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByTestId("find-detail-line-1")).toContainText("ALPHA000123");
  const freshDetail = page.waitForResponse(
    (response) =>
      decodeURIComponent(new URL(response.url()).pathname) === "/api/sell/sales/26-27/FIRST/SAL/1",
  );
  await page.getByTestId("find-reprint").click();
  expect((await freshDetail).status()).toBe(200);
  await expect(page.locator("html")).toHaveAttribute(
    "data-proof-printed-receipt",
    /26-27\/FIRST\/SAL\/1[\s\S]*ALPHA000123/,
  );
  await expect(page.getByTestId("find-print-problem")).toHaveCount(0);
});

test("customer search delayed reprint denies logout before delivery", async ({ page }) => {
  await loginProof(page, "manager");
  await page.goto("/sell/customers?doc=26-27%2FFIRST%2FSAL%2F1");
  await page.getByTestId("find-rows").getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByTestId("find-detail-line-1")).toContainText("ALPHA000123");
  let release!: () => void;
  let intercepted!: () => void;
  const paused = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    intercepted = resolve;
  });
  await page.route(
    (url) => decodeURIComponent(url.pathname) === "/api/sell/sales/26-27/FIRST/SAL/1",
    async (route) => {
      intercepted();
      await paused;
      await route.continue();
    },
  );
  await page.getByTestId("find-reprint").click();
  await started;
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  const logout = await page.request.post("/api/auth/logout", {
    headers: { "X-CSRF-Token": cookie!.value },
  });
  expect(logout.status()).toBe(200);
  const refusal = page.waitForResponse(
    (response) =>
      decodeURIComponent(new URL(response.url()).pathname) === "/api/sell/sales/26-27/FIRST/SAL/1",
  );
  release();
  expect((await refusal).status()).toBe(401);
  await expect(page.locator('iframe[title="Receipt"]')).toHaveCount(0);
  await expect(page.getByTestId("find-detail")).toHaveCount(0);
  const denied = await page.request.get("/api/sell/sales/26-27/FIRST/SAL/1");
  expect(denied.status()).toBe(401);
  expect(await denied.text()).not.toContain("ALPHA000123");
});
