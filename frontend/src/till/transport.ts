// The three calls the till makes, and the one distinction it has to draw (#180).
//
// Everything the sync engine decides comes down to telling two kinds of failure
// apart, so the engine is written against this interface rather than against
// axios: a fake here is a fake server, and the queue's behaviour under a refusal
// is testable without a network.
//
//   · **The network failed.** Nothing is known about the bill. Retry for ever -
//     the bill is printed and the money is in the drawer, and the only wrong
//     answer is to give up on it.
//   · **The server refused.** A 4xx with a code from the contract's table means
//     retrying will produce the same refusal until a human acts, so the queue
//     stops and says which bill and why (`api-contract.md`, step 3).
//
// A 401 is neither: the session expired, `lib/api` refreshes it and replays, and
// if the refresh fails the person signs in again. Treating it as terminal would
// halt a queue over a token.

import { api, apiErrorMessage } from "../lib/api";
import type { ConsentState } from "./consent";
import type { NumberingPayload } from "./invoiceNumbers";
import { stampManualUpi } from "./tender";
import type {
  AcceptedBill,
  DatasetPayload,
  HandoverPayload,
  QueuedBill,
  RegisterPayload,
  TillIdentity,
} from "./types";

/** A refusal the till has to reason about, stripped of the HTTP library. */
export class TillHttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly notIssued: boolean;
  constructor(status: number, code: string, message: string, notIssued = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.notIssued = notIssued;
    this.name = "TillHttpError";
  }

  /** Will this answer be any different if we simply ask again?
   *
   *  Anything the server said in the 4xx range is a decision about *this* bill -
   *  the number is taken, the tender does not add up, the line resolves to
   *  nothing - and a queue that kept re-offering it would spin for ever while
   *  every bill behind it waited. The three exceptions are not decisions about
   *  the bill at all: 401 is a stale token, 408 and 429 are the server asking to
   *  be asked later. */
  get terminal(): boolean {
    if (this.status < 400 || this.status >= 500) return false;
    return ![401, 408, 429].includes(this.status);
  }
}

export interface TillTransport {
  /** `GET /api/sell/dataset` - `since` empty means a full bootstrap. */
  dataset(since: string): Promise<DatasetPayload>;
  /** `GET /api/sell/register` - what the server has accepted from this counter. */
  register(): Promise<RegisterPayload>;
  /** `POST /api/sell/register/handover` - this store bills from this machine now
   *  (#189). A manager's call, not the sync loop's: it is never retried, and a
   *  refusal is shown to the person who asked rather than swallowed. */
  handover(reason: string): Promise<HandoverPayload>;
  /** `GET /api/sell/till` - which device this store's counter is (OPS-09). */
  till(): Promise<TillIdentity>;
  /** `POST /api/sell/till/renew` - another 24 hours of offline billing. Only ever
   *  succeeds online, which is the whole evidence the window rests on. */
  renewTill(): Promise<TillIdentity>;
  pairTill?(pairingCode: string): Promise<unknown>;
  /** `POST /api/sell/till/register` - a manager making this device the store's
   *  counter. `replace` retires the machine that held the series and starts a
   *  fresh one; without it, a store that already has a counter answers
   *  `TILL_TAKEN`. */
  registerTill(options: { replace: boolean; reason: string }): Promise<TillIdentity>;
  /** `POST /api/sell/till/allocations/{version}/release` - pause and let the
   *  store's stock go (change PRD §10.2). `(fy, nextSeq)` is where this counter
   *  would bill next, in the year it is counting in; every number below it must
   *  have arrived. */
  releaseForTransfer(
    version: number,
    reason: string,
    fy: string,
    nextSeq: number,
  ): Promise<unknown>;
  /** `POST /api/sell/till/resume` - end the pause at head office. The counter
   *  stays shut until a full dataset lands. Harmless if not paused. */
  resumeTill(fy: string, nextSeq: number): Promise<unknown>;
  /** `POST /api/sell/sales`. A replay of the same `idempotency_uuid` answers 200
   *  with the original bill and writes nothing; a first arrival answers 201. The
   *  queue drops the bill either way, so the two are not distinguished here. */
  postSale(bill: QueuedBill): Promise<AcceptedBill>;
  finaliseOnline?(bill: QueuedBill, deviceToken: string): Promise<AcceptedBill>;
  /** `PUT /api/sell/held-bills` - the counter's parked carts, whole list, so the
   *  Dashboard can count them (#185). Best effort by construction: a hold that
   *  never reaches the server is still a hold, and nothing about billing waits
   *  on this call. */
  putHeld(held: Record<string, unknown>[]): Promise<{ count: number }>;
  /** `POST /api/sell/till/number-blocks` - invoice numbers in the new series to
   *  bill with offline (ticket 04). `held` is each block this counter still
   *  holds and the next number it would take from it. Optional so a fake server
   *  that predates the new series needs no change. */
  numberBlocks?(held: Record<string, number>): Promise<NumberingPayload>;
  /** `POST /api/sell/consents` - one consent answer from the queue (ticket 15).
   *  A replay of the same id answers 200 and writes nothing. Optional so a fake
   *  server that predates consent needs no change. */
  postConsent?(answer: Record<string, unknown>): Promise<unknown>;
  /** `GET /api/sell/consents?mobile=` - what stands for a number at head office. */
  consentState?(mobile: string): Promise<ConsentState>;
}

