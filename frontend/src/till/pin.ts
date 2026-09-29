// The manager's PIN, checked with the line down (#182, D10 §4, grill Q1).
//
// A late exchange needs a manager's OK, and the counter can be offline - so the
// OK is verified here, on the device, against the hash that
// came down in the dataset (`managers[].till_pin_hash`).
//
// Three things about that are not obvious.
//
// **It is PBKDF2-SHA256 because that is what a browser has.** The project hashes
// passwords with bcrypt, which is the better choice for a secret checked on a
// server and the wrong one here: the Web Crypto API has PBKDF2 and no bcrypt, so
// a bcrypt hash would need a bcrypt implementation shipped to the shop floor. The
// server hashes counter PINs with `PBKDF2PasswordHasher` for exactly this reason
// (`accounts/till_pin.py`), and the iteration count is read out of the string, so
// raising it there needs nothing here.
//
// **Anything it cannot verify is a no.** A hash in an algorithm this does not
// know, a malformed one, a browser with no `crypto.subtle` (an insecure origin) -
// all of them answer "not authorised". The alternative shape, "if we cannot check
// it, allow it", is a cap anybody can lift by breaking the check.
//
// **A four-digit PIN is a weak secret and is meant to be.** What makes it worth
// having is not that it cannot be guessed but that using it leaves a name and a
// time on a bill that a person has to answer for the next morning. The device it
// sits on already holds the store's whole price list; treating the PIN as the
// thing standing between an attacker and the money would be the wrong reading of
// what it is for.

import type { TillManager } from "./types";

/** The one hash format the counter can read - Django's `PBKDF2PasswordHasher`. */
const ALGORITHM = "pbkdf2_sha256";

/**
 * What a manager may be asked to authorise at the counter. The only remaining
 * exception is taking a return after the configured window.
 */
/** Taking one back after the return window has closed - the manager's *second*,
 *  separate answer, and the one the store's morning queue is told about. */
export const LATE_RETURN = "late_return" as const;

/** A day-close cash count that does not match the expected cash (ticket 41).
 *  Confirmed by a manager other than the cashier, and saved online only. */
export const CASH_VARIANCE = "cash_variance" as const;

export type AuthorisationKind = typeof LATE_RETURN | typeof CASH_VARIANCE;

/**
 * One thing on a bill that a manager has to agree to.
 *
 * It names *which* thing and *how much*, and both halves are load-bearing. A
 * manager who nods at ₹200 off line 1 has agreed to ₹200 off line 1 - not to
 * whatever that line says by the time the bill closes, and not to the five other
 * lines the cashier discounted afterwards. Everything about `covers` follows
 * from that.
 */
export interface Ask {
  kind: AuthorisationKind;
  /** What the decision is about: the original bill for a late return, the
   *  business day for a cash count. */
  ref: string;
  /** The value being taken back, in paise. */
  paise: number;
  /** How it reads to the manager - "Line 3", or the note's own number. No money
   *  in it: the screen formats that, in Indian format, where it is rendered. */
  label: string;
}

/** Who authorised what, as the bill will carry it. */
export interface Authorisation {
  user_id: number;
  name: string;
  /** Exactly what was on the screen when they typed the PIN. */
  asks: Ask[];
  /** The moment the PIN was accepted, which is not Save & Print. */
  at: string;
}

/**
 * Does this authorisation cover everything the bill now asks for?
 *
 * Matched ask by ask, and never by kind alone. Approving "a discount past the
 * cap" and letting that stand for the rest of the bill is the whole hole this
 * closes: one tap would otherwise lift the cap on every later line, and on any
 * amount, in a manager's name. So a bigger discount on the same line asks again,
 * a discount on a different line asks again, and a second unknown note asks
 * again. Less than was approved stands - a cashier taking money *off* an
 * exception has not created a new one.
 */
export function covers(authorisation: Authorisation | null, asks: Ask[]): boolean {
  if (!asks.length) return true;
  if (!authorisation) return false;
  return asks.every((ask) =>
    authorisation.asks.some(
      (seen) => seen.kind === ask.kind && seen.ref === ask.ref && ask.paise <= seen.paise,
    ),
  );
}

/**
 * Does `pin` belong to `manager`?
 *
 * False for every failure, deliberately and without distinguishing them: a
 * modal that said "no such algorithm" rather than "wrong PIN" would be telling
 * whoever is standing there which of the two to attack.
 */
export async function verifyPin(manager: TillManager, pin: string): Promise<boolean> {
  const parsed = parseHash(manager.till_pin_hash);
  if (!parsed || !pin) return false;
  const derived = await derive(pin, parsed.salt, parsed.iterations);
  return derived !== null && sameSecret(derived, parsed.digest);
}

