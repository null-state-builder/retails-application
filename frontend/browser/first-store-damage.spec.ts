import { randomUUID } from "node:crypto";
import { expect, test, type Page, type Response } from "@playwright/test";
import {
  login,
  pair,
  post,
  record,
  responseFor,
  snapshot,
  stepUp,
  unchanged,
  widths,
} from "./firstStoreDamage";

const meta = () => ({ command_id: randomUUID(), contract_version: "goods-v1" });
async function report(page: Page, reason: string) {
  await login(page, "manager");
  await page.goto(`/goods/stock?site_id=${record().damage_fixture.site_id}`);
  const row = page
    .getByRole("row")
    .filter({ has: page.getByTestId("stock-mark-damaged") })
    .first();
  await row.getByTestId("stock-mark-damaged").click();
  const form = page.getByTestId("stock-damage-form");
  await form.getByTestId("stock-damage-qty").fill("1");
  await form.getByTestId("stock-damage-reason").fill(reason);
  await widths(page);
  const response = responseFor(page, "/goods-v1/outbound/mark-damaged");
  await form.getByTestId("stock-damage-confirm").click();
  const marked = await response;
  expect(marked.status()).toBe(201);
  return marked;
}
async function review(page: Page, id: string, decision: "confirm" | "reject") {
  await login(page, "owner");
  await page.goto(`/goods/movements?site_id=${record().damage_fixture.site_id}`);
  const row = page.locator(`[data-testid="dmg-row"][data-report="${id}"]`);
  await expect(row).toBeVisible();
  await row
    .getByTestId("dmg-reason")
    .fill(`Fictional independent ${decision} after physical review`);
  await widths(page);
  const path = `/goods-v1/outbound/damage-reports/${id}/decide`;
  const before = snapshot(`DMG ${decision} before password gate`);
  const passwordRequired = responseFor(page, path);
  await row.getByTestId(`dmg-${decision}`).click();
  const refusal = await passwordRequired;
  expect(refusal.status()).toBe(403);
  expect((await refusal.json()).code).toBe("STEP_UP_REQUIRED");
  unchanged(before, snapshot(`DMG ${decision} password gate has no business mutation`));
  // The retry after password confirmation is the response that decides; keep every
  // answer on this path and take the first that is not a password-required refusal,
  // rather than whichever response the page happened to emit first.
  const answers: Response[] = [];
  const collect = (candidate: Response) => {
    if (
      new URL(candidate.url()).pathname === `/api${path}` &&
      candidate.request().method() === "POST"
    )
      answers.push(candidate);
  };
  page.on("response", collect);
  try {
    await stepUp(page);
    await expect
      .poll(() => answers.some((answer) => answer.status() !== 403), { timeout: 30_000 })
      .toBe(true);
  } finally {
    page.off("response", collect);
  }
  const decided = answers.find((answer) => answer.status() !== 403)!;
  expect(decided.status()).toBe(200);
  return decided;
}

