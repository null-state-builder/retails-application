import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import {
  ROOT,
  forceReceiverExpiry,
  pairTransferCounter,
  saveTransferCounterStorage,
  saveTransfer,
  transferHelper,
  transferCounterStorage,
  transferLogin,
  transferPost,
  transferRecord,
  transferStepUp,
  type Fixture,
  type Snapshot,
} from "./firstStoreTransfer";

const API = "/goods-v1/outbound/transfers";
const WIDTHS = [1440, 1366, 768, 375];
let sourceCounterContext: BrowserContext | undefined;
test.afterEach(async () => {
  await sourceCounterContext?.close();
  sourceCounterContext = undefined;
});
interface Transfer {
  id: string;
  state: string;
  reconciliation: { balanced: boolean };
  reserved_qty: number;
  in_transit_qty: number;
  dispatch_preparation: { id: string } | null;
  dispatches: {
    id: string;
    sequence_no: number;
    state: string;
    arrived_at: string | null;
    lines: { line_key: string; sku_id: string; qty: number }[];
  }[];
}
async function read(page: Page, id: string) {
  const response = await page.request.get(`/api${API}/${id}`);
  expect(response.status()).toBe(200);
  return (await response.json()) as Transfer;
}
async function noOverflow(page: Page) {
  const widths = await page
    .locator(".operations-page")
    .evaluate((e) => ({ width: e.clientWidth, content: e.scrollWidth }));
  expect(widths.content).toBeLessThanOrEqual(widths.width + 1);
}
async function fourWidths(page: Page, focusTestId: string) {
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(page.getByTestId("transfer-summary")).toBeVisible();
    await noOverflow(page);
    await page.getByTestId(focusTestId).focus();
    await expect(page.getByTestId(focusTestId)).toBeFocused();
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => document.activeElement !== document.body)).toBe(true);
  }
}
function noFinancialChange(before: Snapshot, after: Snapshot) {
  for (const key of [
    "origins_hash",
    "sales_hash",
    "tenders_hash",
    "cash_hash",
    "value_hash",
  ] as const)
    expect(after[key]).toBe(before[key]);
  expect(after.freeze_ids.every((id) => id === null)).toBe(true);
}

