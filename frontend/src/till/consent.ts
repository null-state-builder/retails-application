// ---------------------------------------------------------------------------
// Customer consent at the counter (store operations ticket 15, ST-CMP-6)
// ---------------------------------------------------------------------------
//
// Once a mobile number is on the bill, the counter can ask the customer two
// separate questions, each off until they say yes: **send my bill** and **send
// me offers**. The customer answers on the customer display themselves
// (ticket 09's question slot). Before offers they are asked whether they are
// under 18, and a yes there keeps offers off without asking.
//
// Staff never answer for the customer. There is no "yes" the till can record
// on its own: without the customer display nothing is asked, and both stay off.
// Staff can record a **withdrawal** of either answer at the counter, and it
// takes effect at once - on this device as it is written, at head office as
// soon as the line allows.
//
// Every answer is kept in the till's own database first (`consents`), with the
// time the customer gave it, and sent from there one at a time. So an answer
// given offline is never lost and reaches head office with its original time.
// Nothing here ever touches a bill: the phone number stays optional, and a
// bill is never waiting on a question.

import type { TillDb } from "./db";
import type { DisplayQuestion } from "./customerDisplay";
import { TillHttpError } from "./transport";
import type { TillTransport } from "./transport";
import { newUuid } from "./uuid";

export type ConsentQuestion = "bill" | "offers";
/** `display`: the customer tapped it. `counter`: staff recorded a withdrawal. */
export type ConsentHow = "display" | "counter";

/** The words the customer is asked in, and their version. */
export interface TillConsentWording {
  version: number;
  bill: string;
  age: string;
  offers: string;
}

/** Baseline B13, what a till asks with before a sync has told it anything. */
export const FIRST_WORDING: TillConsentWording = {
  version: 1,
  bill: "Send my bill to this number",
  age: "Are you under 18?",
  offers: "Send me offers and news",
};

/** One answer, as the till keeps it until head office has it. */
export interface QueuedConsent {
  /** The till's own id for the answer: a replay can never record it twice. */
  id: string;
  mobile: string;
  question: ConsentQuestion;
  given: boolean;
  how: ConsentHow;
  /** The answer to "are you under 18?", asked before offers only. */
  under_18: boolean | null;
  wording_version: number;
  /** When the customer answered (or staff withdrew), by this till's clock. */
  answered_at: string;
  till_number: string;
  attempts: number;
  last_error: string;
  /** Head office refused it for good, and why. Kept on the device, never sent
   *  again, and not counted as standing. "" while it may still go. */
  refused: string;
}

/** What stands for a number on one question. */
export interface ConsentStanding {
  given: boolean;
  how: ConsentHow;
  under_18: boolean | null;
  wording_version: number;
  answered_at: string;
}

/** Both questions. Null means never asked, which means off. */
export interface ConsentState {
  bill: ConsentStanding | null;
  offers: ConsentStanding | null;
}

export const NOT_ASKED: ConsentState = { bill: null, offers: null };

/** The bare 10-digit mobile consent is recorded against, or "" when what was
 *  typed is not one. Normalised the way the server's customer master is:
 *  "+91 98765-43210", "09876543210" and "9876543210" are one number. */
export function consentMobile(typed: string): string {
  let digits = (typed.match(/\d/g) ?? []).join("");
  if (digits.length === 12 && digits.startsWith("91")) digits = digits.slice(2);
  if (digits.length === 11 && digits.startsWith("0")) digits = digits.slice(1);
  return digits.length === 10 ? digits : "";
}

/** A new answer, stamped with an id and the time it was given. */
export function newAnswer(fields: {
  mobile: string;
  question: ConsentQuestion;
  given: boolean;
  how: ConsentHow;
  under_18?: boolean | null;
  wording_version: number;
  till_number: string;
  at?: Date;
}): QueuedConsent {
  return {
    id: newUuid(),
    mobile: fields.mobile,
    question: fields.question,
    given: fields.given,
    how: fields.how,
    under_18: fields.question === "bill" ? null : (fields.under_18 ?? null),
    wording_version: fields.wording_version,
    answered_at: (fields.at ?? new Date()).toISOString(),
    till_number: fields.till_number,
    attempts: 0,
    last_error: "",
    refused: "",
  };
}

