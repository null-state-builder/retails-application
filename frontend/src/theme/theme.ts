/* Theme module — three-way Light / Dark / System with live system tracking.
   No React Context: the theme lives on <html data-theme> (a global), is needed
   both inside AppShell and on Login, and must be applied before React mounts.
   JS always resolves the preference to a concrete data-theme="light|dark". */
import { useSyncExternalStore } from "react";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_KEY = "kdps-theme";

const META_LIGHT = "#faf7f2"; // --paper (light)
const META_DARK = "#201d18"; // --paper (dark)

/** Keep browser chrome in step with the resolved application theme. */
export function refreshThemeColour(): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  const colour = root.dataset.theme === "dark" ? META_DARK : META_LIGHT;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", colour);
}

/** Pure resolver — unit-testable, no globals. */
export function resolveTheme(pref: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (pref === "light") return "light";
  if (pref === "dark") return "dark";
  return systemDark ? "dark" : "light";
}

function hasWindow(): boolean {
  return typeof window !== "undefined";
}

function systemPrefersDark(): boolean {
  if (!hasWindow() || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function getThemePreference(): ThemePreference {
  if (!hasWindow()) return "system";
  try {
    const raw = window.localStorage.getItem(THEME_KEY);
    if (raw === "light" || raw === "dark" || raw === "system") return raw;
  } catch {
    /* localStorage may be unavailable (privacy mode) */
  }
  return "system";
}

function apply(): void {
  if (typeof document === "undefined") return;
  const resolved = resolveTheme(getThemePreference(), systemPrefersDark());
  document.documentElement.dataset.theme = resolved;
  refreshThemeColour();
}

const listeners = new Set<() => void>();

function notify(): void {
  for (const l of listeners) l();
}

export function setThemePreference(pref: ThemePreference): void {
  if (hasWindow()) {
    try {
      window.localStorage.setItem(THEME_KEY, pref);
    } catch {
      /* ignore persistence failures */
    }
  }
  apply();
  notify();
}

let initialised = false;

/** Called once from main.tsx before createRoot — wires the live listeners. */
export function initTheme(): void {
  if (initialised || !hasWindow()) return;
  initialised = true;
  apply();
  if (typeof window.matchMedia === "function") {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onSystemChange = () => {
      if (getThemePreference() === "system") {
        apply();
        notify();
      }
    };
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", onSystemChange);
    } else if (typeof mq.addListener === "function") {
      mq.addListener(onSystemChange);
    }
  }
  window.addEventListener("storage", (e) => {
    if (e.key === THEME_KEY) {
      apply();
      notify();
    }
  });
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/* The store snapshot combines preference + resolved theme so the hook
   re-renders when the OS theme flips under the "system" preference — not only
   when the preference itself changes. (A preference-only snapshot stays
   "system" across a system flip, so React's Object.is bailout would skip the
   re-render and leave `resolved` stale.) Strings compare by value, so building
   a fresh one each call is referentially safe for useSyncExternalStore. */
export function getThemeSnapshot(): string {
  const pref = getThemePreference();
  return `${pref}|${resolveTheme(pref, systemPrefersDark())}`;
}

export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (p: ThemePreference) => void;
} {
  const snapshot = useSyncExternalStore(subscribe, getThemeSnapshot, () => "system|light");
  const [preference, resolved] = snapshot.split("|") as [ThemePreference, ResolvedTheme];
  return { preference, resolved, setPreference: setThemePreference };
}
