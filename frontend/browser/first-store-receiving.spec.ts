import { randomUUID } from "node:crypto";
import { expect, test, type Browser, type Page } from "@playwright/test";
import {
  BASE,
  helper,
  fixture,
  plan,
  snapshot,
  decisionEvidence,
  login,
  pair,
  stepUp,
  post,
  responseFor,
  sourceRead,
  sourcePage,
  noOverflow,
  widthCheckpoint,
  unchangedStock,
  type Artifact,
  record,
} from "./firstStoreReceiving";

const COUNTS = "/goods-v1/outbound/soh-reconciliations";
const meta = (revision?: number) => ({
  command_id: randomUUID(),
  contract_version: "goods-v1",
  ...(revision ? { expected_revision: revision } : {}),
});

async function reviewedSource(
  page: Page,
  name: string,
  quantity: number,
  barcode = "ALPHA000123",
): Promise<string> {
  const artifact = helper<Artifact>("artifact", name, String(quantity), barcode);
  const inputs = plan();
  await login(page, "manager");
  await page.goto("/goods/opening");
  await page.getByLabel("SOH source (.xlsx)", { exact: true }).setInputFiles(artifact.file);
  const uploaded = responseFor(page, "/goods-v1/ptmapper/soh-imports/upload");
  await page.getByRole("button", { name: "Upload and inspect source", exact: true }).click();
  const upload = await uploaded;
  expect(upload.status()).toBe(201);
  const id = (await upload.json()).id as string;
  await expect(page.getByTestId("soh-import")).toBeVisible();
  await widthCheckpoint(page, ["SOH source rows"]);
  await login(page, "admin");
  await sourcePage(page, name);
  const claim = page.getByRole("button", { name: "Start valuation preparation", exact: true });
  if (await claim.count()) await claim.click();
  await page.getByRole("button", { name: "mapping", exact: true }).click();
  for (const [label, mappings] of (quantity >= 0
    ? [
        ["Fictional source brand", inputs.brand_mappings],
        ["Fictional source season", inputs.season_mappings],
        ["M", inputs.size_mappings],
      ]
    : []) as readonly (readonly [string, Record<string, string>])[]) {
    await page
      .getByRole("row")
      .filter({ has: page.getByRole("cell", { name: label, exact: true }) })
      .locator("select")
      .selectOption(mappings[label]!);
  }
  if (quantity >= 0)
    await page
      .getByRole("row")
      .filter({ has: page.getByRole("cell", { name: "Shirt", exact: true }) })
      .getByPlaceholder("Confirmed HSN", { exact: true })
      .fill("6101");
  await widthCheckpoint(page, [
    "SOH brand mappings",
    "SOH season mappings",
    "SOH category mappings",
    "SOH size mappings",
  ]);
  await page.getByRole("button", { name: "controls", exact: true }).click();
  await page
    .getByLabel("Actual source cutoff (date, time and timezone)", { exact: true })
    .fill(artifact.cutoff_at);
  await page
    .getByLabel("Approved identity profile", { exact: true })
    .selectOption(inputs.identity_profile_id);
  await page
    .getByLabel("Approved opening pricing profile", { exact: true })
    .selectOption(inputs.profile_version_id);
  await page.getByLabel("Confirmed Rate meaning", { exact: true }).selectOption("basic_ex_tax");
  for (const [label, button] of [
    ["Valuation evidence", "Upload valuation evidence"],
    ["External reconciliation evidence", "Upload reconciliation evidence"],
  ]) {
    await page.getByLabel(label!, { exact: true }).setInputFiles(artifact.evidence_file);
    const response = responseFor(page, "/goods-v1/files/uploads");
    await page.getByRole("button", { name: button!, exact: true }).click();
    expect((await response).status()).toBe(201);
  }
  await page
    .getByLabel("Source quality and mapping review", { exact: true })
    .fill(`Generated fictional ${name} source, complete shop coverage; stable identity mappings.`);
  await page
    .getByLabel("Explained quantity/value differences and external-books reconciliation", {
      exact: true,
    })
    .fill(
      `Fictional physical count ${quantity} units at 500 INR basic; retained earlier stock, bill and money history. No real books asserted.`,
    );
  await page
    .getByLabel("The source belongs to this store; the filename alone is insufficient.", {
      exact: true,
    })
    .check();
  await page
    .getByLabel("The cutover source is fresh and all subsequent movements are accounted for.", {
      exact: true,
    })
    .check();
  await widthCheckpoint(page);
  const prepared = responseFor(page, `/goods-v1/ptmapper/soh-imports/${id}/prepare`);
  await page
    .getByRole("button", { name: "Save reviewed mappings and valuation controls", exact: true })
    .click();
  expect((await prepared).status()).toBe(200);
  await login(page, "manager");
  await sourcePage(page, name);
  await page.getByRole("button", { name: "physical", exact: true }).click();
  await page
    .getByTestId("soh-import")
    .locator('input[type="file"]')
    .setInputFiles(artifact.physical_file);
  await widthCheckpoint(page);
  const verified = responseFor(page, `/goods-v1/ptmapper/soh-imports/${id}/verify`);
  await page.getByRole("button", { name: "Record physical verification", exact: true }).click();
  expect((await verified).status()).toBe(200);
  await login(page, "admin");
  await sourcePage(page, name);
  await page.getByRole("button", { name: "opening", exact: true }).click();
  const submitted = responseFor(page, `/goods-v1/ptmapper/soh-imports/${id}/submit`);
  await page
    .getByRole("button", { name: "Submit exact source for independent Owner review", exact: true })
    .click();
  const submittedResponse = await submitted;
  if (quantity < 0) {
    expect(submittedResponse.status()).toBe(422);
    expect((await submittedResponse.json()).code).toBe("SOH_NOT_READY");
    expect((await sourceRead(page, id)).state).toBe("review");
    return id;
  }
  expect(submittedResponse.status()).toBe(200);
  await login(page, "owner");
  await sourcePage(page, name);
  await page.getByRole("button", { name: "opening", exact: true }).click();
  await page.getByRole("button", { name: "Approve reviewed source", exact: true }).click();
  await stepUp(page, "owner");
  await expect.poll(async () => (await sourceRead(page, id)).state).toBe("approved");
  return id;
}
async function countRead(page: Page, id: string) {
  const response = await page.request.get(`/api${COUNTS}/${id}`);
  expect(response.status()).toBe(200);
  return (await response.json()) as {
    id: string;
    revision: number;
    state: string;
    content_hash: string;
    data: {
      approval_request_id: string | null;
      journal_batch_id: string | null;
      review: {
        differences: {
          book_qty: number;
          observed_qty: number;
          delta: number;
          value_removed_paise: string;
          source_row_keys: string[];
        }[];
        problems: { code: string }[];
      } | null;
    };
  };
}
async function freeze(page: Page, name: string, source: string) {
  await login(page, "manager");
  await sourcePage(page, name);
  await page.getByRole("button", { name: "opening", exact: true }).click();
  await page
    .getByLabel("Approved count-correction reason code", { exact: true })
    .fill("SOURCE_COUNT");
  await page
    .getByLabel("This is the complete fresh export for the whole store after every till paused.", {
      exact: true,
    })
    .check();
  await page
    .getByLabel(
      "I physically counted the whole store, including empty locations, against this exact source.",
      { exact: true },
    )
    .check();
  await page
    .getByLabel(
      "Goods absent from this complete source are physically absent and must be counted as zero.",
      { exact: true },
    )
    .check();
  await widthCheckpoint(page);
  const created = responseFor(page, COUNTS);
  await page
    .getByRole("button", {
      name: "Freeze this store and prepare the reviewed difference",
      exact: true,
    })
    .click();
  expect((await created).status()).toBe(201);
  const id = (await sourceRead(page, source)).data.stock_reconciliation_id;
  expect(id).toBeTruthy();
  return id!;
}
async function cancel(page: Page, id: string) {
  await page
    .getByLabel("Why this count is cancelled", { exact: true })
    .fill("Fictional rejected source retained for proof; no stock decision.");
  const response = responseFor(page, `${COUNTS}/${id}/cancel`);
  await page
    .getByRole("button", { name: "Cancel count and retain its evidence", exact: true })
    .click();
  expect((await response).status()).toBe(200);
  expect((await countRead(page, id)).state).toBe("cancelled");
}