// One preserved transfer in a separate proof sibling; reruns never draft a
// second movement to hide a committed or partially completed first attempt.
test("TRF-01/03: scan and dispatch, Receiving link and bookmark, destination count and putaway", async ({
  page,
}, testInfo) => {
  test.setTimeout(600_000);
  const fixture = transferHelper<Fixture>("prepare");
  expect(fixture.synthetic_only).toBe(true);
  const artifacts = testInfo.outputPath("checkpoints");
  mkdirSync(artifacts, { recursive: true });
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  if (!transferRecord().transfer_fixture.browser) saveTransfer({ baseline: fixture.snapshot });
  const before = transferRecord().transfer_fixture.browser!.baseline;
  expect(before.source_shelf_qty).toBe(2);
  expect(before.destination_shelf_qty).toBe(0);

  await test.step("Owner checks and approves the fictional destination's ordinary goods readiness", async () => {
    await transferLogin(page, "owner");
    const openingPt = transferRecord().browser_bootstrap.pt_id;
    expect(openingPt).toBeTruthy();
    await page.goto("/goods/pt-work?tab=approve");
    const history = await page.request.get("/api/goods-v1/approvals?limit=100");
    expect(history.status()).toBe(200);
    const decided = (await history.json()).items.filter(
      (row: { state: string; subject_id: string; parent_document?: { id: string } }) =>
        row.state === "approved" && (row.parent_document?.id ?? row.subject_id) === openingPt,
    );
    expect(decided).toHaveLength(1);
    await page.getByTestId(`pt-queue-decided-${decided[0].id}`).click();
    await expect(page.getByTestId("pt-approval-viewer")).toBeVisible();
    await expect(page.getByTestId("pt-approve")).toHaveCount(0);
    await expect(page.getByTestId("pt-revision-superseded")).toHaveCount(0);
    const readiness = await page.request.get(
      `/api/goods-v1/masters/stores/${fixture.destination_site_id}/readiness`,
    );
    expect(readiness.status()).toBe(200);
    if (!(await readiness.json()).data.goods_ready) {
      await page.goto(
        `/setup/organisation?panel=sites&site=${fixture.destination_site_id}&tab=readiness`,
      );
      await expect(page.getByTestId("site-readiness-tab")).toBeVisible();
      await page.getByLabel("Readiness decision reference").fill("SYNTHETIC_TRANSFER_DESTINATION");
      await transferStepUp(page);
      await page.getByTestId("readiness-approve-goods-button").click();
      await expect
        .poll(
          async () =>
            (
              await (
                await page.request.get(
                  `/api/goods-v1/masters/stores/${fixture.destination_site_id}/readiness`,
                )
              ).json()
            ).data.goods_ready,
        )
        .toBe(true);
    }
  });

  let id = transferRecord().transfer_fixture.browser!.transfer_id;
  let startingState = "new";
  let counter: Page | undefined;
  await test.step("source manager drafts one unit and submits its exact review", async () => {
    await transferLogin(page, "manager");
    if (!id) {
      const response = await page.request.get(
        `/api${API}?site=${fixture.source_site_id}&limit=100`,
      );
      expect(response.status()).toBe(200);
      const existing = (await response.json()).items.filter(
        (row: { destination_site_id: string }) =>
          row.destination_site_id === fixture.destination_site_id,
      );
      expect(existing.length).toBeLessThanOrEqual(1);
      if (existing.length) id = existing[0]!.id;
      else {
        await page.goto("/goods/transfers");
        await page.getByTestId("transfers-new").click();
        await page.getByTestId("transfer-destination").selectOption(fixture.destination_site_id);
        const quantity = page.getByTestId(`transfer-new-qty-${fixture.sku_id}`);
        await quantity.fill("3");
        await expect(page.getByTestId("transfer-new-create")).toBeDisabled();
        await quantity.fill("1");
        const created = page.waitForResponse(
          (r) => new URL(r.url()).pathname === `/api${API}` && r.request().method() === "POST",
        );
        await page.getByTestId("transfer-new-create").click();
        const committed = await created;
        expect(committed.status()).toBe(201);
        id = (await committed.json()).id;
      }
      if (!id) throw new Error("The saved transfer did not expose its stable identifier.");
      saveTransfer({ transfer_id: id });
    }
    await page.goto(`/goods/transfers/${id}`);
    const draft = await read(page, id!);
    startingState = draft.state;
    if (draft.state === "draft") {
      await page.getByTestId("transfer-submit").click();
      await expect.poll(async () => (await read(page, id!)).state).toBe("submitted");
    }
  });

  await test.step("the source counter reconciles and pauses before exact pieces may be reserved", async () => {
    const preservedPause = transferRecord().transfer_fixture.browser!.counter_paused;
    if ((await read(page, id!)).state !== "submitted" && !preservedPause) return;
    const unchanged = transferHelper<Snapshot>("snapshot");
    const storage = transferCounterStorage();
    if (preservedPause && !storage.restored)
      throw new Error("The recorded transfer pause lacks its preserved private counter state.");
    sourceCounterContext = await page
      .context()
      .browser()!
      .newContext({
        baseURL: "http://127.0.0.1:5184",
        ...(storage.restored ? { storageState: storage.restored } : {}),
      });
    counter = await sourceCounterContext.newPage();
    await transferLogin(counter, "manager");
    await pairTransferCounter(counter);
    if (!preservedPause) {
      await counter.getByTestId("till-sync-now").click();
      await expect(counter.getByTestId("till-pause-go")).toBeVisible();
      await counter
        .getByTestId("till-pause-reason")
        .fill("Fictional transfer proof; existing bill frontier reconciled");
      await counter.getByTestId("till-pause-go").click();
    }
    await expect(counter.getByTestId("till-pause-state")).toHaveAttribute("data-stage", "paused");
    await saveTransferCounterStorage(counter);
    saveTransfer({ counter_paused: true });
    expect(transferHelper<Snapshot>("snapshot")).toEqual(unchanged);
  });

  await test.step("separate Owner approves the frozen transfer and reservation", async () => {
    await transferLogin(page, "owner");
    await page.goto(`/goods/transfers/${id}`);
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 1000 });
      await expect(page.getByTestId("transfer-summary")).toBeVisible();
      await noOverflow(page);
      await page.screenshot({ path: `${artifacts}/transfer-${width}.png`, fullPage: true });
    }
    if ((await read(page, id!)).state === "submitted") {
      await page.getByTestId("transfer-approve-reason").fill("INDEPENDENT_SYNTHETIC_REVIEW");
      await fourWidths(page, "transfer-approve-reason");
      await transferStepUp(page);
      await page.getByTestId("transfer-approve").click();
      await expect.poll(async () => (await read(page, id!)).state).toBe("approved");
      expect((await read(page, id!)).reserved_qty).toBe(1);
    }
  });

  await test.step("source manager physically scans and dispatches the one approved piece", async () => {
    await transferLogin(page, "manager");
    await page.goto(`/goods/transfers/${id}`);
    let detail = await read(page, id!);
    if (!detail.dispatches.length) {
      if (!detail.dispatch_preparation) await page.getByTestId("transfer-prepare").click();
      await expect(page.getByTestId("prep-scan-form")).toBeVisible();
      if (
        (
          await page
            .getByTestId(/^prep-scanned-/)
            .first()
            .textContent()
        )?.trim() === "0"
      ) {
        await page.getByTestId("prep-scan-tag").fill(fixture.barcode);
        await page.getByTestId("prep-scan-qty").fill("1");
        await page.getByTestId("prep-scan-submit").click();
        await expect(page.getByTestId(/^prep-scanned-/).first()).toHaveText("1");
      }
      await page.getByTestId("transfer-dispatch-ref").fill("FICTIONAL-SHIPMENT-ONLY");
      await fourWidths(page, "transfer-dispatch-ref");
      await page.getByTestId("transfer-dispatch").click();
      await expect.poll(async () => (await read(page, id!)).dispatches.length).toBe(1);
      detail = await read(page, id!);
      expect(detail.in_transit_qty).toBe(1);
      const inTransit = transferHelper<Snapshot>("snapshot");
      noFinancialChange(before, inTransit);
      expect(inTransit.source_shelf_qty).toBe(1);
      expect(inTransit.destination_shelf_qty).toBe(0);
    }
  });

  let detail = await read(page, id!);
  const dispatch = detail.dispatches[0]!;
  await test.step("destination Receiving row and former dispatch bookmark open the canonical transfer", async () => {
    await transferLogin(page, "receiver");
    if (dispatch.state === "in_transit") {
      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 1000 });
        await page.goto("/goods/receive");
        const entry = page.getByTestId(`inbox-open-${dispatch.id}`);
        await expect(entry).toBeVisible();
        await noOverflow(page);
        await entry.click();
        await expect(page).toHaveURL(new RegExp(`/goods/transfers/${id}$`));
        await expect(page.getByTestId("transfer-summary")).toBeVisible();
        await noOverflow(page);
      }
      await page.goto(`/goods/receive/transfer/${dispatch.id}`);
      await expect(page.getByTestId("delivery-open-transfer")).toHaveAttribute(
        "href",
        `/goods/transfers/${id}`,
      );
      await page.getByTestId("delivery-open-transfer").click();
      await expect(page).toHaveURL(new RegExp(`/goods/transfers/${id}$`));
    }
  });

  await test.step("arrival does not make stock sellable; destination counts and independently puts it away", async () => {
    await page.goto(`/goods/transfers/${id}`);
    detail = await read(page, id!);
    let shipment = detail.dispatches[0]!;
    if (shipment.state === "in_transit") {
      if (!shipment.arrived_at) {
        await page
          .getByTestId("arrival-note-1")
          .fill("Fictional shipment arrival for browser proof");
        await fourWidths(page, "arrival-note-1");
        await page.getByTestId("arrival-submit-1").click();
        await expect
          .poll(async () => (await read(page, id!)).dispatches[0]!.arrived_at)
          .not.toBeNull();
      }
      expect(transferHelper<Snapshot>("snapshot").destination_shelf_qty).toBe(0);
      await page.getByTestId("count-1-good").fill("1");
      await fourWidths(page, "count-1-good");
      await page.getByTestId("count-submit-1").click();
      await expect.poll(async () => (await read(page, id!)).dispatches[0]!.state).toBe("counted");
      shipment = (await read(page, id!)).dispatches[0]!;
    }
    if (shipment.state === "counted") {
      expect(transferHelper<Snapshot>("snapshot").destination_shelf_qty).toBe(0);
      const receiverContext = page.context();
      // Source actor sees the transfer, but may not accept at the destination.
      const sourcePage = await receiverContext
        .browser()!
        .newPage({ baseURL: "http://127.0.0.1:5184" });
      try {
        await transferLogin(sourcePage, "manager");
        const beforeDenial = transferHelper<Snapshot>("snapshot");
        const refused = await transferPost(
          sourcePage,
          `${API}/${id}/dispatches/${shipment.id}/accept`,
          {
            command_id: randomUUID(),
            contract_version: "goods-v1",
            lines: [
              {
                line_key: shipment.lines[0]!.line_key,
                qty: 1,
                destination_location_id: fixture.destination_floor_id,
              },
            ],
          },
        );
        expect(refused.status()).toBe(404);
        expect((await refused.json()).code).toBe("NOT_FOUND");
        expect(transferHelper<Snapshot>("snapshot")).toEqual(beforeDenial);
      } finally {
        await sourcePage.close();
      }
      await page.getByTestId("accept-location-1").selectOption(fixture.destination_floor_id);
      await page.getByTestId("accept-qty-1").fill("1");
      await fourWidths(page, "accept-qty-1");
      const request = page.waitForRequest(
        (r) =>
          new URL(r.url()).pathname === `/api${API}/${id}/dispatches/${shipment.id}/accept` &&
          r.method() === "POST",
      );
      await page.getByTestId("accept-submit-1").click();
      saveTransfer({ accept_body: (await request).postDataJSON() as Record<string, unknown> });
      await expect.poll(async () => (await read(page, id!)).state).toBe("completed");
    }
  });

  await test.step("the saved acceptance replays once and refuses a changed replay without another posting", async () => {
    const committed = transferHelper<Snapshot>("snapshot");
    const body = transferRecord().transfer_fixture.browser!.accept_body;
    expect(body).toBeTruthy();
    const response = await transferPost(
      page,
      `${API}/${id}/dispatches/${dispatch.id}/accept`,
      body!,
    );
    expect(response.status()).toBe(200);
    expect(transferHelper<Snapshot>("snapshot")).toEqual(committed);
    const changed = await transferPost(page, `${API}/${id}/dispatches/${dispatch.id}/accept`, {
      ...body,
      lines: [
        {
          line_key: dispatch.lines[0]!.line_key,
          qty: 2,
          destination_location_id: fixture.destination_floor_id,
        },
      ],
    });
    expect(changed.status()).toBe(409);
    expect((await changed.json()).code).toBe("COMMAND_CONFLICT");
    expect(transferHelper<Snapshot>("snapshot")).toEqual(committed);
  });
  await test.step("the source counter resumes normally with a fresh copy after completed putaway", async () => {
    if (!counter) return;
    const committed = transferHelper<Snapshot>("snapshot");
    await counter.goto("/sell/till");
    await expect(counter.getByTestId("till-pause-resume")).toBeEnabled();
    await counter.getByTestId("till-pause-resume").click();
    await expect(counter.getByTestId("till-pause-state")).toHaveCount(0);
    await saveTransferCounterStorage(counter);
    saveTransfer({ counter_paused: false });
    expect(transferHelper<Snapshot>("snapshot")).toEqual(committed);
  });
  await test.step("forced test expiry denies the populated transfer and restores only after a fresh login", async () => {
    await transferLogin(page, "receiver");
    await page.goto(`/goods/transfers/${id}`);
    await expect(page.getByTestId("transfer-summary")).toBeVisible();
    const committed = transferHelper<Snapshot>("snapshot");
    const expiry = await forceReceiverExpiry(page);
    expect(expiry).toEqual(
      expect.objectContaining({
        forced_test_expiry: true,
        session_row_preserved: true,
        business_unchanged: true,
      }),
    );
    const denied = await page.request.get(`/api${API}/${id}`);
    expect(denied.status()).toBe(401);
    const refusal = await denied.json();
    expect(refusal.code).toBe("AUTH_REQUIRED");
    expect(refusal.id).toBeUndefined();
    expect(refusal.dispatches).toBeUndefined();
    const deniedWrite = await transferPost(
      page,
      `${API}/${id}/dispatches/${dispatch.id}/accept`,
      transferRecord().transfer_fixture.browser!.accept_body!,
    );
    expect(deniedWrite.status()).toBe(401);
    expect((await deniedWrite.json()).code).toBe("AUTH_REQUIRED");
    expect(transferHelper<Snapshot>("snapshot")).toEqual(committed);
    await page.reload();
    await expect(page).toHaveURL(/\/login(?:\?|$)/);
    await expect(page.getByTestId("transfer-summary")).toHaveCount(0);
    await transferLogin(page, "receiver");
    await page.goto(`/goods/transfers/${id}`);
    await expect(page.getByTestId("transfer-summary")).toBeVisible();
    expect((await read(page, id!)).state).toBe("completed");
    expect(transferHelper<Snapshot>("snapshot")).toEqual(committed);
  });
  const after = transferHelper<Snapshot>("snapshot");
  noFinancialChange(before, after);
  expect(after.source_shelf_qty).toBe(1);
  expect(after.destination_shelf_qty).toBe(1);
  expect(after.reservations).toBe(before.reservations);
  expect(
    after.positions
      .filter((row) => row.boundary === "physical")
      .reduce((sum, row) => sum + row.qty, 0),
  ).toBe(2);
  expect(
    after.positions
      .filter((row) => row.boundary === "transit")
      .reduce((sum, row) => sum + row.qty, 0),
  ).toBe(0);
  expect(
    after.positions.filter(
      (row) => row.site_id === fixture.destination_site_id && row.boundary === "physical",
    ),
  ).toEqual([
    expect.objectContaining({
      origin_id: fixture.origin_id,
      qty: 1,
      accepted: true,
      unit_cost_paise: "50000",
    }),
  ]);
  expect((await read(page, id!)).reconciliation.balanced).toBe(true);
  expect(pageErrors).toEqual([]);
  const { database, system_identifier } = transferRecord().transfer_rehearsal;
  writeFileSync(
    `${ROOT}/.local/first-store-transfer-evidence.json`,
    JSON.stringify(
      {
        synthetic_only: true,
        database,
        system_identifier,
        transfer_id: id,
        dispatch_id: dispatch.id,
        starting_transfer_state: startingState,
        checkpoints_directory: artifacts,
        widths: WIDTHS,
        accepted_origin_id: fixture.origin_id,
        before,
        after,
        coverage: [
          "TRF-01 approved frozen piece physically scanned and dispatched",
          "source counter recorded pause/release before reservation and normal fresh-copy resume after putaway",
          "TRF-03 destination Receiving link and historical dispatch bookmark",
          "destination arrival/count/putaway UI",
          "source actor destination acceptance NOT_FOUND with unchanged snapshot",
          "exact acceptance replay200 and changed replay409COMMAND_CONFLICT with unchanged snapshot",
          "populated approval/dispatch/arrival/count/putaway forms at four widths with keyboard focus",
          "copied approved opening PT history has no pending-review superseded warning or decision button",
          "forced test expiry: actual read/write401AUTH_REQUIRED, login redirect and fresh-login restoration without business mutation",
        ],
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
});
