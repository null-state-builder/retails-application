// The counter's copy, in memory (#181).
//
// The Billing screen reads the local database once and then works out of arrays.
// That is deliberate, and it is what makes the D10 rule "lookup is local:
// instant, offline, never a spinner" true rather than aspirational: a scan
// resolves inside the same tick as the beep, and typing into the search box
// filters twenty thousand rows without touching IndexedDB per keystroke.
//
// Twenty thousand pieces is a few megabytes on a machine that does nothing else
// all day, and the alternative - a Dexie query per scan and per keystroke - puts
// an await between the scanner and the line appearing. On a counter with a queue
// of customers that await is the whole difference.
//
// It reloads when the dataset lands and after every bill, because the local
// shelf moves at Save & Print and the next scan has to read the new count.
//
// **The customer book is deliberately not here** (#245), and the reload is why.
// Every other table on this hook is bounded by one store's shelf; the customer
// list is all-KDPS and only ever grows, so putting it here would re-read the
// whole business's phone book from IndexedDB into React state after every single
// bill - a stall that gets worse every month, at the one screen where a stall is
// a queue of customers. The typeahead reads it straight from Dexie by its
// `mobile` index instead, which is what design.md's `searchCustomers` specifies
// and what an index is for.

import { useCallback, useEffect, useState } from "react";

import { META, readMeta } from "./db";

/** A stored id if it is a staff record's (a string), else nobody. */
function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}
import type { TillDb } from "./db";
import { DEFAULT_POLICY } from "./sync";
import type {
  TillGstSlab,
  TillTaxSettings,
  TillItem,
  TillManager,
  TillOffer,
  TillPolicy,
  TillSalesperson,
  TillSeason,
  TillStock,
  TillStoreIdentity,
} from "./types";

export interface TillWorld {
  items: TillItem[];
  stock: TillStock[];
  /** The rulebook as it stood at the last sync. Read here rather than queried
   *  per line: pricing runs on every keystroke in the discount box, and an
   *  await between a scan and the offer chip is an await the counter feels. */
  offers: TillOffer[];
  seasons: TillSeason[];
  slabs: TillGstSlab[];
  /** The tax settings last received (ticket 03); null before the first sync
   *  that carried them, which taxes by version 1 as before. */
  tax: TillTaxSettings | null;
  salespeople: TillSalesperson[];
  /** Who may authorise an exception here, with the hash their PIN is checked
   *  against. Empty until an administrator grants somebody the rung. */
  managers: TillManager[];
  policy: TillPolicy;
  store: TillStoreIdentity | null;
  /** The salesperson the counter credited last, defaulted onto the next line so a
   *  busy cashier accepts it with no keystrokes at all (D10 §4). */
  lastSalesperson: string | null;
  loaded: boolean;
}

const EMPTY: TillWorld = {
  items: [],
  stock: [],
  offers: [],
  seasons: [],
  slabs: [],
  tax: null,
  salespeople: [],
  managers: [],
  policy: DEFAULT_POLICY,
  store: null,
  lastSalesperson: null,
  loaded: false,
};

/**
 * Read the whole counter into memory, and again whenever `version` changes.
 *
 * `version` is whatever the caller can point at that means "this is stale" - the
 * last sync time and the number of bills committed. A hook cannot know when
 * IndexedDB moved underneath it, so the screen says.
 */
export function useTillWorld(db: TillDb | null, version: string): TillWorld {
  const [world, setWorld] = useState<TillWorld>(EMPTY);

  const load = useCallback(async (): Promise<TillWorld> => {
    if (!db) return EMPTY;
    const [items, stock, offers, seasons, slabs, salespeople, managers] = await Promise.all([
      db.items.toArray(),
      db.stock.toArray(),
      db.offers.toArray(),
      db.seasons.toArray(),
      db.gstSlabs.toArray(),
      db.salespeople.toArray(),
      db.managers.toArray(),
    ]);
    return {
      items,
      stock,
      offers,
      seasons,
      slabs,
      tax: await readMeta<TillTaxSettings | null>(db, META.taxSettings, null),
      // By name, as the server sent them: the table's own order is the ids'.
      salespeople: [...salespeople].sort((a, b) => a.name.localeCompare(b.name)),
      // A note with nothing left on it stays in the cache so the counter can say
      // "there is nothing left on that note" rather than "never heard of it",
      // which is a different sentence with a different remedy.
      managers,
      policy: await readMeta<TillPolicy>(db, META.policy, DEFAULT_POLICY),
      store: await readMeta<TillStoreIdentity | null>(db, META.store, null),
      // A number here is the retired salesman list's id (before ticket 07): it
      // names nobody on the staff list, so the counter starts with no default.
      lastSalesperson: stringOrNull(await readMeta<unknown>(db, META.lastSalesperson, null)),
      loaded: true,
    };
  }, [db]);

  useEffect(() => {
    let current = true;
    // A load that lost its race is dropped rather than written: two reloads can
    // overlap (a sync landing while a bill commits), and the older one finishing
    // last would put the pre-sale shelf back on the screen.
    void load().then((next) => {
      if (current) setWorld(next);
    });
    return () => {
      current = false;
    };
  }, [load, version]);

  return world;
}