async function loggedOutReceiptDelivery(browser: Browser, grn: string) {
  const before = snapshot("RCV populated detail before normal session invalidation");
  const context = await browser.newContext({
    baseURL: BASE,
    viewport: { width: 375, height: 1000 },
  });
  const expired = await context.newPage();
  let release = () => {};
  try {
    await login(expired, "manager");
    await expired.goto(`/goods/receive/grn/${grn}?step=grn`);
    await expect(expired.getByTestId("gg-comparison")).toContainText("Fictional receiving shirt");
    let reached = () => {};
    const started = new Promise<void>((resolve) => {
      reached = resolve;
    });
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const path = `/api/goods-v1/inbound/grns/${grn}`;
    await expired.route(
      (url) => url.pathname === path,
      async (route) => {
        reached();
        await gate;
        await route.continue(); // The real handler sees this session after logout.
      },
    );
    const refused = expired.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === path && response.request().method() === "GET",
    );
    await expired.getByTestId("delivery-step-discrepancies").click();
    await started;
    const logout = await post(expired, "/auth/logout", {});
    expect(logout.status()).toBe(200);
    release();
    const response = await refused;
    expect(response.status()).toBe(401);
    const bytes = await response.text();
    for (const protectedValue of [
      "Fictional receiving shirt",
      "FICTIONAL-RCV-01",
      fixture().sku_id,
      grn,
    ])
      expect(bytes).not.toContain(protectedValue);
    for (const id of [
      "gg-detail",
      "gg-comparison",
      "gg-lines",
      "gg-held",
      "gg-damage-reports",
      "gg-shortage-submit",
    ])
      await expect(expired.getByTestId(id)).toHaveCount(0);
    const repeat = await expired.request.get(path);
    expect(repeat.status()).toBe(401);
    expect(await repeat.text()).not.toContain(fixture().barcode);
    unchangedStock(
      before,
      snapshot("RCV delayed GRN401 has no protected bytes or business mutation"),
    );
  } finally {
    release();
    await context.close();
  }
}

