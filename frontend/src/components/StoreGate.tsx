// The one place a one-store screen asks which store (desktop redesign audit,
// rule 7). A person working across all their units used to get the server's
// "Pick a store first" as a warning, a red error, or a loading line that never
// ended, depending on the screen. Here they pick the store where they stand,
// and the choice is the same one the top-bar switcher makes.

import type { ReactNode } from "react";
import { Store as StoreIcon } from "lucide-react";

import { useAuth } from "../auth/AuthContext";

/** True while this login spans several stores and none is picked: a one-store
 *  screen has nothing to read yet and should show `StorePicker` instead. One
 *  store in scope is already a choice; the server reads it without asking. */
export function useNeedsStore(): boolean {
  const { user, activeStore } = useAuth();
  return !activeStore && (user?.stores.length ?? 0) > 1;
}

/** The inline picker: same choice as the top-bar switcher, made where the
 *  person is looking. */
export function StorePicker({ purpose }: { purpose: string }) {
  const { user, setActiveStore } = useAuth();
  const stores = user?.stores ?? [];
  return (
    <section className="card section-card store-gate" data-testid="store-gate">
      <StoreIcon size={20} aria-hidden="true" />
      <div className="store-gate-body">
        <h2 className="h3">Choose a store</h2>
        <p className="muted">This screen shows {purpose} for one store at a time.</p>
      </div>
      <label className="field store-gate-field">
        <span className="sr-only">Store</span>
        <select
          className="select"
          data-testid="store-gate-select"
          value=""
          onChange={(event) => {
            const picked = stores.find((s) => String(s.id) === event.target.value);
            if (picked) setActiveStore(picked);
          }}
        >
          <option value="" disabled>
            Choose a store…
          </option>
          {stores.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} ({s.code})
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}

/** `children` once a store is picked, the picker until then. */
export function StoreGate({
  purpose,
  bypass = false,
  children,
}: {
  /** What this screen shows for one store, in a few words: "the day's takings". */
  purpose: string;
  /** True when the screen already names its store (a `?store=` link). */
  bypass?: boolean;
  children: ReactNode;
}) {
  const needsStore = useNeedsStore();
  if (bypass || !needsStore) return <>{children}</>;
  return <StorePicker purpose={purpose} />;
}
