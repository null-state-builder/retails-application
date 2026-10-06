import { execFileSync } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, type Locator, type Page, type Response } from "@playwright/test";

export const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const PYTHON = `${ROOT}/backend/.venv/bin/python`;
const CREDENTIALS = `${ROOT}/.local/first-store-browser-credentials.json`;
const IDENTITY = `${ROOT}/.local/first-store-proof.json`;
const LOG = `${ROOT}/.local/first-store-bootstrap-helpers.log`;

type Person = {
  email: string;
  temporary_password?: string;
  password?: string;
  password_changed?: boolean;
};
export interface BootstrapRecord {
  owner: Person;
  admin: Person;
  manager?: Person;
  rehearsal_identity: { database: string; system_identifier: string };
  browser_bootstrap: {
    proposed_manager_email: string;
    source_id?: string;
    manifest_id?: string;
    pt_id?: string;
    acceptance_session_id?: string;
    sale_number?: string;
    completed?: boolean;
    readiness_corrected?: boolean;
  };
  [key: string]: unknown;
}

function privateJson<T>(path: string): T {
  if ((statSync(path).mode & 0o777) !== 0o600) throw new Error("Proof record is not private.");
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

export function proofRecord(): BootstrapRecord {
  const identity = privateJson<{ database: string; system_identifier: string }>(IDENTITY);
  if (!identity.database.startsWith("kdps_rehearsal_first_store_"))
    throw new Error("The first-store rehearsal is not owned.");
  if (!existsSync(CREDENTIALS)) {
    const nonce = randomUUID().slice(0, 12);
    const record: BootstrapRecord = {
      owner: {
        email: `owner-${nonce}@alpha.example.test`,
        temporary_password: `${randomBytes(24).toString("base64url")}A1!`,
      },
      admin: {
        email: `admin-${nonce}@alpha.example.test`,
        temporary_password: `${randomBytes(24).toString("base64url")}A1!`,
      },
      rehearsal_identity: {
        database: identity.database,
        system_identifier: identity.system_identifier,
      },
      browser_bootstrap: { proposed_manager_email: `manager-${nonce}@alpha.example.test` },
    };
    writeFileSync(CREDENTIALS, JSON.stringify(record), { mode: 0o600, flag: "wx" });
  }
  const record = privateJson<BootstrapRecord>(CREDENTIALS);
  if (
    record.rehearsal_identity?.database !== identity.database ||
    record.rehearsal_identity?.system_identifier !== identity.system_identifier ||
    !record.owner.email.endsWith(".example.test") ||
    !record.admin.email.endsWith(".example.test") ||
    record.owner.email === record.admin.email
  )
    throw new Error("Generated proof people do not belong to this exact rehearsal.");
  return record;
}

export function saveBootstrap(patch: Partial<BootstrapRecord["browser_bootstrap"]>) {
  const record = proofRecord();
  Object.assign(record.browser_bootstrap, patch);
  writeFileSync(CREDENTIALS, JSON.stringify(record), { mode: 0o600 });
}

/** Wrapper performs host/container/database identity checks before every helper. */
export function runHelper(script: string, ...args: string[]) {
  if (
    ![
      "first-store-onboarding-proof.py",
      "first-store-opening-proof.py",
      "first-store-commercial-proof.py",
      "first-store-day-close-proof.py",
      "first-store-stock-review-proof.py",
      "first-store-opening-reconciliation-proof.py",
    ].includes(script)
  )
    throw new Error("This browser proof cannot invoke arbitrary fixture scripts.");
  try {
    const output = execFileSync(
      PYTHON,
      ["scripts/first-store-proof.py", "run", "--", PYTHON, `scripts/${script}`, ...args],
      { cwd: ROOT, encoding: "utf8", timeout: 120_000 },
    );
    appendFileSync(LOG, `${script} ${args.join(" ")}\n${output}\n`, { mode: 0o600 });
  } catch (error) {
    const result = error as { stdout?: string | Buffer };
    if (result.stdout) appendFileSync(LOG, result.stdout, { mode: 0o600 });
    throw new Error(`The owned ${script} helper failed; inspect its private local log.`);
  }
}

export async function fillGeneratedSecret(locator: Locator, value: string) {
  try {
    await locator.fill(value);
  } catch {
    throw new Error("A private generated credential input was unavailable.");
  }
}

export function responseFor(page: Page, path: string): Promise<Response> {
  return page.waitForResponse((r) => new URL(r.url()).pathname === `/api${path}`);
}

export async function loginBootstrap(page: Page, role: "owner" | "admin" | "manager") {
  const person = proofRecord()[role];
  if (!person?.password)
    throw new Error("The generated proof person has not replaced the password.");
  await page.goto("/login");
  await page.request.get("/api/auth/csrf");
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  if (!cookie) throw new Error("The proof session has no CSRF token.");
  const login = await page.request.post("/api/auth/login", {
    headers: { "X-CSRF-Token": cookie.value },
    data: { email: person.email, password: person.password },
  });
  expect(login.status()).toBe(200);
  await page.goto("/");
  await expect(page).not.toHaveURL(/\/login$|\/change-password$/);
}

export async function stepUpBootstrap(page: Page, role: "owner" | "admin") {
  await expect(page.getByTestId("org-stepup-password")).toBeVisible();
  const password = proofRecord()[role].password;
  if (!password) throw new Error("The generated proof password is unavailable.");
  await fillGeneratedSecret(page.getByTestId("org-stepup-password"), password);
  await page.getByTestId("org-stepup-password").press("Enter");
  await expect(page.getByTestId("org-stepup")).toHaveCount(0);
}

export async function guardedClick(page: Page, testId: string, role: "owner" | "admin") {
  // Confirm via the ordinary same-session API before the click so a resumed
  // journey does not depend on whether the five-minute UI prompt is due.
  const cookie = (await page.context().cookies()).find((item) => item.name === "kdps_csrf");
  if (!cookie) throw new Error("The proof session has no CSRF token.");
  const confirmation = await page.request.post("/api/auth/step-up", {
    headers: { "X-CSRF-Token": cookie.value },
    data: { password: proofRecord()[role].password },
  });
  expect(confirmation.status()).toBe(200);
  await page.getByTestId(testId).click();
}

export async function sourceRead(page: Page, sourceId: string) {
  const response = await page.request.get(`/api/goods-v1/ptmapper/soh-imports/${sourceId}`);
  expect(response.status()).toBe(200);
  return (await response.json()) as {
    id: string;
    state: string;
    revision: number;
    content_hash: string;
    source_hash: string;
    allowed_actions: string[];
    data: {
      configuration: Record<string, unknown>;
      approval_request_id: string | null;
      included_quantity: number;
      verified_rows: number;
      batches: { index: number; manifest_id: string; quantity: number }[];
    };
  };
}