/** The real one, over `lib/api` (so it carries the session and its refresh). */
export const httpTransport: TillTransport = {
  async dataset(since: string) {
    return unwrap(api.get("/sell/dataset", { params: since ? { since } : {} }));
  },
  async register() {
    return unwrap(api.get("/sell/register"));
  },
  async handover(reason: string) {
    return unwrap(api.post("/sell/register/handover", { reason }));
  },
  async till() {
    return unwrap(api.get("/sell/till"));
  },
  async pairTill(pairingCode: string) {
    return unwrap(api.post("/sell/till/pair", { pairing_code: pairingCode }));
  },
  async renewTill() {
    return unwrap(api.post("/sell/till/renew", {}));
  },
  async numberBlocks(held: Record<string, number>) {
    return unwrap(api.post("/sell/till/number-blocks", { held }));
  },
  async registerTill(options: { replace: boolean; reason: string }) {
    return unwrap(api.post("/sell/till/register", options));
  },
  async releaseForTransfer(version: number, reason: string, fy: string, nextSeq: number) {
    return unwrap(
      api.post(`/sell/till/allocations/${version}/release`, { reason, fy, next_seq: nextSeq }),
    );
  },
  async resumeTill(fy: string, nextSeq: number) {
    return unwrap(api.post("/sell/till/resume", { fy, next_seq: nextSeq }));
  },
  async finaliseOnline(bill: QueuedBill, deviceToken: string) {
    return unwrap(
      api.post("/sell/sales/finalise-online", billBody(bill), {
        headers: { "X-KDPS-Device": deviceToken },
      }),
    );
  },
  async postSale(bill: QueuedBill) {
    return unwrap(api.post("/sell/sales", billBody(bill)));
  },
  async putHeld(held: Record<string, unknown>[]) {
    return unwrap(api.put("/sell/held-bills", { held }));
  },
  async postConsent(answer: Record<string, unknown>) {
    return unwrap(api.post("/sell/consents", answer));
  },
  async consentState(mobile: string) {
    const state = await unwrap<{ bill: ConsentState["bill"]; offers: ConsentState["offers"] }>(
      api.get("/sell/consents", { params: { mobile } }),
    );
    return { bill: state.bill, offers: state.offers };
  },
};

/** The queue's bookkeeping is the till's, not the server's. Sending `attempts`
 *  or the local row id would put fields in a money payload that the contract does
 *  not name, and the serializer would have to decide what to do with them.
 *
 *  `stampManualUpi` runs here too, not only in `tender.toTenders` (#241): a bill
 *  already sitting in the queue from before this build was committed with no
 *  stamp at all, and sending it as-is would meet the server's new refusal and
 *  halt the whole queue behind it - a queued bill is a printed bill, so it is
 *  stamped at this wire boundary on exactly the reasoning the server's own
 *  historic-data backfill uses. */
export function billBody(bill: QueuedBill): Record<string, unknown> {
  const { id: _id, attempts: _a, last_error: _e, doc_number: _n, ...body } = bill;
  return { ...body, tenders: stampManualUpi(body.tenders) };
}

async function unwrap<T>(request: Promise<{ data: T }>): Promise<T> {
  try {
    return (await request).data;
  } catch (error) {
    throw asTillError(error);
  }
}

/** Flatten whatever axios threw into the two facts the queue reasons about. */
export function asTillError(error: unknown): TillHttpError {
  const response = (
    error as { response?: { status?: number; data?: { code?: string; not_issued?: boolean } } }
  )?.response;
  if (!response?.status) {
    // No response at all: aeroplane mode, a dead access point, DNS. Status 0 is
    // the till's own word for "we never got as far as the server", and it is
    // never terminal.
    return new TillHttpError(0, "NETWORK", "No connection to head office.");
  }
  return new TillHttpError(
    response.status,
    response.data?.code || `HTTP_${response.status}`,
    apiErrorMessage(error),
    response.data?.not_issued === true,
  );
}
