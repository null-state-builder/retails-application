/* Density (design system v2): Counter, Desk or Compact on <html data-density>.
   The role sets the default; a person's own choice, when there is one, wins.
   Only density makes text and controls bigger or smaller (index.css). */
import { useLayoutEffect } from "react";

export type Density = "counter" | "desk" | "compact";

export const DENSITY_KEY = "kdps-density";

/** The role's default. Cashiers get Counter on every screen, not only the till
 *  (rollout decision 1); everyone else starts on Desk and may choose Compact. */
export function roleDensity(roleCode: string): Density {
  return roleCode === "store_person" ? "counter" : "desk";
}

/** A person's own choice, kept in this browser until Profile stores it. */
export function densityOverride(): Density | null {
  try {
    const raw = window.localStorage.getItem(DENSITY_KEY);
    if (raw === "counter" || raw === "desk" || raw === "compact") return raw;
  } catch {
    /* localStorage may be unavailable (privacy mode) */
  }
  return null;
}

/** Set the density on a root element and return the cleanup that restores it. */
export function applyDensity(root: HTMLElement, roleCode: string): () => void {
  const previous = root.dataset.density;
  root.dataset.density = densityOverride() ?? roleDensity(roleCode);
  return () => {
    if (previous === undefined) delete root.dataset.density;
    else root.dataset.density = previous;
  };
}

/** Apply the signed-in person's density before the first frame. */
export function useDensity(roleCode: string): void {
  useLayoutEffect(() => applyDensity(document.documentElement, roleCode), [roleCode]);
}