/**
 * The manager on the cached list whose PIN this is, or null.
 *
 * The counter types a PIN and not a name: at a busy till the manager reaches
 * over the cashier's shoulder, and asking them to find themselves in a dropdown
 * first is a step for the sake of the database. Which of them it was is then a
 * fact the PIN establishes, and the bill records it.
 *
 * **Exactly one, never the first of several.** Four digits is a small space, and
 * two managers of one store can pick the same four - at which point "the first
 * that matches" puts one of their names on a bill the other one approved, and
 * nothing afterwards can tell. So an ambiguous PIN authorises nobody, and the
 * counter is told to have one of them changed. Every manager is checked either
 * way; there is no early exit to time, either.
 */
export interface PinAttempt {
  /** Null unless exactly one manager owns this PIN. */
  authorisation: Authorisation | null;
  /** How many of this counter's managers own it. Two is a thing to say out loud
   *  - "wrong PIN" would send a manager away to keep trying something that can
   *  never work - and it is carried out of here rather than asked for again,
   *  because asking again means deriving every hash a second time. */
  matched: number;
  /** True when the one manager this PIN belongs to is the cashier at this
   *  counter and the store's rules say they may not approve their own bill
   *  (ticket 06, overall PRD §10.2). The authorisation is then null. */
  selfApproval?: boolean;
}

/**
 * Ticket 06: who the manager must not be. `cashierId` is the person signed in
 * at this counter; set it only where the store's switch is on, and the PIN of
 * that person authorises nothing, however right it is.
 */
export interface PinRules {
  cashierId?: number | null;
}

export async function whoAuthorised(
  managers: TillManager[],
  pin: string,
  asks: Ask[],
  now: Date = new Date(),
  rules: PinRules = {},
): Promise<PinAttempt> {
  const matched: TillManager[] = [];
  for (const manager of managers) {
    if (await verifyPin(manager, pin)) matched.push(manager);
  }
  if (matched.length !== 1) return { authorisation: null, matched: matched.length };
  if (rules.cashierId != null && matched[0].user_id === rules.cashierId) {
    return { authorisation: null, matched: 1, selfApproval: true };
  }
  return {
    authorisation: {
      user_id: matched[0].user_id,
      name: matched[0].name,
      // Copied, not referenced: what was on the screen at this moment is the
      // whole of what this authorisation means, and the cart goes on changing.
      asks: asks.map((ask) => ({ ...ask })),
      at: now.toISOString(),
    },
    matched: 1,
  };
}

interface ParsedHash {
  iterations: number;
  salt: string;
  digest: Uint8Array;
}

/** As many rounds as this will ever run. Django's own count is in the hundreds
 *  of thousands and rises every release, so the ceiling is generous - what it is
 *  for is a corrupt row asking for a billion, which would hang the counter's tab
 *  rather than answer anybody. */
const MAX_ITERATIONS = 50_000_000;

function parseHash(encoded: string): ParsedHash | null {
  const parts = (encoded || "").split("$");
  if (parts.length !== 4) return null;
  const [algorithm, iterations, salt, digest] = parts;
  const rounds = Number(iterations);
  if (algorithm !== ALGORITHM || !salt) return null;
  if (!Number.isInteger(rounds) || rounds < 1 || rounds > MAX_ITERATIONS) return null;
  const bytes = fromBase64(digest);
  return bytes && bytes.length ? { iterations: rounds, salt, digest: bytes } : null;
}

async function derive(pin: string, salt: string, iterations: number): Promise<Uint8Array | null> {
  const subtle = globalThis.crypto?.subtle;
  // No `subtle` means a page served over plain http from something that is not
  // localhost. A till is a PWA on https by construction, so this is a developer
  // on a stray origin rather than a counter - and the honest answer to "I cannot
  // check this" is still no.
  if (!subtle) return null;
  try {
    const encoder = new TextEncoder();
    const key = await subtle.importKey("raw", encoder.encode(pin), "PBKDF2", false, [
      "deriveBits",
    ]);
    const bits = await subtle.deriveBits(
      { name: "PBKDF2", salt: encoder.encode(salt), iterations, hash: "SHA-256" },
      key,
      // Django's PBKDF2 hasher derives one SHA-256 digest: 32 bytes, 256 bits.
      256,
    );
    return new Uint8Array(bits);
  } catch {
    // A rejection here - an algorithm the browser withdrew, a key it refused -
    // is the same answer as a wrong PIN and must not escape as an exception: a
    // modal that threw would sit there doing nothing while a queue of customers
    // waited, which reads as "the button is broken" rather than "no".
    return null;
  }
}

/** Compare in constant time. The timing of a PIN check at a counter is not a
 *  realistic attack, and writing the comparison the other way is the habit that
 *  eventually gets applied to something where it is. */
function sameSecret(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i += 1) difference |= a[i] ^ b[i];
  return difference === 0;
}

function fromBase64(text: string): Uint8Array | null {
  try {
    const binary = atob(text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}
