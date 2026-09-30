/** Durable pre-issue intent. No local stock or counter changes until acceptance. */
import { financialYear } from "../lib/fiscal";
import { META, readMeta } from "./db";
import type { PendingOnlineBill, TillDb } from "./db";
import { counterFor, renderBillNumber, renderTillNumber } from "./numbering";
import { recordAcceptedInvoiceNumber, takeInvoiceNumber } from "./invoiceNumbers";
import { TillHttpError } from "./transport";
import type { TillTransport } from "./transport";
import type { BillDraft, QueuedBill } from "./types";
import { newUuid } from "./uuid";

export async function unfinishedSubmission(db: TillDb): Promise<PendingOnlineBill | undefined> {
  return db.onlineSubmissions
    .filter((row) => row.state === "pending" || row.state === "rejected")
    .first();
}

export async function prepareOnline(
  db: TillDb,
  store: string,
  draft: BillDraft,
): Promise<PendingOnlineBill> {
  return db.transaction("rw", [db.meta, db.onlineSubmissions], async () => {
    const existing = await unfinishedSubmission(db);
    if (existing) return existing;
    if (await readMeta(db, META.pause, null)) throw new Error("This counter is paused.");
    const revision = await readMeta(db, META.commercialRevision, "");
    if (!revision) throw new Error("Refresh the store's current prices before issuing a bill.");
    const identity = await readMeta<{ identity?: { series_prefix?: string } } | null>(
      db,
      META.till,
      null,
    );
    const fy = financialYear(new Date(draft.billed_at));
    const seq = await counterFor(db, fy);
    const invoice = await takeInvoiceNumber(db, new Date(draft.billed_at), false);
    const bill: QueuedBill = {
      ...draft,
      idempotency_uuid: newUuid(),
      store,
      fy,
      till_seq: seq,
      origin: "online",
      commercial_revision: revision,
      doc_number: renderBillNumber(fy, store, seq),
      till_number: renderTillNumber(identity?.identity?.series_prefix ?? "", seq),
      attempts: 0,
      ...(invoice ? { tax_invoice_number: invoice } : {}),
    };
    const pending: PendingOnlineBill = {
      idempotency_uuid: bill.idempotency_uuid,
      bill,
      state: "pending",
    };
    await db.onlineSubmissions.add(pending);
    return pending;
  });
}

export async function submitOnline(
  db: TillDb,
  transport: TillTransport,
  pending: PendingOnlineBill,
): Promise<QueuedBill> {
  if (!transport.finaliseOnline) throw new Error("This server does not support online issue.");
  if (pending.state === "rejected")
    throw new Error(pending.error ?? "The server refused this bill. Review it before revising.");
  const token = await readMeta(db, META.deviceToken, "");
  if (!token) throw new Error("Pair this browser with its counter on Till & Sync before selling.");
  let accepted;
  try {
    accepted = await transport.finaliseOnline(pending.bill, token);
  } catch (error) {
    // Only a persisted server refusal proves no bill was issued. Auth/network
    // failure can conceal a previous committed answer; keep its exact UUID.
    const rejected = error instanceof TillHttpError && error.notIssued;
    await db.onlineSubmissions.update(pending.idempotency_uuid, {
      state: rejected ? "rejected" : "pending",
      error: error instanceof Error ? error.message : String(error),
    });
    const message = rejected
      ? (error as Error).message
      : "Checking whether the sale completed. Retry this same submission; do not collect payment again.";
    if (error instanceof TillHttpError)
      throw new TillHttpError(error.status, error.code, message, error.notIssued);
    throw new Error(message);
  }
  await db.transaction("rw", [db.meta, db.onlineSubmissions], async () => {
    const current = await db.onlineSubmissions.get(pending.idempotency_uuid);
    if (current?.state === "accepted") return;
    const seq = await counterFor(db, pending.bill.fy);
    await db.meta.bulkPut([
      { key: META.fy, value: pending.bill.fy },
      { key: META.nextSeq, value: Math.max(seq, pending.bill.till_seq + 1) },
    ]);
    if (pending.bill.tax_invoice_number) {
      if (accepted.tax_invoice_number !== pending.bill.tax_invoice_number) {
        throw new Error(
          "The accepted invoice number needs counter reconciliation. Retry recovery.",
        );
      }
      await recordAcceptedInvoiceNumber(db, pending.bill.tax_invoice_number);
    }
    await db.onlineSubmissions.update(pending.idempotency_uuid, { state: "accepted", error: "" });
  });
  return {
    ...pending.bill,
    doc_number: accepted.doc_number,
    ...(accepted.tax_invoice_number ? { tax_invoice_number: accepted.tax_invoice_number } : {}),
  };
}

/** A rejected intent remains evidence. Revising starts a fresh UUID only after
 * the server has durably confirmed no document was issued. */
export async function reviseRejected(db: TillDb): Promise<void> {
  const pending = await unfinishedSubmission(db);
  if (!pending || pending.state !== "rejected")
    throw new Error("Resolve the unknown outcome before editing this bill.");
  await db.onlineSubmissions.update(pending.idempotency_uuid, { state: "revised" });
}