test("DMG-01: scoped physical damage, quarantine sale refusal, independent confirmation/correction and exact replay", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const before = snapshot("DMG completed source clone baseline");
  expect(before.sellable_qty).toBe(2);
  const marking = await report(page, "FICTIONAL_TORN_SEAM");
  const held = snapshot("DMG immediate quarantine before independent confirmation");
  expect(held.sellable_qty).toBe(1);
  expect(held.origin_hash).toBe(before.origin_hash);
  expect(held.money_hash).toBe(before.money_hash);
  expect(held.stock_cost_paise).toBe(before.stock_cost_paise);
  expect(held.value_legs).toEqual(before.value_legs);
  expect(held.journals).toHaveLength(before.journals.length + 1);
  const first = held.damage_reports.find(
    (row) => !before.damage_reports.some((old) => old.id === row.id),
  )!;
  expect(first).toMatchObject({ state: "pending", quantity: 1, reviewer_id: null });
  const repeated = await post(
    page,
    "/goods-v1/outbound/mark-damaged",
    marking.request().postDataJSON(),
  );
  expect(repeated.status()).toBe(201);
  unchanged(held, snapshot("DMG report exact replay unchanged"));
  const changed = marking.request().postDataJSON();
  changed.lines[0].qty = 2;
  const conflict = await post(page, "/goods-v1/outbound/mark-damaged", changed);
  expect(conflict.status()).toBe(409);
  expect((await conflict.json()).code).toBe("COMMAND_CONFLICT");
  const unauthorized = await post(page, `/goods-v1/outbound/damage-reports/${first.id}/decide`, {
    ...meta(),
    decision: "reject",
    reason: "Fictional unauthorized release attempt",
  });
  expect(unauthorized.status()).toBe(403);
  expect((await unauthorized.json()).code).toBe("ACTION_DENIED");
  unchanged(held, snapshot("DMG changed replay and unauthorized release denied"));

  await pair(page);
  await page.getByTestId("till-sync-now").click();
  await page.goto("/sell");
  await page.getByTestId("bill-sold-by").selectOption({ index: 1 });
  for (let count = 0; count < 2; count++) {
    await page.getByTestId("bill-scan").fill(record().damage_fixture.barcode);
    await page.getByTestId("bill-scan").press("Enter");
    await expect(page.getByTestId("bill-line-1")).toBeVisible();
  }
  await expect(page.getByTestId("bill-qty-1")).toHaveValue("2");
  await page.getByTestId("bill-all-cash").click();
  const sale = responseFor(page, "/sell/sales/finalise-online");
  await page.getByTestId("bill-save").click();
  const refused = await sale;
  expect(refused.status()).toBe(422);
  expect((await refused.json()).code).toBe("INSUFFICIENT_ELIGIBLE_STOCK");
  await expect(page.getByTestId("online-sale-pending")).toContainText("Bill not issued");
  const afterSale = snapshot("DMG real ordinary-sale writer refused quarantine consumption");
  unchanged(held, afterSale);
  expect(afterSale.rejected_intents).toBe(held.rejected_intents + 1);
  expect(afterSale.bills).toBe(before.bills);

  const confirmation = await review(page, first.id, "confirm");
  const confirmed = snapshot("DMG confirmed damage without second posting or value event");
  unchanged(held, confirmed);
  expect(confirmed.damage_reports.find((row) => row.id === first.id)).toMatchObject({
    state: "confirmed",
    quantity: 1,
    release_movement_id: null,
  });
  expect(confirmed.damage_reports.find((row) => row.id === first.id)!.reviewer_id).not.toBe(
    first.reporter_id,
  );
  const replayConfirmation = await post(
    page,
    `/goods-v1/outbound/damage-reports/${first.id}/decide`,
    confirmation.request().postDataJSON(),
  );
  expect(replayConfirmation.status()).toBe(200);
  unchanged(confirmed, snapshot("DMG confirmation exact replay unchanged"));

  await report(page, "FICTIONAL_MISTAKEN_REPORT");
  const secondHeld = snapshot("DMG separate mistaken report quarantines remaining good piece");
  expect(secondHeld.sellable_qty).toBe(0);
  const second = secondHeld.damage_reports.find(
    (row) => !confirmed.damage_reports.some((old) => old.id === row.id),
  )!;
  const correction = await review(page, second.id, "reject");
  const corrected = snapshot("DMG independent correction restores only mistaken report");
  expect(corrected.sellable_qty).toBe(1);
  expect(corrected.damage_reports.find((row) => row.id === first.id)).toEqual(
    confirmed.damage_reports.find((row) => row.id === first.id),
  );
  expect(corrected.damage_reports.find((row) => row.id === second.id)).toMatchObject({
    state: "rejected",
    quantity: 1,
  });
  expect(
    corrected.damage_reports.find((row) => row.id === second.id)!.release_movement_id,
  ).not.toBeNull();
  expect(corrected.stock_cost_paise).toBe(before.stock_cost_paise);
  expect(corrected.origin_hash).toBe(before.origin_hash);
  expect(corrected.money_hash).toBe(before.money_hash);
  expect(corrected.value_legs).toEqual(before.value_legs);
  expect(corrected.journals).toHaveLength(secondHeld.journals.length + 1);
  expect(
    (
      await post(
        page,
        `/goods-v1/outbound/damage-reports/${second.id}/decide`,
        correction.request().postDataJSON(),
      )
    ).status(),
  ).toBe(200);
  unchanged(corrected, snapshot("DMG correction exact replay unchanged"));
  await page.goto(`/goods/stock?site_id=${record().damage_fixture.site_id}&view=quarantine`);
  // A stock row is an aggregate: it speaks for the latest report and counts the earlier
  // confirmed one as "+1 more"; the confirmed decision itself was asserted above.
  await expect(page.getByTestId("stock-damage-review").first()).toContainText(
    /Report rejected.*\(\+1 more\)/,
  );
  await expect(page.getByTestId("stock-available-qty")).toHaveText("0");
  await widths(page);
});
