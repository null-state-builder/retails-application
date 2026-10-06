import { writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { loginProof, pairProof } from "./firstStoreProof";
import {
  ROOT,
  guardedClick,
  fillGeneratedSecret,
  loginBootstrap,
  proofRecord,
  responseFor,
  runHelper,
  saveBootstrap,
  sourceRead,
} from "./firstStoreBootstrap";

const WIDTHS = [1440, 1366, 768, 375] as const;
const SOURCE_NAME = "fictional-opening-soh.xlsx";
const ARTIFACTS = `${ROOT}/.local/first-store-opening-browser`;

interface Plan {
  proof_only: boolean;
  site_id: string;
  cutoff_at: string;
  identity_profile_id: string;
  profile_version_id: string;
  brand_mappings: Record<string, string>;
  season_mappings: Record<string, string>;
  size_mappings: Record<string, string>;
}

async function assertNoPageOverflow(page: Page) {
  const widths = await page.evaluate(() => ({
    visible: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(widths.content).toBeLessThanOrEqual(widths.visible + 1);
}

async function openSource(page: Page, sourceId: string) {
  await page.goto("/goods/opening");
  await page.getByRole("button", { name: SOURCE_NAME, exact: true }).click();
  await expect(page.getByTestId("soh-import")).toContainText("Source SHA-256");
  expect((await sourceRead(page, sourceId)).id).toBe(sourceId);
}

async function readiness(page: Page, site: string) {
  const response = await page.request.get(`/api/goods-v1/masters/stores/${site}/readiness`);
  expect(response.status()).toBe(200);
  return (await response.json()) as {
    revision: number;
    data: { opening_setup_ready: boolean; goods_ready: boolean; sell_ready: boolean };
  };
}

async function readinessPage(page: Page, site: string) {
  await page.goto(`/setup/organisation?panel=sites&site=${site}&tab=readiness`);
  await expect(page.getByTestId("site-readiness-tab")).toBeVisible();
  await page.getByLabel("Readiness decision reference").fill("SYNTHETIC_BROWSER_PROOF");
}

// This is one resumable journey on the exact owned blank-installation database.
// It preserves committed results after interruption and refuses another record.
// It does not claim all SET/SOH denial variants or real-store activation.
test("SET-01/02 and SOH-01–04: joint signup, reviewed opening, physical acceptance and first synthetic bill", async ({
  page,
  browser,
}) => {
  test.setTimeout(600_000);
  const record = proofRecord();
  async function step(name: string, work: () => Promise<void>) {
    console.info(`[proof-stage] ${name}`);
    await test.step(name, work);
  }
  const apiErrors: string[] = [];
  page.on("pageerror", (e) => apiErrors.push(e.message));
  await step("joint fictional company signup through the actual form", async () => {
    await page.goto("/signup");
    const state = await page.request.get("/api/auth/registration");
    expect(state.status()).toBe(200);
    const installation = (await state.json()) as {
      available: boolean;
      pending_confirmation: boolean;
    };
    if (installation.available && !installation.pending_confirmation) {
      await expect(page.getByTestId("signup-form")).toBeVisible();
      await page.getByTestId("signup-submit").click();
      await expect(page.getByRole("alert")).toBeVisible();
      await expect(page.getByTestId("signup-name")).toBeFocused();
      await page.getByTestId("signup-name").fill("Alpha proof company");
      await page.getByTestId("signup-state").selectOption("20");
      await page.getByTestId("signup-legal_name").fill("Alpha Proof Private Limited");
      await page.getByTestId("signup-pan").fill("ABCDE1234F");
      await page.getByTestId("signup-gstin").fill("20ABCDE1234F1Z5");
      await page.getByTestId("signup-billing_address").fill("Fictional proof-only company address");
      await page.getByLabel("Use my own company code", { exact: true }).check();
      await page.getByTestId("signup-code").fill("ALPHA");
      await page.getByTestId("signup-store-name").fill("First proof shop");
      await page.getByTestId("signup-store-city-choice").selectOption("__custom");
      await page.getByTestId("signup-store-city").fill("Ranchi");
      await page.getByTestId("signup-store-address").fill("Fictional proof-only shop address");
      await page.locator("#signup-store-kind-existing").check();
      await page.getByTestId("signup-source-system").fill("Synthetic earlier software");
      await page.getByLabel("Use my own store code", { exact: true }).check();
      await page.getByTestId("signup-store-code").fill("FIRST");
      for (const who of ["owner", "admin"] as const) {
        await page.getByTestId(`signup-${who}-name`).fill(`Fictional ${who}`);
        await page.getByTestId(`signup-${who}-email`).fill(record[who].email);
        await fillGeneratedSecret(
          page.getByTestId(`signup-${who}-password`),
          record[who].temporary_password!,
        );
      }
      await page.getByRole("button", { name: "Add team member", exact: true }).click();
      await page.getByTestId("signup-team-0-name").fill("Fictional manager");
      await page
        .getByTestId("signup-team-0-email")
        .fill(record.browser_bootstrap.proposed_manager_email);
      await page.getByTestId("signup-team-0-role").selectOption("store_person");
      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 1000 });
        await assertNoPageOverflow(page);
      }
      await page.getByTestId("signup-submit").click();
      await expect(page.getByTestId("signup-summary")).toContainText("Alpha Proof Private Limited");
      const staged = responseFor(page, "/auth/registration");
      await page
        .getByRole("button", { name: "Save and continue to confirmation", exact: true })
        .click();
      expect((await staged).status()).toBe(202);
    }
    if (installation.available) {
      for (const who of ["owner", "admin"] as const) {
        await page.getByTestId("signup-confirm-email").fill(record[who].email);
        await fillGeneratedSecret(
          page.getByTestId("signup-confirm-password"),
          record[who].temporary_password!,
        );
        // The summary of the first person's confirmation is still on screen for the
        // second, so wait for this person's own review response before anything else.
        const reviewed = responseFor(page, "/auth/registration/confirm");
        await page.getByRole("button", { name: "Review my initial summary", exact: true }).click();
        expect((await reviewed).status()).toBe(200);
        await expect(page.getByTestId("signup-policy-baseline")).toBeVisible();
        await expect(page.getByTestId("signup-summary")).toContainText(record.owner.email);
        await expect(page.getByTestId("signup-summary")).toContainText(record.admin.email);
        for (const width of WIDTHS) {
          await page.setViewportSize({ width, height: 1000 });
          await assertNoPageOverflow(page);
        }
        await page.getByTestId("signup-acknowledge").check();
        const confirmed = responseFor(page, "/auth/registration/confirm");
        await page.getByTestId("signup-confirm").click();
        const result = await confirmed;
        expect(result.status()).toBe(200);
        expect((await result.json()).confirmed[who]).toBe(true);
        await expect(page.getByTestId("signup-confirm")).toHaveCount(0);
      }
    }
    await page.reload();
    await expect(page.getByTestId("signup-closed")).toBeVisible();
    expect((await (await page.request.get("/api/auth/registration")).json()).available).toBe(false);
  });

  await step("mandatory password replacement and separately reviewed setup/team APIs", async () => {
    if (!proofRecord().owner.password) {
      await page.goto("/login");
      await page.getByTestId("login-email").fill(record.owner.email);
      await fillGeneratedSecret(
        page.getByTestId("login-password"),
        record.owner.temporary_password!,
      );
      await page.getByTestId("login-submit").click();
      await expect(page).toHaveURL(/\/change-password$/);
      await page.goto("/setup/first-store");
      await expect(page).toHaveURL(/\/change-password$/);
    }
    if (!proofRecord().owner.password_changed || !proofRecord().admin.password_changed)
      runHelper("first-store-onboarding-proof.py", "password-change");
    runHelper("first-store-onboarding-proof.py", "calendar");
    runHelper("first-store-onboarding-proof.py", "activate-team");
    runHelper("first-store-onboarding-proof.py", "personal-pin");
    await loginBootstrap(page, "owner");
    await page.goto("/setup/people-access");
    await expect(page.getByTestId("people-table")).toContainText("Fictional manager");
    await loginBootstrap(page, "manager");
    await page.goto("/setup/people-access");
    await expect(page.getByTestId("access-denied")).toBeVisible();
    const denied = await page.request.get("/api/auth/admin/users");
    expect(denied.status()).toBe(403);
    expect(await denied.text()).not.toContain(record.admin.email);
    runHelper("first-store-opening-proof.py");
  });

  const { readFileSync } = await import("node:fs");
  const plan = JSON.parse(readFileSync(`${ARTIFACTS}/mapping-plan.json`, "utf8")) as Plan;
  expect(plan.proof_only).toBe(true);
  let sourceId = proofRecord().browser_bootstrap.source_id ?? "";
  await step("SOH upload remains staged with zero opening stock", async () => {
    await loginBootstrap(page, "manager");
    await page.goto("/goods/opening");
    if (!sourceId) {
      const sources = await page.request.get(
        `/api/goods-v1/ptmapper/soh-imports?site_id=${plan.site_id}`,
      );
      expect(sources.status()).toBe(200);
      const existing = (
        (await sources.json()).items as { id: string; source_name: string }[]
      ).filter((s) => s.source_name === SOURCE_NAME);
      expect(existing.length).toBeLessThanOrEqual(1);
      if (existing.length) sourceId = existing[0]!.id;
      else {
        await page
          .getByLabel("SOH source (.xlsx)", { exact: true })
          .setInputFiles(`${ARTIFACTS}/${SOURCE_NAME}`);
        const uploaded = responseFor(page, "/goods-v1/ptmapper/soh-imports/upload");
        await page.getByRole("button", { name: "Upload and inspect source", exact: true }).click();
        const response = await uploaded;
        expect(response.status()).toBe(201);
        sourceId = (await response.json()).id;
      }
      saveBootstrap({ source_id: sourceId });
    }
    await openSource(page, sourceId);
    await expect(page.getByTestId("soh-import")).toContainText("1 source rows · 3 units");
    const source = await sourceRead(page, sourceId);
    if (source.state === "uploaded") {
      expect(source.data.batches).toEqual([]);
      expect(source.data.approval_request_id).toBeNull();
      await loginBootstrap(page, "owner");
      expect((await readiness(page, plan.site_id)).data.goods_ready).toBe(false);
    }
  });

  await step(
    "SOH stable mappings, cutoff and valuation controls are reviewed through UI",
    async () => {
      await loginBootstrap(page, "admin");
      await openSource(page, sourceId);
      let source = await sourceRead(page, sourceId);
      if (source.allowed_actions.includes("claim")) {
        const claimed = responseFor(page, `/goods-v1/ptmapper/soh-imports/${sourceId}/claim`);
        await page
          .getByRole("button", { name: "Start valuation preparation", exact: true })
          .click();
        expect((await claimed).status()).toBe(200);
        source = await sourceRead(page, sourceId);
      }
      if (
        ["uploaded", "review"].includes(source.state) &&
        !Object.keys(source.data.configuration).length
      ) {
        await page.getByRole("button", { name: "mapping", exact: true }).click();
        for (const [label, targets] of [
          ["Fictional source brand", plan.brand_mappings],
          ["Fictional source season", plan.season_mappings],
          ["M", plan.size_mappings],
        ] as const) {
          const row = page
            .getByRole("row")
            .filter({ has: page.getByRole("cell", { name: label, exact: true }) });
          await row.locator("select").selectOption(targets[label]!);
        }
        await page
          .getByRole("row")
          .filter({ has: page.getByRole("cell", { name: "Shirt", exact: true }) })
          .getByPlaceholder("Confirmed HSN", { exact: true })
          .fill("6101");
        await page.getByRole("button", { name: "controls", exact: true }).click();
        await page
          .getByLabel("Actual source cutoff (date, time and timezone)", { exact: true })
          .fill(plan.cutoff_at);
        await page
          .getByLabel("Approved identity profile", { exact: true })
          .selectOption(plan.identity_profile_id);
        await page
          .getByLabel("Approved opening pricing profile", { exact: true })
          .selectOption(plan.profile_version_id);
        await page
          .getByLabel("Confirmed Rate meaning", { exact: true })
          .selectOption("basic_ex_tax");
        for (const [label, button] of [
          ["Valuation evidence", "Upload valuation evidence"],
          ["External reconciliation evidence", "Upload reconciliation evidence"],
        ]) {
          await page
            .getByLabel(label!, { exact: true })
            .setInputFiles(`${ARTIFACTS}/fictional-valuation-reconciliation.xlsx`);
          const uploaded = responseFor(page, "/goods-v1/files/uploads");
          await page.getByRole("button", { name: button!, exact: true }).click();
          expect((await uploaded).status()).toBe(201);
        }
        await page
          .getByLabel("Source quality and mapping review", { exact: true })
          .fill(
            "Fictional source tags, stable brand, size and season mappings independently prepared.",
          );
        await page
          .getByLabel("Explained quantity/value differences and external-books reconciliation", {
            exact: true,
          })
          .fill(
            "Synthetic scenario: 3 units, basic value 500 INR, source amount 1500 INR; zero discrepancy. No real books or stock asserted.",
          );
        await page
          .getByLabel("The source belongs to this store; the filename alone is insufficient.", {
            exact: true,
          })
          .check();
        await page
          .getByLabel(
            "The cutover source is fresh and all subsequent movements are accounted for.",
            {
              exact: true,
            },
          )
          .check();
        const prepared = responseFor(page, `/goods-v1/ptmapper/soh-imports/${sourceId}/prepare`);
        await page
          .getByRole("button", {
            name: "Save reviewed mappings and valuation controls",
            exact: true,
          })
          .click();
        expect((await prepared).status()).toBe(200);
      }
    },
  );

  await step(
    "physical observations and unauthorised maker denial precede independent source approval",
    async () => {
      await loginBootstrap(page, "manager");
      await openSource(page, sourceId);
      let source = await sourceRead(page, sourceId);
      if (source.state === "review" && source.data.verified_rows === 0) {
        await page.getByRole("button", { name: "physical", exact: true }).click();
        await page
          .getByTestId("soh-import")
          .locator('input[type="file"]')
          .setInputFiles(`${ARTIFACTS}/fictional-physical-count.csv`);
        const observed = responseFor(page, `/goods-v1/ptmapper/soh-imports/${sourceId}/verify`);
        await page
          .getByRole("button", { name: "Record physical verification", exact: true })
          .click();
        expect((await observed).status()).toBe(200);
        source = await sourceRead(page, sourceId);
      }
      if (source.state === "review") {
        await loginBootstrap(page, "admin");
        await openSource(page, sourceId);
        await page.getByRole("button", { name: "opening", exact: true }).click();
        const submitted = responseFor(page, `/goods-v1/ptmapper/soh-imports/${sourceId}/submit`);
        await page
          .getByRole("button", {
            name: "Submit exact source for independent Owner review",
            exact: true,
          })
          .click();
        expect((await submitted).status()).toBe(200);
        source = await sourceRead(page, sourceId);
      }
      if (source.state === "submitted") {
        const makerContext = await browser.newContext({ baseURL: "http://127.0.0.1:5178" });
        try {
          const maker = await makerContext.newPage();
          await loginBootstrap(maker, "admin");
          const cookie = (await makerContext.cookies()).find((c) => c.name === "kdps_csrf");
          const denied = await maker.request.post(
            `/api/goods-v1/approvals/${source.data.approval_request_id}/decide`,
            {
              headers: { "X-CSRF-Token": cookie!.value },
              data: {
                command_id: randomUUID(),
                contract_version: "goods-v1",
                decision: "approve",
                reviewed_hash: source.content_hash,
                expected_revision: source.revision,
              },
            },
          );
          expect(denied.status()).toBe(403);
          expect((await denied.json()).code).toBe("ACTION_DENIED");
          const after = await sourceRead(maker, sourceId);
          expect(after.state).toBe("submitted");
          expect(after.revision).toBe(source.revision);
          expect(after.data.batches).toEqual([]);
        } finally {
          await makerContext.close();
        }
        await loginBootstrap(page, "owner");
        await openSource(page, sourceId);
        await page.getByRole("button", { name: "opening", exact: true }).click();
        // The real command rechecks the independently authenticated checker.
        const cookie = (await page.context().cookies()).find((c) => c.name === "kdps_csrf");
        const stepUp = await page.request.post("/api/auth/step-up", {
          headers: { "X-CSRF-Token": cookie!.value },
          data: { password: proofRecord().owner.password },
        });
        expect(stepUp.status()).toBe(200);
        await page.getByRole("button", { name: "Approve reviewed source", exact: true }).click();
        await expect.poll(async () => (await sourceRead(page, sourceId)).state).toBe("approved");
      }
    },
  );

  await step("opening setup, one governed batch and a separate Owner manifest review", async () => {
    await loginBootstrap(page, "owner");
    if (!(await readiness(page, plan.site_id)).data.opening_setup_ready) {
      await readinessPage(page, plan.site_id);
      await guardedClick(page, "readiness-approve-opening-button", "owner");
      await expect
        .poll(async () => (await readiness(page, plan.site_id)).data.opening_setup_ready)
        .toBe(true);
    }
    await loginBootstrap(page, "admin");
    await openSource(page, sourceId);
    await page.getByRole("button", { name: "opening", exact: true }).click();
    let source = await sourceRead(page, sourceId);
    if (!source.data.batches.length) {
      const applied = responseFor(page, `/goods-v1/ptmapper/soh-imports/${sourceId}/apply`);
      await page
        .getByRole("button", { name: "Create governed opening batch 1", exact: true })
        .click();
      expect((await applied).status()).toBe(200);
      source = await sourceRead(page, sourceId);
    }
    expect(source.data.batches).toHaveLength(1);
    expect(source.data.batches[0]!.quantity).toBe(3);
    saveBootstrap({ manifest_id: source.data.batches[0]!.manifest_id });
    await loginBootstrap(page, "owner");
    await page.goto(`/goods/opening?manifest=${source.data.batches[0]!.manifest_id}`);
    await expect(page.getByTestId("opening-manifest-detail")).toBeVisible();
    const manifestResponse = await page.request.get(
      `/api/goods-v1/ptmapper/opening-manifests/${source.data.batches[0]!.manifest_id}`,
    );
    expect(manifestResponse.status()).toBe(200);
    if (!(await manifestResponse.json()).data.approved) {
      await expect(page.getByTestId("opening-approve-manifest")).toBeVisible();
      await guardedClick(page, "opening-approve-manifest", "owner");
      await expect(page.getByTestId("opening-approve-manifest")).toHaveCount(0);
    }
  });

  await step("opening PT preparation, independent officialisation and putaway UI", async () => {
    const manifest = proofRecord().browser_bootstrap.manifest_id!;
    await loginBootstrap(page, "admin");
    let pt = proofRecord().browser_bootstrap.pt_id;
    if (!pt) {
      await page.goto(`/goods/opening?manifest=${manifest}`);
      const created = responseFor(page, `/goods-v1/ptmapper/files/from-manifest/${manifest}`);
      await page.getByTestId("opening-create-pt").click();
      const response = await created;
      expect([200, 201]).toContain(response.status());
      pt = (await response.json()).id as string;
      saveBootstrap({ pt_id: pt });
    }
    await page.goto(`/goods/pt-work?pt=${pt}`);
    await expect(page.getByTestId("pt-editor")).toBeVisible();
    const reviews = page.locator(
      '[data-testid^="pt-review-"]:not([data-testid="pt-review-selected"])',
    );
    if (await reviews.count()) {
      for (const review of await reviews.all()) await review.click();
      await page.getByTestId("pt-save").click();
      await expect(page.getByTestId("pt-save")).toBeDisabled();
    }
    const preparedPt = await page.request.get(`/api/goods-v1/ptmapper/files/${pt}`);
    expect(preparedPt.status()).toBe(200);
    if ((await preparedPt.json()).state === "draft") {
      await expect(page.getByTestId("pt-submit")).toBeEnabled();
      const submitted = responseFor(page, `/goods-v1/ptmapper/files/${pt}/send`);
      await page.getByTestId("pt-submit").click();
      expect((await submitted).status()).toBe(200);
      await expect
        .poll(
          async () =>
            (await (await page.request.get(`/api/goods-v1/ptmapper/files/${pt}`)).json()).state,
        )
        .toBe("submitted");
    }
    await loginBootstrap(page, "owner");
    await page.goto("/goods/pt-work?tab=approve");
    const inbox = await page.request.get("/api/goods-v1/approvals/inbox?limit=100");
    expect(inbox.status()).toBe(200);
    const matching = (await inbox.json()).items.filter(
      (row: { subject_id: string; parent_document?: { id: string } }) =>
        (row.parent_document?.id ?? row.subject_id) === pt,
    );
    expect(matching.length).toBeLessThanOrEqual(1);
    const queue = page.getByTestId(`pt-queue-${matching[0]?.id ?? "none"}`);
    if (matching.length) {
      await expect(queue).toBeVisible();
      await queue.click();
      await expect(page.getByTestId("pt-approval-viewer")).toBeVisible();
      await guardedClick(page, "pt-approve", "owner");
      await expect(page.getByTestId("pt-approval-viewer")).toContainText(
        "Approved. Stock is valued and stays held.",
      );
    }
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/goods-v1/ptmapper/files/${pt}`)).json()).state,
      )
      .toBe("official");
    const historyResponse = await page.request.get("/api/goods-v1/approvals?limit=100");
    expect(historyResponse.status()).toBe(200);
    const approved = (await historyResponse.json()).items.filter(
      (row: { state: string; subject_id: string; parent_document?: { id: string } }) =>
        row.state === "approved" && (row.parent_document?.id ?? row.subject_id) === pt,
    );
    expect(approved).toHaveLength(1);
    await page.getByTestId(`pt-queue-decided-${approved[0].id}`).click();
    await expect(page.getByTestId("pt-approval-viewer")).toBeVisible();
    await expect(page.getByTestId("pt-approve")).toHaveCount(0);
    await expect(page.getByTestId("pt-revision-superseded")).toHaveCount(0);
    await loginBootstrap(page, "manager");
    await page.goto("/goods/receive");
    const acceptance = page.getByTestId(`accept-open-${pt}`);
    const pendingResponse = await page.request.get(
      `/api/goods-v1/stockledger/pending-acceptance?site_id=${plan.site_id}&limit=100`,
    );
    expect(pendingResponse.status()).toBe(200);
    const pending = (await pendingResponse.json()).items.filter(
      (row: { pt_id: string }) => row.pt_id === pt,
    );
    expect(pending.length).toBeLessThanOrEqual(1);
    if (pending.length) {
      await expect(acceptance).toBeVisible();
      const opened = responseFor(page, "/goods-v1/stockledger/acceptance-sessions");
      await acceptance.click();
      const response = await opened;
      expect(response.status()).toBe(201);
      const session = await response.json();
      const sessionId = session.id as string;
      saveBootstrap({ acceptance_session_id: sessionId });
      await expect(page.getByTestId("accept-scan-form")).toBeVisible();
      expect(session.data.lines.items).toHaveLength(1);
      if (session.data.lines.items[0].remaining_qty > 0) {
        expect(session.data.lines.items[0]).toMatchObject({ accepted_qty: 0, remaining_qty: 3 });
        await page.getByTestId("accept-putaway-toggle").check();
        await page
          .getByTestId("accept-location")
          .selectOption({ label: "Fictional proof sales floor (floor)" });
        await page.getByTestId("accept-mrp").fill("1000");
        await page.getByTestId("accept-qty").fill("3");
        await page.getByTestId("accept-alias").fill("ALPHA000123");
        await page.getByTestId("accept-alias").press("Enter");
      } else {
        expect(session.data.lines.items[0]).toMatchObject({ accepted_qty: 3, remaining_qty: 0 });
      }
      await expect(page.getByTestId("accept-errors")).toHaveCount(0);
      const sessionPath = `/goods-v1/stockledger/acceptance-sessions/${sessionId}`;
      await expect
        .poll(async () => {
          const current = await (await page.request.get(`/api${sessionPath}`)).json();
          return current.data.lines.items[0];
        })
        .toMatchObject({ accepted_qty: 3, remaining_qty: 0 });
      await expect(
        page.getByTestId("accept-lines").getByRole("row").last().getByRole("cell").last(),
      ).toHaveText("0");
      const beforeComplete = await (await page.request.get(`/api${sessionPath}`)).json();
      const completed = responseFor(page, `${sessionPath}/complete`);
      await page.getByTestId("accept-complete").click();
      const completion = await completed;
      expect(completion.status()).toBe(200);
      const closed = await completion.json();
      expect(closed.data.state).toBe("completed");
      expect(closed.data.acknowledged_scan_keys).toEqual(
        beforeComplete.data.acknowledged_scan_keys,
      );
      expect(closed.data.lines.items).toEqual(beforeComplete.data.lines.items);
      await expect(page.getByTestId("accept-completed")).toBeVisible();
    }
    runHelper("first-store-opening-reconciliation-proof.py");
    const opening = JSON.parse(
      readFileSync(`${ROOT}/.local/first-store-opening-reconciliation.json`, "utf8"),
    ) as {
      passed: boolean;
      quantity: number;
      accepted_qty: number;
      remaining_qty: number;
      source_count: number;
    };
    expect(opening).toMatchObject({ passed: true, quantity: 3, accepted_qty: 3, source_count: 1 });
    expect(opening.remaining_qty).toBe(0);
    await loginBootstrap(page, "owner");
    if (
      !proofRecord().browser_bootstrap.readiness_corrected &&
      (await readiness(page, plan.site_id)).data.goods_ready
    ) {
      await readinessPage(page, plan.site_id);
      await guardedClick(page, "readiness-revoke-goods-button", "owner");
      await expect
        .poll(async () => (await readiness(page, plan.site_id)).data.goods_ready)
        .toBe(false);
    }
    if (!(await readiness(page, plan.site_id)).data.goods_ready) {
      await readinessPage(page, plan.site_id);
      await guardedClick(page, "readiness-approve-goods-button", "owner");
      await expect
        .poll(async () => (await readiness(page, plan.site_id)).data.goods_ready)
        .toBe(true);
    }
    saveBootstrap({ readiness_corrected: true });
  });

  await step("same accepted source upload returns the existing effect", async () => {
    await loginBootstrap(page, "manager");
    await page.goto("/goods/opening");
    await page
      .getByLabel("SOH source (.xlsx)", { exact: true })
      .setInputFiles(`${ARTIFACTS}/${SOURCE_NAME}`);
    const uploaded = responseFor(page, "/goods-v1/ptmapper/soh-imports/upload");
    await page.getByRole("button", { name: "Upload and inspect source", exact: true }).click();
    const response = await uploaded;
    expect([200, 201]).toContain(response.status());
    expect((await response.json()).id).toBe(sourceId);
    expect((await sourceRead(page, sourceId)).data.batches).toHaveLength(1);
  });

  await step(
    "synthetic commercial setup, first bill and immutable matching cash count",
    async () => {
      runHelper("first-store-commercial-proof.py");
      runHelper("first-store-day-close-proof.py");
      await loginBootstrap(page, "owner");
      if (!(await readiness(page, plan.site_id)).data.sell_ready) {
        await readinessPage(page, plan.site_id);
        await guardedClick(page, "readiness-approve-sell-button", "owner");
        await expect
          .poll(async () => (await readiness(page, plan.site_id)).data.sell_ready)
          .toBe(true);
      }
      await loginProof(page, "manager");
      await pairProof(page);
      await page.getByTestId("till-sync-now").click();
      const history = await page.request.get("/api/sell/sales?from=2026-04-01&to=2027-03-31");
      expect(history.status()).toBe(200);
      const saved = await history.json();
      const bills = (Array.isArray(saved) ? saved : (saved.items ?? saved.results ?? [])) as {
        doc_number: string;
      }[];
      if (!bills.length) {
        await page.goto("/sell");
        await expect(page.getByTestId("bill-scan")).toBeEnabled();
        const salespersonOption = page
          .getByTestId("bill-sold-by")
          .locator('option:not([value=""])')
          .first();
        await expect(salespersonOption).toBeAttached();
        const salesperson = await salespersonOption.getAttribute("value");
        expect(salesperson).toBeTruthy();
        await page.getByTestId("bill-scan").fill("ALPHA000123");
        await page.getByTestId("bill-scan").press("Enter");
        await expect(page.getByTestId("bill-line-1")).toContainText("ALPHA000123");
        const attribution = page.getByLabel("Salesperson, line 1", { exact: true });
        await attribution.selectOption(salesperson!);
        await expect(attribution).toHaveValue(salesperson!);
        await page.getByTestId("bill-disc-1").fill("100");
        await page.getByTestId("bill-all-cash").click();
        await expect(page.getByTestId("bill-blocked")).toHaveCount(0);
        await expect(page.getByTestId("bill-save")).toBeEnabled();
        const finalised = responseFor(page, "/sell/sales/finalise-online");
        await page.getByTestId("bill-save").click();
        const response = await finalised;
        expect(response.status()).toBe(201);
        const bill = await response.json();
        expect(bill.doc_number).toBe("26-27/FIRST/SAL/1");
        saveBootstrap({ sale_number: bill.doc_number });
        await expect(page.getByRole("dialog", { name: "Bill saved", exact: true })).toBeVisible();
      } else {
        expect(bills).toHaveLength(1);
        expect(bills[0]!.doc_number).toBe("26-27/FIRST/SAL/1");
        saveBootstrap({ sale_number: bills[0]!.doc_number });
      }
      // Finalisation returns identifiers; the scoped saved receipt is the
      // authoritative read of the issued bill, including when resuming it.
      const receipt = await page.request.get("/api/sell/sales/26-27/FIRST/SAL/1");
      expect(receipt.status()).toBe(200);
      const bill = await receipt.json();
      expect(bill.doc_number).toBe("26-27/FIRST/SAL/1");
      expect(Number(bill.net_paise)).toBe(90_000);
      await page.goto("/sell/cash-count");
      const countResponse = await page.request.get("/api/sell/cash-count");
      expect(countResponse.status()).toBe(200);
      const cash = await countResponse.json();
      if (!cash.counted) {
        expect(Number(cash.cash_sales_paise)).toBe(90_000);
        expect(cash.bills).toBe(1);
        if (cash.opening_declared) await page.getByTestId("cash-opening").fill("0");
        await page.getByTestId("cash-note-500").fill("1");
        await page.getByTestId("cash-note-200").fill("2");
        await expect(page.getByTestId("cash-variance")).toHaveAttribute("data-variance", "0");
        await page.getByTestId("cash-save").click();
      }
      await expect(page.getByTestId("cash-counted-today")).toBeVisible();
      const counted = await (await page.request.get("/api/sell/cash-count")).json();
      expect(Number(counted.counted.counted_paise)).toBe(90_000);
      expect(Number(counted.counted.variance_paise)).toBe(0);
      runHelper("first-store-stock-review-proof.py");
      saveBootstrap({ completed: true });
    },
  );
  expect(apiErrors).toEqual([]);
  writeFileSync(
    `${ROOT}/.local/first-store-bootstrap-evidence.json`,
    JSON.stringify(
      {
        synthetic_only: true,
        ...proofRecord().rehearsal_identity,
        ...proofRecord().browser_bootstrap,
        opening_quantity: 3,
        remaining_quantity_after_first_bill: 2,
        first_bill_and_count_paise: "90000",
        signup_widths: WIDTHS,
        actual_browser_actions: [
          "joint signup",
          "SOH upload",
          "stable mapping",
          "physical observations",
          "independent source and manifest approval",
          "PT preparation and independent approval",
          "physical putaway",
          "readiness",
          "sale",
          "cash count",
        ],
        scoped_api_denials: ["manager setup read", "source maker self-approval"],
        preserved_defect_evidence: [
          "Premature goods readiness approval before official PT/acceptance; corrected through ordinary Owner revoke/approve after acceptance.",
        ],
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
});