/** A withdrawal staff record at the counter: always a no, never a yes. */
export function withdrawal(
  mobile: string,
  question: ConsentQuestion,
  context: { wording: TillConsentWording; tillNumber: string; at?: Date },
): QueuedConsent {
  return newAnswer({
    mobile,
    question,
    given: false,
    how: "counter",
    under_18: null,
    wording_version: context.wording.version,
    till_number: context.tillNumber,
    at: context.at,
  });
}

const YES_NO = [
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];

/**
 * Ask the customer, on the customer display, in order: send my bill; are you
 * under 18; send me offers (only after a no to under 18).
 *
 * Each answer is handed to `record` the moment it is given, so a customer who
 * answers the first question and walks away has still answered it. `ask`
 * resolving null (the question was withdrawn, the display went away, the
 * counter moved on) stops the round: nothing more is asked, nothing is
 * guessed. `stopped` is checked between questions for the same reason.
 *
 * An under-18 yes records offers as a no, with the age answer on it, and does
 * not ask about offers at all.
 */
export async function askTheCustomer(
  ask: (question: DisplayQuestion) => Promise<string | null>,
  record: (answer: QueuedConsent) => Promise<void>,
  context: {
    mobile: string;
    wording: TillConsentWording;
    tillNumber: string;
    stopped?: () => boolean;
    now?: () => Date;
  },
): Promise<QueuedConsent[]> {
  const { mobile, wording, tillNumber } = context;
  const now = context.now ?? (() => new Date());
  const stopped = context.stopped ?? (() => false);
  const round = newUuid();
  const given: QueuedConsent[] = [];
  const keep = async (answer: QueuedConsent) => {
    given.push(answer);
    await record(answer);
  };
  const base = { mobile, how: "display" as const, wording_version: wording.version, till_number: tillNumber };

  const bill = await ask({ id: `consent-bill-${round}`, text: wording.bill, choices: YES_NO });
  if (bill === null || stopped()) return given;
  await keep(newAnswer({ ...base, question: "bill", given: bill === "yes", at: now() }));

  const age = await ask({ id: `consent-age-${round}`, text: wording.age, choices: YES_NO });
  if (age === null || stopped()) return given;
  if (age === "yes") {
    await keep(newAnswer({ ...base, question: "offers", given: false, under_18: true, at: now() }));
    return given;
  }

  const offers = await ask({ id: `consent-offers-${round}`, text: wording.offers, choices: YES_NO });
  if (offers === null || stopped()) return given;
  await keep(
    newAnswer({ ...base, question: "offers", given: offers === "yes", under_18: false, at: now() }),
  );
  return given;
}

function standingOf(row: QueuedConsent): ConsentStanding {
  return {
    given: row.given,
    how: row.how,
    under_18: row.under_18,
    wording_version: row.wording_version,
    answered_at: row.answered_at,
  };
}

function newer(a: ConsentStanding | null, b: ConsentStanding | null): ConsentStanding | null {
  if (!a) return b;
  if (!b) return a;
  return Date.parse(b.answered_at) >= Date.parse(a.answered_at) ? b : a;
}

/**
 * What stands for this number: head office's answer (when the counter could
 * ask) with this till's own unsent answers laid over it, the newest by the time
 * it was given winning - the same rule head office applies when they arrive.
 * A refused answer never stands.
 */
export function standing(
  server: ConsentState | null,
  local: QueuedConsent[],
  mobile: string,
): ConsentState {
  const state: ConsentState = { ...(server ?? NOT_ASKED) };
  for (const row of local) {
    if (row.mobile !== mobile || row.refused) continue;
    state[row.question] = newer(state[row.question], standingOf(row));
  }
  return state;
}