test.describe.serial("isolated receiving and inventory correction browser proof", () => {
  test("SOH-05–07: stale and gain snapshots refuse; exact reviewed shortage and zero append once", async ({
    page,
    browser,
  }) => {
    test.setTimeout(600_000);
    helper("prepare");
    const before = snapshot("SOH baseline after intervening first sale");
    expect(before.sellable_qty).toBe(2);
    await login(page, "manager");
    await pair(page);
    await page.getByTestId("till-sync-now").click();
    if (!(await page.getByTestId("till-pause-state").count())) {
      await page.getByTestId("till-pause-reason").fill("Fictional complete SOH browser count");
      await page.getByTestId("till-pause-go").click();
    }
    await expect(page.getByTestId("till-pause-state")).toHaveAttribute("data-stage", "paused");
    await reviewedSource(page, "later-negative-quantity", -1);
    unchangedStock(before, snapshot("SOH negative source remains inactive"));
    const staleSource = await reviewedSource(page, "later-stale", 2);
    const stale = await freeze(page, "later-stale", staleSource);
    unchangedStock(before, snapshot("SOH frozen without stock posting"));
    await page.getByRole("button", { name: "physical", exact: true }).click();
    await page
      .getByTestId("soh-import")
      .locator('input[type="file"]')
      .setInputFiles(`${helper<Artifact>("artifact", "stale-observation", "2").physical_file}`);
    const changed = responseFor(page, `/goods-v1/ptmapper/soh-imports/${staleSource}/verify`);
    await page.getByRole("button", { name: "Record physical verification", exact: true }).click();
    expect((await changed).status()).toBe(200);
    await page.getByRole("button", { name: "opening", exact: true }).click();
    const refused = responseFor(page, `${COUNTS}/${stale}/submit`);
    await page
      .getByRole("button", {
        name: "Submit the exact frozen count for independent Owner review",
        exact: true,
      })
      .click();
    const refusal = await refused;
    expect(refusal.status()).toBe(409);
    expect((await refusal.json()).code).toBe("REVISION_SUPERSEDED");
    unchangedStock(before, snapshot("SOH stale submission refused without mutation"));
    await cancel(page, stale);

    const gainSource = await reviewedSource(page, "later-gain", 3);
    await login(page, "manager");
    const source = await sourceRead(page, gainSource);
    const partial = await post(page, COUNTS, {
      source_import_id: gainSource,
      full_store_export: false,
      whole_store_physically_counted: true,
      omissions_are_zero: true,
      reason_code: "SOURCE_COUNT",
      ...meta(source.revision),
    });
    expect(partial.status()).toBe(400);
    expect((await partial.json()).code).toBe("INVALID_REQUEST");
    expect(snapshot("SOH partial declaration denied").freeze_id).toBeNull();
    const gain = await freeze(page, "later-gain", gainSource);
    await expect(
      page.getByRole("button", {
        name: "Submit the exact frozen count for independent Owner review",
        exact: true,
      }),
    ).toHaveCount(0);
    const frozenGain = await countRead(page, gain);
    const gainResponse = await post(page, `${COUNTS}/${gain}/submit`, {
      reviewed_hash: frozenGain.content_hash,
      ...meta(frozenGain.revision),
    });
    expect(gainResponse.status()).toBe(422);
    expect((await gainResponse.json()).code).toBe("SOH_VARIANCE_PENDING");
    unchangedStock(before, snapshot("SOH gain remains owned and inactive"));
    await cancel(page, gain);

    for (const [name, quantity] of [
      ["later-shortage", 1],
      ["later-explicit-zero", 0],
    ] as const) {
      if (name === "later-explicit-zero") {
        const omittedSource = await reviewedSource(
          page,
          "later-omission-preview",
          0,
          "FICTIONAL-ZERO-CATALOGUE",
        );
        const prior = snapshot("SOH omission before freeze");
        const omitted = await freeze(page, "later-omission-preview", omittedSource);
        await login(page, "owner");
        const review = await countRead(page, omitted);
        expect(review.data.review!.differences[0]).toMatchObject({
          book_qty: 1,
          observed_qty: 0,
          delta: -1,
          source_row_keys: [],
          value_removed_paise: "50000",
        });
        unchangedStock(prior, snapshot("SOH full-store omitted SKU previews zero without posting"));
        await login(page, "manager");
        await sourcePage(page, "later-omission-preview");
        await page.getByRole("button", { name: "opening", exact: true }).click();
        await cancel(page, omitted);
        unchangedStock(prior, snapshot("SOH omitted source cancel preserves the original book"));
      }
      const sourceId = await reviewedSource(page, name, quantity);
      const prior = snapshot(`${name} before freeze`);
      const count = await freeze(page, name, sourceId);
      const submitted = responseFor(page, `${COUNTS}/${count}/submit`);
      await page
        .getByRole("button", {
          name: "Submit the exact frozen count for independent Owner review",
          exact: true,
        })
        .click();
      expect((await submitted).status()).toBe(200);
      const exact = await countRead(page, count);
      const deny = await post(
        page,
        `/goods-v1/approvals/${exact.data.approval_request_id}/decide`,
        { decision: "approve", reviewed_hash: exact.content_hash, ...meta(exact.revision) },
      );
      expect(deny.status()).toBe(403);
      expect((await deny.json()).code).toBe("ACTION_DENIED");
      unchangedStock(prior, snapshot(`${name} maker denial`));
      const context = await browser.newContext({ baseURL: BASE });
      try {
        const reviewer = await context.newPage();
        await login(reviewer, "owner");
        await sourcePage(reviewer, name);
        await reviewer.getByRole("button", { name: "opening", exact: true }).click();
        const review = await countRead(reviewer, count);
        expect(review.data.review!.differences[0]).toMatchObject({
          book_qty: prior.sellable_qty,
          observed_qty: quantity,
          delta: quantity - prior.sellable_qty,
          value_removed_paise: "50000",
        });
        await widthCheckpoint(reviewer, ["SOH inventory differences"]);
        await reviewer
          .getByLabel(
            "I independently reviewed this full source, physical declarations, all differences and original valuation.",
            { exact: true },
          )
          .check();
        const decisionPath = `/goods-v1/approvals/${exact.data.approval_request_id}/decide`;
        let recoveredBody: unknown;
        const lostResponse = name === "later-shortage";
        if (lostResponse) {
          await reviewer.route(`**/api${decisionPath}`, async (route) => {
            const reply = await route.fetch();
            if (reply.status() !== 200) {
              decisionEvidence(
                "SOH exact difference protected response",
                reply.status(),
                await reply.json(),
              );
              await route.fulfill({ response: reply });
              return;
            }
            recoveredBody = route.request().postDataJSON();
            await route.abort("failed"); // The actual stock writer committed; only its response was lost.
          });
        }
        const approved = lostResponse ? undefined : responseFor(reviewer, decisionPath, 200);
        await reviewer
          .getByRole("button", { name: "Approve this exact inventory difference", exact: true })
          .click();
        await expect(reviewer.getByTestId("org-stepup-password")).toBeVisible();
        await expect(reviewer.getByTestId("org-stepup-password")).toBeFocused();
        await reviewer.keyboard.press("Escape");
        await expect(reviewer.getByTestId("org-stepup-password")).toHaveCount(0);
        expect((await countRead(reviewer, count)).state).toBe("submitted");
        unchangedStock(prior, snapshot(`${name} password cancellation retains frozen stock`));
        const retryApproval = reviewer.getByRole("button", {
          name: "Approve this exact inventory difference",
          exact: true,
        });
        await expect(retryApproval).toBeEnabled();
        await expect(retryApproval).toBeFocused();
        await retryApproval.press("Enter");
        await stepUp(reviewer, "owner");
        let body: unknown;
        if (lostResponse) {
          await expect.poll(async () => (await countRead(reviewer, count)).state).toBe("closed");
          expect(recoveredBody).toBeTruthy();
          body = recoveredBody;
          await reviewer.unroute(`**/api${decisionPath}`);
          await sourcePage(reviewer, name);
          await reviewer.getByRole("button", { name: "opening", exact: true }).click();
          await expect(reviewer.getByTestId("soh-import")).toContainText("closed");
        } else {
          const approval = await approved!;
          expect(approval.status()).toBe(200);
          body = approval.request().postDataJSON();
        }
        const result = snapshot(`${name} closed once`);
        expect(result.sellable_qty).toBe(quantity);
        expect(result.stock_cost_paise).toBe(String(quantity * 50_000));
        expect(result.origins).toEqual(before.origins);
        expect(result.sale_hash).toBe(before.sale_hash);
        expect(result.cash_hash).toBe(before.cash_hash);
        expect(result.journals.filter((row) => row.posting_kind === "P13").length).toBe(
          prior.journals.filter((row) => row.posting_kind === "P13").length + 1,
        );
        const replay = await post(reviewer, decisionPath, body);
        expect(replay.status()).toBe(200);
        unchangedStock(result, snapshot(`${name} replay unchanged`));
      } finally {
        await context.close();
      }
    }
    await login(page, "manager");
    await page.goto("/sell/till");
    await page.getByTestId("till-pause-resume").click();
    await expect(page.getByTestId("till-pause-state")).toHaveCount(0);
  });

  test("RCV-01: truthful claimed4/count3, damaged custody, independent shortage/PT review and physical acceptance", async ({
    page,
    browser,
  }) => {
    test.setTimeout(300_000);
    helper("supplier-identity");
    const inputs = fixture();
    const before = snapshot("RCV baseline after SOH");
    await login(page, "manager");
    await page.goto(`/goods/receive/new?site=${inputs.site_id}`);
    await page.getByTestId("ga-site").selectOption(inputs.site_id);
    await page.getByTestId("ga-vendor").selectOption(inputs.vendor_id);
    await page.getByTestId("ga-brand").selectOption(inputs.brand_id);
    await page.getByTestId("ga-transporter").fill("Fictional shipment RCV-01");
    await page.getByTestId("ga-invoice").fill("FICTIONAL-RCV-01");
    await page.getByTestId("ga-invoice-date").fill(new Date().toISOString().slice(0, 10));
    await widthCheckpoint(page);
    const created = responseFor(page, "/goods-v1/inbound/arrivals");
    let arrivalRequests = 0;
    page.on("request", (sent) => {
      if (
        sent.method() === "POST" &&
        new URL(sent.url()).pathname === "/api/goods-v1/inbound/arrivals"
      )
        arrivalRequests += 1;
    });
    await page.getByTestId("ga-save").click();
    // Twice in eleven runs the click after the four-width resize sent nothing at all.
    // Press again only when no request left the page, so a second record is never made.
    await expect
      .poll(() => arrivalRequests, { timeout: 5_000 })
      .toBeGreaterThan(0)
      .catch(async () => {
        console.log("RCV arrival save: no request after the first click; pressing once more");
        await page.getByTestId("ga-save").click();
      });
    const arrivalResponse = await created;
    expect(arrivalResponse.status()).toBe(201);
    const arrival = (await arrivalResponse.json()).id as string;
    await page.getByTestId("ga-inv-desc-0").fill("Fictional receiving shirt");
    await page.getByTestId("ga-inv-qty-0").fill("4");
    await widthCheckpoint(page);
    const claimed = responseFor(page, `/goods-v1/inbound/arrivals/${arrival}/invoice`);
    await page.getByTestId("ga-inv-submit").click();
    expect((await claimed).status()).toBe(200);
    await page.getByTestId("delivery-step-count").click();
    await page.getByTestId("ga-count-open").click();
    for (const [index, quantity, condition] of [
      [0, 2, "good"],
      [1, 1, "damaged"],
    ] as const) {
      if (index) await page.getByTestId("ga-row-add").click();
      await page.getByTestId(`ga-alias-${index}`).fill(inputs.barcode);
      await page.getByTestId(`ga-alias-${index}`).press("Tab");
      await expect(page.getByTestId(`ga-identity-${index}-resolved`)).toBeVisible();
      await page.getByTestId(`ga-desc-${index}`).fill("Fictional receiving shirt");
      await page.getByTestId(`ga-qty-${index}`).fill(String(quantity));
      await page.getByTestId(`ga-condition-${index}-${condition}`).check();
    }
    await widthCheckpoint(page);
    await page.getByTestId("ga-record").click();
    await expect(page.getByTestId("ga-recorded").getByRole("row")).toHaveCount(3);
    await page.getByTestId("ga-issue").click();
    await expect(page.getByTestId("ga-remarks")).toBeVisible();
    for (const input of await page.getByTestId(/^ga-remark-/).all())
      await input.fill("Fictional delivery is one short and one counted piece is damaged.");
    const issued = responseFor(page, "/goods-v1/inbound/grns");
    await page.getByTestId("ga-issue").click();
    const grnResponse = await issued;
    expect(grnResponse.status()).toBe(201);
    const grn = (await grnResponse.json()).id as string;
    await page.goto(`/goods/receive/grn/${grn}?step=grn`);
    await widthCheckpoint(page, [
      "Receipt invoice comparison",
      "Receipt counted coverage",
      "Receipt damage reports",
    ]);
    const afterGrn = snapshot("RCV quantity GRN and unvalued immediate damage hold");
    expect(afterGrn.origins).toEqual(before.origins);
    expect(afterGrn.value_legs).toEqual(before.value_legs);
    expect(afterGrn.sellable_qty).toBe(0);
    expect(
      afterGrn.positions.filter((row) => !row.origin_id).reduce((n, row) => n + row.qty, 0),
    ).toBe(3);
    expect(afterGrn.damage_reports).toHaveLength(before.damage_reports.length + 1);
    await page.goto(`/goods/receive/grn/${grn}?step=discrepancies`);
    await expect(page.getByTestId("gg-shortage")).toHaveCount(0);
    const read = await page.request.get(`/api/goods-v1/inbound/grns/${grn}`);
    const grnDto = await read.json();
    const claimKey = grnDto.data.invoice_comparison[0].claim_line_key;
    const denied = await post(page, `/goods-v1/inbound/grns/${grn}/dispositions`, {
      kind: "accept_shortage",
      source_document_id: grn,
      source_line_key: claimKey,
      qty: 1,
      reason_code: "SHORT",
      reviewed_grn_hash: grnDto.content_hash,
      ...meta(grnDto.revision),
    });
    expect(denied.status()).toBe(403);
    expect((await denied.json()).code).toBe("ACTION_DENIED");
    unchangedStock(afterGrn, snapshot("RCV manager shortage decision denied"));
    await loggedOutReceiptDelivery(browser, grn);
    await login(page, "owner");
    await page.goto(`/goods/receive/grn/${grn}?step=discrepancies`);
    await expect(page.getByTestId("gg-shortage-comparison")).toContainText(
      "Invoice claimed 4; physically counted 3",
    );
    await widthCheckpoint(page);
    await page.getByTestId("gg-shortage-submit").click();
    await expect(page.getByTestId("gg-shortage").getByRole("alert")).toBeVisible();
    await page.getByTestId("gg-shortage-reason").fill("SHORT");
    const shortageSent = responseFor(page, `/goods-v1/inbound/grns/${grn}/dispositions`);
    await page.getByTestId("gg-shortage-submit").click();
    const shortage = await shortageSent;
    expect(shortage.status()).toBe(200);
    expect((await shortage.json()).state).toBe("approval_pending");
    await expect(page.getByTestId(/^gg-approval-self-/)).toHaveCount(1);
    await widthCheckpoint(page, ["Receipt pending decisions"]);
    const requested = snapshot("RCV shortage requested no stock effect");
    unchangedStock(afterGrn, requested);
    const selfTestId = await page.getByTestId(/^gg-approval-self-/).getAttribute("data-testid");
    const approvalId = selfTestId!.replace("gg-approval-self-", "");
    const approvals = await page.request.get("/api/goods-v1/approvals");
    const ownApproval = (await approvals.json()).items.find(
      (row: { id: string }) => row.id === approvalId,
    );
    expect(ownApproval).toBeTruthy();
    expect(
      (await post(page, "/auth/step-up", { password: record().owner.password })).status(),
    ).toBe(200);
    const selfDenied = await post(page, `/goods-v1/approvals/${approvalId}/decide`, {
      decision: "approve",
      reviewed_hash: ownApproval.reviewed_hash,
      ...meta(ownApproval.subject_revision),
    });
    expect(selfDenied.status()).toBe(403);
    expect((await selfDenied.json()).code).toBe("SELF_APPROVAL");
    unchangedStock(requested, snapshot("RCV shortage maker cannot check own decision"));
    const context = await browser.newContext({ baseURL: BASE });
    try {
      const checker = await context.newPage();
      await login(checker, "count_checker");
      await checker.goto(`/goods/receive/grn/${grn}?step=discrepancies`);
      await widthCheckpoint(checker, ["Receipt pending decisions"]);
      await checker.getByTestId(/^gg-approve-/).click();
      await stepUp(checker, "count_checker");
      await expect(checker.getByTestId("gg-approvals")).toContainText(
        "Nothing on this receipt is waiting",
      );
      await checker.reload();
      await expect(checker.getByTestId("gg-shortage")).toContainText(
        "No invoice shortage is waiting for a new decision.",
      );
      await expect(checker.getByTestId("gg-shortage-submit")).toHaveCount(0);
      unchangedStock(
        requested,
        snapshot("RCV independent shortage approved no stock value created"),
      );
      const receipt = await checker.request.get(`/api/goods-v1/inbound/grns/${grn}`);
      expect((await receipt.json()).data.invoice_comparison[0]).toMatchObject({
        claimed_qty: 4,
        counted_qty: 3,
        difference: -1,
        remaining_shortage_qty: 0,
      });
      const shortageReplay = await post(
        page,
        `/goods-v1/inbound/grns/${grn}/dispositions`,
        shortage.request().postDataJSON(),
      );
      expect(shortageReplay.status()).toBe(200);
      unchangedStock(
        requested,
        snapshot("RCV shortage request replay creates no second decision or stock"),
      );
      const retained = await checker.request.get(`/api/goods-v1/inbound/grns/${grn}`);
      expect(
        (await retained.json()).data.dispositions.filter(
          (row: { kind: string; state: string }) =>
            row.kind === "accept_shortage" && row.state === "approved",
        ),
      ).toHaveLength(1);
    } finally {
      await context.close();
    }
    await login(page, "warehouse");
    await page.goto(`/goods/receive/grn/${grn}?step=pt_prepare`);
    await expect(page.getByTestId("pt-grn-uncovered")).toContainText("2 piece(s)");
    await page.getByTestId("pt-start-profile").selectOption(plan().profile_version_id);
    await page.getByTestId("pt-start-direction").selectOption("both_supplied");
    const drafted = responseFor(page, `/goods-v1/ptmapper/files/from-grn/${grn}`);
    await page.getByTestId("pt-start-prefill").click();
    const draftResponse = await drafted;
    expect(draftResponse.status()).toBe(201);
    const pt = (await draftResponse.json()).id as string;
    await expect(page.getByTestId("pt-editor")).toBeVisible();
    // Cell editing is a desktop spreadsheet task; the four-width checkpoints still
    // visit this screen. A dropdown closes on any scroll, and a phone scrolls the grid.
    await page.setViewportSize({ width: 1440, height: 1000 });
    // The cell is a closed control until it is clicked; only then does it hold an input.
    const control = page.getByTestId(/^pt-season-[0-9a-f-]{36}$/);
    const season = page.getByTestId(/^pt-season-[0-9a-f-]{36}-input$/);
    await expect(control).toBeVisible();
    // Open, type and commit as one retried unit: the success test is the committed
    // value in the cell, not the intermediate state of the dropdown.
    await expect(async () => {
      if ((await season.count()) === 0) await control.click();
      await season.fill("Fictional Alpha proof season", { timeout: 3_000 });
      await season.press("ArrowDown", { timeout: 3_000 });
      await season.press("Enter", { timeout: 3_000 });
      await expect(control).toContainText("Fictional Alpha proof season", { timeout: 3_000 });
    }).toPass({ timeout: 40_000 });
    await page.getByRole("textbox", { name: "HSN, row 1", exact: true }).fill("6101");
    await page.getByRole("textbox", { name: "MRP, row 1", exact: true }).fill("1000");
    await page.getByRole("textbox", { name: "BASIC, row 1", exact: true }).fill("500");
    await widthCheckpoint(page, ["PT rows"]);
    await page.getByTestId("pt-save").click();
    await page
      .getByTestId(/^pt-review-/)
      .filter({ hasText: "Review" })
      .first()
      .click();
    await page.getByTestId("pt-save").click();
    const sent = responseFor(page, `/goods-v1/ptmapper/files/${pt}/send`);
    await page.getByTestId("pt-submit").click();
    expect((await sent).status()).toBe(200);
    await login(page, "owner");
    await page.goto(`/goods/receive/pt/${pt}?step=pt_approve`);
    await expect(page.getByTestId("pt-approval-qty")).toContainText("2");
    await widthCheckpoint(page);
    await page.getByTestId("pt-approve").click();
    await stepUp(page, "owner");
    await expect(page.getByTestId("pt-view-held-stock")).toBeVisible();
    await page.reload();
    const history = await page.request.get("/api/goods-v1/approvals?limit=100");
    expect(history.status()).toBe(200);
    const approvedPt = (await history.json()).items.filter(
      (row: { id: string; state: string; subject_id: string; parent_document?: { id: string } }) =>
        row.state === "approved" && (row.parent_document?.id ?? row.subject_id) === pt,
    );
    expect(approvedPt).toHaveLength(1);
    // The decision shows on the queue row; the panel below lists attempts and refusals.
    await expect(page.getByTestId(`pt-queue-decided-${approvedPt[0].id}`)).toContainText(
      "approved",
    );
    await page.getByTestId(`pt-queue-decided-${approvedPt[0].id}`).click();
    await expect(page.getByTestId("pt-approval-history")).toContainText("submitted");
    await expect(page.getByTestId("pt-revision-superseded")).toHaveCount(0);
    await expect(page.getByTestId("pt-approve")).toHaveCount(0);
    expect(
      snapshot("RCV approved PT remains unsellable until actual acceptance").sellable_qty,
    ).toBe(0);
    await login(page, "manager");
    await page.goto(`/goods/receive/pt/${pt}?step=accept`);
    await page.getByTestId(`accept-open-${pt}`).click();
    await page.getByTestId("accept-alias").fill(inputs.barcode);
    await page.getByTestId("accept-mrp").fill("1000");
    await page.getByTestId("accept-putaway-toggle").check();
    await page.getByTestId("accept-location").selectOption(inputs.floor_id);
    await page.getByTestId("accept-qty").fill("2");
    await widthCheckpoint(page);
    await page.getByTestId("accept-submit-scan").click();
    // Until the scan is recorded the page still has pieces remaining and "Complete"
    // only asks for confirmation; wait for the recorded scan before completing.
    await expect(page.getByText("2 recorded")).toBeVisible();
    const complete = page.waitForResponse(
      (r) => new URL(r.url()).pathname.endsWith("/complete") && r.request().method() === "POST",
    );
    await page.getByTestId("accept-complete").click();
    const completion = await complete;
    expect(completion.status()).toBe(200);
    await expect(page.getByTestId("accept-completed")).toBeVisible();
    const accepted = snapshot(
      "RCV two actual good pieces accepted; damaged one retains unknown cost",
    );
    expect(accepted.sellable_qty).toBe(2);
    expect(accepted.stock_cost_paise).toBe("100000");
    expect(accepted.origins).toHaveLength(before.origins.length + 1);
    expect(accepted.sale_hash).toBe(before.sale_hash);
    expect(accepted.cash_hash).toBe(before.cash_hash);
    expect(
      accepted.positions
        .filter((row) => row.condition === "damaged" && !row.origin_id)
        .reduce((n, row) => n + row.qty, 0),
    ).toBe(1);
    const replay = await post(
      page,
      new URL(completion.url()).pathname.slice(4),
      completion.request().postDataJSON(),
    );
    expect(replay.status()).toBe(200);
    unchangedStock(accepted, snapshot("RCV acceptance replay unchanged"));
  });

  test("DMG-01: physical report quarantines once, scoped denial and independent confirmation retain original cost", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const inputs = fixture();
    const before = snapshot("DMG baseline");
    expect(before.sellable_qty).toBe(2);
    await login(page, "manager");
    await page.goto(`/goods/stock?site_id=${inputs.site_id}`);
    const row = page
      .getByRole("row")
      .filter({ has: page.getByTestId("stock-mark-damaged") })
      .first();
    await row.getByTestId("stock-mark-damaged").click();
    const damageForm = page.getByTestId("stock-damage-form");
    await expect(damageForm).toBeVisible();
    await damageForm.getByTestId("stock-damage-qty").fill("1");
    await damageForm.getByTestId("stock-damage-reason").fill("FICTIONAL_TORN");
    await widthCheckpoint(page);
    const marked = responseFor(page, "/goods-v1/outbound/mark-damaged");
    await damageForm.getByTestId("stock-damage-confirm").click();
    const marking = await marked;
    expect(marking.status()).toBe(201);
    const held = snapshot("DMG one accepted piece quarantined immediately");
    expect(held.sellable_qty).toBe(1);
    expect(held.stock_cost_paise).toBe(before.stock_cost_paise);
    expect(held.origins).toEqual(before.origins);
    expect(held.sale_hash).toBe(before.sale_hash);
    expect(held.cash_hash).toBe(before.cash_hash);
    const report = held.damage_reports.find(
      (report) => !before.damage_reports.some((old) => old.id === report.id),
    )!;
    expect(report.state).toBe("pending");
    const replay = await post(
      page,
      "/goods-v1/outbound/mark-damaged",
      marking.request().postDataJSON(),
    );
    expect(replay.status()).toBe(201);
    unchangedStock(held, snapshot("DMG report replay unchanged"));
    const denied = await post(page, `/goods-v1/outbound/damage-reports/${report.id}/decide`, {
      decision: "reject",
      reason: "Fictional unauthorized release attempt",
      ...meta(),
    });
    expect(denied.status()).toBe(403);
    expect((await denied.json()).code).toBe("ACTION_DENIED");
    unchangedStock(held, snapshot("DMG unauthorized release denied"));
    // The normal online stock writer refuses quantity two against availability
    // one, retaining the rejected submission and creating no bill or money.
    await pair(page);
    await page.getByTestId("till-sync-now").click();
    await page.goto("/sell");
    await page.getByTestId("bill-sold-by").selectOption({ index: 1 });
    await page.getByTestId("bill-scan").fill(inputs.barcode);
    await page.getByTestId("bill-scan").press("Enter");
    await expect(page.getByTestId("bill-line-1")).toBeVisible();
    await page.getByTestId("bill-scan").fill(inputs.barcode);
    await page.getByTestId("bill-scan").press("Enter");
    await expect(page.getByTestId("bill-qty-1")).toHaveValue("2");
    await page.getByTestId("bill-all-cash").click();
    const saleDenied = responseFor(page, "/sell/sales/finalise-online");
    await page.getByTestId("bill-save").click();
    const saleRefusal = await saleDenied;
    expect(saleRefusal.status()).toBe(422);
    expect((await saleRefusal.json()).code).toBe("INSUFFICIENT_ELIGIBLE_STOCK");
    await expect(page.getByTestId("online-sale-pending")).toContainText("Bill not issued");
    unchangedStock(held, snapshot("DMG sale cannot take quarantined quantity"));
    await login(page, "owner");
    await page.goto(`/goods/movements?site_id=${inputs.site_id}`);
    const damage = page
      .getByTestId("dmg-row")
      .filter({ has: page.locator(`[id="dmg-reason-${report.id}"]`) });
    await damage
      .getByTestId("dmg-reason")
      .fill("Fictional independent physical damage confirmation");
    await widthCheckpoint(page);
    const confirmed = responseFor(
      page,
      `/goods-v1/outbound/damage-reports/${report.id}/decide`,
      200,
    );
    await damage.getByTestId("dmg-confirm").click();
    await stepUp(page, "owner");
    const confirmation = await confirmed;
    expect(confirmation.status()).toBe(200);
    const after = snapshot("DMG independently confirmed without value or release");
    unchangedStock(held, after);
    expect(after.damage_reports.find((row) => row.id === report.id)).toMatchObject({
      state: "confirmed",
      quantity: 1,
    });
    const replayReview = await post(
      page,
      `/goods-v1/outbound/damage-reports/${report.id}/decide`,
      confirmation.request().postDataJSON(),
    );
    expect(replayReview.status()).toBe(200);
    unchangedStock(after, snapshot("DMG confirmation replay unchanged"));
    for (const width of [1440, 1366, 768, 375]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`/goods/stock?site_id=${inputs.site_id}&view=quarantine`);
      await noOverflow(page);
    }
  });
});
