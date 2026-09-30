import { describe, expect, it } from "vitest";
import { databaseName } from "./db";
import { submitOnline } from "./online";
import type { PendingOnlineBill, TillDb } from "./db";
import type { TillTransport } from "./transport";
import { TillHttpError } from "./transport";

function fixture() {
  const pending: PendingOnlineBill = {
    idempotency_uuid: "same-uuid",
    state: "pending",
    bill: {
      idempotency_uuid: "same-uuid",
      store: "SAME",
      fy: "26-27",
      till_seq: 4,
      origin: "online",
      doc_number: "26-27/SAME/SAL/4",
      billed_at: "2026-09-30T10:00:00+05:30",
      lines: [],
      tenders: [],
      totals: {
        gross_paise: 1000,
        discount_paise: 0,
        net_paise: 1000,
        gst_paise: 48,
        round_paise: 0,
      },
      attempts: 0,
    },
  };
  const meta = new Map<string, unknown>([
    ["deviceToken", "proof-device"],
    ["fy", "26-27"],
    ["nextSeq", 4],
  ]);
  const db = {
    meta: {
      get: async (key: string) => (meta.has(key) ? { key, value: meta.get(key) } : undefined),
      put: async (row: { key: string; value: unknown }) => meta.set(row.key, row.value),
      bulkPut: async (rows: { key: string; value: unknown }[]) =>
        rows.forEach((row) => meta.set(row.key, row.value)),
    },
    onlineSubmissions: {
      get: async () => pending,
      update: async (_key: string, changes: Partial<PendingOnlineBill>) =>
        Object.assign(pending, changes),
    },
    transaction: async (_mode: string, _tables: unknown[], run: () => unknown) => run(),
  } as unknown as TillDb;
  return { db, pending, meta };
}

describe("authoritative online issue", () => {
  it("keeps unknown outcomes pending with unchanged numbers and the exact UUID", async () => {
    const { db, pending, meta } = fixture();
    const transport = {
      finaliseOnline: async () => {
        throw new TillHttpError(0, "NETWORK", "timeout");
      },
    } as unknown as TillTransport;
    await expect(submitOnline(db, transport, pending)).rejects.toThrow(
      "do not collect payment again",
    );
    expect(pending.state).toBe("pending");
    expect(pending.bill.idempotency_uuid).toBe("same-uuid");
    expect(meta.get("nextSeq")).toBe(4);
  });

  it("recognises only a durable server refusal as not issued", async () => {
    const { db, pending, meta } = fixture();
    const transport = {
      finaliseOnline: async () => {
        throw new TillHttpError(409, "PRICING_STALE", "Review prices", true);
      },
    } as unknown as TillTransport;
    await expect(submitOnline(db, transport, pending)).rejects.toThrow("Review prices");
    expect(pending.state).toBe("rejected");
    expect(meta.get("nextSeq")).toBe(4);
  });

  it("preserves access denials for the counter while retaining the exact unresolved intent", async () => {
    const { db, pending, meta } = fixture();
    const transport = {
      finaliseOnline: async () => {
        throw new TillHttpError(403, "FIELD_DENIED", "Field revoked");
      },
    } as unknown as TillTransport;
    const error = await submitOnline(db, transport, pending).catch((refusal: unknown) => refusal);
    expect(error).toBeInstanceOf(TillHttpError);
    expect((error as TillHttpError).code).toBe("FIELD_DENIED");
    expect((error as TillHttpError).status).toBe(403);
    expect(pending.state).toBe("pending");
    expect(pending.bill.idempotency_uuid).toBe("same-uuid");
    expect(meta.get("nextSeq")).toBe(4);
  });

  it("advances the local number only after acceptance and once across replay", async () => {
    const { db, pending, meta } = fixture();
    const ids: string[] = [];
    const transport = {
      finaliseOnline: async (bill: PendingOnlineBill["bill"]) => {
        ids.push(bill.idempotency_uuid);
        return { id: 1, doc_number: bill.doc_number, flags: [] };
      },
    } as unknown as TillTransport;
    await submitOnline(db, transport, pending);
    await submitOnline(db, transport, pending);
    expect(pending.state).toBe("accepted");
    expect(meta.get("nextSeq")).toBe(5);
    expect(ids).toEqual(["same-uuid", "same-uuid"]);
  });

  it("separates equal store labels by trusted tenant, site and device identities", () => {
    expect(databaseName("tenantA:12:deviceA")).not.toBe(databaseName("tenantB:12:deviceA"));
    expect(databaseName("tenantA:12:deviceA")).not.toBe(databaseName("tenantA:12:deviceB"));
  });

  it("recovers an accepted invoice after month close without spending the new month's number", async () => {
    const { db, pending, meta } = fixture();
    pending.bill.tax_invoice_number = "AAA/26-27/4";
    const numbering = {
      on: true,
      new_format_from: "2026-04-01",
      prefix: "AAA",
      block_size: 300,
      problem: "",
      blocks: [
        { id: 2, prefix: "AAA", fy: "26-27", month: "2026-10", first: 301, last: 600, next: 301 },
      ],
    };
    meta.set("numbering", numbering);
    const transport = {
      finaliseOnline: async () => ({
        id: 1,
        doc_number: pending.bill.doc_number,
        tax_invoice_number: pending.bill.tax_invoice_number,
        flags: [],
      }),
    } as unknown as TillTransport;
    await submitOnline(db, transport, pending);
    expect(pending.state).toBe("accepted");
    expect(meta.get("numbering")).toEqual(numbering);
    expect(meta.get("nextSeq")).toBe(5);
  });

  it("records only the accepted invoice and does not consume it again on replay", async () => {
    const { db, pending, meta } = fixture();
    pending.bill.tax_invoice_number = "AAA/26-27/4";
    meta.set("numbering", {
      on: true,
      blocks: [
        { id: 1, prefix: "AAA", fy: "26-27", month: "2026-09", first: 1, last: 300, next: 4 },
      ],
    });
    const transport = {
      finaliseOnline: async () => ({
        id: 1,
        doc_number: pending.bill.doc_number,
        tax_invoice_number: pending.bill.tax_invoice_number,
        flags: [],
      }),
    } as unknown as TillTransport;
    await submitOnline(db, transport, pending);
    await submitOnline(db, transport, pending);
    expect((meta.get("numbering") as { blocks: { next: number }[] }).blocks[0]?.next).toBe(5);
  });
});