// ------------------------------------------------------------ the queue ------

/** Keep an answer on the device. It stays until head office has it. */
export async function queueConsent(db: TillDb, answer: QueuedConsent): Promise<void> {
  await db.consents.put(answer);
}

/** This till's answers for a number that have not reached head office yet. */
export async function unsentFor(db: TillDb, mobile: string): Promise<QueuedConsent[]> {
  return db.consents.where("mobile").equals(mobile).toArray();
}

/** The answer as the wire carries it: the queue's bookkeeping stays here. */
export function consentBody(row: QueuedConsent): Record<string, unknown> {
  const { attempts: _a, last_error: _e, refused: _r, ...body } = row;
  return body;
}

export interface ConsentDrain {
  sent: number;
  refused: number;
  pending: number;
  /** The line went down part way: try again later. */
  stalled: boolean;
}

const inFlight = new Map<string, Promise<ConsentDrain>>();

/** Head office's refusals that are about the answer itself, so sending it again
 *  would only be refused again. Anything else - a login problem, a permission,
 *  a server that is busy - says nothing about the answer, and it waits to be
 *  sent again: a withdrawal must never be dropped over a till's login. */
export const FINAL_REFUSALS = new Set([
  "CONSENT_NOT_CUSTOMERS",
  "CONSENT_UNDER_18",
  "CONSENT_CONFLICT",
  "FEATURE_OFF",
  "VALIDATION",
]);

/** A refused answer keeps only the last four digits of the number: it stands
 *  for nothing any more, and the device has no need of the customer's phone. */
function maskedMobile(mobile: string): string {
  return "*".repeat(Math.max(mobile.length - 4, 0)) + mobile.slice(-4);
}

/**
 * Send unsent answers, oldest first, until the line stops.
 *
 * Unlike the bill queue, an answer head office refuses does not stop the ones
 * behind it: answers are not numbered, so there is no order to protect, and a
 * refused answer must never keep a later withdrawal from arriving. A refused
 * one is kept on the device with the reason (and only the last four digits of
 * the number) and never offered again. Only a refusal about the answer itself
 * is final (`FINAL_REFUSALS`); any other stops the round and it goes again.
 */
export function drainConsents(db: TillDb, transport: TillTransport): Promise<ConsentDrain> {
  const running = inFlight.get(db.name);
  if (running) return running;
  const attempt = drainOnce(db, transport).finally(() => inFlight.delete(db.name));
  inFlight.set(db.name, attempt);
  return attempt;
}

async function drainOnce(db: TillDb, transport: TillTransport): Promise<ConsentDrain> {
  const result: ConsentDrain = { sent: 0, refused: 0, pending: 0, stalled: false };
  const post = transport.postConsent?.bind(transport);
  const rows = (await db.consents.toArray())
    .filter((row) => !row.refused)
    .sort((a, b) => Date.parse(a.answered_at) - Date.parse(b.answered_at));
  if (post) {
    for (const row of rows) {
      try {
        await post(consentBody(row));
      } catch (error) {
        const refusal =
          error instanceof TillHttpError
            ? error
            : new TillHttpError(0, "NETWORK", "No connection to head office.");
        if (refusal.terminal && FINAL_REFUSALS.has(refusal.code)) {
          await db.consents.update(row.id, {
            mobile: maskedMobile(row.mobile),
            attempts: row.attempts + 1,
            last_error: refusal.message,
            refused: refusal.message || refusal.code,
          });
          result.refused += 1;
          continue;
        }
        await db.consents.update(row.id, { attempts: row.attempts + 1, last_error: refusal.message });
        result.stalled = true;
        break;
      }
      await db.consents.delete(row.id);
      result.sent += 1;
    }
  }
  result.pending = (await db.consents.toArray()).filter((row) => !row.refused).length;
  return result;
}
