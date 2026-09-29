/** What the top-bar switcher offers this person (issue #88).
 *
 *  Pure, so the rule that matters — *the client never infers scope* — can be
 *  asserted directly. Everything here reads the authenticated-user payload:
 *  which units, whether a network view exists, and whether this person picks
 *  units at all (a brand manager picks brands, because their scope cuts across
 *  every store). Nothing is derived from the role code.
 *
 *  Fail-closed by construction: no units and no network view ⇒ no options, and
 *  the switcher renders as a locked label. There is no path from an empty
 *  payload to "All stores".
 */

import type { Brand, Store, User } from "../auth/AuthContext";

export type SwitcherOption =
  | { kind: "all-units"; label: string; hint: string }
  | { kind: "unit"; label: string; hint: string; store: Store }
  | { kind: "all-brands"; label: string; hint: string }
  | { kind: "brand"; label: string; hint: string; brand: Brand };

export interface SwitcherModel {
  mode: "units" | "brands";
  /** Nothing to choose between — render a plain label, not a menu. */
  locked: boolean;
  label: string;
  options: SwitcherOption[];
}

const NO_UNIT = "No unit assigned";
const NO_BRAND = "No brand assigned";

// The bar says only the unit's name; the code is a hint the menu draws beside
// it, and the state the unit bills under is not the bar's to say at all
// (ruled 1 Aug 2026) — it belongs to the page, not the chrome.
function unitOption(store: Store): SwitcherOption {
  return { kind: "unit", label: store.name, hint: store.code, store };
}

function brandOption(brand: Brand): SwitcherOption {
  return { kind: "brand", label: brand.name, hint: "", brand };
}

function unitsModel(user: User, active: Store | null): SwitcherModel {
  const units = user.business_units ?? user.stores ?? [];
  // An "all" option means "no single unit" — which is what the server already
  // answers when no unit is sent, so it is exactly this person's own scope.
  // Worth offering whenever there is more than one unit to aggregate; only the
  // network-wide grant may call it the network.
  const network = Boolean(user.all_business_units);
  const aggregate = network || units.length > 1;
  const allLabel = network ? "All business units" : "All my business units";
  const options: SwitcherOption[] = [
    ...(aggregate ? [{ kind: "all-units" as const, label: allLabel, hint: "" }] : []),
    ...units.map(unitOption),
  ];
  const label = active ? active.name : options.length ? allLabel : NO_UNIT;
  return {
    mode: "units",
    // One option is not a choice: a store person sees their store, full stop.
    locked: options.length <= 1,
    label,
    options,
  };
}

function brandsModel(user: User, active: Brand | null): SwitcherModel {
  const brands = user.assigned_brands ?? [];
  const options: SwitcherOption[] =
    brands.length > 1
      ? [
          { kind: "all-brands", label: "All my brands", hint: "" },
          ...brands.map(brandOption),
        ]
      : brands.map(brandOption);
  const label = active ? active.name : brands.length ? "All my brands" : NO_BRAND;
  return {
    mode: "brands",
    locked: options.length <= 1,
    label,
    options,
  };
}

/** A stable identity for one menu row.
 *
 *  The label is not one. Since the bar stopped saying the code (ruled 1 Aug
 *  2026) a unit's label is just its name, and KDPS really does run two units
 *  with the same name in different towns - so keying the menu on the label
 *  would hand React two rows with one key and let it reuse the wrong one. The
 *  code is what the server keys on, so it is what the list keys on. */
export function optionKey(option: SwitcherOption): string {
  if (option.kind === "unit") return `unit:${option.store.code}`;
  if (option.kind === "brand") return `brand:${option.brand.code}`;
  return option.kind;
}

export function switcherModel(
  user: User,
  activeStore: Store | null,
  activeBrand: Brand | null,
): SwitcherModel {
  return user.business_unit_mode === "brands"
    ? brandsModel(user, activeBrand)
    : unitsModel(user, activeStore);
}

/** How the dashboard names the unit whose numbers are on screen. Code and
 *  name - a report heading, not a chip - but the state stays off it, same as
 *  the bar: the state a unit bills under is the page's fact to state where it
 *  matters, not the chrome's. */
export function unitContextLabel(activeStore: Store | null): string {
  return activeStore ? `${activeStore.code} · ${activeStore.name}` : "All business units";
}

/** Key that changes whenever the working context changes — the shell remounts
 *  the page on it, so every screen refetches under the new unit instead of
 *  showing the previous one's numbers. */
export function contextKey(activeStore: Store | null, activeBrand: Brand | null): string {
  return `${activeStore?.code ?? "all"}|${activeBrand?.code ?? "all"}`;
}
