import { expect, test } from "@playwright/test";
import { loginProof, pairProof } from "./firstStoreProof";

// This journey writes only to the separately recorded clone of the fictional
// shop. The initial three-unit opening, first ₹900 bill and history stay intact.
test("online sale, lost response replay, exchange, physical acceptance and post-close disclosure", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await loginProof(page, "manager");
  await pairProof(page);
  await page.getByTestId("till-sync-now").click();
  await page.goto("/sell");
  await expect(page.getByTestId("bill-scan")).toBeEnabled();
  const salesperson = await page
    .getByTestId("bill-sold-by")
    .locator("option")
    .last()
    .getAttribute("value");
  await page.getByTestId("bill-sold-by").selectOption(salesperson!);
  await page.getByTestId("bill-scan").fill("ALPHA000123");
  await page.getByTestId("bill-scan").press("Enter");
  await expect(page.getByTestId("bill-line-1")).toContainText("ALPHA000123");
  await page.getByTestId("bill-all-cash").click();
  await expect(page.getByTestId("bill-save")).toBeEnabled();
  let original = "";
  let lost = false;
  await page.route("**/api/sell/sales/finalise-online", async (route) => {
    if (lost) {
      await route.continue();
      return;
    }
    lost = true;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    original = (await response.json()).doc_number;
    await route.abort("failed"); // The server committed; the cashier got no answer.
  });
  await page.getByTestId("bill-save").click();
  await expect(page.getByTestId("online-sale-pending")).toBeVisible();
  await page.getByRole("button", { name: "Retry same submission", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Bill saved", exact: true })).toBeVisible();
  expect(original).toBeTruthy();
  await expect(page.getByRole("dialog")).toContainText(original);
  await page.unroute("**/api/sell/sales/finalise-online");
  const first = await page.request.get(`/api/sell/sales/${encodeURIComponent(original)}`);
  expect(first.status()).toBe(200);
  const immutable = await first.json();
  expect(Number(immutable.net_paise)).toBe(100_000);
  await page.getByRole("button", { name: "Next bill", exact: false }).click();
  await page.getByRole("button", { name: "Return & exchange", exact: true }).click();
  await page.getByTestId("bill-scan").fill(original);
  await page.getByTestId("bill-scan").press("Enter");
  await expect(page.getByTestId("return-against-bill")).toBeVisible();
  await page.getByLabel("Quantity of line 1 coming back", { exact: true }).fill("1");
  await page.getByLabel("Reason for returning line 1", { exact: true }).selectOption("size");
  await page.getByLabel("Scan pieces going out", { exact: false }).check();
  await page.getByTestId("bill-scan").fill("ALPHA000123");
  await page.getByTestId("bill-scan").press("Enter");
  await expect(page.getByTestId("bill-line-1")).toBeVisible();
  await expect(page.getByTestId("bill-due")).toContainText("0");
  await page.setViewportSize({ width: 375, height: 1000 });
  await expect(page.getByTestId("bill-save")).toBeEnabled();
  const exchange = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/sell/sales/finalise-online",
  );
  await page.getByTestId("bill-save").click();
  const response = await exchange;
  expect(response.status()).toBe(201);
  const exchanged = await response.json();
  await expect(page.getByRole("dialog", { name: "Exchange saved", exact: true })).toBeVisible();
  const exchangeReceipt = await page.request.get(
    `/api/sell/sales/${encodeURIComponent(exchanged.doc_number)}`,
  );
  expect(exchangeReceipt.status()).toBe(200);
  expect(Number((await exchangeReceipt.json()).net_paise)).toBe(0);
  const retained = await page.request.get(`/api/sell/sales/${encodeURIComponent(original)}`);
  const after = await retained.json();
  expect(after.doc_number).toBe(immutable.doc_number);
  expect(after.net_paise).toBe(immutable.net_paise);
  // A good return is unavailable until its bill-linked receiving row is physically accepted.
  await page.goto("/goods/receive");
  const returned = page.getByTestId(/^inbox-open-return:/);
  await expect(returned).toHaveCount(1);
  await returned.click();
  await expect(page.getByTestId("return-acceptance")).toBeVisible();
  await page.getByTestId("return-putaway").selectOption({ label: "Fictional proof sales floor" });
  await page.getByTestId("return-physical-check").check();
  await page.getByTestId("return-accept").click();
  await expect(page.getByTestId("return-accepted")).toBeVisible();
  await page.goto("/sell/cash-count");
  const cash = await page.request.get("/api/sell/cash-count");
  expect(cash.status()).toBe(200);
  const position = await cash.json();
  expect(Number(position.cash_sales_paise)).toBe(100_000);
  expect(position.bills).toBe(2);
  // The original shop already closed this business day. Preserve that count;
  // never rewrite it to absorb sales arriving later or claim another close.
  await expect(page.getByTestId("cash-counted-today")).toBeVisible();
  await expect(page.getByTestId("cash-after-close")).toContainText("1,900");
  await expect(page.getByTestId("cash-after-close")).toContainText("2 uncounted bill(s)");
  await page.goto("/reports/day-summary");
  await expect(page.getByTestId("day-mode-cash")).toContainText("1,900");
});
