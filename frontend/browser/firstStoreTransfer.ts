import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, type Page } from "@playwright/test";

export const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const CREDENTIALS = `${ROOT}/.local/first-store-transfer-credentials.json`;
const IDENTITY = `${ROOT}/.local/first-store-transfer-proof.json`;
type Person = { email: string; password: string };
export interface Snapshot {
  source_shelf_qty: number;
  destination_shelf_qty: number;
  origins_hash: string;
  sales_hash: string;
  tenders_hash: string;
  cash_hash: string;
  value_hash: string;
  reservations: number;
  journals: { id: string; posting_kind: string }[];
  positions: {
    site_id: string;
    origin_id: string;
    qty: number;
    boundary: string;
    accepted: boolean;
    condition: string;
    unit_cost_paise: string;
  }[];
  freeze_ids: (string | null)[];
}
export interface Fixture {
  synthetic_only: boolean;
  source_site_id: string;
  destination_site_id: string;
  destination_floor_id: string;
  sku_id: string;
  origin_id: string;
  barcode: string;
  snapshot: Snapshot;
  browser?: {
    baseline: Snapshot;
    transfer_id?: string;
    accept_body?: Record<string, unknown>;
    counter_paused?: boolean;
  };
}
interface TransferProofRecord {
  owner: Person;
  admin: Person;
  manager: Person;
  receiver: Person;
  commercial_fixture: { device_token?: string; synthetic_only?: boolean };
  browser_bootstrap: { pt_id: string };
  transfer_rehearsal: { database: string; system_identifier: string };
  transfer_fixture: Fixture;
}
function privateRead<T>(path: string): T {
  if ((statSync(path).mode & 0o777) !== 0o600)
    throw new Error("Transfer proof records must be private.");
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
function privateWrite(path: string, value: unknown) {
  const temporary = `${path}-${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
    renameSync(temporary, path);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}
export function transferRecord(): TransferProofRecord {
  const record = privateRead<TransferProofRecord>(CREDENTIALS);
  const identity = privateRead<TransferProofRecord["transfer_rehearsal"]>(IDENTITY);
  if (
    !identity.database.startsWith("kdps_rehearsal_transfer_") ||
    record.transfer_rehearsal.database !== identity.database ||
    record.transfer_rehearsal.system_identifier !== identity.system_identifier
  )
    throw new Error("The generated transfer people belong to another rehearsal.");
  return record;
}
export function saveTransfer(patch: Partial<NonNullable<Fixture["browser"]>>) {
  const record = transferRecord();
  record.transfer_fixture.browser = { ...record.transfer_fixture.browser, ...patch } as NonNullable<
    Fixture["browser"]
  >;
  privateWrite(CREDENTIALS, record);
}
export function transferHelper<T>(
  action: "prepare" | "snapshot" | "expire-receiver-session",
  input?: { session_hash: string },
): T {
  const log = `${ROOT}/.local/first-store-transfer-helpers-${transferRecord().transfer_rehearsal.database}.log`;
  let output: string;
  try {
    output = execFileSync(
      `${ROOT}/backend/.venv/bin/python`,
      [
        "scripts/first-store-transfer-proof.py",
        "run",
        "--",
        `${ROOT}/backend/.venv/bin/python`,
        "scripts/first-store-transfer-fixture.py",
        action,
      ],
      { cwd: ROOT, encoding: "utf8", timeout: 120_000, input: JSON.stringify(input ?? {}) },
    );
    appendFileSync(log, `${action}\n${output}\n`, { mode: 0o600 });
  } catch (error) {
    const failure = error as { stdout?: string | Buffer; stderr?: string | Buffer };
    appendFileSync(log, `${action} failed\n${failure.stdout ?? ""}\n${failure.stderr ?? ""}\n`, {
      mode: 0o600,
    });
    throw new Error(
      "The positively verified synthetic transfer helper failed; inspect the preserved proof run.",
    );
  }
  return JSON.parse(output.trim().split("\n").at(-1)!) as T;
}
export async function forceReceiverExpiry(page: Page) {
  const cookie = (await page.context().cookies()).find((c) => c.name === "kdps_session");
  if (!cookie) throw new Error("The generated receiver has no current browser session.");
  return transferHelper<{
    forced_test_expiry: boolean;
    session_row_preserved: boolean;
    business_unchanged: boolean;
  }>("expire-receiver-session", {
    session_hash: createHash("sha256").update(cookie.value).digest("hex"),
  });
}
export function transferCounterStorage() {
  const { database } = transferRecord().transfer_rehearsal;
  if (!/^kdps_rehearsal_transfer_[0-9a-f]{12}$/.test(database))
    throw new Error("The counter storage must belong to the generated transfer clone.");
  const path = `${ROOT}/.local/transfer-counter-${database}.json`;
  if (!existsSync(path)) return { path, restored: undefined };
  privateRead<unknown>(path);
  return { path, restored: path };
}
export async function saveTransferCounterStorage(page: Page) {
  const { path } = transferCounterStorage();
  const storage = await page.context().storageState({ indexedDB: true });
  privateWrite(path, storage);
}
export async function pairTransferCounter(page: Page) {
  const fixture = transferRecord().commercial_fixture;
  if (!fixture?.synthetic_only || !fixture.device_token)
    throw new Error("The copied fictional counter fixture is absent.");
  await page.goto("/sell/till");
  const input = page.getByLabel("Pairing code", { exact: true });
  await expect(input).toBeVisible();
  try {
    await input.fill(fixture.device_token);
    await page.getByRole("button", { name: "Pair counter", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("This browser is paired.");
  } catch {
    throw new Error("The copied fictional counter could not be paired normally.");
  } finally {
    await input.fill("").catch(() => undefined);
  }
}
export async function transferLogin(page: Page, who: "owner" | "manager" | "receiver") {
  const person = transferRecord()[who];
  if (!person.email.endsWith(".example.test"))
    throw new Error("A transfer proof person is not fictional.");
  await page.goto("/login");
  await page.request.get("/api/auth/csrf");
  const cookie = (await page.context().cookies()).find((c) => c.name === "kdps_csrf");
  const response = await page.request.post("/api/auth/login", {
    headers: { "X-CSRF-Token": cookie!.value },
    data: { email: person.email, password: person.password },
  });
  expect(response.status()).toBe(200);
  await page.goto("/");
  await expect(page).not.toHaveURL(/\/login$/);
}
export async function transferStepUp(page: Page) {
  const cookie = (await page.context().cookies()).find((c) => c.name === "kdps_csrf");
  const response = await page.request.post("/api/auth/step-up", {
    headers: { "X-CSRF-Token": cookie!.value },
    data: { password: transferRecord().owner.password },
  });
  expect(response.status()).toBe(200);
}
export async function transferPost(page: Page, path: string, body: Record<string, unknown>) {
  const cookie = (await page.context().cookies()).find((c) => c.name === "kdps_csrf");
  return page.request.post(`/api${path}`, {
    headers: { "X-CSRF-Token": cookie!.value },
    data: body,
  });
}
